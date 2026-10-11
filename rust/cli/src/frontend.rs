//! Formal aria / aria-code argument routing. Legacy/headless commands retain
//! their output contracts; supported interactive invocations use the Rust UI.
use crate::{runtime, Mode, Options};
use std::{env, ffi::OsString, path::PathBuf, time::Duration};

pub fn options(mut args: Vec<OsString>, terminal: bool) -> Result<Options, String> {
    let original = args.clone();
    let python = runtime::default_python();
    let invocation = env::current_dir().map_err(|e| e.to_string())?;
    let mut workspace = invocation.clone();
    let mut forwarded = Vec::new();
    if args.first().is_some_and(|s| s == "code") {
        args.remove(0);
    }
    let mut iter = args.into_iter();
    let mut interactive = terminal && env::var("ARIA_FRONTEND").as_deref() != Ok("python");
    while let Some(arg) = iter.next() {
        let Some(flag) = arg.to_str() else {
            interactive = false;
            break;
        };
        let (name, inline) = flag
            .split_once('=')
            .map_or((flag, None), |(k, v)| (k, Some(v)));
        match name {
            "--thinking" | "--local" | "--no-banner" | "--dangerously-skip-permissions"
                if inline.is_none() =>
            {
                forwarded.push(arg)
            }
            "--resume" if inline.is_none() => forwarded.push("--resume-last".into()),
            "--model" | "--url" | "--session" | "--banner" | "--allow-tools" | "--add-dir"
            | "--read-dir" | "-C" | "--cd" => {
                let value = if let Some(value) = inline {
                    OsString::from(value)
                } else {
                    iter.next()
                        .ok_or_else(|| format!("{name} requires a value"))?
                };
                let value = if matches!(name, "--add-dir" | "--read-dir" | "-C" | "--cd") {
                    // Resolve before chdir, exactly like the Python entrypoint.
                    let path = PathBuf::from(&value);
                    if path.starts_with("~") {
                        interactive = false;
                        break;
                    }
                    if path.is_absolute() {
                        path
                    } else {
                        invocation.join(path)
                    }
                    .into_os_string()
                } else {
                    value
                };
                match name {
                    "-C" | "--cd" => workspace = value.into(),
                    "--session" => forwarded.extend(["--resume".into(), value]),
                    _ => forwarded.extend([name.into(), value]),
                }
            }
            _ => {
                interactive = false;
                break;
            }
        }
    }
    Ok(Options {
        python,
        workspace: if interactive { workspace } else { invocation },
        timeout: Duration::from_secs(300),
        mode: if interactive {
            Mode::Chat {
                args: forwarded,
                jsonl: false,
            }
        } else {
            Mode::Run(original)
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(values: &[&str], terminal: bool) -> Options {
        options(values.iter().map(OsString::from).collect(), terminal).unwrap()
    }
    #[test]
    fn aliases_and_preferences_open_native_chat() {
        for input in [
            vec![],
            vec!["code"],
            vec!["--thinking", "--model", "google/gemini-3.5-flash"],
            vec![
                "--model=google/gemini-3.5-flash",
                "--resume",
                "--banner",
                "off",
            ],
        ] {
            assert!(matches!(args(&input, true).mode, Mode::Chat { .. }));
        }
    }
    #[test]
    fn headless_commands_and_pipes_keep_the_python_contract() {
        for input in [
            vec!["-p", "hello"],
            vec!["--help"],
            vec!["update"],
            vec!["health"],
            vec!["quote", "AAPL"],
            vec!["review"],
            vec!["--unknown"],
        ] {
            assert!(matches!(args(&input, true).mode, Mode::Run(_)));
        }
        assert!(matches!(args(&[], false).mode, Mode::Run(_)));
    }
    #[test]
    fn project_roots_resolve_relative_to_invocation() {
        let out = args(
            &[
                "-C",
                "project",
                "--read-dir",
                "reference",
                "--session",
                "abcd",
            ],
            true,
        );
        assert_eq!(out.workspace, env::current_dir().unwrap().join("project"));
        let Mode::Chat { args, .. } = out.mode else {
            panic!()
        };
        assert_eq!(args[1], env::current_dir().unwrap().join("reference"));
        assert_eq!(args[2], "--resume");
    }
}
