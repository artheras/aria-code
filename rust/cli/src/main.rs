mod chat;
mod completions;
mod engine;
mod frontend;
mod render;
mod runtime;
mod sessions;
mod state;
mod stream;
mod updates;
mod worker;

use serde_json::{json, Value};
use std::io::{IsTerminal, Read};
use std::{env, ffi::OsString, path::PathBuf, time::Duration};

/// A repository file list; bounded as in the native indexer.
const MAX_INDEX_REQUEST: u64 = 256 * 1024 * 1024;

const HELP: &str = "aria-native — experimental Rust entry point (Python runtime required for tools/chat)

Usage:
  aria-native [--python EXE] [-C DIR] [--timeout-ms N] chat [--jsonl] [--resume ID] [-- PYTHON OPTIONS...]
  aria-native [--python EXE] [-C DIR] run [--] [ARIA ARGUMENTS...]
  aria-native [--python EXE] [-C DIR] [--timeout-ms N] exec [--jsonl] PROMPT [-- PYTHON OPTIONS...]
  aria-native render [--jsonl] < EVENTS.jsonl
  aria-native [--python EXE] [-C DIR] [--timeout-ms N] tool [--approve-write] NAME JSON
  aria-native config [paths|show]
  aria-native sessions list [--limit N]
  aria-native sessions search QUERY [--limit N]
  aria-native sessions show ID
  aria-native [--python EXE] [-C DIR] resume ID [--] [ARIA ARGUMENTS...]
  aria-native update [check] [--current VERSION] [--channel native|npm|pip|source] [--offline|--refresh]
  aria-native update install --to VERSION
  aria-native update auto-install --current VERSION
  aria-native update rollback
  aria-native physical [jsonl|agent-jsonl|mcp|serve --listen 127.0.0.1:8765]
  aria-native index imports|symbols < REQUEST.json
  aria-native completions bash|zsh|fish
  aria-native --version

Examples:
  aria-native run -- --health
  aria-native -C ./project tool read_file '{\"path\":\"README.md\"}'
  aria-native -C ./project tool --approve-write edit_file '{\"path\":\"app.py\",\"old_string\":\"old\",\"new_string\":\"new\"}'

Tools: read_file, list_files, search_code, write_file, edit_file.
Tool requests are confined to DIR (default: current directory); writes require
explicit per-invocation --approve-write. Persistent tool denials still apply.
Timeout: tools 30000 ms, exec 300000 ms by default. Tool stdout contains JSON only.
Exec renders streamed answer text to stdout and progress to stderr. --jsonl keeps
validated events on stdout. Render replays JSONL without Python or model calls.
Exec options: --model, --url, --thinking, --local, --allow-tools,
--dangerously-skip-permissions, --add-dir, --read-dir. Approvals are never added.
Use --python or ARIA_PYTHON to select the installed Python Aria environment.
Index imports/symbols read one JSON request on stdin and emit one JSON response.
Chat is a persistent Rust terminal interface (native by default; pass --with-python for Python runtime, --jsonl for protocol).
Chat options match exec; --resume ID restores a saved session. F1 shows native shortcuts.
This prototype does not replace the stable aria/aria-code commands.
Config/session inspection and update metadata checking execute in Rust without Python.
Config show includes only non-secret stored preferences, not the effective project config.
Update --current is the installed aria-code product version, not the prototype version.
";

#[derive(Debug)]
struct Options {
    python: OsString,
    workspace: PathBuf,
    timeout: Duration,
    mode: Mode,
}

#[derive(Debug, PartialEq)]
enum IndexKind {
    Imports,
    Symbols,
    Changes,
}

#[derive(Debug)]
enum Mode {
    Help,
    Completions(String),
    Physical(Vec<String>),
    Version,
    Index(IndexKind),
    Run(Vec<OsString>),
    Chat {
        args: Vec<OsString>,
        jsonl: bool,
    },
    Exec {
        prompt: String,
        args: Vec<OsString>,
        jsonl: bool,
    },
    Render(bool),
    Config(bool),
    Scan,
    Setup {
        provider: Option<String>,
        model: Option<String>,
        api_key: Option<String>,
    },
    Health {
        ollama_url: String,
    },
    Ask {
        provider: String,
        model: String,
        ollama_url: String,
        prompt: String,
    },
    Agent {
        provider: String,
        model: String,
        ollama_url: String,
        allow_write: bool,
        prompt: String,
    },
    McpTools {
        program: String,
        args: Vec<String>,
    },
    Sessions {
        action: String,
        argument: Option<String>,
        limit: usize,
    },
    Resume {
        id: String,
        args: Vec<OsString>,
    },
    Update(updates::Check),
    UpdateAuto(updates::Check),
    UpdateInstall(String),
    UpdateAutoInstall(String),
    UpdateRollback,
    Tool {
        name: String,
        params: Value,
        approved: bool,
    },
}

fn parse(args: Vec<OsString>) -> Result<Options, String> {
    let mut python = runtime::default_python();
    let mut workspace = env::current_dir().map_err(|e| e.to_string())?;
    let mut timeout = Duration::from_secs(30);
    let mut timeout_set = false;
    let mut args = args.into_iter();
    let mode = loop {
        let Some(arg) = args.next() else {
            break Mode::Help;
        };
        match arg.to_str() {
            Some("--help" | "-h") => break Mode::Help,
            Some("--version" | "-V") => break Mode::Version,
            Some("--python") => python = args.next().ok_or("--python requires an executable")?,
            Some("-C" | "--workspace") => {
                workspace = args.next().ok_or("-C requires a directory")?.into()
            }
            Some("--timeout-ms") => {
                timeout_set = true;
                let value = args.next().ok_or("--timeout-ms requires a number")?;
                let ms: u64 = value
                    .to_str()
                    .and_then(|s| s.parse().ok())
                    .ok_or("Invalid timeout")?;
                if !(1..=300_000).contains(&ms) {
                    return Err("Timeout must be 1..300000 ms".into());
                }
                timeout = Duration::from_millis(ms);
            }
            Some("physical") => {
                let rest = args
                    .map(|s| {
                        s.into_string()
                            .map_err(|_| "Physical arguments must be UTF-8".to_string())
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                break Mode::Physical(rest);
            }
            Some("index") => {
                let kind = match args.next().as_ref().and_then(|a| a.to_str()) {
                    Some("imports") => IndexKind::Imports,
                    Some("symbols") => IndexKind::Symbols,
                    Some("changes") => IndexKind::Changes,
                    _ => {
                        return Err(
                            "index requires a subcommand: imports, symbols or changes".into()
                        )
                    }
                };
                if args.next().is_some() {
                    return Err("index reads its request from stdin".into());
                }
                break Mode::Index(kind);
            }
            Some("run") => {
                let mut rest: Vec<_> = args.collect();
                if rest.first().is_some_and(|arg| arg == "--") {
                    rest.remove(0);
                }
                break Mode::Run(rest);
            }
            Some("chat") => {
                let mut rest = Vec::new();
                let mut jsonl = false;
                let mut resume = None;
                while let Some(arg) = args.next() {
                    if arg == "--" {
                        rest.extend(args);
                        break;
                    }
                    match arg.to_str() {
                        Some("--jsonl") => jsonl = true,
                        Some("--resume") => {
                            let id = utf8(args.next(), "--resume requires a session ID")?;
                            sessions::validate_id(&id)?;
                            resume = Some(id);
                        }
                        _ => rest.push(arg),
                    }
                }
                stream::validate_args(&rest)?;
                if let Some(id) = resume {
                    rest.extend([OsString::from("--resume"), id.into()]);
                }
                if !timeout_set {
                    timeout = Duration::from_secs(300);
                }
                break Mode::Chat { args: rest, jsonl };
            }
            Some("render") => {
                let jsonl = match args.next().as_ref().and_then(|a| a.to_str()) {
                    None => false,
                    Some("--jsonl") => true,
                    _ => return Err("Usage: render [--jsonl] < EVENTS.jsonl".into()),
                };
                if args.next().is_some() {
                    return Err("Unexpected render argument".into());
                }
                break Mode::Render(jsonl);
            }
            Some("exec") => {
                let mut next = args.next();
                let jsonl = next.as_deref() == Some(std::ffi::OsStr::new("--jsonl"));
                if jsonl {
                    next = args.next();
                }
                if next.as_deref() == Some(std::ffi::OsStr::new("--")) {
                    next = args.next();
                }
                let prompt = utf8(next, "exec requires a prompt")?;
                if prompt.trim().is_empty() || prompt.len() > 65_536 {
                    return Err("exec prompt must be non-empty and at most 64 KiB".into());
                }
                let mut rest: Vec<_> = args.collect();
                if rest.first().is_some_and(|a| a == "--") {
                    rest.remove(0);
                }
                stream::validate_args(&rest)?;
                if !timeout_set {
                    timeout = Duration::from_secs(300);
                }
                break Mode::Exec {
                    prompt,
                    args: rest,
                    jsonl,
                };
            }
            Some("config") => {
                let action = args.next().unwrap_or_else(|| "show".into());
                let paths = match action.to_str() {
                    Some("paths") => true,
                    Some("show") => false,
                    _ => return Err("Usage: config [paths|show]".into()),
                };
                if args.next().is_some() {
                    return Err("Unexpected config argument".into());
                }
                break Mode::Config(paths);
            }
            Some("scan") => {
                if args.next().is_some() {
                    return Err("Usage: scan".into());
                }
                break Mode::Scan;
            }
            Some("setup") => {
                let mut provider = None;
                let mut model = None;
                let mut api_key = None;
                while let Some(arg) = args.next() {
                    match arg.to_str() {
                        Some("--provider") => {
                            provider =
                                Some(utf8(args.next(), "Missing provider after --provider")?);
                        }
                        Some("--model") => {
                            model = Some(utf8(args.next(), "Missing model after --model")?);
                        }
                        Some("--api-key") => {
                            api_key = Some(utf8(args.next(), "Missing value after --api-key")?);
                        }
                        _ => return Err(
                            "Usage: setup [--provider PROVIDER] [--model MODEL] [--api-key KEY]"
                                .into(),
                        ),
                    }
                }
                break Mode::Setup {
                    provider,
                    model,
                    api_key,
                };
            }
            Some("health") => {
                let mut ollama_url = env::var("OLLAMA_HOST")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "http://127.0.0.1:11434".to_owned());
                while let Some(arg) = args.next() {
                    match arg.to_str() {
                        Some("--url") => {
                            ollama_url = utf8(args.next(), "Missing URL after --url")?;
                        }
                        _ => return Err("Usage: health [--url URL]".into()),
                    }
                }
                break Mode::Health { ollama_url };
            }
            Some("mcp-tools") => {
                let program = utf8(args.next(), "Usage: mcp-tools PROGRAM [ARGS...]")?;
                let rest = args
                    .map(|s| {
                        s.into_string()
                            .map_err(|_| "MCP args must be UTF-8".to_string())
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                break Mode::McpTools {
                    program,
                    args: rest,
                };
            }
            Some("agent") => {
                let mut provider = env::var("ARIA_PROVIDER")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "ollama".to_owned());
                let mut model = env::var("ARIA_MODEL")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "qwen2.5:7b".to_owned());
                let mut ollama_url = env::var("OLLAMA_HOST")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "http://127.0.0.1:11434".to_owned());
                let mut allow_write = false;
                let mut prompt_parts: Vec<String> = Vec::new();
                while let Some(arg) = args.next() {
                    match arg.to_str() {
                        Some("--provider") => {
                            provider = utf8(args.next(), "Missing provider after --provider")?;
                        }
                        Some("--model") => {
                            model = utf8(args.next(), "Missing model after --model")?;
                        }
                        Some("--url") => {
                            ollama_url = utf8(args.next(), "Missing URL after --url")?;
                        }
                        Some("--allow-write") => allow_write = true,
                        Some(other) => prompt_parts.push(other.to_owned()),
                        None => return Err("Prompt must be valid UTF-8".into()),
                    }
                }
                let prompt = prompt_parts.join(" ");
                if prompt.trim().is_empty() {
                    return Err(
                        "Usage: agent [--provider PROVIDER] [--model MODEL] [--url URL] [--allow-write] PROMPT".into(),
                    );
                }
                break Mode::Agent {
                    provider,
                    model,
                    ollama_url,
                    allow_write,
                    prompt,
                };
            }
            Some("ask") => {
                let mut provider = env::var("ARIA_PROVIDER")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "ollama".to_owned());
                let mut model = env::var("ARIA_MODEL")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "qwen2.5:7b".to_owned());
                let mut ollama_url = env::var("OLLAMA_HOST")
                    .ok()
                    .filter(|s| !s.trim().is_empty())
                    .unwrap_or_else(|| "http://127.0.0.1:11434".to_owned());
                let mut prompt_parts: Vec<String> = Vec::new();
                while let Some(arg) = args.next() {
                    match arg.to_str() {
                        Some("--provider") => {
                            provider = utf8(args.next(), "Missing provider after --provider")?;
                        }
                        Some("--model") => {
                            model = utf8(args.next(), "Missing model after --model")?;
                        }
                        Some("--url") => {
                            ollama_url = utf8(args.next(), "Missing URL after --url")?;
                        }
                        Some(other) => prompt_parts.push(other.to_owned()),
                        None => return Err("Prompt must be valid UTF-8".into()),
                    }
                }
                let prompt = prompt_parts.join(" ");
                if prompt.trim().is_empty() {
                    return Err(
                        "Usage: ask [--provider PROVIDER] [--model MODEL] [--url URL] PROMPT"
                            .into(),
                    );
                }
                break Mode::Ask {
                    provider,
                    model,
                    ollama_url,
                    prompt,
                };
            }
            Some("completions") => {
                break Mode::Completions(utf8(args.next(), "Usage: completions bash|zsh|fish")?);
            }
            Some("sessions") => {
                let action = utf8(args.next(), "sessions requires list, search or show")?;
                let argument = match action.as_str() {
                    "list" => None,
                    "search" | "show" => Some(utf8(
                        args.next(),
                        "sessions search/show requires an argument",
                    )?),
                    _ => return Err("Usage: sessions list|search QUERY|show ID".into()),
                };
                let mut limit = 20;
                while let Some(arg) = args.next() {
                    if action == "show" || arg != "--limit" {
                        return Err("Unexpected sessions argument".into());
                    }
                    limit = utf8(args.next(), "--limit requires a number")?
                        .parse()
                        .map_err(|_| "Invalid session limit")?;
                    if !(1..=1000).contains(&limit) {
                        return Err("Session limit must be 1..1000".into());
                    }
                }
                break Mode::Sessions {
                    action,
                    argument,
                    limit,
                };
            }
            Some("resume") => {
                let id = utf8(args.next(), "resume requires a session ID")?;
                sessions::validate_id(&id)?;
                let mut rest: Vec<_> = args.collect();
                if rest.first().is_some_and(|v| v == "--") {
                    rest.remove(0);
                }
                if rest.iter().any(|v| {
                    v.to_str().is_some_and(|s| {
                        s == "--session" || s == "--resume" || s.starts_with("--session=")
                    })
                }) {
                    return Err("resume cannot forward another session selector".into());
                }
                break Mode::Resume { id, args: rest };
            }
            Some("update") => {
                let first = args.next();
                let first_str = first.as_ref().and_then(|a| a.to_str());
                if first_str == Some("rollback") {
                    if args.next().is_some() {
                        return Err("Usage: update rollback".into());
                    }
                    break Mode::UpdateRollback;
                }
                if first_str == Some("install") {
                    if args.next().as_deref() != Some(std::ffi::OsStr::new("--to")) {
                        return Err("Usage: update install --to VERSION".into());
                    }
                    let version = utf8(args.next(), "--to requires a stable release version")?;
                    if args.next().is_some() {
                        return Err("Usage: update install --to VERSION".into());
                    }
                    break Mode::UpdateInstall(version);
                }
                if first_str == Some("auto-install") {
                    if args.next().as_deref() != Some(std::ffi::OsStr::new("--current")) {
                        return Err("Usage: update auto-install --current VERSION".into());
                    }
                    let version = utf8(args.next(), "--current requires the product version")?;
                    if args.next().is_some() {
                        return Err("Usage: update auto-install --current VERSION".into());
                    }
                    break Mode::UpdateAutoInstall(version);
                }
                let mut is_check = false;
                let mut check = updates::Check {
                    current: String::new(),
                    channel: env::var("ARIA_CODE_INSTALL_CHANNEL")
                        .unwrap_or_else(|_| "native".into()),
                    offline: false,
                    refresh: false,
                    timeout: Duration::from_secs(8),
                };
                let mut current_opt = first;
                while let Some(arg) = current_opt.take().or_else(|| args.next()) {
                    match arg.to_str() {
                        Some("check") | Some("--check") => is_check = true,
                        Some("--current") => {
                            check.current =
                                utf8(args.next(), "--current requires the product version")?
                        }
                        Some("--channel") => {
                            check.channel = utf8(args.next(), "--channel requires a channel")?
                        }
                        Some("--offline") => check.offline = true,
                        Some("--refresh") => check.refresh = true,
                        _ => return Err("Unexpected update argument".into()),
                    }
                }
                if check.offline && check.refresh {
                    return Err("--offline and --refresh are mutually exclusive".into());
                }
                if is_check {
                    if check.current.is_empty() {
                        return Err(
                            "--current is required; use your installed aria-code --version".into(),
                        );
                    }
                    break Mode::Update(check);
                } else {
                    if check.current.is_empty() {
                        check.current = env!("ARIA_PRODUCT_VERSION").to_string();
                    }
                    if !check.offline {
                        check.refresh = true;
                    }
                    break Mode::UpdateAuto(check);
                }
            }
            Some("tool") => {
                let mut name = args.next().ok_or("tool requires NAME and JSON")?;
                let approved = name == "--approve-write";
                if approved {
                    name = args.next().ok_or("tool requires NAME and JSON")?;
                }
                let name = name.into_string().map_err(|_| "Tool name must be UTF-8")?;
                let raw = args.next().ok_or("tool requires NAME and JSON")?;
                let raw = raw.to_str().ok_or("Tool JSON must be UTF-8")?;
                if raw.len() > worker::MAX_MESSAGE {
                    return Err("Tool request exceeds 1 MiB".into());
                }
                let params: Value =
                    serde_json::from_str(raw).map_err(|e| format!("Invalid tool JSON: {e}"))?;
                if !params.is_object() {
                    return Err("Tool parameters must be a JSON object".into());
                }
                if args.next().is_some() {
                    return Err("Unexpected argument after tool JSON".into());
                }
                break Mode::Tool {
                    name,
                    params,
                    approved,
                };
            }
            _ => {
                return Err(format!(
                    "Unknown argument: {}. Use --help.",
                    arg.to_string_lossy()
                ))
            }
        }
    };
    // A relative interpreter path belongs to the caller's cwd, before -C.
    let interpreter = PathBuf::from(&python);
    if !interpreter.is_absolute() && interpreter.components().count() > 1 {
        python = env::current_dir()
            .map_err(|e| e.to_string())?
            .join(interpreter)
            .into_os_string();
    }
    Ok(Options {
        python,
        workspace,
        timeout,
        mode,
    })
}

fn utf8(value: Option<OsString>, message: &str) -> Result<String, String> {
    value
        .ok_or_else(|| message.to_string())?
        .into_string()
        .map_err(|_| "Argument must be UTF-8".into())
}

fn run_python(
    python: OsString,
    workspace: PathBuf,
    args: Vec<OsString>,
    state_root: Option<PathBuf>,
) -> Result<i32, String> {
    let workspace = workspace
        .canonicalize()
        .map_err(|e| format!("Workspace: {e}"))?;
    if !workspace.is_dir() {
        return Err("Workspace must be a directory".into());
    }
    let mut command = runtime::command(&python, "cli");
    command
        .args(args)
        // Piped input/output must use the same UTF-8 contract as the tool bridge,
        // including Windows where Python otherwise uses the locale code page.
        .env("PYTHONIOENCODING", "utf-8")
        .current_dir(workspace);
    if let Some(root) = state_root {
        command.env("ARIA_HOME", root);
    }
    // Unix exec preserves the terminal, signals and Python exit status.
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(format!("Python runtime: {}", command.exec()))
    }
    #[cfg(not(unix))]
    {
        command
            .status()
            .map(|s| s.code().unwrap_or(1))
            .map_err(|e| format!("Python runtime: {e}"))
    }
}

fn run(options: Options) -> Result<i32, String> {
    match options.mode {
        Mode::Help => {
            print!("{HELP}");
            Ok(0)
        }
        Mode::Completions(shell) => {
            print!("{}", completions::script(&shell)?);
            Ok(0)
        }
        Mode::Version => {
            println!("aria-native {} (experimental)", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        Mode::Physical(args) => aria_physical::entry(&args),
        Mode::Index(kind) => {
            let mut raw = String::new();
            std::io::stdin()
                .take(MAX_INDEX_REQUEST + 1)
                .read_to_string(&mut raw)
                .map_err(|e| format!("Index request: {e}"))?;
            if raw.len() as u64 > MAX_INDEX_REQUEST {
                return Err("Index request exceeds 256 MiB".into());
            }
            let invalid = |e: serde_json::Error| format!("Invalid index request: {e}");
            let out = match kind {
                IndexKind::Imports => serde_json::to_string(&aria_graph::resolve(
                    &serde_json::from_str(&raw).map_err(invalid)?,
                )),
                IndexKind::Symbols => serde_json::to_string(&aria_graph::symbols::extract(
                    &serde_json::from_str(&raw).map_err(invalid)?,
                )),
                IndexKind::Changes => serde_json::to_string(&aria_graph::semantic::answer(
                    &serde_json::from_str(&raw).map_err(invalid)?,
                )),
            }
            .map_err(|e| e.to_string())?;
            println!("{out}");
            Ok(0)
        }
        Mode::Run(args) => run_python(options.python, options.workspace, args, None),
        Mode::Chat { args, jsonl } => chat::run(
            &options.python,
            &options.workspace,
            &args,
            options.timeout,
            jsonl,
        ),
        Mode::Render(jsonl) => render::replay(&mut std::io::stdin().lock(), jsonl),
        Mode::Exec {
            prompt,
            args,
            jsonl,
        } => stream::exec(
            &options.python,
            &options.workspace,
            &prompt,
            &args,
            options.timeout,
            jsonl,
        ),
        Mode::Config(paths) => {
            println!(
                "{}",
                if paths {
                    state::paths()?
                } else {
                    state::config()?
                }
            );
            Ok(0)
        }
        Mode::Scan => {
            let report = engine::tools::project_scan(&options.workspace)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&report).unwrap_or_default()
            );
            Ok(0)
        }
        Mode::Setup {
            provider,
            model,
            api_key,
        } => {
            let home = env::var_os("HOME")
                .or_else(|| env::var_os("USERPROFILE"))
                .map(PathBuf::from)
                .ok_or("Cannot determine home directory")?;
            let result = run_setup_in_dir(
                &home.join(".aria"),
                provider.as_deref(),
                model.as_deref(),
                api_key.as_deref(),
            )?;
            println!(
                "{}",
                serde_json::to_string_pretty(&result).unwrap_or_default()
            );
            Ok(0)
        }
        Mode::Health { ollama_url } => {
            let report = engine::ollama::probe_ollama(&ollama_url, Duration::from_secs(5));
            let (cloud_ok, cloud_ms, cloud_err) =
                engine::cloud::probe_arthera_backend(Duration::from_secs(3));
            println!(
                "{}",
                json!({
                    "service": "ollama",
                    "url": ollama_url,
                    "ok": report.ok,
                    "latency_ms": report.latency_ms,
                    "models": report.models,
                    "error": report.error,
                    "arthera_backend": {
                        "url": engine::cloud::CloudProvider::arthera_base_url(),
                        "ok": cloud_ok,
                        "latency_ms": cloud_ms,
                        "error": cloud_err,
                    }
                })
            );
            Ok(if report.ok { 0 } else { 1 })
        }
        Mode::McpTools { program, args } => {
            let arg_refs: Vec<&str> = args.iter().map(String::as_str).collect();
            let mut client = engine::mcp::McpStdioClient::spawn(&program, &arg_refs)?;
            let tools = client.list_tools()?;
            println!(
                "{}",
                serde_json::to_string_pretty(&tools).unwrap_or_default()
            );
            Ok(0)
        }
        Mode::Agent {
            provider,
            model,
            ollama_url,
            allow_write,
            prompt,
        } => {
            let log_tool = |tool: &str, args: &serde_json::Value, result: &serde_json::Value| {
                let ok = result["ok"].as_bool().unwrap_or(false);
                if ok {
                    eprintln!("[tool] {tool} {args} -> ok");
                } else {
                    eprintln!(
                        "[tool] {tool} {args} -> err: {}",
                        result["error"].as_str().unwrap_or("")
                    );
                }
            };
            let answer = if let Some(cloud) = engine::cloud::CloudProvider::from_id(&provider) {
                let api_key = cloud.resolve_api_key().ok_or_else(|| {
                    format!(
                        "Missing API key for {} ({})",
                        cloud.display_name(),
                        cloud.api_key_env().join(" or ")
                    )
                })?;
                engine::agent::run_cloud_agent(
                    cloud,
                    &api_key,
                    &model,
                    &options.workspace,
                    &prompt,
                    allow_write,
                    8,
                    Duration::from_secs(120),
                    log_tool,
                )?
            } else {
                engine::agent::run_native_agent(
                    &ollama_url,
                    &model,
                    &options.workspace,
                    &prompt,
                    allow_write,
                    8,
                    Duration::from_secs(120),
                    log_tool,
                )?
            };
            println!("{answer}");
            Ok(0)
        }
        Mode::Ask {
            provider,
            model,
            ollama_url,
            prompt,
        } => {
            use std::io::Write;
            let _conv_only = engine::policy::is_conversation_only_message(&prompt);
            let sys = engine::policy::general_system_prompt("today");
            let mut out = std::io::stdout().lock();
            if let Some(cloud) = engine::cloud::CloudProvider::from_id(&provider) {
                engine::cloud::stream_cloud_chat_cancelable(
                    cloud,
                    &model,
                    &sys,
                    &prompt,
                    Duration::from_secs(120),
                    || false,
                    |delta| {
                        let _ = out.write_all(delta.as_bytes());
                        let _ = out.flush();
                    },
                )?;
            } else {
                engine::ollama::stream_chat(
                    &ollama_url,
                    &model,
                    &sys,
                    &prompt,
                    Duration::from_secs(120),
                    |delta| {
                        let _ = out.write_all(delta.as_bytes());
                        let _ = out.flush();
                    },
                )?;
            }
            let _ = out.write_all(b"\n");
            Ok(0)
        }
        Mode::Sessions {
            action,
            argument,
            limit,
        } => {
            let root = state::root()?.join("sessions");
            let data = if action == "show" {
                sessions::show(&root, argument.as_deref().unwrap(), false)?
            } else {
                sessions::list(&root, limit, argument.as_deref())?
            };
            println!("{data}");
            Ok(0)
        }
        Mode::Resume { id, args } => {
            let root = state::root()?;
            let root = if root.is_absolute() {
                root
            } else {
                env::current_dir().map_err(|e| e.to_string())?.join(root)
            };
            sessions::show(&root.join("sessions"), &id, true)?;
            let mut forwarded = vec![OsString::from("--session"), id.into()];
            forwarded.extend(args);
            run_python(options.python, options.workspace, forwarded, Some(root))
        }
        Mode::Update(check) => {
            let result = updates::check(&state::root()?, &check)?;
            let status = if result["status"] == "unavailable"
                || (result["status"] == "stale" && !result["error"].is_null())
            {
                1
            } else {
                0
            };
            println!("{result}");
            Ok(status)
        }
        Mode::UpdateAuto(check) => {
            println!("Checking for updates (current: v{})...", check.current);
            let result = updates::check(&state::root()?, &check)?;
            let status = result["status"].as_str().unwrap_or("");
            if status == "unavailable" || (status == "stale" && !result["error"].is_null()) {
                let err_msg = result["error"]
                    .as_str()
                    .unwrap_or("Network error or update metadata unavailable");
                eprintln!("Update check failed: {err_msg}");
                return Ok(1);
            }
            let is_available = result["update_available"].as_bool().unwrap_or(false);
            if !is_available {
                println!("✓ Aria Code is already up to date (v{}).", check.current);
                return Ok(0);
            }
            let latest = match result["latest"].as_str() {
                Some(v) => v,
                None => {
                    println!("✓ Aria Code is already up to date (v{}).", check.current);
                    return Ok(0);
                }
            };
            if check.channel == "npm" || check.channel == "pip" {
                if let Some(cmd) = result["update_command"].as_str() {
                    println!("Update available: v{} -> v{}", check.current, latest);
                    println!(
                        "Installed via {}. To update, run:\n  {}",
                        check.channel, cmd
                    );
                    return Ok(0);
                }
            }
            println!(
                "Found new version: v{} (installed: v{}).",
                latest, check.current
            );
            println!("Downloading and updating release...");
            let command = updates::install(latest)?;
            println!(
                "✓ Successfully updated Aria Code to v{} ({})! Restart aria to use it.",
                latest,
                command.display()
            );
            Ok(0)
        }
        Mode::UpdateRollback => {
            let command = updates::rollback()?;
            println!(
                "Restored previous release: {}. Restart Aria to use it.",
                command.display()
            );
            Ok(0)
        }
        Mode::UpdateInstall(version) => {
            let command = updates::install(&version)?;
            println!(
                "Installed {version}: {}. Restart Aria to use it.",
                command.display()
            );
            Ok(0)
        }
        Mode::UpdateAutoInstall(version) => {
            println!("{}", updates::auto_install(&version)?);
            Ok(0)
        }
        Mode::Tool {
            name,
            params,
            approved,
        } => {
            let request = json!({"jsonrpc":"2.0", "id":1, "method":"tools.call", "params":{"name":name, "arguments":params}});
            let response = worker::call(
                &options.python,
                &options.workspace,
                options.timeout,
                approved.then_some(name.as_str()),
                &request,
            )?;
            println!("{response}");
            Ok(
                if response.get("error").is_some()
                    || response.pointer("/result/success") == Some(&Value::Bool(false))
                {
                    1
                } else {
                    0
                },
            )
        }
    }
}

fn main() {
    let mut args: Vec<OsString> = env::args_os().skip(1).collect();
    if args == [OsString::from("--entry-protocol")] {
        println!("1");
        return;
    }
    let explicit = args.first().is_some_and(|s| s == "--app");
    if explicit {
        args.remove(0);
    }
    let formal = explicit
        || env::args_os()
            .next()
            .is_some_and(|name| frontend::formal_executable(&PathBuf::from(name)))
        || env::current_exe()
            .ok()
            .is_some_and(|p| frontend::formal_executable(&p));
    if formal && (args == [OsString::from("--version")] || args == [OsString::from("-V")]) {
        println!("aria-code {}", env!("ARIA_PRODUCT_VERSION"));
        return;
    }
    let options = if formal {
        frontend::options(
            args,
            std::io::stdin().is_terminal() && std::io::stdout().is_terminal(),
        )
    } else {
        parse(args)
    };
    let code = match options.and_then(run) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("aria-native: {error}");
            if error == "Cancelled" {
                130
            } else {
                2
            }
        }
    };
    std::process::exit(code);
}

fn run_setup_in_dir(
    aria_dir: &std::path::Path,
    provider: Option<&str>,
    model: Option<&str>,
    api_key: Option<&str>,
) -> Result<serde_json::Value, String> {
    std::fs::create_dir_all(aria_dir)
        .map_err(|e| format!("Failed to create {}: {e}", aria_dir.display()))?;
    let cfg_path = aria_dir.join("config.json");
    let mut cfg: serde_json::Value = std::fs::read_to_string(&cfg_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| serde_json::json!({}));
    let obj = cfg
        .as_object_mut()
        .ok_or("config.json must be a JSON object")?;
    if let Some(p) = provider.filter(|s| !s.trim().is_empty()) {
        obj.insert("provider".into(), serde_json::json!(p.trim()));
    } else if !obj.contains_key("provider") {
        obj.insert("provider".into(), serde_json::json!("ollama"));
    }
    if let Some(m) = model.filter(|s| !s.trim().is_empty()) {
        obj.insert("model".into(), serde_json::json!(m.trim()));
    } else if !obj.contains_key("model") {
        obj.insert("model".into(), serde_json::json!("qwen2.5:7b"));
    }
    std::fs::write(
        &cfg_path,
        serde_json::to_string_pretty(&obj).unwrap_or_else(|_| "{}".into()),
    )
    .map_err(|e| format!("Failed to write {}: {e}", cfg_path.display()))?;

    let env_path = aria_dir.join(".env");
    if let Some(key) = api_key.filter(|s| !s.trim().is_empty()) {
        let env_name = match obj
            .get("provider")
            .and_then(|v| v.as_str())
            .unwrap_or("ollama")
            .to_ascii_lowercase()
            .as_str()
        {
            "openai" => "OPENAI_API_KEY",
            "anthropic" => "ANTHROPIC_API_KEY",
            "gemini" | "google" => "GEMINI_API_KEY",
            "deepseek" => "DEEPSEEK_API_KEY",
            "openrouter" => "OPENROUTER_API_KEY",
            _ => "ARIA_API_KEY",
        };
        let mut lines: Vec<String> = std::fs::read_to_string(&env_path)
            .unwrap_or_default()
            .lines()
            .filter(|l| !l.starts_with(&format!("{env_name}=")))
            .map(str::to_owned)
            .collect();
        lines.push(format!("{env_name}={}", key.trim()));
        std::fs::write(&env_path, lines.join("\n") + "\n")
            .map_err(|e| format!("Failed to write {}: {e}", env_path.display()))?;
    }
    Ok(serde_json::json!({
        "config": cfg_path.to_string_lossy(),
        "env": env_path.to_string_lossy(),
        "provider": obj.get("provider"),
        "model": obj.get("model"),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn options(args: &[&str]) -> Result<Options, String> {
        parse(args.iter().map(OsString::from).collect())
    }
    #[test]
    fn forwarding_preserves_arguments() {
        match options(&["run", "--", "--model", "google/gemini", "你好; $(false)"])
            .unwrap()
            .mode
        {
            Mode::Run(args) => assert_eq!(
                args,
                vec![
                    OsString::from("--model"),
                    OsString::from("google/gemini"),
                    OsString::from("你好; $(false)")
                ]
            ),
            _ => panic!("wrong mode"),
        }
    }
    #[test]
    fn rejects_invalid_parameters() {
        for args in [
            vec!["tool", "read_file", "[]"],
            vec!["tool", "read_file", "{broken"],
            vec!["--timeout-ms", "0"],
            vec!["--timeout-ms", "300001"],
            vec!["--python"],
            vec!["index"],
            vec!["index", "exports"],
            vec!["index", "imports", "request.json"],
        ] {
            assert!(options(&args).is_err(), "{args:?}");
        }
    }
    #[test]
    fn index_imports_reads_stdin() {
        assert!(matches!(
            options(&["index", "imports"]).unwrap().mode,
            Mode::Index(IndexKind::Imports)
        ));
        assert!(matches!(
            options(&["index", "symbols"]).unwrap().mode,
            Mode::Index(IndexKind::Symbols)
        ));
        assert!(matches!(
            options(&["index", "changes"]).unwrap().mode,
            Mode::Index(IndexKind::Changes)
        ));
    }
    #[test]
    fn approval_is_a_host_option() {
        assert!(matches!(
            options(&["tool", "read_file", "{\"approved\":true}"])
                .unwrap()
                .mode,
            Mode::Tool {
                approved: false,
                ..
            }
        ));
        assert!(matches!(
            options(&["tool", "--approve-write", "write_file", "{}"])
                .unwrap()
                .mode,
            Mode::Tool { approved: true, .. }
        ));
    }

    #[test]
    fn setup_writes_config_and_env_files() {
        let dir = tempfile::tempdir().unwrap();
        let out = run_setup_in_dir(
            dir.path(),
            Some("openai"),
            Some("gpt-4o"),
            Some("sk-test-123"),
        )
        .unwrap();
        assert_eq!(out["provider"], "openai");
        assert_eq!(out["model"], "gpt-4o");
        let cfg = std::fs::read_to_string(dir.path().join("config.json")).unwrap();
        assert!(cfg.contains("\"gpt-4o\""));
        let env_txt = std::fs::read_to_string(dir.path().join(".env")).unwrap();
        assert!(env_txt.contains("OPENAI_API_KEY=sk-test-123"));
    }
}
