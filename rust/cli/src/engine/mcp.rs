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

fn insert_mcp_entry(name: &str, cfg: &Value, out: &mut HashMap<String, McpServerConfig>) {
    let Some(command) = cfg.get("command").and_then(Value::as_str) else {
        return;
    };
    if name.trim().is_empty() || command.trim().is_empty() {
        return;
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
        name.to_owned(),
        McpServerConfig {
            name: name.to_owned(),
            command: command.to_owned(),
            args,
            env,
        },
    );
}

fn parse_mcp_file(path: &Path, out: &mut HashMap<String, McpServerConfig>) {
    let Ok(raw) = fs::read_to_string(path) else {
        return;
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return;
    };
    if let Some(servers) = val.get("mcpServers").and_then(Value::as_object) {
        for (name, cfg) in servers {
            insert_mcp_entry(name, cfg, out);
        }
    }
    if let Some(servers) = val.get("servers").and_then(Value::as_array) {
        for cfg in servers {
            if let Some(name) = cfg.get("name").and_then(Value::as_str) {
                insert_mcp_entry(name, cfg, out);
            }
        }
    }
}

pub fn load_mcp_server_configs(workspace: &Path) -> Vec<McpServerConfig> {
    let mut map = HashMap::new();
    if !cfg!(test) {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            let h = Path::new(&home);
            parse_mcp_file(&h.join(".arthera").join("mcp_servers.json"), &mut map);
            parse_mcp_file(&h.join(".aria").join("mcp_servers.json"), &mut map);
            parse_mcp_file(&h.join(".aria").join("mcp.json"), &mut map);
        }
    }
    parse_mcp_file(&workspace.join(".mcp.json"), &mut map);
    let mut list: Vec<McpServerConfig> = map.into_values().collect();
    list.sort_by(|a, b| a.name.cmp(&b.name));
    list
}

/// Detect sibling Arthera ecosystem MCP servers (`quant_engine`, `commerce`, `erp-service`,
/// `financial-service`, `healthcare-service`, `aria-logistics`).
pub fn discover_ecosystem_mcp_servers(workspace: &Path) -> Vec<McpServerConfig> {
    let mut found = Vec::new();
    let base = workspace
        .canonicalize()
        .unwrap_or_else(|_| workspace.to_path_buf());
    let Some(parent) = base.parent() else {
        return found;
    };

    let arthera = parent.join("Arthera");
    let quant_mcp = arthera
        .join("packages")
        .join("quant_engine")
        .join("mcp_server.py");
    if quant_mcp.is_file() {
        let mut env = HashMap::new();
        env.insert("PYTHONPATH".into(), arthera.to_string_lossy().into_owned());
        found.push(McpServerConfig {
            name: "arthera-quant".into(),
            command: "python3".into(),
            args: vec![quant_mcp.to_string_lossy().into_owned()],
            env,
        });
    }

    let commerce_mcp = arthera
        .join("packages")
        .join("commerce_mcp_server")
        .join("server.py");
    if commerce_mcp.is_file() {
        let mut env = HashMap::new();
        env.insert(
            "PYTHONPATH".into(),
            arthera.join("packages").to_string_lossy().into_owned(),
        );
        found.push(McpServerConfig {
            name: "arthera-commerce".into(),
            command: "python3".into(),
            args: vec![commerce_mcp.to_string_lossy().into_owned()],
            env,
        });
    }

    let erp_root = parent.join("erp-service");
    let erp_venv_bin = erp_root.join(".venv").join("bin").join("erp-service-mcp");
    let erp_mcp = erp_root
        .join("src")
        .join("erp_service")
        .join("mcp_server.py");
    if erp_venv_bin.is_file() {
        found.push(McpServerConfig {
            name: "erp-service".into(),
            command: erp_venv_bin.to_string_lossy().into_owned(),
            args: Vec::new(),
            env: HashMap::new(),
        });
    } else if erp_mcp.is_file() {
        let mut env = HashMap::new();
        env.insert(
            "PYTHONPATH".into(),
            erp_root.join("src").to_string_lossy().into_owned(),
        );
        found.push(McpServerConfig {
            name: "erp-service".into(),
            command: "python3".into(),
            args: vec![erp_mcp.to_string_lossy().into_owned()],
            env,
        });
    }

    // `artheras/financial-service`: read-only finance (`arthera-finance`) and
    // separate paper/trading server (`arthera-trading`). Prefer the service
    // venv entrypoint or `python -I -m ...` (isolated mode). Never hardcode
    // FINANCIAL_SERVICE_NETWORK or FINANCIAL_SERVICE_BROKERS_ENABLED here.
    let fin_root = parent.join("financial-service");
    let fin_venv_bin = fin_root
        .join(".venv")
        .join("bin")
        .join("arthera-financial-service");
    let fin_venv_py = fin_root.join(".venv").join("bin").join("python");
    let fin_src = fin_root.join("src");
    let fin_server_py = fin_src.join("arthera_finance").join("server.py");
    if fin_venv_bin.is_file() {
        found.push(McpServerConfig {
            name: "arthera-finance".into(),
            command: fin_venv_bin.to_string_lossy().into_owned(),
            args: Vec::new(),
            env: HashMap::new(),
        });
    } else if fin_venv_py.is_file() && fin_server_py.is_file() {
        found.push(McpServerConfig {
            name: "arthera-finance".into(),
            command: fin_venv_py.to_string_lossy().into_owned(),
            args: vec!["-I".into(), "-m".into(), "arthera_finance.server".into()],
            env: HashMap::new(),
        });
    } else if fin_server_py.is_file() {
        let mut env = HashMap::new();
        env.insert("PYTHONPATH".into(), fin_src.to_string_lossy().into_owned());
        found.push(McpServerConfig {
            name: "arthera-finance".into(),
            command: "python3".into(),
            args: vec!["-m".into(), "arthera_finance.server".into()],
            env,
        });
    }

    let trading_venv_bin = fin_root.join(".venv").join("bin").join("arthera-trading");
    let trading_server_py = fin_src.join("arthera_finance").join("trading_server.py");
    if trading_venv_bin.is_file() {
        found.push(McpServerConfig {
            name: "arthera-trading".into(),
            command: trading_venv_bin.to_string_lossy().into_owned(),
            args: Vec::new(),
            env: HashMap::new(),
        });
    } else if fin_venv_py.is_file() && trading_server_py.is_file() {
        found.push(McpServerConfig {
            name: "arthera-trading".into(),
            command: fin_venv_py.to_string_lossy().into_owned(),
            args: vec![
                "-I".into(),
                "-m".into(),
                "arthera_finance.trading_server".into(),
            ],
            env: HashMap::new(),
        });
    } else if trading_server_py.is_file() {
        let mut env = HashMap::new();
        env.insert("PYTHONPATH".into(), fin_src.to_string_lossy().into_owned());
        found.push(McpServerConfig {
            name: "arthera-trading".into(),
            command: "python3".into(),
            args: vec!["-m".into(), "arthera_finance.trading_server".into()],
            env,
        });
    }

    // `artheras/healthcare-service`: biomedical literature & evidence over MCP.
    let hc_root = parent.join("healthcare-service");
    let hc_venv_bin = hc_root.join(".venv").join("bin").join("aria-healthcare");
    let hc_src = hc_root.join("src");
    let hc_server_py = hc_src.join("aria_healthcare").join("server.py");
    if hc_venv_bin.is_file() {
        found.push(McpServerConfig {
            name: "aria-healthcare".into(),
            command: hc_venv_bin.to_string_lossy().into_owned(),
            args: Vec::new(),
            env: HashMap::new(),
        });
    } else if hc_server_py.is_file() {
        let mut env = HashMap::new();
        env.insert("PYTHONPATH".into(), hc_src.to_string_lossy().into_owned());
        found.push(McpServerConfig {
            name: "aria-healthcare".into(),
            command: "python3".into(),
            args: vec!["-m".into(), "aria_healthcare.server".into()],
            env,
        });
    }

    let bots_root = base.join("services").join("aria-bots");
    let bots_mcp = bots_root
        .join("src")
        .join("aria_bots")
        .join("logistics")
        .join("mcp_server.py");
    if bots_mcp.is_file() {
        let mut env = HashMap::new();
        env.insert(
            "PYTHONPATH".into(),
            bots_root.join("src").to_string_lossy().into_owned(),
        );
        found.push(McpServerConfig {
            name: "aria-logistics".into(),
            command: "python3".into(),
            args: vec![bots_mcp.to_string_lossy().into_owned()],
            env,
        });
    }

    found
}

/// Merge discovered or specified MCP server configs into `<workspace>/.mcp.json`.
pub fn write_workspace_mcp_json(
    workspace: &Path,
    servers: &[McpServerConfig],
) -> Result<usize, String> {
    let path = workspace.join(".mcp.json");
    let mut root: Value = fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .filter(|v: &Value| v.is_object())
        .unwrap_or_else(|| json!({ "mcpServers": {} }));
    let obj = root
        .as_object_mut()
        .ok_or("Invalid .mcp.json root object")?;
    let entry = obj.entry("mcpServers").or_insert_with(|| json!({}));
    if !entry.is_object() {
        *entry = json!({});
    }
    let map = entry.as_object_mut().ok_or("Invalid mcpServers object")?;
    for srv in servers {
        map.insert(
            srv.name.clone(),
            json!({
                "command": srv.command,
                "args": srv.args,
                "env": srv.env,
            }),
        );
    }
    let pretty = serde_json::to_string_pretty(&root).map_err(|e| e.to_string())?;
    fs::write(&path, format!("{pretty}\n"))
        .map_err(|e| format!("Failed to write .mcp.json: {e}"))?;
    Ok(servers.len())
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

    #[test]
    fn discovers_sibling_ecosystem_servers_and_writes_mcp_json() {
        let root = tempfile::tempdir().unwrap();
        let ws = root.path().join("aria-code");
        let erp = root
            .path()
            .join("erp-service")
            .join("src")
            .join("erp_service");
        let fin = root
            .path()
            .join("financial-service")
            .join("src")
            .join("arthera_finance");
        let hc = root
            .path()
            .join("healthcare-service")
            .join("src")
            .join("aria_healthcare");
        fs::create_dir_all(&ws).unwrap();
        fs::create_dir_all(&erp).unwrap();
        fs::create_dir_all(&fin).unwrap();
        fs::create_dir_all(&hc).unwrap();
        fs::write(erp.join("mcp_server.py"), "# stub").unwrap();
        fs::write(fin.join("server.py"), "# stub").unwrap();
        fs::write(fin.join("trading_server.py"), "# stub").unwrap();
        fs::write(hc.join("server.py"), "# stub").unwrap();

        let found = discover_ecosystem_mcp_servers(&ws);
        assert_eq!(found.len(), 4);
        assert!(found.iter().any(|c| c.name == "erp-service"));
        assert!(found.iter().any(|c| c.name == "arthera-finance"));
        assert!(found.iter().any(|c| c.name == "arthera-trading"));
        assert!(found.iter().any(|c| c.name == "aria-healthcare"));
        assert_eq!(write_workspace_mcp_json(&ws, &found).unwrap(), 4);

        let reloaded = load_mcp_server_configs(&ws);
        assert!(reloaded.iter().any(|c| c.name == "erp-service"));
        assert!(reloaded.iter().any(|c| c.name == "arthera-finance"));
        assert!(reloaded.iter().any(|c| c.name == "aria-healthcare"));
    }
}
