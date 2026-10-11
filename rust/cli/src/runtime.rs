//! One command contract for interpreters and the bundled frozen application.
use std::{env, ffi::OsStr, path::Path, process::Command};

pub fn default_python() -> std::ffi::OsString {
    if let Some(value) = env::var_os("ARIA_PYTHON") {
        return value;
    }
    if let Ok(exe) = env::current_exe() {
        if let Some(parent) = exe.parent() {
            // Both the frontend and its _internal indexer discover the worker.
            for root in [Some(parent), parent.parent()].into_iter().flatten() {
                let worker = root.join(if cfg!(windows) {
                    "aria-code-worker.exe"
                } else {
                    "aria-code-worker"
                });
                if worker.is_file() {
                    return worker.into_os_string();
                }
            }
        }
    }
    if cfg!(windows) { "python" } else { "python3" }.into()
}

pub fn command(python: &OsStr, mode: &str) -> Command {
    let mut cmd = Command::new(python);
    let frozen = Path::new(python)
        .file_stem()
        .is_some_and(|s| s == "aria-code-worker");
    if frozen {
        cmd.args(["--aria-worker", mode]);
    } else {
        let code = match mode {
            "app" => "import sys; sys.stdin.buffer.read(1) == b'\\n' or sys.exit(2); from aria_code.apps.cli.app_server import main; main()",
            "stream" => "import sys; sys.stdin.buffer.read(1) == b'\\n' or sys.exit(2); from aria_code.apps.cli.main import main; main()",
            "bridge" => "from aria_code.apps.cli.native_bridge import main; main()",
            _ => "from aria_code.apps.cli.main import main; main()",
        };
        cmd.args(["-u", "-c", code]);
    }
    cmd.env("ARIA_FRONTEND", "python")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1");
    cmd
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_worker_uses_modes_not_interpreter_flags() {
        let cmd = command(OsStr::new("/somewhere/aria-code-worker.exe"), "app");
        assert_eq!(cmd.get_args().collect::<Vec<_>>(), ["--aria-worker", "app"]);
        let cmd = command(OsStr::new("python3"), "app");
        assert_eq!(cmd.get_args().next(), Some(OsStr::new("-u")));
    }
}
