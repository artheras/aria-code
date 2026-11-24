//! Native Rust MCP stdio JSON-RPC 2.0 server (`aria-native mcp-serve`).
//!
//! Exposes `aria-code` workspace tools over MCP stdio without requiring Python.

use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use std::path::Path;

use super::tools;

const PROTOCOL_VERSION: &str = "2024-11-05";

fn is_read_only_tool(name: &str) -> bool {
    matches!(
        name,
        "list_files"
            | "read_file"
            | "search_code"
            | "git_status"
            | "git_diff"
            | "git_log"
            | "find_symbol"
            | "repo_map"
            | "glob_files"
            | "project_scan"
    )
}

pub fn mcp_tools_list(allow_write: bool) -> Vec<Value> {
    let schemas = tools::tool_schemas();
    let Some(arr) = schemas.as_array() else {
        return Vec::new();
    };
    arr.iter()
        .filter_map(|entry| {
            let func = entry.get("function")?;
            let name = func.get("name")?.as_str()?;
            let read_only = is_read_only_tool(name);
            if !allow_write && !read_only {
                return None;
            }
            Some(json!({
                "name": name,
                "description": func.get("description").and_then(Value::as_str).unwrap_or(""),
                "inputSchema": func.get("parameters").cloned().unwrap_or_else(|| json!({"type": "object"})),
                "annotations": {
                    "readOnlyHint": read_only
                }
            }))
        })
        .collect()
}

pub fn handle_jsonrpc_request(req: &Value, workspace: &Path, allow_write: bool) -> Option<Value> {
    let id = req.get("id").cloned();
    let method = req.get("method").and_then(Value::as_str).unwrap_or("");
    let params = req.get("params").cloned().unwrap_or_else(|| json!({}));

    // Notifications (no id) must not emit a response.
    let Some(id) = id else {
        return None;
    };

    let response = match method {
        "initialize" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": { "listChanged": false }
                },
                "serverInfo": {
                    "name": "aria-native",
                    "version": env!("CARGO_PKG_VERSION")
                }
            }
        }),
        "ping" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {}
        }),
        "tools/list" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "tools": mcp_tools_list(allow_write)
            }
        }),
        "tools/call" => {
            let name = params.get("name").and_then(Value::as_str).unwrap_or("");
            let args = params
                .get("arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            if name.is_empty() {
                json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "error": { "code": -32602, "message": "Missing tool name" }
                })
            } else {
                let result = tools::execute_tool(workspace, name, &args, allow_write);
                let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
                let text = serde_json::to_string_pretty(&result).unwrap_or_else(|_| result.to_string());
                json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "result": {
                        "content": [{ "type": "text", "text": text }],
                        "isError": !ok
                    }
                })
            }
        }
        _ => json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": { "code": -32601, "message": format!("Method not found: {method}") }
        }),
    };

    Some(response)
}

pub fn serve_stdio_with_io<R: BufRead, W: Write>(
    reader: R,
    mut writer: W,
    workspace: &Path,
    allow_write: bool,
) -> Result<(), String> {
    for line_res in reader.lines() {
        let line = line_res.map_err(|e| format!("MCP stdin read error: {e}"))?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let resp = match serde_json::from_str::<Value>(trimmed) {
            Ok(req) => handle_jsonrpc_request(&req, workspace, allow_write),
            Err(err) => Some(json!({
                "jsonrpc": "2.0",
                "id": Value::Null,
                "error": { "code": -32700, "message": format!("Parse error: {err}") }
            })),
        };
        if let Some(payload) = resp {
            let encoded = serde_json::to_string(&payload).map_err(|e| e.to_string())?;
            writeln!(writer, "{encoded}").map_err(|e| format!("MCP stdout write error: {e}"))?;
            writer
                .flush()
                .map_err(|e| format!("MCP stdout flush error: {e}"))?;
        }
    }
    Ok(())
}

pub fn serve_stdio(workspace: &Path, allow_write: bool) -> Result<(), String> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    serve_stdio_with_io(stdin.lock(), stdout.lock(), workspace, allow_write)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn mcp_serve_initialize_list_and_notification() {
        let dir = tempfile::tempdir().unwrap();
        let input = concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\n",
            "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n",
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"ping\"}\n",
            "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/list\",\"params\":{}}\n"
        );
        let mut out = Vec::new();
        serve_stdio_with_io(Cursor::new(input), &mut out, dir.path(), false).unwrap();
        let lines: Vec<Value> = String::from_utf8(out)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines.len(), 3);
        assert_eq!(lines[0]["result"]["serverInfo"]["name"], "aria-native");
        assert_eq!(lines[1]["id"], 2);
        let tools = lines[2]["result"]["tools"].as_array().unwrap();
        assert!(tools.iter().any(|t| t["name"] == "read_file"));
        assert!(!tools.iter().any(|t| t["name"] == "write_file"));
    }

    #[test]
    fn mcp_serve_tools_call_respects_write_policy() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("hello.txt"), "world\n").unwrap();
        let input = concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"read_file\",\"arguments\":{\"path\":\"hello.txt\"}}}\n",
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"write_file\",\"arguments\":{\"path\":\"blocked.txt\",\"content\":\"x\"}}}\n"
        );
        let mut out = Vec::new();
        serve_stdio_with_io(Cursor::new(input), &mut out, dir.path(), false).unwrap();
        let lines: Vec<Value> = String::from_utf8(out)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines[0]["result"]["isError"], false);
        assert!(lines[0]["result"]["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains("world"));
        assert_eq!(lines[1]["result"]["isError"], true);
        assert!(!dir.path().join("blocked.txt").exists());
    }

    #[test]
    fn mcp_serve_unknown_method_returns_32601() {
        let dir = tempfile::tempdir().unwrap();
        let input = "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"unknown/op\"}\n";
        let mut out = Vec::new();
        serve_stdio_with_io(Cursor::new(input), &mut out, dir.path(), true).unwrap();
        let resp: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(resp["error"]["code"], -32601);
    }
}
