mod app;
mod dialogs;
mod editor;
mod hang;
mod labels;
mod mark;
mod markdown;
mod phase;
mod robot;
mod theme;
mod transport;
mod ui;

use app::App;
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};
use serde_json::{json, Value};
use std::{
    ffi::{OsStr, OsString},
    io::{self, BufReader, IsTerminal, Write},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use transport::Session;

struct Screen;
impl Drop for Screen {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableBracketedPaste,
            DisableMouseCapture,
            LeaveAlternateScreen,
            crossterm::cursor::Show
        );
    }
}

pub fn run(
    python: &OsStr,
    workspace: &Path,
    args: &[OsString],
    timeout: Duration,
    jsonl: bool,
) -> Result<i32, String> {
    if !jsonl && (!io::stdin().is_terminal() || !io::stdout().is_terminal()) {
        return Err(
            "chat requires a terminal; use chat --jsonl for the application protocol".into(),
        );
    }
    let cancelled = Arc::new(AtomicBool::new(false));
    let signal = cancelled.clone();
    ctrlc::set_handler(move || signal.store(true, Ordering::SeqCst)).map_err(|e| e.to_string())?;
    let with_python = args.iter().any(|a| a == "--with-python")
        || std::env::args_os().any(|a| a == "--python")
        || std::env::var_os("ARIA_PYTHON").is_some_and(|v| !v.is_empty());
    let has_external_runtime = std::env::var_os("ARIA_RUNTIME").is_some_and(|v| !v.is_empty());
    let native_only = !jsonl && !with_python && !has_external_runtime;
    let mut session = if native_only {
        None
    } else {
        Some(Session::spawn(python, workspace, args)?)
    };
    if jsonl {
        let mut s = session.ok_or("--jsonl requires runtime session")?;
        return machine(&mut s, timeout, &cancelled);
    }
    let _screen = Screen;
    enable_raw_mode().map_err(|e| e.to_string())?;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture
    )
    .map_err(|e| e.to_string())?;
    let mut terminal =
        Terminal::new(CrosstermBackend::new(io::stdout())).map_err(|e| e.to_string())?;
    let mut app = App {
        status: if native_only {
            "Native ready".into()
        } else {
            "Starting Aria runtime…".into()
        },
        ..App::default()
    };
    if native_only {
        let ws = workspace
            .canonicalize()
            .unwrap_or_else(|_| workspace.to_path_buf())
            .to_string_lossy()
            .into_owned();
        let probe = crate::engine::ollama::probe_ollama(&ollama_base_url(), Duration::from_secs(2));
        let default_model = std::env::var("ARIA_MODEL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| probe.models.first().cloned())
            .unwrap_or_else(|| "qwen2.5:7b".to_owned());
        app.event(json!({
            "protocol": 1,
            "type": "session.ready",
            "session_id": "native",
            "model": default_model,
            "provider": "ollama",
            "workspace": ws,
            "commands": native_commands_json()
        }))?;
        app.status = "Native ready".into();
    }
    let mut native_allow_write = std::env::var("ARIA_ALLOW_WRITE")
        .ok()
        .is_some_and(|v| v == "1" || v.eq_ignore_ascii_case("true"));
    let mut deadline = if native_only {
        None
    } else {
        Some(Instant::now() + timeout)
    };
    let mut shutting_down = false;
    let mut external_signal = false;
    let mut dirty = true;
    let mut tick = Instant::now();
    let (native_tx, native_rx) = mpsc::channel::<Value>();
    let mut native_active = false;
    let native_cancel = Arc::new(AtomicBool::new(false));
    let mut native_picker_models: Vec<(String, String)> = Vec::new();
    loop {
        if let Some(ref mut sess) = session {
            for _ in 0..128 {
                match sess.events.try_recv() {
                    Ok(Ok(Some(value))) => {
                        dirty = true;
                        let kind = value["type"].as_str().unwrap_or("").to_owned();
                        app.event(value)?;
                        if !shutting_down
                            && matches!(
                                kind.as_str(),
                                "session.ready"
                                    | "turn.completed"
                                    | "approval.requested"
                                    | "input.requested"
                            )
                        {
                            deadline = None;
                        } else if !shutting_down && kind == "dialog.closed" {
                            deadline = Some(Instant::now() + timeout);
                        }
                    }
                    Ok(Ok(None)) => {
                        if !app.closed {
                            return Err("Application runtime disconnected unexpectedly".into());
                        }
                        break;
                    }
                    Ok(Err(error)) => return Err(error),
                    Err(mpsc::TryRecvError::Empty) => break,
                    Err(_) => return Err("Application reader disconnected".into()),
                }
            }
        }
        for _ in 0..128 {
            match native_rx.try_recv() {
                Ok(value) => {
                    dirty = true;
                    let kind = value["type"].as_str().unwrap_or("").to_owned();
                    app.event(value)?;
                    if kind == "turn.completed" {
                        native_active = false;
                        if !shutting_down {
                            deadline = None;
                        }
                    }
                }
                Err(mpsc::TryRecvError::Empty) => break,
                Err(mpsc::TryRecvError::Disconnected) => break,
            }
        }
        // A message queued while the last turn ran goes out as soon as that
        // turn has completed, in the order it was sent.
        if !shutting_down {
            if let Some(request) = app.next_queued() {
                dirty = true;
                deadline = Some(Instant::now() + timeout);
                if try_dispatch_native_turn(
                    &mut app,
                    &request,
                    &native_tx,
                    &native_cancel,
                    &mut native_picker_models,
                    &mut native_allow_write,
                    timeout,
                ) {
                    native_active = true;
                } else if let Some(ref mut sess) = session {
                    sess.send(request)?;
                } else {
                    let _ = native_tx
                        .send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                }
            }
        }
        if app.turn.is_some() && tick.elapsed() >= Duration::from_secs(1) {
            dirty = true;
            tick = Instant::now();
        }
        if dirty {
            terminal
                .draw(|frame| ui::draw(frame, &app))
                .map_err(|e| e.to_string())?;
            dirty = false;
        }
        if app.closed {
            if app.failed {
                return Err(app
                    .entries
                    .iter()
                    .rev()
                    .find(|e| e.role == "error")
                    .map(|e| e.text.clone())
                    .unwrap_or_else(|| "Application runtime failed".into()));
            }
            break;
        }
        if cancelled.swap(false, Ordering::SeqCst) {
            external_signal = true;
            shutting_down = true;
            if let Some(ref mut sess) = session {
                sess.send(json!({"type":"shutdown","protocol":1}))?;
                deadline = Some(Instant::now() + Duration::from_secs(2));
            } else {
                app.closed = true;
                break;
            }
        }
        if deadline.is_some_and(|end| Instant::now() >= end) {
            if shutting_down {
                return Ok(if external_signal { 130 } else { 0 });
            }
            return Err(
                "Application runtime timed out; worker and child processes were stopped".into(),
            );
        }
        if let Some(ref mut sess) = session {
            if let Some(status) = sess.worker.child.try_wait().map_err(|e| e.to_string())? {
                return Err(format!("Application runtime exited with {status}"));
            }
        }
        if let Some(text) = app.clipboard.take() {
            let mut out = io::stdout();
            let _ = out
                .write_all(osc52(&text).as_bytes())
                .and_then(|_| out.flush());
        }
        if event::poll(Duration::from_millis(25)).map_err(|e| e.to_string())? {
            dirty = true;
            let request = match event::read().map_err(|e| e.to_string())? {
                Event::Key(key) if key.kind != KeyEventKind::Release => key_request(&mut app, key),
                Event::Paste(text) => {
                    if let Some(d) = &mut app.dialog {
                        if d.choices.is_empty() {
                            d.editor.insert(&text);
                        }
                    } else {
                        app.editor.insert(&text);
                    }
                    None
                }
                Event::Mouse(mouse) => {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => app.scroll = app.scroll.saturating_add(3),
                        MouseEventKind::ScrollDown => app.scroll = app.scroll.saturating_sub(3),
                        _ => (),
                    }
                    None
                }
                _ => None,
            };
            if let Some(request) = request {
                let kind = request["type"].as_str().unwrap_or("");
                if kind == "shutdown" {
                    shutting_down = true;
                }
                if kind == "dialog.respond"
                    && request["request_id"].as_str() == Some("native-model-picker")
                {
                    let idx = request["choice"].as_i64().unwrap_or(-1);
                    let _ = native_tx.send(json!({
                        "protocol": 1,
                        "type": "dialog.closed",
                        "request_id": "native-model-picker"
                    }));
                    if idx >= 0 {
                        if let Some((prov, mdl)) = native_picker_models.get(idx as usize) {
                            let ws = app.workspace.clone();
                            let _ = native_tx.send(json!({
                                "protocol": 1,
                                "type": "session.ready",
                                "session_id": "native",
                                "model": mdl,
                                "provider": prov,
                                "workspace": ws,
                                "commands": [
                                    {"name": "/help", "description": "Show help"},
                                    {"name": "/model", "description": "Choose or list models"},
                                    {"name": "/health", "description": "Check configured services"},
                                    {"name": "/new", "description": "Start a new session"}
                                ]
                            }));
                            let msg = if app.zh() {
                                format!("已切换模型为 `{mdl}` ({prov})")
                            } else {
                                format!("Switched model to `{mdl}` ({prov})")
                            };
                            let _ = native_tx
                                .send(json!({"protocol": 1, "type": "answer.delta", "text": msg}));
                        }
                    }
                    let _ = native_tx
                        .send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                    native_picker_models.clear();
                    deadline = None;
                } else if kind == "turn.cancel" && native_active {
                    native_cancel.store(true, Ordering::Relaxed);
                    deadline = Some(Instant::now() + Duration::from_secs(2));
                } else {
                    deadline = Some(
                        Instant::now()
                            + if kind == "turn.cancel" || shutting_down {
                                Duration::from_secs(2)
                            } else {
                                timeout
                            },
                    );
                    if kind == "session.new" && session.is_none() {
                        app.entries.clear();
                        app.turn = None;
                        let _ = native_tx.send(json!({
                            "protocol": 1,
                            "type": "session.ready",
                            "session_id": "native",
                            "model": app.model,
                            "provider": app.provider,
                            "workspace": app.workspace,
                            "commands": native_commands_json()
                        }));
                        deadline = None;
                    } else if kind == "turn.submit"
                        && request["text"].as_str().map(str::trim) == Some("/help")
                        && session.is_none()
                    {
                        app.help = true;
                        let _ = native_tx
                            .send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                        deadline = None;
                    } else if try_dispatch_native_turn(
                        &mut app,
                        &request,
                        &native_tx,
                        &native_cancel,
                        &mut native_picker_models,
                        &mut native_allow_write,
                        timeout,
                    ) {
                        native_active = true;
                    } else if let Some(ref mut sess) = session {
                        sess.send(request)?;
                    } else if kind == "shutdown" {
                        app.closed = true;
                        break;
                    } else {
                        let _ = native_tx
                            .send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                    }
                }
            }
        }
    }
    let Some(mut sess) = session else {
        return Ok(if external_signal { 130 } else { 0 });
    };
    // A closed event is not sufficient to report success if Python exits 1.
    let end = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = sess.worker.child.try_wait().map_err(|e| e.to_string())? {
            return Ok(if external_signal {
                130
            } else if app.failed || !status.success() {
                status.code().filter(|c| *c != 0).unwrap_or(1)
            } else {
                0
            });
        }
        if Instant::now() >= end {
            return Err("Application did not exit after session.closed".into());
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn native_commands_json() -> Value {
    json!([
        {"name": "/help", "description": "Show help"},
        {"name": "/model", "description": "Choose or list models"},
        {"name": "/health", "description": "Check configured services"},
        {"name": "/sessions", "description": "List saved sessions"},
        {"name": "/review", "description": "Review uncommitted git changes"},
        {"name": "/scan", "description": "Scan workspace structure and languages"},
        {"name": "/diff", "description": "Show uncommitted git diff"},
        {"name": "/log", "description": "Show recent git commit log"},
        {"name": "/verify", "description": "Run workspace test/verification suite"},
        {"name": "/map", "description": "Show workspace symbol map"},
        {"name": "/rewind", "description": "Rewind the last file edit"},
        {"name": "/worktree", "description": "Manage isolated git worktrees (list|create|remove)"},
        {"name": "/mcp", "description": "List, connect, or probe ecosystem MCP servers (/mcp [connect [--uvx]|probe])"},
        {"name": "/skills", "description": "List discovered project and user skills"},
        {"name": "/init", "description": "Generate ARIA.md for the current project (/init [--force])"},
        {"name": "/doctor", "description": "Run native runtime, provider, MCP, hook, and sandbox diagnostics"},
        {"name": "/config", "description": "Show native runtime configuration (provider, model, sandbox, MCP)"},
        {"name": "/permissions", "description": "Inspect write permission, OS sandbox, and lifecycle hook policy"},
        {"name": "/write-mode", "description": "Toggle workspace write permission (on|off)"},
        {"name": "/new", "description": "Start a new session"}
    ])
}

fn format_mcp_probe_report(
    results: &[crate::engine::mcp::McpServerHealth],
) -> (String, &'static str) {
    if results.is_empty() {
        return (
            "No configured MCP servers found to probe (run `/mcp connect` first if sibling services are present).".to_owned(),
            "ok",
        );
    }
    let mut lines = Vec::with_capacity(results.len() + 1);
    let mut all_ok = true;
    lines.push("MCP server live probe:".to_owned());
    for h in results {
        if h.connected {
            let preview = if h.tool_names.is_empty() {
                "no tools".to_owned()
            } else if h.tool_names.len() <= 6 {
                h.tool_names.join(", ")
            } else {
                format!(
                    "{}, +{} more",
                    h.tool_names[..6].join(", "),
                    h.tool_names.len() - 6
                )
            };
            lines.push(format!(
                "- `{}`: ok ({} tools, {} read-only, {} ms) — `{}`",
                h.name, h.tool_count, h.read_only_count, h.latency_ms, preview
            ));
        } else {
            all_ok = false;
            lines.push(format!(
                "- `{}`: failed ({} ms) — {}",
                h.name,
                h.latency_ms,
                h.error.as_deref().unwrap_or("unknown error")
            ));
        }
    }
    (lines.join("\n"), if all_ok { "ok" } else { "failed" })
}

fn build_native_doctor_report(workspace: &std::path::Path) -> String {
    let mut lines = Vec::new();
    lines.push(format!(
        "Aria Code Native Doctor (v{})",
        env!("CARGO_PKG_VERSION")
    ));
    lines.push(format!("- Workspace: `{}`", workspace.display()));
    let instructions = crate::engine::skills::load_project_instructions(workspace);
    lines.push(match instructions {
        Some((p, _)) => format!("- Project instructions: `{}`", p.display()),
        None => "- Project instructions: none (run `/init` to generate `ARIA.md`)".to_owned(),
    });
    let skills = crate::engine::skills::discover_skills(workspace);
    lines.push(format!("- Skills discovered: {}", skills.len()));
    let hooks = crate::engine::hooks::HookRegistry::load(workspace);
    lines.push(format!(
        "- Lifecycle hooks: {} rule(s) (run `/hooks init` to scaffold `.aria/hooks.json`)",
        hooks.rules().len()
    ));
    let mode = crate::engine::sandbox::SandboxMode::from_env();
    let mode_str = match mode {
        crate::engine::sandbox::SandboxMode::Off => "off",
        crate::engine::sandbox::SandboxMode::ReadOnly => "read-only",
        crate::engine::sandbox::SandboxMode::WorkspaceWrite => "workspace-write",
    };
    lines.push(format!("- Sandbox mode: `{mode_str}`"));
    let configured_mcp = crate::engine::mcp::load_mcp_server_configs(workspace);
    let sibling_mcp = crate::engine::mcp::discover_ecosystem_mcp_servers(workspace);
    lines.push(format!(
        "- MCP servers: {} configured, {} sibling ecosystem detected (run `/mcp probe` to verify)",
        configured_mcp.len(),
        sibling_mcp.len()
    ));
    let mut providers = Vec::new();
    for (label, env_key) in [
        ("OpenAI", "OPENAI_API_KEY"),
        ("Anthropic", "ANTHROPIC_API_KEY"),
        ("Gemini", "GEMINI_API_KEY"),
        ("Google", "GOOGLE_API_KEY"),
    ] {
        if std::env::var(env_key)
            .map(|v| !v.trim().is_empty())
            .unwrap_or(false)
        {
            providers.push(label);
        }
    }
    let provider_line = if providers.is_empty() {
        "none in env (using local Ollama or saved config)".to_owned()
    } else {
        providers.join(", ")
    };
    lines.push(format!("- Cloud LLM env keys: {provider_line}"));
    lines.join("\n")
}

fn ollama_base_url() -> String {
    std::env::var("OLLAMA_HOST")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:11434".to_owned())
}

#[derive(Debug, PartialEq, Eq)]
enum NativeRoute {
    Help,
    Clear,
    Health,
    ModelList(Option<String>),
    Sessions(Option<String>),
    Review(Option<String>),
    Diff(Option<String>),
    Log(usize),
    Scan,
    Map,
    Rewind,
    Worktree(String, Option<String>),
    Mcp(String),
    Skills,
    Init(bool),
    Doctor,
    Config,
    Permissions(String),
    Hooks(String),
    Sandbox,
    Compact,
    Roi,
    Memory(String),
    Todo(String),
    Note(String),
    Verify(Option<String>),
    WriteMode(Option<bool>),
    OllamaChat,
    OllamaAgent,
    CloudChat(crate::engine::cloud::CloudProvider),
}

fn classify_native_route(app: &App, request: &Value) -> Option<NativeRoute> {
    if request["type"].as_str() != Some("turn.submit") {
        return None;
    }
    let text = request["text"].as_str().unwrap_or("").trim();
    if text.is_empty() {
        return None;
    }
    let is_ollama = app.provider.eq_ignore_ascii_case("ollama");
    if text == "/help" {
        return Some(NativeRoute::Help);
    }
    if text == "/clear" || text == "/new" {
        return Some(NativeRoute::Clear);
    }
    if text == "/health" && (is_ollama || app.provider.is_empty()) {
        return Some(NativeRoute::Health);
    }
    if text == "/scan" {
        return Some(NativeRoute::Scan);
    }
    if text == "/map" {
        return Some(NativeRoute::Map);
    }
    if text == "/rewind" {
        return Some(NativeRoute::Rewind);
    }
    if text == "/worktree" || text.starts_with("/worktree ") {
        let rest = text.strip_prefix("/worktree").unwrap_or("").trim();
        let mut parts = rest.split_whitespace();
        let action = parts.next().unwrap_or("list").to_owned();
        let name = parts.next().map(str::to_owned);
        return Some(NativeRoute::Worktree(action, name));
    }
    if text == "/mcp" || text.starts_with("/mcp ") {
        let arg = text.strip_prefix("/mcp").unwrap_or("").trim().to_owned();
        return Some(NativeRoute::Mcp(arg));
    }
    if text == "/skills" {
        return Some(NativeRoute::Skills);
    }
    if text == "/init" || text.starts_with("/init ") {
        let force = text
            .split_whitespace()
            .skip(1)
            .any(|tok| tok == "--force" || tok == "-f");
        return Some(NativeRoute::Init(force));
    }
    if text == "/doctor" {
        return Some(NativeRoute::Doctor);
    }
    if text == "/config" {
        return Some(NativeRoute::Config);
    }
    if text == "/permissions" || text.starts_with("/permissions ") {
        let arg = text
            .strip_prefix("/permissions")
            .unwrap_or("")
            .trim()
            .to_owned();
        return Some(NativeRoute::Permissions(arg));
    }
    if text == "/hooks" || text.starts_with("/hooks ") {
        let arg = text.strip_prefix("/hooks").unwrap_or("").trim().to_owned();
        return Some(NativeRoute::Hooks(arg));
    }
    if text == "/sandbox" {
        return Some(NativeRoute::Sandbox);
    }
    if text == "/compact" {
        return Some(NativeRoute::Compact);
    }
    if text == "/roi" || text == "/cost" || text == "/usage" {
        return Some(NativeRoute::Roi);
    }
    if text == "/memory" || text.starts_with("/memory ") {
        let arg = text.strip_prefix("/memory").unwrap_or("").trim().to_owned();
        return Some(NativeRoute::Memory(arg));
    }
    if text == "/todo" || text.starts_with("/todo ") {
        let arg = text.strip_prefix("/todo").unwrap_or("").trim().to_owned();
        return Some(NativeRoute::Todo(arg));
    }
    if text == "/note" || text.starts_with("/note ") {
        let arg = text.strip_prefix("/note").unwrap_or("").trim().to_owned();
        return Some(NativeRoute::Note(arg));
    }
    if text == "/diff" || text.starts_with("/diff ") {
        let target = text.strip_prefix("/diff").unwrap_or("").trim();
        return Some(NativeRoute::Diff(
            (!target.is_empty()).then(|| target.to_owned()),
        ));
    }
    if text == "/log" || text.starts_with("/log ") {
        let arg = text.strip_prefix("/log").unwrap_or("").trim();
        let limit = arg.parse::<usize>().unwrap_or(15).clamp(1, 50);
        return Some(NativeRoute::Log(limit));
    }
    if text == "/verify" || text.starts_with("/verify ") {
        let target = text.strip_prefix("/verify").unwrap_or("").trim();
        return Some(NativeRoute::Verify(
            (!target.is_empty()).then(|| target.to_owned()),
        ));
    }
    if text == "/write-mode" || text.starts_with("/write-mode ") {
        let arg = text
            .strip_prefix("/write-mode")
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        let mode = match arg.as_str() {
            "on" | "1" | "true" | "enable" => Some(true),
            "off" | "0" | "false" | "disable" => Some(false),
            _ => None,
        };
        return Some(NativeRoute::WriteMode(mode));
    }
    if text == "/sessions" || text.starts_with("/sessions ") {
        let query = text.strip_prefix("/sessions").unwrap_or("").trim();
        return Some(NativeRoute::Sessions(
            (!query.is_empty()).then(|| query.to_owned()),
        ));
    }
    if text == "/review" || text.starts_with("/review ") {
        let target = text.strip_prefix("/review").unwrap_or("").trim();
        return Some(NativeRoute::Review(
            (!target.is_empty()).then(|| target.to_owned()),
        ));
    }
    if text == "/model" || text.starts_with("/model ") {
        let arg = text.strip_prefix("/model").unwrap_or("").trim();
        let target = if arg.is_empty() {
            None
        } else {
            Some(arg.to_owned())
        };
        if is_ollama || app.provider.is_empty() {
            return Some(NativeRoute::ModelList(target));
        }
    }
    if !app.model.is_empty() {
        if crate::engine::policy::is_conversation_only_message(text)
            && app.entries.iter().filter(|e| e.role == "user").count() <= 1
        {
            if is_ollama {
                return Some(NativeRoute::OllamaChat);
            }
            if let Some(cloud) = crate::engine::cloud::CloudProvider::from_id(&app.provider) {
                if cloud.resolve_api_key().is_some() {
                    return Some(NativeRoute::CloudChat(cloud));
                }
            }
        } else if !text.starts_with('/')
            && (app.status == "Native ready" || app.status == "已完成" || app.status == "Completed")
        {
            if is_ollama && app.provider == "ollama" {
                return Some(NativeRoute::OllamaAgent);
            }
            if let Some(cloud) = crate::engine::cloud::CloudProvider::from_id(&app.provider) {
                if cloud.resolve_api_key().is_some() {
                    return Some(NativeRoute::CloudChat(cloud));
                }
            }
        }
    }
    None
}

fn sessions_root() -> Option<std::path::PathBuf> {
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(
        std::path::PathBuf::from(home)
            .join(".aria")
            .join("sessions"),
    )
}

fn persist_native_turn(workspace: &str, model: &str, prompt: &str, answer: &str) {
    if prompt.trim().is_empty() || answer.trim().is_empty() {
        return;
    }
    let Some(root) = sessions_root() else {
        return;
    };
    let _ = crate::sessions::save_native_turn(&root, "native", workspace, model, prompt, answer);
}

fn collect_agent_history(app: &App, current_prompt: &str) -> Vec<(String, String)> {
    let mut history: Vec<(String, String)> = app
        .entries
        .iter()
        .filter(|e| (e.role == "user" || e.role == "assistant") && !e.text.trim().is_empty())
        .map(|e| (e.role.clone(), e.text.clone()))
        .collect();
    if let Some((role, text)) = history.last() {
        if role == "user" && text.trim() == current_prompt.trim() {
            history.pop();
        }
    }
    if history.len() > 10 {
        history.drain(..history.len() - 10);
    }
    history
}

fn try_dispatch_native_turn(
    app: &mut App,
    request: &Value,
    tx: &mpsc::Sender<Value>,
    cancel: &Arc<AtomicBool>,
    picker_models: &mut Vec<(String, String)>,
    allow_write_state: &mut bool,
    timeout: Duration,
) -> bool {
    let Some(route) = classify_native_route(app, request) else {
        return false;
    };
    if let NativeRoute::WriteMode(requested) = route {
        let next = requested.unwrap_or(!*allow_write_state);
        *allow_write_state = next;
        let msg = if app.zh() {
            if next {
                "已开启写入模式（Write Mode ON）：原生 coding agent 现在可以修改文件并执行工作区命令。"
            } else {
                "已关闭写入模式（Write Mode OFF）：原生 coding agent 处于只读保护状态。"
            }
        } else if next {
            "Write mode ON: native coding agent can now edit files and run workspace commands."
        } else {
            "Write mode OFF: native coding agent is in read-only safe mode."
        };
        let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": msg}));
        let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
        return true;
    }
    if matches!(route, NativeRoute::Clear) {
        app.entries.clear();
        app.scroll = 0;
        let msg = if app.zh() {
            "已清空当前对话，开启新会话。"
        } else {
            "Started a fresh conversation."
        };
        let _ = tx.send(json!({"protocol": 1, "type": "turn.started"}));
        let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": msg}));
        let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
        return true;
    }
    let allow_write = *allow_write_state;
    cancel.store(false, Ordering::Relaxed);
    let tx = tx.clone();
    let cancel = Arc::clone(cancel);
    let url = ollama_base_url();
    let model = app.model.clone();
    let workspace = app.workspace.clone();
    let zh = app.zh();
    let prompt = request["text"].as_str().unwrap_or("").trim().to_owned();
    let history = collect_agent_history(app, &prompt);
    let provider = app.provider.clone();

    if matches!(route, NativeRoute::ModelList(None)) {
        let probe = crate::engine::ollama::probe_ollama(&url, Duration::from_secs(2));
        let mut choices: Vec<Value> = Vec::new();
        let mut groups: Vec<String> = Vec::new();
        picker_models.clear();
        for m in &probe.models {
            picker_models.push(("ollama".to_owned(), m.clone()));
            groups.push(if zh {
                "本地 Ollama".into()
            } else {
                "Local Ollama".into()
            });
            let note = if *m == model {
                if zh {
                    "当前选中 · 本地推理"
                } else {
                    "active · local"
                }
            } else {
                if zh {
                    "本地推理"
                } else {
                    "local"
                }
            };
            choices.push(json!([m, note]));
        }
        let cloud_candidates = [
            ("arthera", "aria-pro", "Arthera Backend (API)"),
            ("openai", "gpt-4o", "OpenAI API"),
            ("anthropic", "claude-3-7-sonnet-latest", "Anthropic API"),
            ("google", "gemini-3.8-flash", "Google Gemini API"),
            ("deepseek", "deepseek-chat", "DeepSeek API"),
        ];
        for (prov, mdl, label) in cloud_candidates {
            if let Some(cp) = crate::engine::cloud::CloudProvider::from_id(prov) {
                if cp.resolve_api_key().is_some() {
                    picker_models.push((prov.to_owned(), mdl.to_owned()));
                    groups.push(if zh {
                        "云端 API".into()
                    } else {
                        "Cloud API".into()
                    });
                    choices.push(json!([mdl, label]));
                }
            }
        }
        if !choices.is_empty() {
            let _ = tx.send(json!({"protocol": 1, "type": "turn.started"}));
            let _ = tx.send(json!({
                "protocol": 1,
                "type": "input.requested",
                "request_id": "native-model-picker",
                "title": if zh { "选择模型" } else { "Select a model" },
                "picker": true,
                "selected": 0,
                "groups": groups,
                "choices": choices,
            }));
            return true;
        }
    }

    thread::spawn(move || {
        let _ = tx.send(json!({"protocol": 1, "type": "turn.started"}));
        match route {
            NativeRoute::Clear => {}
            NativeRoute::Help => {
                let text = if zh {
                    "**Aria Code 原生命令：**\n\
                     - `/help` — 查看命令帮助\n\
                     - `/clear` | `/new` — 清空当前对话并开启新会话\n\
                     - `/model [模型名]` — 列出或切换模型\n\
                     - `/health` | `/doctor` — 检查服务与环境健康状态\n\
                     - `/config` | `/permissions [init]` — 查看运行配置与 `.aria/settings.json` 权限规则\n\
                     - `/hooks [init]` | `/sandbox` — 管理生命周期 Hook 与 OS 沙箱\n\
                     - `/mcp [connect|connect --uvx|probe]` — 连接或探测外部 MCP 服务\n\
                     - `/skills` | `/init [--force]` — 查看工作区技能或初始化 `ARIA.md`\n\
                     - `/scan` | `/map` | `/diff` | `/log` | `/review` | `/verify` — 代码检查与验证\n\
                     - `/roi` | `/cost` — 查看 AI ROI 与 Token 效率计分卡\n\
                     - `/memory` | `/todo` | `/note` — 项目记忆、待办清单与 `ARIA.md` 笔记\n\
                     - `/rewind` | `/worktree` | `/write-mode on|off` | `/compact` | `/sessions` — 工作区与会话管理"
                } else {
                    "**Aria Code Native Commands:**\n\
                     - `/help` — Show this command reference\n\
                     - `/clear` | `/new` — Clear history and start a fresh session\n\
                     - `/model [name]` — List or switch models\n\
                     - `/health` | `/doctor` — Check services and workspace diagnostics\n\
                     - `/config` | `/permissions [init]` — Inspect config and `.aria/settings.json` rules\n\
                     - `/hooks [init]` | `/sandbox` — Manage lifecycle hooks and OS sandbox\n\
                     - `/mcp [connect|connect --uvx|probe]` — Connect or probe external MCP servers\n\
                     - `/skills` | `/init [--force]` — List workspace skills or initialize `ARIA.md`\n\
                     - `/scan` | `/map` | `/diff` | `/log` | `/review` | `/verify` — Code inspection and verification\n\
                     - `/roi` | `/cost` — View Enterprise AI ROI & Token Efficiency Scorecard\n\
                     - `/memory` | `/todo` | `/note` — Manage project memory, tasks, and `ARIA.md` notes\n\
                     - `/rewind` | `/worktree` | `/write-mode on|off` | `/compact` | `/sessions` — Workspace and session controls"
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Health => {
                let probe = crate::engine::ollama::probe_ollama(&url, Duration::from_secs(5));
                let (cloud_ok, cloud_ms, cloud_err) =
                    crate::engine::cloud::probe_arthera_backend(Duration::from_secs(3));
                let cloud_line = if cloud_ok {
                    format!("● **Arthera Backend**  connected ({cloud_ms} ms)")
                } else {
                    let err = cloud_err.unwrap_or_else(|| "offline".to_owned());
                    format!("○ **Arthera Backend**  {err} ({cloud_ms} ms)")
                };
                let body = if probe.ok {
                    let models = if probe.models.is_empty() {
                        if zh {
                            "暂无已拉取模型".to_owned()
                        } else {
                            "no models pulled".to_owned()
                        }
                    } else {
                        probe
                            .models
                            .iter()
                            .take(4)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join(", ")
                    };
                    if zh {
                        format!(
                            "● **Ollama**  已连接 · {} ({} ms)\n{}\n- 当前模型：`{}`\n- 工作区：`{}`\n- 提示：输入 `/health --model` 可测试模型实时生成，`/model` 可切换模型。",
                            models, probe.latency_ms, cloud_line, model, workspace
                        )
                    } else {
                        format!(
                            "● **Ollama**  connected · {} ({} ms)\n{}\n- Active model: `{}`\n- Workspace: `{}`\n- Tip: run `/health --model` to test live generation, or `/model` to switch.",
                            models, probe.latency_ms, cloud_line, model, workspace
                        )
                    }
                } else {
                    let err = probe.error.unwrap_or_else(|| "unreachable".to_owned());
                    if zh {
                        format!("● **Ollama**  未连接 ({err})\n{}\n- 请确认 Ollama 已启动 (`ollama serve`)，或输入 `/model` 切换云端模型。", cloud_line)
                    } else {
                        format!("● **Ollama**  offline ({err})\n{}\n- Start Ollama (`ollama serve`) or run `/model` to switch to a cloud model.", cloud_line)
                    }
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": body}));
                let _ = tx.send(json!({
                    "protocol": 1,
                    "type": "turn.completed",
                    "status": if probe.ok { "ok" } else { "failed" }
                }));
            }
            NativeRoute::ModelList(target) => {
                let probe = crate::engine::ollama::probe_ollama(&url, Duration::from_secs(3));
                if let Some(next_model) = target {
                    let _ = tx.send(json!({
                        "protocol": 1,
                        "type": "session.ready",
                        "session_id": "native",
                        "model": next_model,
                        "provider": "ollama",
                        "workspace": workspace,
                        "commands": [
                            {"name": "/help", "description": "Show help"},
                            {"name": "/model", "description": "Choose or list models"},
                            {"name": "/health", "description": "Check configured services"},
                            {"name": "/new", "description": "Start a new session"}
                        ]
                    }));
                    let msg = if zh {
                        format!("已切换模型为 `{next_model}` (Ollama)")
                    } else {
                        format!("Switched model to `{next_model}` (Ollama)")
                    };
                    let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": msg}));
                    let _ =
                        tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                } else {
                    let mut lines = Vec::new();
                    if probe.ok && !probe.models.is_empty() {
                        lines.push(if zh {
                            "**本地 Ollama 可用模型：**".to_owned()
                        } else {
                            "**Available local Ollama models:**".to_owned()
                        });
                        for m in &probe.models {
                            let mark = if *m == model { " (当前)" } else { "" };
                            lines.push(format!("- `{m}`{mark}"));
                        }
                    } else {
                        lines.push(if zh {
                            "未检测到运行中的 Ollama 模型。".to_owned()
                        } else {
                            "No local Ollama models detected.".to_owned()
                        });
                    }
                    lines.push(if zh {
                        "\n输入 `/model <模型名>` 即可切换模型。".to_owned()
                    } else {
                        "\nRun `/model <name>` to switch models.".to_owned()
                    });
                    let _ = tx.send(
                        json!({"protocol": 1, "type": "answer.delta", "text": lines.join("\n")}),
                    );
                    let _ =
                        tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                }
            }
            NativeRoute::Sessions(query) => {
                let mut lines: Vec<String> = Vec::new();
                match sessions_root().map(|r| crate::sessions::list(&r, 12, query.as_deref())) {
                    Some(Ok(data)) => {
                        let rows = data["sessions"].as_array().cloned().unwrap_or_default();
                        if rows.is_empty() {
                            lines.push(if zh {
                                "未找到匹配的历史会话。".to_owned()
                            } else {
                                "No saved sessions found.".to_owned()
                            });
                        } else {
                            lines.push(if zh {
                                "最近会话（`aria sessions show <id>` 查看详情）：".to_owned()
                            } else {
                                "Recent sessions (`aria sessions show <id>` to inspect):".to_owned()
                            });
                            for r in rows {
                                let id = r["id"].as_str().unwrap_or("?");
                                let msgs = r["messages"].as_u64().unwrap_or(0);
                                let updated = r["updated"].as_str().unwrap_or("");
                                let title = r["title"].as_str().unwrap_or("Untitled");
                                lines.push(format!("- `{id}` · {msgs} msgs · {updated} · {title}"));
                            }
                        }
                    }
                    Some(Err(e)) => lines.push(format!("[sessions error: {e}]")),
                    None => lines.push("Sessions directory unavailable.".to_owned()),
                }
                let _ = tx
                    .send(json!({"protocol": 1, "type": "answer.delta", "text": lines.join("\n")}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Scan => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let text = match crate::engine::tools::project_scan(&ws_path) {
                    Ok(scan) => {
                        let types = scan["project_types"]
                            .as_array()
                            .map(|a| {
                                a.iter()
                                    .filter_map(Value::as_str)
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            })
                            .unwrap_or_default();
                        let keys = scan["key_files"]
                            .as_array()
                            .map(|a| {
                                a.iter()
                                    .filter_map(Value::as_str)
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            })
                            .unwrap_or_default();
                        let sampled = scan["sampled_files"].as_u64().unwrap_or(0);
                        let git = scan["git_status"].as_str().unwrap_or("").trim();
                        format!(
                            "Workspace: `{workspace}`\nLanguages/Stacks: {}\nKey Manifests: {}\nSampled Files: {sampled}{}",
                            if types.is_empty() { "unknown" } else { &types },
                            if keys.is_empty() { "none" } else { &keys },
                            if git.is_empty() {
                                String::new()
                            } else {
                                format!("\n\nGit Status:\n```\n{git}\n```")
                            }
                        )
                    }
                    Err(e) => format!("[scan error: {e}]"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Diff(target) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let mut diff_out =
                    crate::engine::tools::git_diff(&ws_path, target.as_deref(), false)
                        .ok()
                        .and_then(|v| v["diff"].as_str().map(str::to_owned))
                        .unwrap_or_default();
                if diff_out.trim().is_empty() {
                    diff_out = crate::engine::tools::git_diff(&ws_path, target.as_deref(), true)
                        .ok()
                        .and_then(|v| v["diff"].as_str().map(str::to_owned))
                        .unwrap_or_default();
                }
                let text = if diff_out.trim().is_empty() {
                    if zh {
                        "工作区干净，暂无未提交 diff。".to_owned()
                    } else {
                        "Working tree is clean — no uncommitted diff.".to_owned()
                    }
                } else {
                    format!("```diff\n{}\n```", diff_out.trim())
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Log(limit) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let text = match crate::engine::tools::git_log(&ws_path, None, limit) {
                    Ok(v) => {
                        let out = v["log"].as_str().unwrap_or("").trim();
                        if out.is_empty() {
                            "No git commits found.".to_owned()
                        } else {
                            format!("```\n{out}\n```")
                        }
                    }
                    Err(e) => format!("[git log error: {e}]"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::WriteMode(_) => {}
            NativeRoute::Map => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let text = match crate::engine::tools::repo_map(&ws_path, 40) {
                    Ok(v) => {
                        let files = v["files"].as_array().cloned().unwrap_or_default();
                        if files.is_empty() {
                            "No source symbols found.".to_owned()
                        } else {
                            files
                                .iter()
                                .map(|f| {
                                    let p = f["path"].as_str().unwrap_or("?");
                                    let syms = f["symbols"]
                                        .as_array()
                                        .map(|a| {
                                            a.iter()
                                                .filter_map(Value::as_str)
                                                .collect::<Vec<_>>()
                                                .join(", ")
                                        })
                                        .unwrap_or_default();
                                    format!("- `{p}`: {syms}")
                                })
                                .collect::<Vec<_>>()
                                .join("\n")
                        }
                    }
                    Err(e) => format!("[repo_map error: {e}]"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Rewind => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = match crate::engine::tools::restore_checkpoint(&ws_path) {
                    Ok(v) => (
                        format!(
                            "Rewound `{}` (remaining checkpoints: {}).",
                            v["restored"].as_str().unwrap_or("?"),
                            v["remaining"].as_u64().unwrap_or(0)
                        ),
                        "ok",
                    ),
                    Err(e) => (format!("[rewind: {e}]"), "failed"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Worktree(action, name) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = match crate::engine::tools::git_worktree(
                    &ws_path,
                    &action,
                    name.as_deref(),
                    allow_write,
                ) {
                    Ok(v) => (
                        serde_json::to_string_pretty(&v).unwrap_or_else(|_| v.to_string()),
                        "ok",
                    ),
                    Err(e) => (format!("[worktree: {e}]"), "failed"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Mcp(arg) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let discovered = crate::engine::mcp::discover_ecosystem_mcp_servers(&ws_path);
                let lower = arg.to_ascii_lowercase();
                let (text, status) = if lower == "connect --uvx" || lower == "uvx" {
                    let uvx_servers = crate::engine::mcp::ecosystem_uvx_mcp_configs();
                    match crate::engine::mcp::write_workspace_mcp_json(&ws_path, &uvx_servers) {
                        Ok(n) => {
                            let names: Vec<&str> =
                                uvx_servers.iter().map(|s| s.name.as_str()).collect();
                            (
                                format!(
                                    "Connected {n} zero-clone `uvx` ecosystem MCP server(s) in `.mcp.json`: `{}`.\nRun `/mcp probe` to verify connectivity.",
                                    names.join("`, `")
                                ),
                                "ok",
                            )
                        }
                        Err(e) => (format!("[mcp connect --uvx error: {e}]"), "failed"),
                    }
                } else if lower == "connect" {
                    if discovered.is_empty() {
                        (
                            "No sibling ecosystem MCP servers detected (expected `../financial-service`, `../healthcare-service`, `../erp-service`, `../Arthera`, or `services/aria-bots`).\nTip: run `/mcp connect --uvx` to connect published ecosystem services without cloning them.".to_owned(),
                            "ok",
                        )
                    } else {
                        match crate::engine::mcp::write_workspace_mcp_json(&ws_path, &discovered) {
                            Ok(n) => {
                                let names: Vec<&str> =
                                    discovered.iter().map(|s| s.name.as_str()).collect();
                                (
                                    format!(
                                        "Connected {n} ecosystem MCP server(s) in `.mcp.json`: `{}`.",
                                        names.join("`, `")
                                    ),
                                    "ok",
                                )
                            }
                            Err(e) => (format!("[mcp connect error: {e}]"), "failed"),
                        }
                    }
                } else if lower == "probe" || lower == "status" {
                    let results = crate::engine::mcp::probe_workspace_mcp_servers(&ws_path);
                    format_mcp_probe_report(&results)
                } else {
                    let configured = crate::engine::mcp::load_mcp_server_configs(&ws_path);
                    let mut lines = Vec::new();
                    if configured.is_empty() {
                        lines.push("Configured MCP servers: (none)".to_owned());
                    } else {
                        lines.push(
                            "Configured MCP servers (run `/mcp probe` to test live connections):"
                                .to_owned(),
                        );
                        for s in &configured {
                            lines.push(format!(
                                "- `{}`: `{}` {}",
                                s.name,
                                s.command,
                                s.args.join(" ")
                            ));
                        }
                    }
                    if !discovered.is_empty() {
                        lines.push("\nDetected sibling ecosystem MCP servers (run `/mcp connect` to write `.mcp.json`):".to_owned());
                        for s in &discovered {
                            lines.push(format!(
                                "- `{}`: `{}` {}",
                                s.name,
                                s.command,
                                s.args.join(" ")
                            ));
                        }
                    }
                    (lines.join("\n"), "ok")
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Skills => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let skills = crate::engine::skills::discover_skills(&ws_path);
                let text = if skills.is_empty() {
                    "No skills found in `.aria/skills/` or `~/.aria/skills/`.".to_owned()
                } else {
                    let mut lines = vec![format!("Discovered {} skill(s):", skills.len())];
                    for s in skills {
                        let desc = if s.description.is_empty() {
                            String::new()
                        } else {
                            format!(" — {}", s.description)
                        };
                        lines.push(format!("- `{}` [{}]{}", s.name, s.scope, desc));
                    }
                    lines.join("\n")
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Init(force) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = match crate::engine::skills::generate_aria_md(&ws_path, force)
                {
                    Ok((path, true)) => (
                        format!("Generated `{}` for this project.", path.display()),
                        "ok",
                    ),
                    Ok((path, false)) => (
                        format!(
                            "`{}` already exists (pass `/init --force` to overwrite).",
                            path.display()
                        ),
                        "ok",
                    ),
                    Err(e) => (format!("[init error: {e}]"), "failed"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Doctor => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let text = build_native_doctor_report(&ws_path);
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Config => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let mode = crate::engine::sandbox::SandboxMode::from_env();
                let mode_str = match mode {
                    crate::engine::sandbox::SandboxMode::Off => "off",
                    crate::engine::sandbox::SandboxMode::ReadOnly => "read-only",
                    crate::engine::sandbox::SandboxMode::WorkspaceWrite => "workspace-write",
                };
                let mcp_count = crate::engine::mcp::load_mcp_server_configs(&ws_path).len();
                let hooks_count = crate::engine::hooks::HookRegistry::load(&ws_path)
                    .rules()
                    .len();
                let text = format!(
                    "Native Configuration:\n- Provider / Model: `{}` / `{}`\n- Workspace: `{}`\n- Write mode: `{}` (toggle with `/write-mode on|off`)\n- Sandbox mode: `{mode_str}` (`ARIA_SANDBOX_MODE`)\n- MCP servers configured: {mcp_count} (`/mcp` · `/mcp probe`)\n- Lifecycle hooks: {hooks_count} (`/hooks` · `/hooks init`)",
                    if provider.is_empty() { "ollama" } else { &provider },
                    if model.is_empty() { "default" } else { &model },
                    ws_path.display(),
                    if allow_write { "enabled" } else { "read-only" },
                );
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Permissions(arg) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = if arg.eq_ignore_ascii_case("init") {
                    match crate::engine::permissions::write_default_settings_if_missing(&ws_path) {
                        Ok(path) => (
                            format!(
                                "Permission settings ready at `{}` (edit `permissions.allow` / `permissions.deny`).",
                                path.display()
                            ),
                            "ok",
                        ),
                        Err(e) => (format!("[permissions init error: {e}]"), "failed"),
                    }
                } else {
                    let mode = crate::engine::sandbox::SandboxMode::from_env();
                    let mode_str = match mode {
                        crate::engine::sandbox::SandboxMode::Off => "off",
                        crate::engine::sandbox::SandboxMode::ReadOnly => "read-only",
                        crate::engine::sandbox::SandboxMode::WorkspaceWrite => "workspace-write",
                    };
                    let hooks_count = crate::engine::hooks::HookRegistry::load(&ws_path)
                        .rules()
                        .len();
                    let policy = crate::engine::permissions::PermissionPolicy::load(&ws_path);
                    let allow_summary = if policy.allow.is_empty() {
                        "none".to_owned()
                    } else {
                        policy.allow.join(", ")
                    };
                    let deny_summary = if policy.deny.is_empty() {
                        "none".to_owned()
                    } else {
                        policy.deny.join(", ")
                    };
                    (
                        format!(
                            "Permissions & Guardrails:\n- Declarative mode (`.aria/settings.json`): `{}` (run `/permissions init` to create starter rules)\n- Allow rules: {}\n- Deny rules: {}\n- Workspace writes: `{}` (change with `/write-mode on` or `/write-mode off`)\n- OS shell sandbox: `{mode_str}` (set `ARIA_SANDBOX_MODE=workspace-write|read-only`)\n- Pre/Post/Stop hooks: {hooks_count} rule(s) loaded (manage with `/hooks` or `/hooks init`)",
                            policy.mode.as_str(),
                            allow_summary,
                            deny_summary,
                            if allow_write { "allowed" } else { "read-only" },
                        ),
                        "ok",
                    )
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Hooks(arg) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = if arg.eq_ignore_ascii_case("init") {
                    match crate::engine::hooks::write_default_hooks_if_missing(&ws_path) {
                        Ok((path, true)) => (
                            format!("Created starter lifecycle hooks at `{}`.", path.display()),
                            "ok",
                        ),
                        Ok((path, false)) => (
                            format!("`{}` already exists (left unchanged).", path.display()),
                            "ok",
                        ),
                        Err(e) => (format!("[hooks init error: {e}]"), "failed"),
                    }
                } else {
                    let reg = crate::engine::hooks::HookRegistry::load(&ws_path);
                    let rules = reg.rules();
                    if rules.is_empty() {
                        (
                            "No lifecycle hooks configured. Run `/hooks init` to create `.aria/hooks.json` (`PreToolUse`, `PostToolUse`, `Stop`).".to_owned(),
                            "ok",
                        )
                    } else {
                        let mut lines =
                            vec![format!("Configured lifecycle hooks ({}):", rules.len())];
                        for r in rules {
                            lines.push(format!(
                                "- `{}` matcher=`{}` -> `{}`",
                                r.event.as_str(),
                                r.matcher,
                                r.command
                            ));
                        }
                        (lines.join("\n"), "ok")
                    }
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Sandbox => {
                let mode = crate::engine::sandbox::SandboxMode::from_env();
                let mode_str = match mode {
                    crate::engine::sandbox::SandboxMode::Off => "off",
                    crate::engine::sandbox::SandboxMode::ReadOnly => "read-only",
                    crate::engine::sandbox::SandboxMode::WorkspaceWrite => "workspace-write",
                };
                #[cfg(target_os = "macos")]
                let backend = if std::path::Path::new("/usr/bin/sandbox-exec").is_file() {
                    "macOS Seatbelt (`/usr/bin/sandbox-exec` available)"
                } else {
                    "macOS Seatbelt unavailable"
                };
                #[cfg(target_os = "linux")]
                let backend = "Linux `bwrap` (when installed on PATH)";
                #[cfg(not(any(target_os = "macos", target_os = "linux")))]
                let backend = "OS process sandbox not available on this platform";
                let text = format!(
                    "Sandbox mode: `{mode_str}` ({backend}).\nSet `ARIA_SANDBOX_MODE=workspace-write` or `read-only` to enforce OS-level shell isolation."
                );
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Compact => {
                let mut msgs: Vec<Value> = history
                    .iter()
                    .map(|(role, content)| json!({ "role": role, "content": content }))
                    .collect();
                let before_tokens = crate::engine::compact::estimate_messages_tokens(&msgs);
                let before_count = msgs.len();
                let compacted = crate::engine::compact::compact_messages_if_needed(&mut msgs, 1, 4);
                let after_tokens = crate::engine::compact::estimate_messages_tokens(&msgs);
                let text = if compacted {
                    format!(
                        "Context compacted: {before_count} -> {} messages (~{before_tokens} -> ~{after_tokens} tokens). Auto-compaction also runs automatically at 24,000 tokens.",
                        msgs.len()
                    )
                } else {
                    format!(
                        "Context is already compact ({before_count} messages, ~{before_tokens} tokens)."
                    )
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Roi => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let text = crate::engine::roi::build_roi_scorecard(
                    &ws_path,
                    &provider,
                    &model,
                    allow_write,
                    &history,
                    zh,
                );
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
            }
            NativeRoute::Memory(arg) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) =
                    crate::engine::memory::handle_memory_command(&ws_path, &arg, zh);
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Todo(arg) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = crate::engine::memory::handle_todo_command(&ws_path, &arg, zh);
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Note(arg) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) = match crate::engine::memory::append_aria_note(&ws_path, &arg) {
                    Ok(path) => (format!("Saved note to `{}`.", path.display()), "ok"),
                    Err(e) => (e, "ok"),
                };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Verify(target) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let (text, status) =
                    match crate::engine::tools::verify_workspace(&ws_path, target.as_deref()) {
                        Ok(v) => {
                            let passed = v["passed"].as_bool().unwrap_or(false);
                            let cmd = v["command"].as_str().unwrap_or("");
                            let stdout = v["stdout"].as_str().unwrap_or("").trim();
                            let stderr = v["stderr"].as_str().unwrap_or("").trim();
                            let mut body = format!(
                                "Verification `{cmd}`: **{}**",
                                if passed { "PASSED" } else { "FAILED" }
                            );
                            if !stdout.is_empty() {
                                body.push_str(&format!("\n```\n{stdout}\n```"));
                            }
                            if !stderr.is_empty() {
                                body.push_str(&format!("\n```\n{stderr}\n```"));
                            }
                            (body, if passed { "ok" } else { "failed" })
                        }
                        Err(e) => (format!("[verify error: {e}]"), "failed"),
                    };
                let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": text}));
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::Review(target) => {
                let ws_path = std::path::PathBuf::from(&workspace);
                let status_out = crate::engine::tools::git_status(&ws_path)
                    .ok()
                    .and_then(|v| v["status"].as_str().map(str::to_owned))
                    .unwrap_or_default();
                let mut diff_out =
                    crate::engine::tools::git_diff(&ws_path, target.as_deref(), false)
                        .ok()
                        .and_then(|v| v["diff"].as_str().map(str::to_owned))
                        .unwrap_or_default();
                if diff_out.trim().is_empty() {
                    diff_out = crate::engine::tools::git_diff(&ws_path, target.as_deref(), true)
                        .ok()
                        .and_then(|v| v["diff"].as_str().map(str::to_owned))
                        .unwrap_or_default();
                }
                if status_out.trim().is_empty() && diff_out.trim().is_empty() {
                    let msg = if zh {
                        "工作区干净，暂无未提交改动需要审查。"
                    } else {
                        "Working tree is clean — nothing to review."
                    };
                    let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": msg}));
                    let _ =
                        tx.send(json!({"protocol": 1, "type": "turn.completed", "status": "ok"}));
                    return;
                }
                let clipped_diff: String = diff_out.chars().take(12000).collect();
                let review_prompt = format!(
                    "Review these uncommitted workspace changes for bugs, security issues, edge cases, and code quality. Be concise and actionable.\n\n## git status\n```\n{}\n```\n\n## git diff\n```diff\n{}\n```",
                    status_out.trim(),
                    clipped_diff.trim()
                );
                let mut status = "ok";
                if !model.is_empty() && provider.eq_ignore_ascii_case("ollama") {
                    let sys = crate::engine::policy::general_system_prompt("today");
                    match crate::engine::ollama::stream_chat_cancelable(
                        &url,
                        &model,
                        &sys,
                        &review_prompt,
                        timeout,
                        || cancel.load(Ordering::Relaxed),
                        |delta| {
                            let _ = tx.send(json!({
                                "protocol": 1,
                                "type": "answer.delta",
                                "text": delta
                            }));
                        },
                    ) {
                        Ok(_) => {}
                        Err(e) if e == "cancelled" => status = "cancelled",
                        Err(e) => {
                            let _ = tx.send(json!({
                                "protocol": 1,
                                "type": "answer.delta",
                                "text": format!("\n[{e}]")
                            }));
                            status = "failed";
                        }
                    }
                } else if !model.is_empty() {
                    if let Some(cloud) = crate::engine::cloud::CloudProvider::from_id(&provider) {
                        if cloud.resolve_api_key().is_some() {
                            let sys = crate::engine::policy::general_system_prompt("today");
                            match crate::engine::cloud::stream_cloud_chat_cancelable(
                                cloud,
                                &model,
                                &sys,
                                &review_prompt,
                                timeout,
                                || cancel.load(Ordering::Relaxed),
                                |delta| {
                                    let _ = tx.send(json!({
                                        "protocol": 1,
                                        "type": "answer.delta",
                                        "text": delta
                                    }));
                                },
                            ) {
                                Ok(_) => {}
                                Err(e) if e == "cancelled" => status = "cancelled",
                                Err(e) => {
                                    let _ = tx.send(json!({
                                        "protocol": 1,
                                        "type": "answer.delta",
                                        "text": format!("\n[{e}]")
                                    }));
                                    status = "failed";
                                }
                            }
                        } else {
                            let _ = tx.send(json!({"protocol": 1, "type": "answer.delta", "text": review_prompt}));
                        }
                    } else {
                        let _ = tx.send(
                            json!({"protocol": 1, "type": "answer.delta", "text": review_prompt}),
                        );
                    }
                } else {
                    let _ = tx.send(
                        json!({"protocol": 1, "type": "answer.delta", "text": review_prompt}),
                    );
                }
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::OllamaAgent => {
                let _ = tx.send(json!({
                    "protocol": 1,
                    "type": "turn.phase",
                    "phase": "plan",
                    "label": if zh {
                        format!("等待 {model} 规划工具调用…")
                    } else {
                        format!("Waiting for {model} to plan…")
                    }
                }));
                let ws_path = std::path::PathBuf::from(&workspace);
                let mut streamed = String::new();
                let res = crate::engine::agent::run_native_agent_with_history_streaming(
                    &url,
                    &model,
                    &ws_path,
                    &history,
                    &prompt,
                    allow_write,
                    6,
                    timeout,
                    |tool, args| {
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "tool.started",
                            "tool": tool,
                            "params": args
                        }));
                    },
                    |tool, _args, result| {
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "tool.completed",
                            "tool": tool,
                            "success": result["ok"].as_bool().unwrap_or(false),
                            "output": result.to_string()
                        }));
                    },
                    |delta| {
                        streamed.push_str(delta);
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "answer.delta",
                            "text": delta
                        }));
                    },
                );
                let status = match res {
                    Ok(answer) => {
                        if streamed.is_empty() {
                            let _ = tx.send(json!({
                                "protocol": 1,
                                "type": "answer.delta",
                                "text": answer
                            }));
                        } else if let Some(tail) = answer.strip_prefix(&streamed) {
                            if !tail.is_empty() {
                                let _ = tx.send(json!({
                                    "protocol": 1,
                                    "type": "answer.delta",
                                    "text": tail
                                }));
                            }
                        }
                        persist_native_turn(&workspace, &model, &prompt, &answer);
                        "ok"
                    }
                    Err(e) => {
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "answer.delta",
                            "text": format!("[{e}]")
                        }));
                        "failed"
                    }
                };
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::OllamaChat => {
                let _ = tx.send(json!({
                    "protocol": 1,
                    "type": "turn.phase",
                    "phase": "plan",
                    "label": if zh {
                        format!("等待 {model} 响应…")
                    } else {
                        format!("Waiting for {model}…")
                    }
                }));
                let sys = crate::engine::policy::general_system_prompt("today");
                let mut reply = String::new();
                let res = crate::engine::ollama::stream_chat_cancelable(
                    &url,
                    &model,
                    &sys,
                    &prompt,
                    timeout,
                    || cancel.load(Ordering::Relaxed),
                    |delta| {
                        reply.push_str(delta);
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "answer.delta",
                            "text": delta
                        }));
                    },
                );
                let status = match res {
                    Ok(_) => {
                        persist_native_turn(&workspace, &model, &prompt, &reply);
                        "ok"
                    }
                    Err(e) if e == "cancelled" => "cancelled",
                    Err(e) => {
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "answer.delta",
                            "text": format!("\n[{e}]")
                        }));
                        "failed"
                    }
                };
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
            NativeRoute::CloudChat(cloud) => {
                let _ = tx.send(json!({
                    "protocol": 1,
                    "type": "turn.phase",
                    "phase": "plan",
                    "label": if zh {
                        format!("等待 {model} 响应…")
                    } else {
                        format!("Waiting for {model}…")
                    }
                }));
                let use_agent = !crate::engine::policy::is_conversation_only_message(&prompt);
                if use_agent {
                    if let Some(api_key) = cloud.resolve_api_key() {
                        let ws_path = Path::new(&workspace).to_path_buf();
                        let res = crate::engine::agent::run_cloud_agent_with_history(
                            cloud,
                            &api_key,
                            &model,
                            &ws_path,
                            &history,
                            &prompt,
                            allow_write,
                            6,
                            timeout,
                            |tool, args, result| {
                                let _ = tx.send(json!({
                                    "protocol": 1,
                                    "type": "tool.started",
                                    "tool": tool,
                                    "params": args
                                }));
                                let _ = tx.send(json!({
                                    "protocol": 1,
                                    "type": "tool.completed",
                                    "tool": tool,
                                    "success": result["ok"].as_bool().unwrap_or(false),
                                    "output": result.to_string()
                                }));
                            },
                        );
                        let status = match res {
                            Ok(answer) => {
                                let _ = tx.send(json!({
                                    "protocol": 1,
                                    "type": "answer.delta",
                                    "text": answer
                                }));
                                persist_native_turn(&workspace, &model, &prompt, &answer);
                                "ok"
                            }
                            Err(e) => {
                                let _ = tx.send(json!({
                                    "protocol": 1,
                                    "type": "answer.delta",
                                    "text": format!("\n[{e}]")
                                }));
                                "failed"
                            }
                        };
                        let _ = tx.send(
                            json!({"protocol": 1, "type": "turn.completed", "status": status}),
                        );
                        return;
                    }
                }
                let sys = crate::engine::policy::general_system_prompt("today");
                let res = crate::engine::cloud::stream_cloud_chat_cancelable(
                    cloud,
                    &model,
                    &sys,
                    &prompt,
                    timeout,
                    || cancel.load(Ordering::Relaxed),
                    |delta| {
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "answer.delta",
                            "text": delta
                        }));
                    },
                );
                let status = match res {
                    Ok(_) => "ok",
                    Err(e) if e == "cancelled" => "cancelled",
                    Err(e) => {
                        let _ = tx.send(json!({
                            "protocol": 1,
                            "type": "answer.delta",
                            "text": format!("\n[{e}]")
                        }));
                        "failed"
                    }
                };
                let _ = tx.send(json!({"protocol": 1, "type": "turn.completed", "status": status}));
            }
        }
    });
    true
}

/// The OSC 52 sequence that puts text on the terminal's clipboard.
pub(crate) fn osc52(text: &str) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let bytes = text.as_bytes();
    let mut encoded = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= chunk.len() {
                encoded.push(ALPHABET[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                encoded.push('=');
            }
        }
    }
    format!("\x1b]52;c;{encoded}\x07")
}

fn key_request(app: &mut App, key: KeyEvent) -> Option<Value> {
    let request = key_request_inner(app, key);
    app.sync_popup();
    // Typing an `@file` asks the runtime for matches as you go.
    if request.is_none() && app.wants_reference_lookup() {
        app.file_asked = app.editor.text().to_owned();
        return Some(
            json!({"type":"completion.request","protocol":1,"session_id":app.session,"text":app.editor.text()}),
        );
    }
    request
}

fn key_request_inner(app: &mut App, key: KeyEvent) -> Option<Value> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    if key.code == KeyCode::F(1) {
        app.help = !app.help;
        return None;
    }
    if app.help {
        if key.code == KeyCode::Esc {
            app.help = false;
        }
        return None;
    }
    if ctrl && key.code == KeyCode::Char('o') {
        app.details = !app.details;
        return None;
    }
    if let Some(d) = app.dialog.as_mut().filter(|d| d.picker) {
        match key.code {
            KeyCode::Up => d.step(-1),
            KeyCode::Down => d.step(1),
            KeyCode::PageUp => d.step(-8),
            KeyCode::PageDown => d.step(8),
            KeyCode::Enter => {
                if d.visible().contains(&d.selected) {
                    return app.respond(None);
                }
            }
            // Esc clears the search first; with nothing typed it closes the picker.
            KeyCode::Esc if !d.filter.is_empty() => {
                d.filter.clear();
                d.refilter();
            }
            KeyCode::Esc => return app.respond(Some(-1)),
            KeyCode::Char('c') if ctrl => return app.respond(Some(-1)),
            KeyCode::Char('u') if ctrl => {
                d.filter.clear();
                d.refilter();
            }
            KeyCode::Backspace => {
                d.filter.pop();
                d.refilter();
            }
            KeyCode::Char(c) if !ctrl && !alt => {
                d.filter.push(c);
                d.refilter();
            }
            _ => (),
        }
        return None;
    }
    if let Some(d) = app.dialog.as_mut() {
        match key.code {
            KeyCode::PageUp => {
                d.context_scroll = d.context_scroll.saturating_sub(5);
                return None;
            }
            KeyCode::PageDown => {
                d.context_scroll = d
                    .context_scroll
                    .saturating_add(5)
                    .min(d.context_max_scroll.get());
                return None;
            }
            _ => (),
        }
        if key.code == KeyCode::Esc || (ctrl && key.code == KeyCode::Char('c')) {
            if d.choices.is_empty() {
                return app.cancel();
            }
            return app.respond(Some(-1));
        }
        if !d.choices.is_empty() {
            match key.code {
                KeyCode::Up => d.selected = d.selected.saturating_sub(1),
                KeyCode::Down => d.selected = (d.selected + 1).min(d.choices.len() - 1),
                KeyCode::Enter => return app.respond(None),
                KeyCode::Char(c) => {
                    let choice = d.shortcuts[c.to_string()]
                        .as_i64()
                        .or_else(|| c.to_digit(10).map(|i| i64::from(i) - 1));
                    if let Some(i) = choice.filter(|i| *i >= 0 && (*i as usize) < d.choices.len()) {
                        return app.respond(Some(i));
                    }
                }
                _ => (),
            }
            return None;
        }
        if key.code == KeyCode::Enter && !alt && !key.modifiers.contains(KeyModifiers::SHIFT) {
            return app.respond(None);
        }
        edit(&mut d.editor, key);
        return None;
    }
    // The command list that opens on `/` takes the arrow keys, Tab, Enter and
    // Esc while it is showing; everything else types as usual.
    if !app.popup_rows().0.is_empty() && !ctrl && !alt {
        match key.code {
            KeyCode::Up => {
                app.popup_move(false);
                return None;
            }
            KeyCode::Down => {
                app.popup_move(true);
                return None;
            }
            KeyCode::Tab => {
                app.popup_accept();
                return None;
            }
            // Enter fills in the highlighted command rather than running it, so a
            // partly typed `/mo` cannot run the wrong thing; a second Enter runs it.
            KeyCode::Enter if !key.modifiers.contains(KeyModifiers::SHIFT) => {
                // Already the highlighted command, in full: Enter runs it.
                let (items, files) = app.popup_rows();
                let typed = app.editor.text().to_owned();
                let highlighted = items.get(app.popup_index).map(|(name, _)| name.as_str());
                // A file is always filled in, never sent: Enter on `@src/` must not submit.
                if (files || highlighted != Some(typed.as_str())) && app.popup_accept() {
                    return None;
                }
            }
            KeyCode::Esc => {
                app.popup_dismissed = Some(app.editor.text().to_owned());
                return None;
            }
            _ => (),
        }
    }
    match key.code {
        KeyCode::Esc => {
            if app.turn.is_some() {
                return app.cancel();
            }
            app.scroll = 0;
        }
        KeyCode::Char('c') if ctrl => {
            if app.turn.is_some() {
                return app.cancel();
            }
            app.editor.clear();
        }
        KeyCode::Char('d') if ctrl && app.editor.is_empty() => {
            return Some(json!({"type":"shutdown","protocol":1}))
        }
        KeyCode::Enter if !alt && !key.modifiers.contains(KeyModifiers::SHIFT) => {
            return app.submit()
        }
        KeyCode::Tab => {
            if app.ready
                && app.reference_completion
                && app.turn.is_none()
                && app.editor.at_end()
                && app.editor.text().contains('@')
            {
                return Some(
                    json!({"type":"completion.request","protocol":1,"session_id":app.session,"text":app.editor.text()}),
                );
            }
            app.complete();
        }
        KeyCode::PageUp => app.scroll = app.scroll.saturating_add(10),
        KeyCode::PageDown => app.scroll = app.scroll.saturating_sub(10),
        _ => {
            if matches!(key.code, KeyCode::Char(_)) && !ctrl {
                app.scroll = 0;
            }
            edit(&mut app.editor, key);
        }
    }
    None
}
fn edit(editor: &mut editor::Editor, key: KeyEvent) {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Enter | KeyCode::Char('j') if key.code == KeyCode::Enter || ctrl => {
            editor.insert("\n")
        }
        KeyCode::Char('u') if ctrl => editor.kill_before(),
        KeyCode::Char('k') if ctrl => editor.kill_after(),
        KeyCode::Char('w') if ctrl => editor.kill_word(),
        KeyCode::Char('a') if ctrl => editor.home(),
        KeyCode::Char('e') if ctrl => editor.end(),
        KeyCode::Char('d') if ctrl => editor.delete(),
        KeyCode::Backspace if key.modifiers.contains(KeyModifiers::ALT) => editor.kill_word(),
        KeyCode::Backspace => editor.backspace(),
        KeyCode::Delete => editor.delete(),
        KeyCode::Left => editor.left(),
        KeyCode::Right => editor.right(),
        KeyCode::Home => editor.home(),
        KeyCode::End => editor.end(),
        KeyCode::Up => {
            if !editor.up() {
                editor.history_prev();
            }
        }
        KeyCode::Down => {
            if !editor.down() {
                editor.history_next();
            }
        }
        KeyCode::Char(c) if !ctrl && !key.modifiers.contains(KeyModifiers::ALT) => {
            editor.insert(&c.to_string())
        }
        _ => (),
    }
}

fn machine(
    session: &mut Session,
    timeout: Duration,
    cancelled: &AtomicBool,
) -> Result<i32, String> {
    let (tx, requests) = mpsc::sync_channel(32);
    thread::spawn(move || {
        let mut reader = BufReader::new(io::stdin());
        loop {
            let result = crate::render::read_event(&mut reader).and_then(|v| {
                v.map(|bytes| serde_json::from_slice::<Value>(&bytes).map_err(|e| e.to_string()))
                    .transpose()
            });
            let done = !matches!(result, Ok(Some(_)));
            if tx.send(result).is_err() || done {
                break;
            }
        }
    });
    let (out_tx, out_rx) = mpsc::sync_channel::<Value>(32);
    let (written_tx, written_rx) = mpsc::channel();
    thread::spawn(move || {
        let mut out = io::stdout().lock();
        let result = (|| {
            while let Ok(value) = out_rx.recv() {
                serde_json::to_writer(&mut out, &value).map_err(|e| e.to_string())?;
                out.write_all(b"\n")
                    .and_then(|_| out.flush())
                    .map_err(|e| e.to_string())?;
            }
            Ok::<_, String>(())
        })();
        let _ = written_tx.send(result);
    });
    let mut deadline = Some(Instant::now() + timeout);
    let mut eof = false;
    let mut closed = false;
    loop {
        if cancelled.load(Ordering::SeqCst) {
            return Err("Cancelled".into());
        }
        if deadline.is_some_and(|t| Instant::now() >= t) {
            return Err("Application runtime timed out".into());
        }
        match session.events.recv_timeout(Duration::from_millis(10)) {
            Ok(Ok(Some(value))) => {
                if value["protocol"] != 1 || !value["type"].is_string() {
                    return Err("Invalid application event".into());
                }
                match value["type"].as_str().unwrap() {
                    "session.ready" | "turn.completed" if !eof => deadline = None,
                    "approval.requested" | "input.requested" if !eof => deadline = None,
                    "dialog.closed" => deadline = Some(Instant::now() + timeout),
                    "session.closed" => {
                        closed = true;
                        deadline = Some(Instant::now() + Duration::from_secs(2));
                    }
                    _ => (),
                }
                out_tx
                    .try_send(value)
                    .map_err(|_| "Application output queue is full or closed")?;
            }
            Ok(Ok(None)) => {
                if !closed {
                    return Err("Application runtime disconnected unexpectedly".into());
                }
                break;
            }
            Ok(Err(error)) => return Err(error),
            Err(mpsc::RecvTimeoutError::Timeout) => (),
            Err(_) => return Err("Application event reader disconnected".into()),
        }
        if !eof {
            match requests.try_recv() {
                Ok(Ok(Some(request))) => {
                    match request["type"].as_str() {
                        Some(
                            "turn.submit" | "dialog.respond" | "session.resume" | "session.new",
                        ) => deadline = Some(Instant::now() + timeout),
                        Some("shutdown" | "turn.cancel") => {
                            if request["type"] == "shutdown" {
                                eof = true;
                            }
                            deadline = Some(Instant::now() + Duration::from_secs(2))
                        }
                        _ => (),
                    }
                    session.send(request)?;
                }
                Ok(Ok(None)) => {
                    eof = true;
                    session.send(json!({"type":"shutdown","protocol":1}))?;
                    deadline = Some(Instant::now() + Duration::from_secs(2));
                }
                Ok(Err(error)) => return Err(error),
                Err(mpsc::TryRecvError::Empty) => (),
                Err(_) => eof = true,
            }
        }
    }
    drop(out_tx);
    written_rx
        .recv_timeout(timeout)
        .map_err(|_| "Application output could not be flushed")??;
    let end = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = session.worker.child.try_wait().map_err(|e| e.to_string())? {
            return Ok(status.code().unwrap_or(1));
        }
        if Instant::now() >= end {
            return Err("Application did not exit after session.closed".into());
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn approval_shortcuts_are_explicit_and_scoped() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.editor.insert("write a file");
        app.submit();
        app.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1","request_id":"a",
            "choices":[["Yes","once"],["No","decline"]],"shortcuts":{"y":0,"n":1}}),
        )
        .unwrap();
        let request = key_request(
            &mut app,
            KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE),
        )
        .unwrap();
        assert_eq!(request["choice"], 1);
        assert_eq!(request["turn_id"], "turn-1");
        assert_eq!(request["request_id"], "a");
        assert!(app.dialog.is_none());
    }
    #[test]
    fn preview_navigation_never_answers_an_approval() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.editor.insert("edit a file");
        app.submit();
        app.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"a","context":"long preview","choices":[["Allow",""],["Decline",""]]}),
        )
        .unwrap();
        app.dialog.as_ref().unwrap().context_max_scroll.set(12);
        for _ in 0..5 {
            assert!(key_request(
                &mut app,
                KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)
            )
            .is_none());
        }
        assert_eq!(app.dialog.as_ref().unwrap().context_scroll, 12);
        assert_eq!(app.dialog.as_ref().unwrap().selected, 0);
        assert!(
            key_request(&mut app, KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE)).is_none()
        );
        assert_eq!(app.dialog.as_ref().unwrap().context_scroll, 7);
        assert_eq!(app.turn.as_deref(), Some("turn-1"));
        let request =
            key_request(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)).unwrap();
        assert_eq!(request["choice"], -1);
    }
    #[test]
    fn draft_and_cancel_are_independent() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.editor.insert("task");
        app.submit();
        app.editor.insert("next draft");
        assert!(key_request(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)).is_none());
        let cancel =
            key_request(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)).unwrap();
        assert_eq!(cancel["type"], "turn.cancel");
        assert_eq!(app.editor.text(), "next draft");
    }

    #[test]
    fn osc52_encodes_the_text_for_the_clipboard() {
        assert_eq!(super::osc52("hi"), "\x1b]52;c;aGk=\x07");
        assert_eq!(super::osc52("答案"), "\x1b]52;c;562U5qGI\x07");
        assert_eq!(super::osc52(""), "\x1b]52;c;\x07");
    }

    #[test]
    fn copy_takes_the_last_answer_without_a_turn() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.add("assistant", "first", false);
        app.add("assistant", "second", false);
        app.editor.insert("/copy");
        assert!(app.submit().is_none());
        assert_eq!(app.clipboard.as_deref(), Some("second"));
        assert!(app.turn.is_none());
        let mut empty = App {
            ready: true,
            ..App::default()
        };
        empty.editor.insert("/copy");
        assert!(empty.submit().is_none());
        assert!(empty.clipboard.is_none());
    }

    // ── The command list that opens on `/` ──────────────────────────────
    fn with_commands() -> App {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        let info = [
            ("/help", "Show help"),
            ("/model", "Choose a model"),
            ("/models", "List models"),
            ("/review", "Review changes"),
            ("/memory", "Manage memory"),
        ];
        app.commands = info.iter().map(|(n, _)| n.to_string()).collect();
        app.command_order = app.commands.clone();
        app.command_info = info
            .iter()
            .map(|(n, d)| (n.to_string(), d.to_string()))
            .collect();
        app
    }
    fn press(app: &mut App, code: KeyCode) -> Option<Value> {
        key_request(app, KeyEvent::new(code, KeyModifiers::NONE))
    }
    fn typed(app: &mut App, text: &str) {
        for c in text.chars() {
            press(app, KeyCode::Char(c));
        }
    }
    fn names(app: &App) -> Vec<String> {
        app.slash_items().into_iter().map(|(n, _)| n).collect()
    }

    #[test]
    fn typing_a_slash_opens_the_list_and_narrows_as_you_type() {
        let mut app = with_commands();
        typed(&mut app, "/");
        assert_eq!(names(&app).len(), 5, "a bare slash lists the commands");
        typed(&mut app, "mo");
        // Starts-with first, then contains ("memory" has m..o inside), in the runtime's order.
        assert_eq!(names(&app), ["/model", "/models", "/memory"]);
        assert_eq!(
            app.slash_items()[0].1,
            "Choose a model",
            "each row carries its description"
        );
        typed(&mut app, "x");
        assert!(
            names(&app).is_empty(),
            "nothing matches, so the list closes"
        );
    }

    #[test]
    fn letters_in_order_find_a_command_the_way_mdl_finds_model() {
        let mut app = with_commands();
        typed(&mut app, "/mdl");
        assert_eq!(names(&app), ["/model", "/models"]);
    }

    #[test]
    fn arrows_move_the_highlight_and_wrap() {
        let mut app = with_commands();
        typed(&mut app, "/m");
        assert_eq!(app.popup_index, 0);
        press(&mut app, KeyCode::Down);
        assert_eq!(app.popup_index, 1);
        press(&mut app, KeyCode::Up);
        press(&mut app, KeyCode::Up);
        assert_eq!(
            app.popup_index,
            names(&app).len() - 1,
            "Up from the first row wraps to the last"
        );
        press(&mut app, KeyCode::Down);
        assert_eq!(app.popup_index, 0);
        assert!(
            app.editor.text() == "/m",
            "arrows must not touch the text or the history"
        );
    }

    #[test]
    fn tab_fills_in_the_highlighted_command_ready_for_arguments() {
        let mut app = with_commands();
        typed(&mut app, "/mo");
        press(&mut app, KeyCode::Down);
        assert!(press(&mut app, KeyCode::Tab).is_none());
        assert_eq!(app.editor.text(), "/models ");
        assert!(
            names(&app).is_empty(),
            "the list closes once the command word is complete"
        );
    }

    #[test]
    fn enter_fills_in_a_partial_command_and_only_a_second_enter_runs_it() {
        let mut app = with_commands();
        typed(&mut app, "/rev");
        assert!(
            press(&mut app, KeyCode::Enter).is_none(),
            "the first Enter must not send anything"
        );
        assert_eq!(app.editor.text(), "/review ");
        let request = press(&mut app, KeyCode::Enter).expect("the second Enter sends");
        assert_eq!(request["type"], "turn.submit");
    }

    #[test]
    fn enter_on_a_complete_command_runs_it_at_once() {
        let mut app = with_commands();
        typed(&mut app, "/model");
        assert_eq!(names(&app), ["/model", "/models"]);
        let request = press(&mut app, KeyCode::Enter).expect("a typed-out command runs on Enter");
        assert_eq!(request["type"], "turn.submit");
        assert!(request["text"].as_str().unwrap().starts_with("/model"));
    }

    #[test]
    fn escape_closes_the_list_for_this_text_and_typing_reopens_it() {
        let mut app = with_commands();
        typed(&mut app, "/m");
        assert!(press(&mut app, KeyCode::Esc).is_none());
        assert!(names(&app).is_empty());
        assert_eq!(app.editor.text(), "/m", "Esc must not clear what was typed");
        typed(&mut app, "o");
        assert!(!names(&app).is_empty(), "more typing brings the list back");
    }

    #[test]
    fn the_list_stays_out_of_the_way_when_it_does_not_apply() {
        let mut app = with_commands();
        typed(&mut app, "hello /mo");
        assert!(
            names(&app).is_empty(),
            "a slash mid-sentence is not a command"
        );
        let mut app = with_commands();
        typed(&mut app, "/model gpt");
        assert!(
            names(&app).is_empty(),
            "after the command word it is arguments"
        );
        let mut app = with_commands();
        app.turn = Some("turn-1".into());
        typed(&mut app, "/mo");
        assert!(names(&app).is_empty(), "no list while a turn is running");
        let mut app = with_commands();
        app.ready = false;
        app.editor.insert("/mo");
        assert!(names(&app).is_empty());
    }

    // ── The file list that opens on `@` ──────────────────────────────────
    fn answer(app: &mut App, candidates: &[&str]) {
        let text = app.editor.text().to_owned();
        app.event(
            json!({"protocol":1,"type":"completion.result","session_id":app.session,
            "text":text,"candidates":candidates}),
        )
        .unwrap();
    }

    #[test]
    fn typing_at_asks_for_files_once_per_change_and_lists_the_answer() {
        let mut app = with_commands();
        app.reference_completion = true;
        typed(&mut app, "read @");
        // The last keystroke asked; nothing is listed until the answer arrives.
        assert_eq!(app.file_asked, "read @");
        assert!(app.file_items().is_empty());
        answer(&mut app, &["read @src/", "read @README.md"]);
        let rows: Vec<String> = app.file_items().into_iter().map(|(l, _)| l).collect();
        assert_eq!(rows, vec!["@src/", "@README.md"]);
        // An answer for older text is ignored.
        press(&mut app, KeyCode::Char('x'));
        assert!(app.file_items().is_empty());
    }

    #[test]
    fn no_lookup_without_runtime_support_or_outside_a_token() {
        let mut app = with_commands();
        let sent = typed_last(&mut app, "read @a");
        assert!(
            sent.is_none(),
            "runtime did not advertise reference_completion"
        );
        let mut app = with_commands();
        app.reference_completion = true;
        let sent = typed_last(&mut app, "mail me@a");
        assert!(sent.is_none(), "an @ inside a word is not a file reference");
    }

    fn typed_last(app: &mut App, text: &str) -> Option<Value> {
        let mut last = None;
        for c in text.chars() {
            last = press(app, KeyCode::Char(c));
        }
        last
    }

    #[test]
    fn tab_and_enter_fill_in_a_file_and_a_folder_keeps_listing() {
        let mut app = with_commands();
        app.reference_completion = true;
        typed(&mut app, "see @s");
        answer(&mut app, &["see @src/", "see @setup.py"]);
        assert!(press(&mut app, KeyCode::Down).is_none());
        // Enter fills the file in; it must not submit the prompt.
        assert!(press(&mut app, KeyCode::Enter).is_none());
        assert_eq!(app.editor.text(), "see @setup.py ");
        assert!(app.turn.is_none());
        // A folder is filled without a space, and the next level is asked for.
        let mut app = with_commands();
        app.reference_completion = true;
        typed(&mut app, "see @s");
        answer(&mut app, &["see @src/"]);
        let request = press(&mut app, KeyCode::Tab).expect("asks for the folder's contents");
        assert_eq!(request["type"], "completion.request");
        assert_eq!(request["text"], "see @src/");
    }

    #[test]
    fn esc_closes_the_file_list_until_the_text_changes() {
        let mut app = with_commands();
        app.reference_completion = true;
        typed(&mut app, "see @s");
        answer(&mut app, &["see @src/"]);
        press(&mut app, KeyCode::Esc);
        assert!(app.file_items().is_empty());
    }

    // ── The searchable model picker ──────────────────────────────────────
    fn picker_app() -> App {
        let mut app = with_commands();
        app.turn = Some("t".into());
        app.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"t","request_id":"pick",
            "title":"Select Model","picker":true,
            "choices":[["Gemini Flash","google/gemini-3.5-flash"],["Qwen 2.5","qwen2.5:7b"],
                       ["Qwen 2.5","qwen2.5:14b"],["Llama 3.1","llama3.1:8b"]],
            "groups":["Aria","Aria","Aria","Community"],"selected":1}),
        )
        .unwrap();
        app
    }

    #[test]
    fn typing_in_the_picker_searches_instead_of_choosing_by_number() {
        let mut app = picker_app();
        let d = app.dialog.as_ref().unwrap();
        assert!(d.picker && d.groups.len() == 4);
        assert_eq!(d.selected, 1, "opens on the current model");
        typed(&mut app, "14");
        let d = app.dialog.as_ref().unwrap();
        assert_eq!(d.visible(), vec![2], "the id in the detail is searchable");
        assert_eq!(d.selected, 2);
        assert!(app.dialog.is_some(), "digits did not answer the dialog");
    }

    #[test]
    fn picker_arrows_skip_hidden_rows_and_enter_answers_with_the_original_index() {
        let mut app = picker_app();
        typed(&mut app, "qwen");
        press(&mut app, KeyCode::Down);
        assert_eq!(app.dialog.as_ref().unwrap().selected, 2);
        press(&mut app, KeyCode::Down);
        assert_eq!(
            app.dialog.as_ref().unwrap().selected,
            2,
            "stops at the last match"
        );
        let request = press(&mut app, KeyCode::Enter).expect("answers");
        assert_eq!(request["type"], "dialog.respond");
        assert_eq!(request["choice"], 2);
    }

    #[test]
    fn picker_esc_clears_the_search_first_then_closes_and_enter_needs_a_match() {
        let mut app = picker_app();
        typed(&mut app, "zzz");
        assert!(
            press(&mut app, KeyCode::Enter).is_none(),
            "nothing matches, nothing to choose"
        );
        assert!(app.dialog.is_some());
        assert!(press(&mut app, KeyCode::Esc).is_none());
        let d = app.dialog.as_ref().unwrap();
        assert!(d.filter.is_empty() && d.visible().len() == 4);
        let request = press(&mut app, KeyCode::Esc).expect("closes");
        assert_eq!(request["choice"], -1);
    }

    #[test]
    fn picker_backspace_widens_the_search_again() {
        let mut app = picker_app();
        typed(&mut app, "llama");
        assert_eq!(app.dialog.as_ref().unwrap().visible(), vec![3]);
        for _ in 0..5 {
            press(&mut app, KeyCode::Backspace);
        }
        assert_eq!(app.dialog.as_ref().unwrap().visible().len(), 4);
    }

    #[test]
    fn native_route_intercepts_ollama_health_and_capability_turns() {
        let mut app = App {
            provider: "ollama".into(),
            model: "qwen2.5:7b".into(),
            ..App::default()
        };
        let health = json!({"type": "turn.submit", "text": "/health"});
        assert_eq!(
            classify_native_route(&app, &health),
            Some(NativeRoute::Health)
        );

        let cap = json!({"type": "turn.submit", "text": "你的health service有哪些服务以及能帮助我做什么"});
        assert_eq!(
            classify_native_route(&app, &cap),
            Some(NativeRoute::OllamaChat)
        );

        let code = json!({"type": "turn.submit", "text": "fix src/main.rs"});
        assert_eq!(classify_native_route(&app, &code), None);

        app.provider = "google".into();
        assert_eq!(classify_native_route(&app, &cap), None);
    }

    #[test]
    fn native_route_supports_sessions_review_and_cloud_coding() {
        let mut app = App {
            provider: "openai".into(),
            model: "gpt-4o".into(),
            status: "Native ready".into(),
            ..App::default()
        };
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/sessions"})),
            Some(NativeRoute::Sessions(None))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/scan"})),
            Some(NativeRoute::Scan)
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/map"})),
            Some(NativeRoute::Map)
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/rewind"})),
            Some(NativeRoute::Rewind)
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/worktree create wt1"})
            ),
            Some(NativeRoute::Worktree("create".into(), Some("wt1".into())))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/mcp connect"})
            ),
            Some(NativeRoute::Mcp("connect".into()))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/mcp probe"})),
            Some(NativeRoute::Mcp("probe".into()))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/skills"})),
            Some(NativeRoute::Skills)
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/init --force"})
            ),
            Some(NativeRoute::Init(true))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/doctor"})),
            Some(NativeRoute::Doctor)
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/help"})),
            Some(NativeRoute::Help)
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/clear"})),
            Some(NativeRoute::Clear)
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/new"})),
            Some(NativeRoute::Clear)
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/config"})),
            Some(NativeRoute::Config)
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/permissions"})
            ),
            Some(NativeRoute::Permissions(String::new()))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/permissions init"})
            ),
            Some(NativeRoute::Permissions("init".into()))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/mcp connect --uvx"})
            ),
            Some(NativeRoute::Mcp("connect --uvx".into()))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/hooks init"})),
            Some(NativeRoute::Hooks("init".into()))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/diff src/main.rs"})
            ),
            Some(NativeRoute::Diff(Some("src/main.rs".into())))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/log 5"})),
            Some(NativeRoute::Log(5))
        );
        assert_eq!(
            classify_native_route(&app, &json!({"type": "turn.submit", "text": "/verify"})),
            Some(NativeRoute::Verify(None))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/write-mode on"})
            ),
            Some(NativeRoute::WriteMode(Some(true)))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/sessions auth"})
            ),
            Some(NativeRoute::Sessions(Some("auth".into())))
        );
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "/review src/main.rs"})
            ),
            Some(NativeRoute::Review(Some("src/main.rs".into())))
        );

        unsafe { std::env::set_var("OPENAI_API_KEY", "test-key") };
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "fix src/main.rs"})
            ),
            Some(NativeRoute::CloudChat(
                crate::engine::cloud::CloudProvider::OpenAI
            ))
        );
        app.provider = "ollama".into();
        assert_eq!(
            classify_native_route(
                &app,
                &json!({"type": "turn.submit", "text": "fix src/main.rs"})
            ),
            Some(NativeRoute::OllamaAgent)
        );
    }

    #[test]
    fn formats_mcp_probe_report_for_ok_and_failed_servers() {
        use crate::engine::mcp::McpServerHealth;

        let (empty_msg, empty_status) = format_mcp_probe_report(&[]);
        assert_eq!(empty_status, "ok");
        assert!(empty_msg.contains("No configured MCP servers"));

        let (msg, status) = format_mcp_probe_report(&[
            McpServerHealth {
                name: "erp-service".into(),
                command: "erp-service-mcp".into(),
                connected: true,
                tool_count: 2,
                read_only_count: 2,
                tool_names: vec!["erp.warehouse.analyze".into(), "erp.inventory.plan".into()],
                latency_ms: 12,
                error: None,
            },
            McpServerHealth {
                name: "arthera-finance".into(),
                command: "arthera-financial-service".into(),
                connected: false,
                tool_count: 0,
                read_only_count: 0,
                tool_names: vec![],
                latency_ms: 5,
                error: Some("not installed".into()),
            },
        ]);
        assert_eq!(status, "failed");
        assert!(msg.contains("erp-service"));
        assert!(msg.contains("2 tools, 2 read-only, 12 ms"));
        assert!(msg.contains("arthera-finance"));
        assert!(msg.contains("not installed"));
    }
}
