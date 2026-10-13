package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"strings"
	"time"
)

type stagedChange struct {
	ID        string  `json:"change_id"`
	Path      string  `json:"path"`
	Before    []byte  `json:"before"`
	After     []byte  `json:"after"`
	Existed   bool    `json:"existed"`
	Mode      uint32  `json:"mode"`
	Hash      string  `json:"before_hash"`
	Workspace string  `json:"workspace"`
	Session   string  `json:"session_id"`
	Source    string  `json:"source"`
	Status    string  `json:"status"`
	Created   float64 `json:"created_at"`
}

func (c stagedChange) file() fileChange {
	return fileChange{path: c.Path, before: c.Before, after: c.After, existed: c.Existed, mode: fs.FileMode(c.Mode) & 0777, sha: c.Hash}
}
func stageOnly(args map[string]any) (bool, error) {
	value, exists := args["stage_only"]
	if !exists {
		return false, nil
	}
	flag, ok := value.(bool)
	if !ok {
		return false, errors.New("stage_only must be a boolean")
	}
	return flag, nil
}
func stageRecordedChange(ctx context.Context, scope *scope, root, session, source string, c fileChange) toolResult {
	store, err := openCheckpoints(root)
	if err != nil {
		return failed(err)
	}
	defer store.Close()
	id, err := newID()
	if err != nil {
		return failed(err)
	}
	change := stagedChange{ID: id, Path: c.path, Before: c.before, After: c.after, Existed: c.existed, Mode: uint32(c.mode), Hash: c.sha, Workspace: scope.workspace, Session: session, Source: source, Status: "pending", Created: float64(time.Now().UnixNano()) / 1e9}
	body, err := json.Marshal(change)
	if err != nil {
		return failed(err)
	}
	_, err = store.db.ExecContext(ctx, "INSERT INTO native_staged_changes (change_id,workspace,session_id,status,created_at,record_json) VALUES (?,?,?,'pending',?,?)", id, scope.workspace, session, change.Created, string(body))
	if err != nil {
		return failed(err)
	}
	return toolResult{Success: true, Data: map[string]any{"staged": true, "change_id": id, "path": c.path, "preview": changePreview(c)}}
}
func (s *checkpointStore) getStage(ctx context.Context, scope *scope, ref string) (stagedChange, error) {
	if !validID.MatchString(ref) {
		return stagedChange{}, errors.New("invalid change ID")
	}
	rows, err := s.db.QueryContext(ctx, "SELECT record_json,status FROM native_staged_changes WHERE workspace=? AND substr(change_id,1,?)=? LIMIT 2", scope.workspace, len(ref), ref)
	if err != nil {
		return stagedChange{}, err
	}
	defer rows.Close()
	var matches []stagedChange
	for rows.Next() {
		var body, status string
		if err := rows.Scan(&body, &status); err != nil {
			return stagedChange{}, err
		}
		var c stagedChange
		if len(body) > maxState || json.Unmarshal([]byte(body), &c) != nil {
			return stagedChange{}, errors.New("invalid staged change record")
		}
		if !validID.MatchString(c.ID) || !strings.HasPrefix(c.ID, ref) || c.Workspace != scope.workspace || c.Hash != bytesHash(c.Before) || len(c.Before) > 4<<20 || len(c.After) > 4<<20 {
			return stagedChange{}, errors.New("staged change image is invalid")
		}
		p, err := scope.open(c.Path, false)
		if err != nil {
			return stagedChange{}, err
		}
		redirected := p.absolute != c.Path
		p.Close()
		if redirected {
			return stagedChange{}, errors.New("staged path resolves to a different file")
		}
		c.Status = status
		matches = append(matches, c)
	}
	if err := rows.Err(); err != nil {
		return stagedChange{}, err
	}
	if len(matches) != 1 {
		return stagedChange{}, errors.New("unknown or ambiguous change ID")
	}
	return matches[0], nil
}
func stagedList(ctx context.Context, scope *scope, root, session string, all bool) toolResult {
	store, err := openCheckpoints(root)
	if err != nil {
		return failed(err)
	}
	defer store.Close()
	where := "workspace=?"
	params := []any{scope.workspace}
	if session != "" {
		where += " AND session_id=?"
		params = append(params, session)
	}
	if !all {
		where += " AND status='pending'"
	}
	rows, err := store.db.QueryContext(ctx, "SELECT change_id,status,record_json FROM native_staged_changes WHERE "+where+" ORDER BY created_at,change_id LIMIT 50", params...)
	if err != nil {
		return failed(err)
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var id, status, body string
		if err := rows.Scan(&id, &status, &body); err != nil {
			return failed(err)
		}
		var c stagedChange
		if len(body) > maxState || json.Unmarshal([]byte(body), &c) != nil {
			return failed(errors.New("invalid staged change record"))
		}
		p, err := scope.open(c.Path, false)
		if err != nil {
			continue
		}
		p.Close()
		items = append(items, map[string]any{"change_id": id, "status": status, "path": c.Path, "source": c.Source, "preview": changePreview(c.file())})
	}
	if err := rows.Err(); err != nil {
		return failed(err)
	}
	return toolResult{Success: true, Data: map[string]any{"items": items, "count": len(items)}}
}
func stagedReject(ctx context.Context, scope *scope, root, ref string) toolResult {
	store, err := openCheckpoints(root)
	if err != nil {
		return failed(err)
	}
	defer store.Close()
	c, err := store.getStage(ctx, scope, ref)
	if err != nil {
		return failed(err)
	}
	res, err := store.db.ExecContext(ctx, "UPDATE native_staged_changes SET status='rejected' WHERE change_id=? AND status='pending'", c.ID)
	if err != nil {
		return failed(err)
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return failed(errors.New("change is already closed or being applied"))
	}
	return toolResult{Success: true, Data: map[string]any{"change_id": c.ID, "rejected": true}}
}
func stagedApply(ctx context.Context, scope *scope, root string, c stagedChange) toolResult {
	store, err := openCheckpoints(root)
	if err != nil {
		return failed(err)
	}
	defer store.Close()
	current, err := store.getStage(ctx, scope, c.ID)
	if err != nil {
		return failed(err)
	}
	// Reload after approval, never replace the content that the user reviewed.
	if current.Hash != c.Hash || bytesHash(current.After) != bytesHash(c.After) || current.Path != c.Path || current.Source != c.Source || current.Existed != c.Existed || current.Mode != c.Mode || current.Session != c.Session {
		return failed(errors.New("staged change changed during approval"))
	}
	res, err := store.db.ExecContext(ctx, "UPDATE native_staged_changes SET status='applying' WHERE change_id=? AND status='pending'", c.ID)
	if err != nil {
		return failed(err)
	}
	if n, _ := res.RowsAffected(); n != 1 {
		return failed(errors.New("change is already closed or being applied"))
	}
	result := applyRecordedChange(ctx, scope, root, c.Session, c.Source, c.file())
	status := "pending"
	if result.Success {
		status = "applied"
	} else if result.Data["file_written"] == true {
		status = "needs-review"
	}
	finish, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := store.db.ExecContext(finish, "UPDATE native_staged_changes SET status=? WHERE change_id=?", status, c.ID); err != nil {
		return toolResult{Error: fmt.Sprintf("change %s completion needs review: %v", c.ID, err), Data: result.Data}
	}
	if result.Success {
		result.Data["change_id"] = c.ID
	}
	return result
}
func (s *server) stageCommand(ctx context.Context, turn, command string, args []string) (string, error) {
	var result toolResult
	if command == "/changes" {
		if len(args) > 1 || (len(args) == 1 && args[0] != "--all") {
			return "", errors.New("usage: /changes [--all]")
		}
		result = stagedList(ctx, s.scope, s.opts.root, s.snap.ID, len(args) > 0)
	} else {
		if len(args) != 1 {
			return "", fmt.Errorf("usage: %s <change_id>", command)
		}
		if command == "/reject-change" {
			result = stagedReject(ctx, s.scope, s.opts.root, args[0])
		} else {
			store, err := openCheckpoints(s.opts.root)
			if err != nil {
				return "", err
			}
			c, err := store.getStage(ctx, s.scope, args[0])
			store.Close()
			if err != nil {
				return "", err
			}
			if c.Status != "pending" {
				return "", errors.New("change is already closed or being applied")
			}
			allowed, reason, err := s.approve(ctx, c.Source, map[string]any{"path": c.Path}, c.file())
			if err != nil {
				return "", err
			}
			if !allowed {
				return "Change not applied: " + reason, nil
			}
			result = stagedApply(ctx, s.scope, s.opts.root, c)
		}
	}
	if !result.Success {
		return "", errors.New(result.Error)
	}
	body, err := json.MarshalIndent(result.Data, "", "  ")
	return string(body), err
}
