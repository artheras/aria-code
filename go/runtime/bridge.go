package main

// The one-request bridge is deliberately narrower than the application: its
// scope is the selected workspace and its grant names one write for one call.
import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

func rpcError(code int, message string, id any) map[string]any {
	return map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": message}}
}

func bridgeDispatch(ctx context.Context, raw []byte, workspace, root, approved string) map[string]any {
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		return rpcError(-32700, "Parse error", nil)
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return rpcError(-32700, "Parse error", nil)
	}
	req, ok := value.(map[string]any)
	if !ok || req == nil {
		return rpcError(-32600, "Expected a single JSON-RPC request object", nil)
	}
	id, hasID := req["id"]
	validID := id == nil
	switch value := id.(type) {
	case string:
		validID = true
	case json.Number:
		validID = !strings.ContainsAny(string(value), ".eE")
	}
	method, methodOK := req["method"].(string)
	if req["jsonrpc"] != "2.0" || !methodOK || !validID {
		return rpcError(-32600, "Invalid JSON-RPC request", nil)
	}
	// A notification never executes, even when a host write grant was supplied.
	if !hasID {
		return nil
	}
	if method != "tools.call" {
		return rpcError(-32601, "Method not found", id)
	}
	params, ok := req["params"].(map[string]any)
	if !ok {
		return rpcError(-32602, "Expected name and arguments object", id)
	}
	name, nameOK := params["name"].(string)
	args, argsOK := params["arguments"].(map[string]any)
	if !nameOK || !argsOK || args == nil {
		return rpcError(-32602, "Expected name and arguments object", id)
	}
	// File offsets expect ordinary JSON numeric values. Re-decode only args;
	// the request ID stays exact rather than passing through float64.
	encoded, _ := json.Marshal(args)
	if err := json.Unmarshal(encoded, &args); err != nil {
		return rpcError(-32602, "Invalid tool arguments", id)
	}
	result := bridgeTool(ctx, workspace, root, approved, name, args)
	return map[string]any{"jsonrpc": "2.0", "id": id, "result": result}
}

func bridgeTool(ctx context.Context, workspace, root, approved, name string, args map[string]any) toolResult {
	write := name == "write_file" || name == "edit_file" || name == "restore_checkpoint" || name == "apply_change"
	if !write && name != "read_file" && name != "list_files" && name != "search_code" && name != "list_checkpoints" && name != "list_changes" && name != "reject_change" {
		return failed(fmt.Errorf("Tool not exposed by native bridge: %s", name))
	}
	if err := noHostFlags(args); err != nil {
		return failed(err)
	}
	policy, err := persistentToolPolicy(root, name)
	if err != nil {
		return failed(err)
	}
	if policy == "deny" {
		return failed(errors.New("Blocked by persistent tool policy"))
	}
	granted := write && approved == name
	if policy == "ask" && !granted {
		return failed(errors.New("Tool requires explicit approval; use the interactive application"))
	}
	if write && !granted {
		return failed(errors.New("Writes require host --approve-write for this invocation"))
	}
	scope, err := newScope(workspace, nil, nil, false)
	if err != nil {
		return failed(err)
	}
	info, err := os.Stat(scope.workspace)
	if err != nil {
		return failed(err)
	}
	if !info.IsDir() {
		return failed(errors.New("workspace must be a directory"))
	}
	scope.readOnly = !granted
	if ctx.Err() != nil {
		return failed(ctx.Err())
	}
	if name == "list_checkpoints" || name == "restore_checkpoint" {
		return checkpointTool(ctx, scope, root, name, args)
	}
	if name == "list_changes" {
		return stagedList(ctx, scope, root, str(args, "session_id", ""), args["include_closed"] == true)
	}
	if name == "reject_change" {
		return stagedReject(ctx, scope, root, str(args, "change_id", ""))
	}
	if name == "apply_change" {
		store, err := openCheckpoints(root)
		if err != nil {
			return failed(err)
		}
		change, err := store.getStage(ctx, scope, str(args, "change_id", ""))
		store.Close()
		if err != nil {
			return failed(err)
		}
		policy, err := persistentToolPolicy(root, change.Source)
		if err != nil {
			return failed(err)
		}
		if policy == "deny" {
			return failed(errors.New("Blocked by persistent tool policy"))
		}
		return stagedApply(ctx, scope, root, change)
	}
	if write {
		change, err := propose(scope, name, args)
		if err != nil {
			return failed(err)
		}
		if staged, _ := stageOnly(args); staged {
			return stageRecordedChange(ctx, scope, root, "", name, change)
		}
		return applyRecordedChange(ctx, scope, root, "", name, change)
	}
	switch name {
	case "read_file":
		return fileRead(ctx, scope, args)
	case "list_files":
		return fileList(ctx, scope, args)
	default:
		return searchCode(ctx, scope, args)
	}
}

func bridgeCommand(ctx context.Context, args []string, in io.Reader, out, diagnostics io.Writer) int {
	flags := flag.NewFlagSet("bridge", flag.ContinueOnError)
	flags.SetOutput(diagnostics)
	var workspace, approved string
	flags.StringVar(&workspace, "workspace", "", "selected workspace")
	flags.StringVar(&approved, "approve-tool", "", "one explicitly approved write")
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if workspace == "" || len(flags.Args()) != 0 || (approved != "" && approved != "write_file" && approved != "edit_file" && approved != "restore_checkpoint" && approved != "apply_change") {
		fmt.Fprintln(diagnostics, "bridge requires --workspace and optionally one supported --approve-tool")
		return 2
	}
	// This native mode shares the host containment handshake with app/stream.
	if err := hostBarrier(ctx, in); err != nil {
		return 2
	}
	root, err := stateRoot()
	if err != nil {
		fmt.Fprintln(diagnostics, err)
		return 1
	}
	raw, err := bufio.NewReader(io.LimitReader(in, maxMessage+1)).ReadBytes('\n')
	var response map[string]any
	if len(raw) > maxMessage {
		response = rpcError(-32600, "Request exceeds 1 MiB", nil)
	} else if err != nil && err != io.EOF {
		response = rpcError(-32700, "Parse error", nil)
	} else {
		response = bridgeDispatch(ctx, raw, workspace, root, approved)
	}
	if response == nil {
		return 0
	}
	body, err := json.Marshal(response)
	if err != nil {
		return 1
	}
	if len(body)+1 > maxMessage {
		body, _ = json.Marshal(rpcError(-32603, "Response exceeds 1 MiB", response["id"]))
	}
	n, err := out.Write(append(body, '\n'))
	if err != nil || n != len(body)+1 {
		return 1
	}
	return 0
}
