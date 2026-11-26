//! Native Rust MCP stdio JSON-RPC 2.0 server (`aria-native mcp-serve`).
//!
//! Exposes `aria-code` workspace tools over MCP stdio without requiring Python.

use serde_json::{json, Value};
use std::io::{self, BufRead, Write};
use std::path::Path;

use super::{audit, license, skills, sql_readonly, tools};

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
            | "sql_query_readonly"
    )
}

fn sql_query_readonly_schema() -> Value {
    json!({
        "name": "sql_query_readonly",
        "description": "Execute a read-only SQL query (SELECT/WITH/EXPLAIN/PRAGMA) against a workspace SQLite database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "db": { "type": "string", "description": "Relative path to the SQLite file inside the workspace" },
                "sql": { "type": "string", "description": "Read-only SQL statement" },
                "max_rows": { "type": "integer", "description": "Maximum rows to return (1-500, default 100)" }
            },
            "required": ["db", "sql"]
        },
        "annotations": {
            "readOnlyHint": true
        }
    })
}

pub fn verify_external_license(
    token: Option<&str>,
    secret: Option<&str>,
    expected_package: Option<&str>,
    now_secs: u64,
) -> Result<(), String> {
    let secret = secret.map(str::trim).filter(|s| !s.is_empty());
    let Some(secret) = secret else {
        return Ok(());
    };
    let token = token
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            "ARIA_LICENSE_KEY is required when ARIA_LICENSE_SECRET is set".to_string()
        })?;
    license::verify_license(token, secret, expected_package, now_secs).map(|_| ())
}

fn check_env_license() -> Result<(), String> {
    let token = std::env::var("ARIA_LICENSE_KEY").ok();
    let secret = std::env::var("ARIA_LICENSE_SECRET").ok();
    let pkg = std::env::var("ARIA_LICENSE_PACKAGE")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    verify_external_license(
        token.as_deref(),
        secret.as_deref(),
        pkg.as_deref(),
        audit::now_unix_secs(),
    )
}

fn record_mcp_audit(workspace: &Path, tool_name: &str, approved: bool, outcome: &str) {
    let actor = std::env::var("ARIA_MCP_ACTOR")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "mcp-client".to_string());
    let event = audit::AuditEvent {
        timestamp_secs: audit::now_unix_secs(),
        actor,
        package: "aria-native-mcp".to_string(),
        action: "mcp_tools_call".to_string(),
        tool: Some(tool_name.to_string()),
        approved,
        outcome: outcome.to_string(),
    };
    let _ = audit::record_audit_event(workspace, &event);
}

pub fn mcp_tools_list(allow_write: bool) -> Vec<Value> {
    let schemas = tools::tool_schemas();
    let mut out: Vec<Value> = schemas
        .as_array()
        .map(|arr| {
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
        })
        .unwrap_or_default();
    out.push(sql_query_readonly_schema());
    out
}

pub fn handle_jsonrpc_request(req: &Value, workspace: &Path, allow_write: bool) -> Option<Value> {
    let id = req.get("id").cloned();
    let method = req.get("method").and_then(Value::as_str).unwrap_or("");
    let params = req.get("params").cloned().unwrap_or_else(|| json!({}));

    // Notifications (no id) must not emit a response.
    let id = id?;

    let response = match method {
        "initialize" => json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": { "listChanged": false },
                    "skills": { "listChanged": false }
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
        "skills/list" => {
            let discovered: Vec<Value> = skills::discover_skills(workspace)
                .into_iter()
                .map(|s| {
                    json!({
                        "name": s.name,
                        "description": s.description,
                        "scope": s.scope,
                        "path": s.path.display().to_string(),
                    })
                })
                .collect();
            json!({
                "jsonrpc": "2.0",
                "id": id,
                "result": {
                    "skills": discovered
                }
            })
        }
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
            } else if let Err(msg) = check_env_license() {
                record_mcp_audit(workspace, name, false, &format!("denied: {msg}"));
                let payload = json!({ "ok": false, "error": msg });
                json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "result": {
                        "content": [{ "type": "text", "text": payload.to_string() }],
                        "isError": true
                    }
                })
            } else {
                let result = if name == "sql_query_readonly" {
                    let db = args.get("db").and_then(Value::as_str).unwrap_or("");
                    let sql = args.get("sql").and_then(Value::as_str).unwrap_or("");
                    let max_rows =
                        args.get("max_rows").and_then(Value::as_u64).unwrap_or(100) as usize;
                    match sql_readonly::query_sqlite_readonly(workspace, db, sql, max_rows) {
                        Ok(data) => json!({ "ok": true, "data": data }),
                        Err(err) => json!({ "ok": false, "error": err }),
                    }
                } else {
                    tools::execute_tool(workspace, name, &args, allow_write)
                };
                let ok = result.get("ok").and_then(Value::as_bool).unwrap_or(false);
                record_mcp_audit(workspace, name, ok, if ok { "ok" } else { "error" });
                let text =
                    serde_json::to_string_pretty(&result).unwrap_or_else(|_| result.to_string());
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

    #[test]
    fn mcp_serve_skills_list_and_sql_readonly_audit() {
        let dir = tempfile::tempdir().unwrap();
        let skill_dir = dir.path().join(".aria").join("skills").join("ops-audit");
        std::fs::create_dir_all(&skill_dir).unwrap();
        std::fs::write(
            skill_dir.join("SKILL.md"),
            "---\nname: ops-audit\ndescription: Ops audit skill\n---\n\nRun checks.\n",
        )
        .unwrap();

        let input = concat!(
            "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"skills/list\",\"params\":{}}\n",
            "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"sql_query_readonly\",\"arguments\":{\"db\":\"missing.db\",\"sql\":\"DROP TABLE users\"}}}\n"
        );
        let mut out = Vec::new();
        serve_stdio_with_io(Cursor::new(input), &mut out, dir.path(), false).unwrap();
        let lines: Vec<Value> = String::from_utf8(out)
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        let skills_arr = lines[0]["result"]["skills"].as_array().unwrap();
        assert!(skills_arr.iter().any(|s| s["name"] == "ops-audit"));
        assert_eq!(lines[1]["result"]["isError"], true);
        let events = audit::read_audit_events(dir.path(), 10);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["tool"].as_str(), Some("sql_query_readonly"));
        assert_eq!(events[0]["approved"].as_bool(), Some(false));
    }

    #[test]
    fn mcp_serve_external_license_verification() {
        assert!(verify_external_license(None, None, None, 1000).is_ok());
        assert!(verify_external_license(None, Some("secret"), None, 1000).is_err());

        let claims = license::LicenseClaims {
            customer: "grok-bot".into(),
            package: "aria-mcp".into(),
            tier: "enterprise".into(),
            expires_at: 2000,
            seats: 10,
        };
        let token = license::issue_license(&claims, "top-secret").unwrap();
        assert!(
            verify_external_license(Some(&token), Some("top-secret"), Some("aria-mcp"), 1000)
                .is_ok()
        );
        assert!(verify_external_license(
            Some(&token),
            Some("wrong-secret"),
            Some("aria-mcp"),
            1000
        )
        .is_err());
        assert!(
            verify_external_license(Some(&token), Some("top-secret"), Some("other-pkg"), 1000)
                .is_err()
        );
    }
}
