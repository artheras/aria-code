//! Native Rust MCP (Model Context Protocol) JSON-RPC 2.0 stdio client.

use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
};

pub struct McpStdioClient {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: AtomicU64,
}

impl McpStdioClient {
    pub fn spawn(program: &str, args: &[&str]) -> Result<Self, String> {
        let arg_strings: Vec<String> = args.iter().map(|s| (*s).to_owned()).collect();
        Self::spawn_with_env(program, &arg_strings, &HashMap::new())
    }

    pub fn spawn_with_env(
        program: &str,
        args: &[String],
        env: &HashMap<String, String>,
    ) -> Result<Self, String> {
        let mut cmd = Command::new(program);
        cmd.args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("Failed to spawn MCP server `{program}`: {e}"))?;
        let stdin = child.stdin.take().ok_or("Missing MCP stdin")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("Missing MCP stdout")?);
        let mut client = Self {
            child,
            stdin,
            stdout,
            next_id: AtomicU64::new(1),
        };
        client.initialize()?;
        Ok(client)
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let msg = format_jsonrpc_request(id, method, params);
        let line = serde_json::to_string(&msg).map_err(|e| e.to_string())?;
        self.stdin
            .write_all(line.as_bytes())
            .and_then(|_| self.stdin.write_all(b"\n"))
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("MCP write failed: {e}"))?;

        let mut buf = String::new();
        loop {
            buf.clear();
            let n = self
                .stdout
                .read_line(&mut buf)
                .map_err(|e| format!("MCP read failed: {e}"))?;
            if n == 0 {
                return Err("MCP server closed stdout".into());
            }
            let Ok(resp) = serde_json::from_str::<Value>(&buf) else {
                continue;
            };
            if resp.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(err) = resp.get("error") {
                    return Err(format!("MCP error: {err}"));
                }
                return Ok(resp.get("result").cloned().unwrap_or(Value::Null));
            }
        }
    }

    fn initialize(&mut self) -> Result<(), String> {
        let _ = self.request(
            "initialize",
            json!({
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": { "name": "aria-native", "version": env!("CARGO_PKG_VERSION") }
            }),
        )?;
        let notif = json!({ "jsonrpc": "2.0", "method": "notifications/initialized" });
        let line = serde_json::to_string(&notif).map_err(|e| e.to_string())?;
        let _ = self.stdin.write_all(line.as_bytes());
        let _ = self.stdin.write_all(b"\n");
        let _ = self.stdin.flush();
        Ok(())
    }

    pub fn list_tools(&mut self) -> Result<Vec<Value>, String> {
        let res = self.request("tools/list", json!({}))?;
        Ok(res
            .get("tools")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    #[allow(dead_code)]
    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value, String> {
        self.request(
            "tools/call",
            json!({ "name": name, "arguments": arguments }),
        )
    }
}

impl Drop for McpStdioClient {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Format a JSON-RPC 2.0 request line for MCP callers.
pub fn format_jsonrpc_request(id: u64, method: &str, params: Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": params,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpServerConfig {
    pub name: String,
    pub command: String,
    pub args: Vec<String>,
    pub env: HashMap<String, String>,
}

fn parse_mcp_file(path: &Path, out: &mut HashMap<String, McpServerConfig>) {
    let Ok(raw) = fs::read_to_string(path) else {
        return;
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return;
    };
    let Some(servers) = val.get("mcpServers").and_then(Value::as_object) else {
        return;
    };
    for (name, cfg) in servers {
        let Some(command) = cfg.get("command").and_then(Value::as_str) else {
            continue;
        };
        if command.trim().is_empty() {
            continue;
        }
        let args = cfg
            .get("args")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let env = cfg
            .get("env")
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_owned())))
                    .collect()
            })
            .unwrap_or_default();
        out.insert(
            name.clone(),
            McpServerConfig {
                name: name.clone(),
                command: command.to_owned(),
                args,
                env,
            },
        );
    }
}

pub fn load_mcp_server_configs(workspace: &Path) -> Vec<McpServerConfig> {
    let mut map = HashMap::new();
    if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
        parse_mcp_file(&Path::new(&home).join(".aria").join("mcp.json"), &mut map);
    }
    parse_mcp_file(&workspace.join(".mcp.json"), &mut map);
    let mut list: Vec<McpServerConfig> = map.into_values().collect();
    list.sort_by(|a, b| a.name.cmp(&b.name));
    list
}

/// Live registry of external stdio MCP servers discovered from `~/.aria/mcp.json` and `<workspace>/.mcp.json`.
pub struct McpRegistry {
    clients: HashMap<String, McpStdioClient>,
    routes: HashMap<String, (String, String)>,
    schemas: Vec<Value>,
}

impl McpRegistry {
    pub fn load_from_workspace(workspace: &Path) -> Self {
        let mut clients = HashMap::new();
        let mut routes = HashMap::new();
        let mut schemas = Vec::new();
        for cfg in load_mcp_server_configs(workspace) {
            let Ok(mut client) = McpStdioClient::spawn_with_env(&cfg.command, &cfg.args, &cfg.env)
            else {
                continue;
            };
            let Ok(tools) = client.list_tools() else {
                continue;
            };
            let safe_server = cfg.name.replace(['.', '-', ' '], "_");
            for tool in tools {
                let Some(orig_name) = tool.get("name").and_then(Value::as_str) else {
                    continue;
                };
                let safe_tool = orig_name.replace(['.', '-', ' '], "_");
                let exposed = format!("mcp__{safe_server}__{safe_tool}");
                let desc = tool
                    .get("description")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let params = tool
                    .get("inputSchema")
                    .cloned()
                    .unwrap_or_else(|| json!({"type": "object", "properties": {}}));
                schemas.push(json!({
                    "type": "function",
                    "function": {
                        "name": exposed,
                        "description": format!("[MCP:{}] {}", cfg.name, desc),
                        "parameters": params,
                    }
                }));
                routes.insert(exposed, (cfg.name.clone(), orig_name.to_owned()));
            }
            clients.insert(cfg.name, client);
        }
        Self {
            clients,
            routes,
            schemas,
        }
    }

    pub fn tool_schemas(&self) -> &[Value] {
        &self.schemas
    }

    pub fn call_tool(&mut self, exposed_name: &str, arguments: Value) -> Option<Value> {
        let (server, orig_name) = self.routes.get(exposed_name)?.clone();
        let client = self.clients.get_mut(&server)?;
        match client.call_tool(&orig_name, arguments) {
            Ok(data) => Some(json!({ "ok": true, "data": data })),
            Err(err) => Some(json!({ "ok": false, "error": err })),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_valid_jsonrpc_mcp_requests() {
        let req = format_jsonrpc_request(7, "tools/call", json!({"name": "ping"}));
        assert_eq!(req["jsonrpc"], "2.0");
        assert_eq!(req["id"], 7);
        assert_eq!(req["method"], "tools/call");
        assert_eq!(req["params"]["name"], "ping");
    }

    #[test]
    fn loads_mcp_server_configs_from_workspace_mcp_json() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join(".mcp.json"),
            r#"{
                "mcpServers": {
                    "erp-service": {
                        "command": "erp-service-mcp",
                        "args": ["--stdio"],
                        "env": { "WAREHOUSE_ERP_BASE_URL": "https://erp.example.com" }
                    }
                }
            }"#,
        )
        .unwrap();
        let cfgs = load_mcp_server_configs(dir.path());
        assert!(cfgs.iter().any(|c| {
            c.name == "erp-service"
                && c.command == "erp-service-mcp"
                && c.args == vec!["--stdio"]
                && c.env.get("WAREHOUSE_ERP_BASE_URL").map(String::as_str)
                    == Some("https://erp.example.com")
        }));
    }
}
