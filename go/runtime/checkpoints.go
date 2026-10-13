package main

// Checkpoints share the legacy SQLite tables. Native records also keep exact
// physical bytes in metadata so CRLF, binary preimages and modes survive a
// native rewind. The ordinary text columns remain readable by the old worker.
import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

type checkpointStore struct{ db *sql.DB }
type checkpointFile struct {
	Path                  string `json:"path"`
	Existed               bool   `json:"existed_before"`
	Before, After         string
	BeforeHash, AfterHash string
	Mode                  *uint32
}
type checkpoint struct {
	ID       string           `json:"checkpoint_id"`
	Sequence int64            `json:"sequence"`
	Run      sql.NullString   `json:"-"`
	Session  string           `json:"session_id"`
	Source   string           `json:"source"`
	Label    string           `json:"label"`
	Status   string           `json:"status"`
	Created  float64          `json:"created_at"`
	Metadata json.RawMessage  `json:"-"`
	Files    []checkpointFile `json:"files"`
}
type physicalImage struct {
	Before []byte `json:"before"`
	After  []byte `json:"after"`
}
type checkpointMetadata struct {
	Native    map[string]physicalImage `json:"native_physical_images,omitempty"`
	Workspace string                   `json:"native_workspace,omitempty"`
}

func bytesHash(body []byte) string {
	hash := sha256.Sum256(body)
	return hex.EncodeToString(hash[:])
}
func logicalText(body []byte) string {
	return strings.ReplaceAll(strings.ReplaceAll(strings.ToValidUTF8(string(body), "�"), "\r\n", "\n"), "\r", "\n")
}

func openCheckpoints(root string) (*checkpointStore, error) {
	folder := filepath.Join(root, "runtime")
	if err := os.MkdirAll(folder, 0700); err != nil {
		return nil, err
	}
	path := filepath.Join(folder, "runs.sqlite3")
	// A new database is private before SQLite starts writing pages. Existing
	// user databases retain their permissions and tables.
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err := f.Close(); err != nil {
		return nil, err
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	uriPath := filepath.ToSlash(absolute)
	if !strings.HasPrefix(uriPath, "/") {
		uriPath = "/" + uriPath
	}
	uri := (&url.URL{Scheme: "file", Path: uriPath}).String()
	db, err := sql.Open("sqlite", uri+"?_pragma=foreign_keys(1)&_pragma=busy_timeout(10000)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_run_id TEXT,
 status TEXT NOT NULL, prompt TEXT NOT NULL, workspace TEXT NOT NULL,
 provider TEXT NOT NULL DEFAULT '', owner_pid INTEGER NOT NULL, owner_host TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, finished_at REAL,
 error TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
 FOREIGN KEY(parent_run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS checkpoints (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, checkpoint_id TEXT NOT NULL UNIQUE,
 run_id TEXT, session_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
 label TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
 created_at REAL NOT NULL, restored_at REAL, metadata_json TEXT NOT NULL DEFAULT '{}',
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS run_events (
 event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
 event_type TEXT NOT NULL, timestamp REAL NOT NULL, data_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(run_id,sequence), FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS checkpoint_files (
 checkpoint_id TEXT NOT NULL, path TEXT NOT NULL, existed_before INTEGER NOT NULL,
 before_content TEXT NOT NULL, after_content TEXT NOT NULL,
 before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, before_mode INTEGER,
 PRIMARY KEY(checkpoint_id,path),
 FOREIGN KEY(checkpoint_id) REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run_sequence ON checkpoints(run_id,sequence DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_session_sequence ON checkpoints(session_id,sequence DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_status_sequence ON checkpoints(status,sequence DESC);
CREATE TABLE IF NOT EXISTS native_staged_changes (
 change_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, session_id TEXT NOT NULL,
 status TEXT NOT NULL, created_at REAL NOT NULL, record_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_native_staged_workspace ON native_staged_changes(workspace,status,created_at);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &checkpointStore{db}, nil
}
func (s *checkpointStore) Close() { s.db.Close() }

func (s *checkpointStore) prepare(ctx context.Context, c fileChange, workspace, session, source string) (string, error) {
	id, err := newID()
	if err != nil {
		return "", err
	}
	before, after := logicalText(c.before), logicalText(c.after)
	metadata, err := json.Marshal(checkpointMetadata{Native: map[string]physicalImage{c.path: {Before: c.before, After: c.after}}, Workspace: workspace})
	if err != nil {
		return "", err
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, `INSERT INTO checkpoints (checkpoint_id, session_id, source, label, status, created_at, metadata_json) VALUES (?,?,?,?, 'native-pending',?,?)`, id, session, source, source+": "+filepath.Base(c.path), float64(time.Now().UnixNano())/1e9, string(metadata))
	if err != nil {
		return "", err
	}
	var mode any
	if c.existed {
		mode = uint32(c.mode.Perm())
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO checkpoint_files (checkpoint_id,path,existed_before,before_content,after_content,before_hash,after_hash,before_mode) VALUES (?,?,?,?,?,?,?,?)`, id, c.path, c.existed, before, after, bytesHash([]byte(before)), bytesHash([]byte(after)), mode)
	if err != nil {
		return "", err
	}
	return id, tx.Commit()
}

// Prepare is committed before touching a project file. If activation fails,
// the durable pending record still has the exact images needed to recover.
func applyRecordedChange(ctx context.Context, scope *scope, root, session, source string, c fileChange) toolResult {
	if err := ctx.Err(); err != nil {
		return failed(err)
	}
	if c.existed && bytes.Equal(c.before, c.after) {
		return applyChange(scope, c)
	}
	store, err := openCheckpoints(root)
	if err != nil {
		return failed(fmt.Errorf("cannot prepare file checkpoint: %w", err))
	}
	defer store.Close()
	id, err := store.prepare(ctx, c, scope.workspace, session, source)
	if err != nil {
		return failed(err)
	}
	var result toolResult
	if err := ctx.Err(); err != nil {
		result = failed(err)
	} else {
		result = applyChange(scope, c)
	}
	status := "active"
	if !result.Success {
		status = "native-failed"
	}
	// Completion must be journaled even when cancellation arrives just after
	// the rename. Use a bounded context independent of the cancelled turn.
	finish, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err = store.db.ExecContext(finish, "UPDATE checkpoints SET status=? WHERE checkpoint_id=?", status, id)
	if err != nil {
		return toolResult{Error: fmt.Sprintf("file operation completion could not be journaled; inspect checkpoint %s: %v", id, err), Data: map[string]any{"checkpoint_id": id, "file_written": result.Success}}
	}
	if result.Success {
		result.Data["checkpoint_id"] = id
	}
	return result
}

type checkpointQuery interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func queryCheckpoints(ctx context.Context, q checkpointQuery, where string, args ...any) ([]checkpoint, error) {
	rows, err := q.QueryContext(ctx, `SELECT checkpoint_id,sequence,run_id,session_id,source,label,status,created_at,CASE WHEN length(CAST(metadata_json AS BLOB))<=16777216 THEN metadata_json ELSE '' END FROM checkpoints `+where+` ORDER BY sequence DESC LIMIT 10000`, args...)
	if err != nil {
		return nil, err
	}
	var records []checkpoint
	loaded := 0
	for rows.Next() {
		var r checkpoint
		var metadata string
		if err := rows.Scan(&r.ID, &r.Sequence, &r.Run, &r.Session, &r.Source, &r.Label, &r.Status, &r.Created, &metadata); err != nil {
			rows.Close()
			return nil, err
		}
		loaded += len(metadata)
		if metadata == "" || loaded > 64<<20 {
			rows.Close()
			return nil, errors.New("checkpoint group exceeds the read limit; restore individual checkpoints")
		}
		r.Metadata = json.RawMessage(metadata)
		records = append(records, r)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	for i := range records {
		files, err := q.QueryContext(ctx, `SELECT path,existed_before,CASE WHEN length(CAST(before_content AS BLOB))<=12582912 THEN before_content ELSE NULL END,CASE WHEN length(CAST(after_content AS BLOB))<=12582912 THEN after_content ELSE NULL END,before_hash,after_hash,before_mode FROM checkpoint_files WHERE checkpoint_id=? ORDER BY path`, records[i].ID)
		if err != nil {
			return nil, err
		}
		for files.Next() {
			var f checkpointFile
			var mode sql.NullInt64
			if err := files.Scan(&f.Path, &f.Existed, &f.Before, &f.After, &f.BeforeHash, &f.AfterHash, &mode); err != nil {
				files.Close()
				return nil, err
			}
			loaded += len(f.Before) + len(f.After)
			if loaded > 64<<20 {
				files.Close()
				return nil, errors.New("checkpoint group exceeds the read limit; restore individual checkpoints")
			}
			if mode.Valid {
				value := uint32(mode.Int64)
				f.Mode = &value
			}
			records[i].Files = append(records[i].Files, f)
		}
		err = files.Err()
		files.Close()
		if err != nil {
			return nil, err
		}
	}
	return records, nil
}

const openCheckpointFilter = "status IN ('active','native-pending')"

func (s *checkpointStore) selectRecords(ctx context.Context, session, ref string) ([]checkpoint, error) {
	if ref != "" {
		if !validID.MatchString(ref) {
			return nil, errors.New("invalid checkpoint or run ID")
		}
		matches, err := queryCheckpoints(ctx, s.db, "WHERE checkpoint_id=?", ref)
		if err != nil {
			return nil, err
		}
		if len(matches) == 0 {
			rows, e := s.db.QueryContext(ctx, "SELECT checkpoint_id FROM checkpoints WHERE substr(checkpoint_id,1,?)=? ORDER BY sequence DESC LIMIT 2", len(ref), ref)
			if e != nil {
				return nil, e
			}
			ids := []string{}
			for rows.Next() {
				var id string
				if e := rows.Scan(&id); e != nil {
					rows.Close()
					return nil, e
				}
				ids = append(ids, id)
			}
			e = rows.Err()
			rows.Close()
			if e != nil {
				return nil, e
			}
			if len(ids) > 1 {
				return nil, errors.New("ambiguous checkpoint ID; use the full ID")
			}
			if len(ids) == 1 {
				matches, err = queryCheckpoints(ctx, s.db, "WHERE checkpoint_id=?", ids[0])
			}
			if err != nil {
				return nil, err
			}
		}
		if len(matches) > 1 {
			return nil, errors.New("ambiguous checkpoint ID; use the full ID")
		}
		if len(matches) == 1 {
			if matches[0].Status != "active" && matches[0].Status != "native-pending" {
				return nil, errors.New("checkpoint is already closed")
			}
			return matches, nil
		}
		matches, err = queryCheckpoints(ctx, s.db, "WHERE "+openCheckpointFilter+" AND run_id=?", ref)
		if err != nil {
			return nil, err
		}
		if len(matches) == 0 {
			return nil, errors.New("no restorable checkpoint found")
		}
		return matches, nil
	}
	where := "WHERE " + openCheckpointFilter
	args := []any{}
	if session != "" {
		where += " AND session_id=?"
		args = append(args, session)
	}
	var latest string
	if err := s.db.QueryRowContext(ctx, "SELECT checkpoint_id FROM checkpoints "+where+" ORDER BY sequence DESC LIMIT 1", args...).Scan(&latest); err != nil {
		if !errors.Is(err, sql.ErrNoRows) {
			return nil, err
		}
		return nil, errors.New("no restorable checkpoint in this session")
	}
	records, err := queryCheckpoints(ctx, s.db, "WHERE checkpoint_id=?", latest)
	if err != nil {
		return nil, err
	}
	if len(records) != 1 {
		return nil, errors.New("checkpoint was removed while selecting it")
	}
	if records[0].Run.Valid {
		return queryCheckpoints(ctx, s.db, "WHERE "+openCheckpointFilter+" AND run_id=?", records[0].Run.String)
	}
	return records[:1], nil
}

func checkpointImages(r checkpoint, f checkpointFile) (physicalImage, bool, error) {
	if len(f.Before) > 12<<20 || len(f.After) > 12<<20 || bytesHash([]byte(f.Before)) != f.BeforeHash || bytesHash([]byte(f.After)) != f.AfterHash {
		return physicalImage{}, false, errors.New("checkpoint image or hash is invalid")
	}
	var metadata checkpointMetadata
	if err := json.Unmarshal(r.Metadata, &metadata); err != nil {
		return physicalImage{}, false, err
	}
	image, exact := metadata.Native[f.Path]
	if !exact {
		return physicalImage{Before: []byte(f.Before), After: []byte(f.After)}, false, nil
	}
	if len(image.Before) > 4<<20 || len(image.After) > 4<<20 || logicalText(image.Before) != f.Before || logicalText(image.After) != f.After {
		return physicalImage{}, false, errors.New("physical checkpoint image disagrees with text")
	}
	return image, true, nil
}

type restoreStep struct {
	change       fileChange
	remove       bool
	rollbackMode fs.FileMode
}

func planRestore(ctx context.Context, scope *scope, records []checkpoint) ([]restoreStep, error) {
	var steps []restoreStep
	type imageState struct {
		bytes  []byte
		exists bool
		mode   fs.FileMode
	}
	simulated := map[string]imageState{}
	for _, record := range records {
		if len(record.Files) == 0 {
			return nil, errors.New("checkpoint contains no files")
		}
		for _, file := range record.Files {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			p, err := scope.open(file.Path, true)
			if err != nil {
				return nil, err
			}
			if p.absolute != lexicalPath(file.Path) {
				p.Close()
				return nil, errors.New("checkpoint path resolves to a different file")
			}
			state, seen := simulated[p.absolute]
			if !seen {
				info, e := p.root.Stat(p.relative)
				if e != nil {
					p.Close()
					return nil, e
				}
				state.bytes, e = readRegular(p, 4<<20)
				if e != nil {
					p.Close()
					return nil, e
				}
				state.exists, state.mode = true, info.Mode().Perm()
			}
			p.Close()
			image, exact, err := checkpointImages(record, file)
			if err != nil {
				return nil, err
			}
			matches := bytes.Equal(state.bytes, image.After)
			if !exact {
				matches = logicalText(state.bytes) == file.After
			}
			if !state.exists || !matches {
				return nil, fmt.Errorf("refusing to overwrite a file changed after its checkpoint: %s", file.Path)
			}
			before := image.Before
			// Legacy checkpoints store logical text. Retain a CRLF file's
			// physical style when restoring that text through the native worker.
			if !exact && bytes.Contains(state.bytes, []byte("\r\n")) {
				before = []byte(strings.ReplaceAll(file.Before, "\n", "\r\n"))
			}
			mode := state.mode
			if file.Mode != nil {
				mode = fs.FileMode(*file.Mode) & 0777
			}
			steps = append(steps, restoreStep{change: fileChange{path: lexicalPath(file.Path), before: state.bytes, after: before, existed: true, mode: mode, sha: bytesHash(state.bytes)}, remove: !file.Existed, rollbackMode: state.mode})
			simulated[lexicalPath(file.Path)] = imageState{bytes: before, exists: file.Existed, mode: mode}
		}
	}
	return steps, nil
}

func applyRestoreStep(scope *scope, step restoreStep) toolResult {
	if !step.remove {
		return applyChange(scope, step.change)
	}
	p, err := scope.open(step.change.path, true)
	if err != nil {
		return failed(err)
	}
	defer p.Close()
	if p.absolute != step.change.path {
		return failed(errors.New("checkpoint path resolves to a different file during restore"))
	}
	body, err := readRegular(p, 4<<20)
	if err != nil {
		return failed(err)
	}
	if bytesHash(body) != step.change.sha {
		return failed(errors.New("file changed while rewind was running"))
	}
	if err := p.root.Remove(p.relative); err != nil {
		return failed(err)
	}
	return toolResult{Success: true}
}

func (s *checkpointStore) restore(ctx context.Context, scope *scope, records []checkpoint) ([]string, error) {
	// Serialize native restores with other SQLite writers while refreshing the
	// selected statuses. Files and SQLite cannot form one crash transaction.
	conn, err := s.db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	if _, err = conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	defer conn.ExecContext(context.Background(), "ROLLBACK")
	for i, r := range records {
		current, e := queryCheckpoints(ctx, conn, "WHERE checkpoint_id=?", r.ID)
		if e != nil {
			return nil, e
		}
		if len(current) != 1 || (current[0].Status != "active" && current[0].Status != "native-pending") {
			return nil, errors.New("checkpoint is no longer active")
		}
		records[i] = current[0]
	}
	steps, err := planRestore(ctx, scope, records)
	if err != nil {
		return nil, err
	}
	completed := []restoreStep{}
	rollback := func(cause error) error {
		// Roll back only changes this operation made and only if their postimage
		// is still present. A concurrent user edit must never be overwritten.
		for i := len(completed) - 1; i >= 0; i-- {
			step := completed[i]
			undo := fileChange{path: step.change.path, before: step.change.after, after: step.change.before, existed: !step.remove, mode: step.rollbackMode, sha: bytesHash(step.change.after)}
			if step.remove {
				undo.before = nil
				undo.sha = bytesHash(nil)
			}
			if result := applyChange(scope, undo); !result.Success {
				cause = fmt.Errorf("%w; rollback needs manual review for %s: %s", cause, undo.path, result.Error)
			}
		}
		return cause
	}
	paths := []string{}
	seen := map[string]bool{}
	for _, step := range steps {
		if err := ctx.Err(); err != nil {
			return nil, rollback(err)
		}
		if result := applyRestoreStep(scope, step); !result.Success {
			return nil, rollback(errors.New(result.Error))
		}
		completed = append(completed, step)
		if !seen[step.change.path] {
			paths = append(paths, step.change.path)
			seen[step.change.path] = true
		}
	}
	for _, r := range records {
		if _, err := conn.ExecContext(ctx, "UPDATE checkpoints SET status='restored',restored_at=? WHERE checkpoint_id=?", float64(time.Now().UnixNano())/1e9, r.ID); err != nil {
			return nil, rollback(err)
		}
	}
	if len(records) > 0 && records[0].Run.Valid {
		run := records[0].Run.String
		sameRun := true
		ids := []string{}
		for _, r := range records {
			sameRun = sameRun && r.Run.Valid && r.Run.String == run
			ids = append(ids, r.ID)
		}
		if sameRun {
			id, err := newID()
			if err != nil {
				return nil, rollback(err)
			}
			body, err := json.Marshal(map[string]any{"checkpoint_ids": ids, "paths": paths})
			if err != nil {
				return nil, rollback(err)
			}
			_, err = conn.ExecContext(ctx, `INSERT INTO run_events (event_id,run_id,sequence,event_type,timestamp,data_json) VALUES (?,?,(SELECT COALESCE(MAX(sequence),0)+1 FROM run_events WHERE run_id=?),'checkpoint_restored',?,?)`, id, run, run, float64(time.Now().UnixNano())/1e9, string(body))
			if err != nil {
				return nil, rollback(err)
			}
		}
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		return nil, rollback(err)
	}
	return paths, nil
}
