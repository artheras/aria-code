package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"google.golang.org/genai"
)

func checkpointSummaries(scope *scope, records []checkpoint) []map[string]any {
	items := []map[string]any{}
	for _, r := range records {
		paths := []string{}
		visible := len(r.Files) > 0
		for _, f := range r.Files {
			p, err := scope.open(f.Path, false)
			if err != nil {
				visible = false
				break
			}
			paths = append(paths, p.absolute)
			p.Close()
		}
		if !visible {
			continue
		}
		items = append(items, map[string]any{"checkpoint_id": r.ID, "sequence": r.Sequence, "session_id": r.Session, "source": r.Source, "status": r.Status, "paths": paths, "created_at": r.Created})
		if len(items) == 50 {
			break
		}
	}
	return items
}

func checkpointTool(ctx context.Context, scope *scope, root, name string, args map[string]any) toolResult {
	store, err := openCheckpoints(root)
	if err != nil {
		return failed(err)
	}
	defer store.Close()
	session := str(args, "session_id", "")
	if name == "list_checkpoints" {
		where := "WHERE " + openCheckpointFilter
		params := []any{}
		if session != "" {
			where += " AND session_id=?"
			params = append(params, session)
		}
		records, err := checkpointSummaryRecords(ctx, store, where, params...)
		if err != nil {
			return failed(err)
		}
		items := checkpointSummaries(scope, records)
		return toolResult{Success: true, Data: map[string]any{"items": items, "count": len(items)}}
	}
	ref := str(args, "checkpoint_id", "")
	if ref == "" && session == "" {
		return failed(errors.New("restore_checkpoint requires checkpoint_id or session_id"))
	}
	records, err := store.selectRecords(ctx, session, ref)
	if err != nil {
		return failed(err)
	}
	paths, err := store.restore(ctx, scope, records)
	if err != nil {
		return failed(err)
	}
	ids := []string{}
	for _, r := range records {
		ids = append(ids, r.ID)
	}
	return toolResult{Success: true, Data: map[string]any{"checkpoint_ids": ids, "restored_paths": paths}}
}

// Listing history does not load file preimages: large code checkpoints should
// not slow the prompt or allocate their full contents just to show filenames.
func checkpointSummaryRecords(ctx context.Context, store *checkpointStore, where string, args ...any) ([]checkpoint, error) {
	rows, err := store.db.QueryContext(ctx, `SELECT c.checkpoint_id,c.sequence,c.session_id,c.source,c.status,c.created_at,f.path FROM checkpoints c JOIN checkpoint_files f ON f.checkpoint_id=c.checkpoint_id `+where+` ORDER BY c.sequence DESC,f.path LIMIT 200`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	records := []checkpoint{}
	for rows.Next() {
		var r checkpoint
		var path string
		if err := rows.Scan(&r.ID, &r.Sequence, &r.Session, &r.Source, &r.Status, &r.Created, &path); err != nil {
			return nil, err
		}
		if len(records) == 0 || records[len(records)-1].ID != r.ID {
			records = append(records, r)
		}
		i := len(records) - 1
		records[i].Files = append(records[i].Files, checkpointFile{Path: path})
	}
	return records, rows.Err()
}

func (s *server) undoConversation() (string, error) {
	start := -1
	for i := len(s.snap.Messages) - 1; i >= 0; i-- {
		if s.snap.Messages[i]["role"] == "user" {
			start = i
			break
		}
	}
	if start < 0 {
		return "Nothing to undo.", nil
	}
	prompt, _ := s.snap.Messages[start]["content"].(string)
	boundary := -1
	if prompt != "" {
		for i := len(s.contents) - 1; i >= 0; i-- {
			content := s.contents[i]
			if content.Role == "user" && len(content.Parts) == 1 && content.Parts[0].Text == prompt && content.Parts[0].FunctionResponse == nil {
				boundary = i
				break
			}
		}
	}
	var contents []*genai.Content
	if boundary >= 0 {
		contents = settlePending(s.contents[:boundary])
	} else {
		var err error
		contents, err = legacyContents(s.snap.Messages[:start])
		if err != nil {
			return "", err
		}
	}
	removed := len(s.snap.Messages) - start
	s.snap.Messages = s.snap.Messages[:start]
	s.contents = contents
	s.historyReset = true
	return fmt.Sprintf("Undone (%d messages removed).", removed), nil
}

func (s *server) rewindCommand(ctx context.Context, turn string, args []string) (string, error) {
	yes := false
	parts := []string{}
	for _, arg := range args {
		if arg == "--yes" || arg == "-y" {
			yes = true
		} else {
			parts = append(parts, arg)
		}
	}
	mode, ref := "code", ""
	if len(parts) > 2 {
		return "", errors.New("usage: /rewind list|code|both|conversation [checkpoint_id] [--yes]")
	}
	if len(parts) > 0 {
		switch parts[0] {
		case "list", "code", "both", "conversation", "chat":
			mode = parts[0]
		default:
			ref = parts[0]
		}
	}
	if len(parts) == 2 {
		if ref != "" {
			return "", errors.New("unexpected rewind argument")
		}
		ref = parts[1]
	}
	if mode == "conversation" || mode == "chat" {
		if ref != "" {
			return "", errors.New("conversation undo does not accept a checkpoint ID")
		}
		return s.undoConversation()
	}
	if mode == "list" {
		if ref != "" {
			return "", errors.New("usage: /rewind list")
		}
		result := checkpointTool(ctx, s.scope, s.opts.root, "list_checkpoints", map[string]any{"session_id": s.snap.ID})
		if !result.Success {
			return "", errors.New(result.Error)
		}
		if result.Data["count"] == 0 {
			return "No active code checkpoints in this session.", nil
		}
		body, err := json.MarshalIndent(result.Data["items"], "", "  ")
		return string(body), err
	}
	store, err := openCheckpoints(s.opts.root)
	if err != nil {
		return "", err
	}
	defer store.Close()
	records, err := store.selectRecords(ctx, s.snap.ID, ref)
	if err != nil {
		return "", err
	}
	steps, err := planRestore(ctx, s.scope, records)
	if err != nil {
		return "", err
	}
	var preview strings.Builder
	for _, r := range records {
		fmt.Fprintf(&preview, "Checkpoint: %s (%s)\n", r.ID, r.Source)
	}
	for _, step := range steps {
		fmt.Fprintf(&preview, "\nFile: %s\n", step.change.path)
		if step.remove {
			preview.WriteString("Remove file created by this checkpoint.\n")
		}
		preview.WriteString(changePreview(step.change))
	}
	policy, err := persistentToolPolicy(s.opts.root, "restore_checkpoint")
	if err != nil {
		return "", err
	}
	if policy == "deny" {
		return "", errors.New("Blocked by persistent tool policy")
	}
	if !yes || policy == "ask" {
		allowed, reason, err := s.approve(ctx, "restore_checkpoint", map[string]any{"preview": capText(preview.String(), 20000)}, fileChange{})
		if err != nil {
			return "", err
		}
		if !allowed {
			return "Rewind cancelled: " + reason, nil
		}
	}
	paths, err := store.restore(ctx, s.scope, records)
	if err != nil {
		return "", err
	}
	message := fmt.Sprintf("Rewound %d file(s).\n%s", len(paths), strings.Join(paths, "\n"))
	if mode == "both" {
		undo, err := s.undoConversation()
		if err != nil {
			return message, err
		}
		message += "\n" + undo
	}
	return message, nil
}
