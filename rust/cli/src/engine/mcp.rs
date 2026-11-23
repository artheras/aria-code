//! Native Rust MCP (Model Context Protocol) JSON-RPC 2.0 stdio client.

use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::Instant,
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

const MCP_HEADER_ENV_PREFIX: &str = "MCP_HEADER:";

pub fn is_remote_http_url(command: &str) -> bool {
    let trimmed = command.trim();
    trimmed.starts_with("http://") || trimmed.starts_with("https://")
}

/// Parse either plain JSON-RPC or Streamable HTTP SSE (`data: {...}`) response body.
pub fn parse_http_jsonrpc_body(body: &str, expected_id: u64) -> Result<Value, String> {
    let trimmed = body.trim();
    if let Ok(val) = serde_json::from_str::<Value>(trimmed) {
        if let Some(err) = val.get("error") {
            return Err(format!("MCP HTTP error: {err}"));
        }
        return Ok(val.get("result").cloned().unwrap_or(Value::Null));
    }
    for line in trimmed.lines() {
        let Some(payload) = line.strip_prefix("data:").map(str::trim) else {
            continue;
        };
        if payload.is_empty() || payload == "[DONE]" {
            continue;
        }
        if let Ok(val) = serde_json::from_str::<Value>(payload) {
            if val.get("id").and_then(Value::as_u64) == Some(expected_id)
                || val.get("result").is_some()
                || val.get("error").is_some()
            {
                if let Some(err) = val.get("error") {
                    return Err(format!("MCP HTTP SSE error: {err}"));
                }
                return Ok(val.get("result").cloned().unwrap_or(Value::Null));
            }
        }
    }
    Err("Unparseable MCP HTTP response body".into())
}

/// Native Rust Streamable HTTP MCP JSON-RPC 2.0 client (zero local Python required).
pub struct McpHttpClient {
    url: String,
    headers: HashMap<String, String>,
    session_id: Option<String>,
    next_id: AtomicU64,
}

impl McpHttpClient {
    pub fn connect(url: &str, headers: &HashMap<String, String>) -> Result<Self, String> {
        let mut client = Self {
            url: url.trim().to_owned(),
            headers: headers.clone(),
            session_id: None,
            next_id: AtomicU64::new(1),
        };
        client.initialize()?;
        Ok(client)
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let msg = format_jsonrpc_request(id, method, params);
        let payload = serde_json::to_string(&msg).map_err(|e| e.to_string())?;
        let mut req = ureq::post(&self.url)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json, text/event-stream");
        if let Some(sid) = &self.session_id {
            req = req.header("Mcp-Session-Id", sid);
        }
        for (k, v) in &self.headers {
            req = req.header(k, v);
        }
        let mut resp = req
            .send(payload.as_bytes())
            .map_err(|e| format!("MCP HTTP request failed: {e}"))?;
        if let Some(sid) = resp
            .headers()
            .get("mcp-session-id")
            .and_then(|h| h.to_str().ok())
        {
            self.session_id = Some(sid.to_owned());
        }
        let body = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("MCP HTTP read failed: {e}"))?;
        parse_http_jsonrpc_body(&body, id)
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

    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value, String> {
        self.request(
            "tools/call",
            json!({ "name": name, "arguments": arguments }),
        )
    }
}

/// Unified native Rust MCP client supporting both local stdio and remote Streamable HTTP.
pub enum McpClient {
    Stdio(McpStdioClient),
    Http(McpHttpClient),
}

impl McpClient {
    pub fn connect(cfg: &McpServerConfig) -> Result<Self, String> {
        if is_remote_http_url(&cfg.command) {
            let headers: HashMap<String, String> = cfg
                .env
                .iter()
                .filter_map(|(k, v)| {
                    k.strip_prefix(MCP_HEADER_ENV_PREFIX)
                        .map(|name| (name.to_owned(), v.clone()))
                })
                .collect();
            Ok(Self::Http(McpHttpClient::connect(&cfg.command, &headers)?))
        } else {
            Ok(Self::Stdio(McpStdioClient::spawn_with_env(
                &cfg.command,
                &cfg.args,
                &cfg.env,
            )?))
        }
    }

    pub fn list_tools(&mut self) -> Result<Vec<Value>, String> {
        match self {
            Self::Stdio(c) => c.list_tools(),
            Self::Http(c) => c.list_tools(),
        }
    }

    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value, String> {
        match self {
            Self::Stdio(c) => c.call_tool(name, arguments),
            Self::Http(c) => c.call_tool(name, arguments),
        }
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
    if name.trim().is_empty() {
        return;
    }
    if let Some(url) = cfg.get("url").and_then(Value::as_str) {
        if is_remote_http_url(url) {
            let mut env = HashMap::new();
            if let Some(headers) = cfg.get("headers").and_then(Value::as_object) {
                for (k, v) in headers {
                    if let Some(s) = v.as_str() {
                        env.insert(format!("{MCP_HEADER_ENV_PREFIX}{k}"), s.to_owned());
                    }
                }
            }
            out.insert(
                name.to_owned(),
                McpServerConfig {
                    name: name.to_owned(),
                    command: url.trim().to_owned(),
                    args: Vec::new(),
                    env,
                },
            );
            return;
        }
    }
    let Some(command) = cfg.get("command").and_then(Value::as_str) else {
        return;
    };
    if command.trim().is_empty() {
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
    if !cfg!(test) {
        for srv in discover_ecosystem_mcp_servers(workspace) {
            map.entry(srv.name.clone()).or_insert(srv);
        }
    }
    let mut list: Vec<McpServerConfig> = map.into_values().collect();
    list.sort_by(|a, b| a.name.cmp(&b.name));
    list
}

fn venv_bin_path(root: &Path, bin_name: &str) -> Option<PathBuf> {
    let unix = root.join(".venv").join("bin").join(bin_name);
    if unix.is_file() {
        return Some(unix);
    }
    let win_exe = root
        .join(".venv")
        .join("Scripts")
        .join(format!("{bin_name}.exe"));
    if win_exe.is_file() {
        return Some(win_exe);
    }
    let win = root.join(".venv").join("Scripts").join(bin_name);
    if win.is_file() {
        return Some(win);
    }
    None
}

fn venv_python_path(root: &Path) -> Option<PathBuf> {
    venv_bin_path(root, "python").or_else(|| venv_bin_path(root, "python3"))
}

fn build_python_service_config(
    name: &str,
    root: &Path,
    venv_bin: &str,
    src_dir: &Path,
    module: &str,
    entry_file: &Path,
) -> Option<McpServerConfig> {
    if let Some(bin) = venv_bin_path(root, venv_bin) {
        return Some(McpServerConfig {
            name: name.into(),
            command: bin.to_string_lossy().into_owned(),
            args: Vec::new(),
            env: HashMap::new(),
        });
    }
    if !entry_file.is_file() {
        return None;
    }
    if let Some(py) = venv_python_path(root) {
        return Some(McpServerConfig {
            name: name.into(),
            command: py.to_string_lossy().into_owned(),
            args: vec!["-I".into(), "-m".into(), module.into()],
            env: HashMap::new(),
        });
    }
    let mut env = HashMap::new();
    env.insert("PYTHONPATH".into(), src_dir.to_string_lossy().into_owned());
    Some(McpServerConfig {
        name: name.into(),
        command: "python3".into(),
        args: vec!["-m".into(), module.into()],
        env,
    })
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
    let erp_src = erp_root.join("src");
    let erp_mcp = erp_src.join("erp_service").join("mcp_server.py");
    if let Some(cfg) = build_python_service_config(
        "erp-service",
        &erp_root,
        "erp-service-mcp",
        &erp_src,
        "erp_service.mcp_server",
        &erp_mcp,
    ) {
        found.push(cfg);
    }

    // `artheras/financial-service`: read-only finance (`arthera-finance`) and
    // separate paper/trading server (`arthera-trading`). Prefer `<workspace>/services/financial-service`
    // or sibling `../financial-service` using isolated `python -I -m ...` or venv entrypoint.
    // Never hardcode FINANCIAL_SERVICE_NETWORK or FINANCIAL_SERVICE_BROKERS_ENABLED here.
    let fin_candidates = [
        base.join("services").join("financial-service"),
        parent.join("financial-service"),
    ];
    for fin_root in &fin_candidates {
        let fin_src = fin_root.join("src");
        let fin_server_py = fin_src.join("arthera_finance").join("server.py");
        if let Some(cfg) = build_python_service_config(
            "arthera-finance",
            fin_root,
            "arthera-financial-service",
            &fin_src,
            "arthera_finance.server",
            &fin_server_py,
        ) {
            found.push(cfg);
            let trading_server_py = fin_src.join("arthera_finance").join("trading_server.py");
            if let Some(tcfg) = build_python_service_config(
                "arthera-trading",
                fin_root,
                "arthera-trading",
                &fin_src,
                "arthera_finance.trading_server",
                &trading_server_py,
            ) {
                found.push(tcfg);
            }
            break;
        }
    }

    // `artheras/healthcare-service`: biomedical literature & evidence over MCP.
    let hc_root = parent.join("healthcare-service");
    let hc_src = hc_root.join("src");
    let hc_server_py = hc_src.join("aria_healthcare").join("server.py");
    if let Some(cfg) = build_python_service_config(
        "aria-healthcare",
        &hc_root,
        "aria-healthcare",
        &hc_src,
        "aria_healthcare.server",
        &hc_server_py,
    ) {
        found.push(cfg);
    }

    // `artheras/aria-plugins` creative-media plugin: Figma, Canva, OpenAI Images, Kling, Runway.
    let creative_py = parent
        .join("aria-plugins")
        .join("plugins")
        .join("creative-media")
        .join("python");
    let creative_mcp = creative_py.join("aria_creative").join("mcp_server.py");
    if creative_mcp.is_file() {
        let mut env = HashMap::new();
        env.insert(
            "PYTHONPATH".into(),
            creative_py.to_string_lossy().into_owned(),
        );
        found.push(McpServerConfig {
            name: "aria-creative-media".into(),
            command: "python3".into(),
            args: vec!["-m".into(), "aria_creative.mcp_server".into()],
            env,
        });
    }

    // Isolated browser Computer Use MCP service (`aria-computer-use`),
    // backed by the first-class Node.js/TypeScript Playwright server.
    // Never hardcode ARIA_COMPUTER_ORIGINS here; the host explicitly authorizes origins.
    let ts_cu_cli = base
        .join("services")
        .join("aria-computer-use")
        .join("dist")
        .join("cli.js");
    if ts_cu_cli.is_file() {
        found.push(McpServerConfig {
            name: "aria-computer-use".into(),
            command: "node".into(),
            args: vec![ts_cu_cli.to_string_lossy().into_owned()],
            env: HashMap::new(),
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

/// Zero-clone `uvx` MCP server entries for published Arthera ecosystem services.
/// Never hardcodes `FINANCIAL_SERVICE_NETWORK` or `FINANCIAL_SERVICE_BROKERS_ENABLED`.
pub fn ecosystem_uvx_mcp_configs() -> Vec<McpServerConfig> {
    vec![
        McpServerConfig {
            name: "arthera-finance".into(),
            command: "uvx".into(),
            args: vec![
                "--from".into(),
                "git+https://github.com/artheras/financial-service.git".into(),
                "arthera-financial-service".into(),
            ],
            env: HashMap::new(),
        },
        McpServerConfig {
            name: "aria-healthcare".into(),
            command: "uvx".into(),
            args: vec![
                "--from".into(),
                "git+https://github.com/artheras/healthcare-service.git".into(),
                "aria-healthcare".into(),
            ],
            env: HashMap::new(),
        },
        McpServerConfig {
            name: "erp-service".into(),
            command: "uvx".into(),
            args: vec![
                "--from".into(),
                "git+https://github.com/artheras/erp-service.git".into(),
                "erp-service-mcp".into(),
            ],
            env: HashMap::new(),
        },
    ]
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

/// Connection health snapshot for a configured stdio MCP server.
#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)]
pub struct McpServerHealth {
    pub name: String,
    pub command: String,
    pub connected: bool,
    pub tool_count: usize,
    pub read_only_count: usize,
    pub tool_names: Vec<String>,
    pub latency_ms: u64,
    pub error: Option<String>,
}

/// Probe a single MCP server (stdio or remote Streamable HTTP) via `initialize` + `tools/list`.
#[allow(dead_code)]
pub fn probe_mcp_server(cfg: &McpServerConfig) -> McpServerHealth {
    let started = Instant::now();
    match McpClient::connect(cfg) {
        Ok(mut client) => match client.list_tools() {
            Ok(tools) => {
                let mut tool_names = Vec::new();
                let mut read_only_count = 0usize;
                for t in &tools {
                    if let Some(name) = t.get("name").and_then(Value::as_str) {
                        tool_names.push(name.to_owned());
                        let ann = parse_tool_annotations(t);
                        if ann.read_only && !ann.destructive {
                            read_only_count += 1;
                        }
                    }
                }
                McpServerHealth {
                    name: cfg.name.clone(),
                    command: cfg.command.clone(),
                    connected: true,
                    tool_count: tool_names.len(),
                    read_only_count,
                    tool_names,
                    latency_ms: started.elapsed().as_millis() as u64,
                    error: None,
                }
            }
            Err(err) => McpServerHealth {
                name: cfg.name.clone(),
                command: cfg.command.clone(),
                connected: false,
                tool_count: 0,
                read_only_count: 0,
                tool_names: Vec::new(),
                latency_ms: started.elapsed().as_millis() as u64,
                error: Some(err),
            },
        },
        Err(err) => McpServerHealth {
            name: cfg.name.clone(),
            command: cfg.command.clone(),
            connected: false,
            tool_count: 0,
            read_only_count: 0,
            tool_names: Vec::new(),
            latency_ms: started.elapsed().as_millis() as u64,
            error: Some(err),
        },
    }
}

/// Probe every MCP server configured for `workspace`.
#[allow(dead_code)]
pub fn probe_workspace_mcp_servers(workspace: &Path) -> Vec<McpServerHealth> {
    load_mcp_server_configs(workspace)
        .iter()
        .map(probe_mcp_server)
        .collect()
}

/// Standard MCP tool annotations parsed from `tools/list` (`annotations` field).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
#[allow(dead_code)]
pub struct McpToolAnnotations {
    pub read_only: bool,
    pub destructive: bool,
    pub idempotent: bool,
}

fn parse_tool_annotations(tool: &Value) -> McpToolAnnotations {
    let Some(ann) = tool.get("annotations").and_then(Value::as_object) else {
        return McpToolAnnotations::default();
    };
    McpToolAnnotations {
        read_only: ann
            .get("readOnlyHint")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        destructive: ann
            .get("destructiveHint")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        idempotent: ann
            .get("idempotentHint")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    }
}

/// Live registry of external MCP servers (stdio + remote Streamable HTTP) discovered from config.
pub struct McpRegistry {
    clients: HashMap<String, McpClient>,
    routes: HashMap<String, (String, String)>,
    annotations: HashMap<String, McpToolAnnotations>,
    schemas: Vec<Value>,
    statuses: Vec<McpServerHealth>,
}

impl McpRegistry {
    pub fn load_from_workspace(workspace: &Path) -> Self {
        let configs = load_mcp_server_configs(workspace);
        Self::load_from_configs(&configs)
    }

    pub fn load_from_configs(configs: &[McpServerConfig]) -> Self {
        let mut clients = HashMap::new();
        let mut routes = HashMap::new();
        let mut annotations = HashMap::new();
        let mut schemas = Vec::new();
        let mut statuses = Vec::new();
        for cfg in configs {
            let started = Instant::now();
            let mut client = match McpClient::connect(cfg) {
                Ok(c) => c,
                Err(err) => {
                    statuses.push(McpServerHealth {
                        name: cfg.name.clone(),
                        command: cfg.command.clone(),
                        connected: false,
                        tool_count: 0,
                        read_only_count: 0,
                        tool_names: Vec::new(),
                        latency_ms: started.elapsed().as_millis() as u64,
                        error: Some(err),
                    });
                    continue;
                }
            };
            let tools = match client.list_tools() {
                Ok(t) => t,
                Err(err) => {
                    statuses.push(McpServerHealth {
                        name: cfg.name.clone(),
                        command: cfg.command.clone(),
                        connected: false,
                        tool_count: 0,
                        read_only_count: 0,
                        tool_names: Vec::new(),
                        latency_ms: started.elapsed().as_millis() as u64,
                        error: Some(err),
                    });
                    continue;
                }
            };
            let safe_server = cfg.name.replace(['.', '-', ' '], "_");
            let mut tool_names = Vec::new();
            let mut read_only_count = 0usize;
            for tool in tools {
                let Some(orig_name) = tool.get("name").and_then(Value::as_str) else {
                    continue;
                };
                tool_names.push(orig_name.to_owned());
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
                let ann = parse_tool_annotations(&tool);
                if ann.read_only && !ann.destructive {
                    read_only_count += 1;
                }
                annotations.insert(exposed.clone(), ann);
                routes.insert(exposed, (cfg.name.clone(), orig_name.to_owned()));
            }
            statuses.push(McpServerHealth {
                name: cfg.name.clone(),
                command: cfg.command.clone(),
                connected: true,
                tool_count: tool_names.len(),
                read_only_count,
                tool_names,
                latency_ms: started.elapsed().as_millis() as u64,
                error: None,
            });
            clients.insert(cfg.name.clone(), client);
        }
        Self {
            clients,
            routes,
            annotations,
            schemas,
            statuses,
        }
    }

    #[allow(dead_code)]
    pub fn statuses(&self) -> &[McpServerHealth] {
        &self.statuses
    }

    #[allow(dead_code)]
    pub fn tool_annotations(&self, exposed_name: &str) -> Option<&McpToolAnnotations> {
        self.annotations.get(exposed_name)
    }

    #[allow(dead_code)]
    pub fn is_tool_read_only(&self, exposed_name: &str) -> bool {
        self.annotations
            .get(exposed_name)
            .is_some_and(|a| a.read_only && !a.destructive)
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

    #[test]
    fn discovers_isolated_venv_python_for_ecosystem_services() {
        let root = tempfile::tempdir().unwrap();
        let ws = root.path().join("aria-code");
        fs::create_dir_all(&ws).unwrap();

        for (dir, pkg, file) in [
            ("erp-service", "erp_service", "mcp_server.py"),
            ("financial-service", "arthera_finance", "server.py"),
            ("financial-service", "arthera_finance", "trading_server.py"),
            ("healthcare-service", "aria_healthcare", "server.py"),
        ] {
            let svc = root.path().join(dir);
            let py_dir = svc.join(".venv").join("bin");
            let pkg_dir = svc.join("src").join(pkg);
            fs::create_dir_all(&py_dir).unwrap();
            fs::create_dir_all(&pkg_dir).unwrap();
            fs::write(py_dir.join("python"), "#!/bin/sh\n").unwrap();
            fs::write(pkg_dir.join(file), "# stub").unwrap();
        }

        let found = discover_ecosystem_mcp_servers(&ws);
        assert_eq!(found.len(), 4);
        for cfg in &found {
            assert_eq!(cfg.args.first().map(String::as_str), Some("-I"));
            assert_eq!(cfg.args.get(1).map(String::as_str), Some("-m"));
            assert!(!cfg.env.contains_key("FINANCIAL_SERVICE_NETWORK"));
            assert!(!cfg.env.contains_key("FINANCIAL_SERVICE_BROKERS_ENABLED"));
        }
    }

    #[test]
    fn stdio_client_and_registry_end_to_end_with_mock_mcp_server() {
        let py = if Command::new("python3").arg("--version").output().is_ok() {
            "python3"
        } else if Command::new("python").arg("--version").output().is_ok() {
            "python"
        } else {
            return;
        };

        let dir = tempfile::tempdir().unwrap();
        let server_script = dir.path().join("mock_mcp.py");
        fs::write(
            &server_script,
            r#"import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    req_id = req.get("id")
    method = req.get("method")
    if req_id is None:
        continue
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    elif method == "tools/list":
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
            {"name": "echo.ping", "description": "Echo tool", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}}, "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}},
            {"name": "order.create", "description": "Create order", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": False, "destructiveHint": True}}
        ]}}
    elif method == "tools/call":
        args = req.get("params", {}).get("arguments", {})
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"pong:{args.get('msg', '')}"}]}}
    else:
        resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown"}}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"#,
        )
        .unwrap();

        let good_cfg = McpServerConfig {
            name: "demo-srv".into(),
            command: py.into(),
            args: vec![server_script.to_string_lossy().into_owned()],
            env: HashMap::new(),
        };
        let bad_cfg = McpServerConfig {
            name: "missing-srv".into(),
            command: "__nonexistent_mcp_binary_aria__".into(),
            args: Vec::new(),
            env: HashMap::new(),
        };

        assert_eq!(
            write_workspace_mcp_json(dir.path(), &[good_cfg.clone(), bad_cfg.clone()]).unwrap(),
            2
        );

        let health = probe_workspace_mcp_servers(dir.path());
        assert_eq!(health.len(), 2);
        let good_h = health.iter().find(|h| h.name == "demo-srv").unwrap();
        assert!(good_h.connected);
        assert_eq!(good_h.tool_count, 2);
        assert_eq!(good_h.read_only_count, 1);
        assert_eq!(good_h.tool_names, vec!["echo.ping", "order.create"]);
        assert!(good_h.error.is_none());

        let bad_h = health.iter().find(|h| h.name == "missing-srv").unwrap();
        assert!(!bad_h.connected);
        assert!(bad_h.error.is_some());

        let mut reg = McpRegistry::load_from_workspace(dir.path());
        assert_eq!(reg.statuses().len(), 2);
        assert_eq!(reg.tool_schemas().len(), 2);
        assert!(reg.is_tool_read_only("mcp__demo_srv__echo_ping"));
        assert!(!reg.is_tool_read_only("mcp__demo_srv__order_create"));
        assert_eq!(
            reg.tool_annotations("mcp__demo_srv__echo_ping"),
            Some(&McpToolAnnotations {
                read_only: true,
                destructive: false,
                idempotent: true,
            })
        );
        let call = reg
            .call_tool("mcp__demo_srv__echo_ping", json!({"msg": "hello"}))
            .unwrap();
        assert_eq!(call["ok"], true);
        assert_eq!(call["data"]["content"][0]["text"], "pong:hello");
    }

    #[test]
    fn builds_uvx_ecosystem_configs_without_hardcoded_finance_env() {
        let cfgs = ecosystem_uvx_mcp_configs();
        assert_eq!(cfgs.len(), 3);
        for cfg in &cfgs {
            assert_eq!(cfg.command, "uvx");
            assert!(cfg.env.is_empty());
        }
        assert!(cfgs.iter().any(|c| c.name == "arthera-finance"));
        assert!(cfgs.iter().any(|c| c.name == "aria-healthcare"));
        assert!(cfgs.iter().any(|c| c.name == "erp-service"));
    }

    #[test]
    fn discovers_creative_media_plugin_alongside_sibling_services() {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("aria-code");
        fs::create_dir_all(&ws).unwrap();
        let creative_mcp = dir
            .path()
            .join("aria-plugins")
            .join("plugins")
            .join("creative-media")
            .join("python")
            .join("aria_creative")
            .join("mcp_server.py");
        fs::create_dir_all(creative_mcp.parent().unwrap()).unwrap();
        fs::write(&creative_mcp, "# stub\n").unwrap();

        let found = discover_ecosystem_mcp_servers(&ws);
        let creative = found
            .iter()
            .find(|c| c.name == "aria-creative-media")
            .expect("aria-creative-media should be discovered");
        assert_eq!(creative.args, vec!["-m", "aria_creative.mcp_server"]);
        assert!(creative.env.contains_key("PYTHONPATH"));
    }

    #[test]
    fn parses_remote_streamable_http_mcp_config_and_sse_body() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(
            dir.path().join(".mcp.json"),
            r#"{
              "mcpServers": {
                "arthera-finance-remote": {
                  "url": "https://finance.arthera.dev/mcp",
                  "headers": { "Authorization": "Bearer test-token" }
                }
              }
            }"#,
        )
        .unwrap();
        let configs = load_mcp_server_configs(dir.path());
        let remote = configs
            .iter()
            .find(|c| c.name == "arthera-finance-remote")
            .expect("remote HTTP MCP server should load");
        assert!(is_remote_http_url(&remote.command));
        assert_eq!(remote.command, "https://finance.arthera.dev/mcp");
        assert_eq!(
            remote
                .env
                .get("MCP_HEADER:Authorization")
                .map(String::as_str),
            Some("Bearer test-token")
        );

        let sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"tools\":[{\"name\":\"financial_dcf_valuation\"}]}}\n\n";
        let parsed = parse_http_jsonrpc_body(sse, 2).unwrap();
        assert_eq!(
            parsed["tools"][0]["name"].as_str(),
            Some("financial_dcf_valuation")
        );
    }

    #[test]
    fn discovers_financial_service_inside_workspace_services_dir() {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("aria-code");
        let fin_server = ws
            .join("services")
            .join("financial-service")
            .join("src")
            .join("arthera_finance")
            .join("server.py");
        fs::create_dir_all(fin_server.parent().unwrap()).unwrap();
        fs::write(&fin_server, "# stub\n").unwrap();

        let found = discover_ecosystem_mcp_servers(&ws);
        assert!(
            found.iter().any(|c| c.name == "arthera-finance"),
            "services/financial-service should be auto-discovered"
        );
    }
}
