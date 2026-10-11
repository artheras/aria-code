mod chat;
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
  aria-native update check --current VERSION [--channel native|npm|pip|source] [--offline|--refresh]
  aria-native index imports|symbols < REQUEST.json
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
Chat is a persistent Rust terminal interface; --jsonl exposes its bidirectional application protocol.
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
}

#[derive(Debug)]
enum Mode {
    Help,
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
            Some("index") => {
                let kind = match args.next().as_ref().and_then(|a| a.to_str()) {
                    Some("imports") => IndexKind::Imports,
                    Some("symbols") => IndexKind::Symbols,
                    _ => return Err("index requires a subcommand: imports or symbols".into()),
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
                if args.next().as_deref() != Some(std::ffi::OsStr::new("check")) {
                    return Err("Usage: update check --current VERSION".into());
                }
                let mut check = updates::Check {
                    current: String::new(),
                    channel: env::var("ARIA_CODE_INSTALL_CHANNEL")
                        .unwrap_or_else(|_| "native".into()),
                    offline: false,
                    refresh: false,
                    timeout: Duration::from_secs(4),
                };
                while let Some(arg) = args.next() {
                    match arg.to_str() {
                        Some("--current") => {
                            check.current =
                                utf8(args.next(), "--current requires the product version")?
                        }
                        Some("--channel") => {
                            check.channel = utf8(args.next(), "--channel requires a channel")?
                        }
                        Some("--offline") => check.offline = true,
                        Some("--refresh") => check.refresh = true,
                        _ => return Err("Unexpected update check argument".into()),
                    }
                }
                if check.current.is_empty() {
                    return Err(
                        "--current is required; use your installed aria-code --version".into(),
                    );
                }
                if check.offline && check.refresh {
                    return Err("--offline and --refresh are mutually exclusive".into());
                }
                break Mode::Update(check);
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
        Mode::Version => {
            println!("aria-native {} (experimental)", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
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
        || env::current_exe()
            .ok()
            .is_some_and(|p| p.file_stem().is_some_and(|s| s == "aria-code-bin"));
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
}
