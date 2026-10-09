mod worker;

use serde_json::{json, Value};
use std::{env, ffi::OsString, path::PathBuf, process::Command, time::Duration};

const HELP: &str = "aria-native — experimental Rust entry point (Python runtime required for tools/chat)

Usage:
  aria-native [--python EXE] [-C DIR] run [--] [ARIA ARGUMENTS...]
  aria-native [--python EXE] [-C DIR] [--timeout-ms N] tool [--approve-write] NAME JSON
  aria-native --version

Examples:
  aria-native run -- --health
  aria-native -C ./project tool read_file '{\"path\":\"README.md\"}'
  aria-native -C ./project tool --approve-write edit_file '{\"path\":\"app.py\",\"old_string\":\"old\",\"new_string\":\"new\"}'

Tools: read_file, list_files, search_code, write_file, edit_file.
Tool requests are confined to DIR (default: current directory); writes require
explicit per-invocation --approve-write. Persistent tool denials still apply.
Timeout: 30000 ms by default, tool mode only. Tool stdout contains JSON only.
Use --python or ARIA_PYTHON to select the installed Python Aria environment.
This prototype does not replace the stable aria/aria-code commands or their TUI.
";

#[derive(Debug)]
struct Options {
    python: OsString,
    workspace: PathBuf,
    timeout: Duration,
    mode: Mode,
}

#[derive(Debug)]
enum Mode {
    Help,
    Version,
    Run(Vec<OsString>),
    Tool {
        name: String,
        params: Value,
        approved: bool,
    },
}

fn parse(args: Vec<OsString>) -> Result<Options, String> {
    let mut python = env::var_os("ARIA_PYTHON")
        .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into());
    let mut workspace = env::current_dir().map_err(|e| e.to_string())?;
    let mut timeout = Duration::from_secs(30);
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
            Some("run") => {
                let mut rest: Vec<_> = args.collect();
                if rest.first().is_some_and(|arg| arg == "--") {
                    rest.remove(0);
                }
                break Mode::Run(rest);
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
        Mode::Run(args) => {
            let workspace = options
                .workspace
                .canonicalize()
                .map_err(|e| format!("Workspace: {e}"))?;
            if !workspace.is_dir() {
                return Err("Workspace must be a directory".into());
            }
            let mut command = Command::new(options.python);
            command
                .args(["-c", "from aria_code.apps.cli.main import main; main()"])
                .args(args)
                .current_dir(workspace);
            // Same process and inherited terminal on Unix: Ctrl-C, stdin and the
            // Python exit status retain the stable CLI's existing semantics.
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
    let code = match parse(env::args_os().skip(1).collect()).and_then(run) {
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
        ] {
            assert!(options(&args).is_err(), "{args:?}");
        }
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
