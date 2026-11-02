//! One command contract for interpreters and the bundled frozen application.
use std::{env, ffi::OsStr, path::Path, process::Command};

pub fn default_python() -> std::ffi::OsString {
    if let Some(value) = env::var_os("ARIA_RUNTIME") {
        return value;
    }
    if let Some(value) = env::var_os("ARIA_PYTHON") {
        return value;
    }
    // macOS may return the command symlink, unlike Linux's resolved /proc path.
    if let Ok(exe) = env::current_exe().and_then(|exe| exe.canonicalize()) {
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

/// The engine for `mode`. A native release ships the Go runtime
/// (`aria-runtime`) next to the frozen Python worker; it serves the
/// application, stream and tool-bridge modes. The `cli` mode (product
/// commands such as `--help`) stays on the Python worker, and
/// `ARIA_ENGINE=python` keeps the Python engine for everything.
pub fn engine(python: &OsStr, mode: &str, prefer_python: bool) -> std::ffi::OsString {
    if prefer_python || mode == "cli" || is_native(python) {
        return python.to_os_string();
    }
    let worker = Path::new(python);
    if worker.file_stem().is_some_and(|s| s == "aria-code-worker") {
        if let Some(dir) = worker.parent() {
            let native = dir.join(if cfg!(windows) {
                "aria-runtime.exe"
            } else {
                "aria-runtime"
            });
            if native.is_file() {
                return native.into_os_string();
            }
        }
    }
    python.to_os_string()
}

pub fn command(python: &OsStr, mode: &str) -> Command {
    let prefer_python = env::var("ARIA_ENGINE").is_ok_and(|v| v.eq_ignore_ascii_case("python"));
    let chosen = engine(python, mode, prefer_python);
    let python = chosen.as_os_str();
    let mut cmd = Command::new(python);
    if is_native(python) {
        cmd.arg(mode);
        return cmd;
    }
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

pub fn is_native(executable: &OsStr) -> bool {
    Path::new(executable)
        .file_stem()
        .is_some_and(|s| s == "aria-runtime")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_worker_uses_modes_not_interpreter_flags() {
        let native = command(OsStr::new("/somewhere/aria-runtime.exe"), "app");
        assert_eq!(native.get_args().collect::<Vec<_>>(), ["app"]);
        let cmd = command(OsStr::new("/somewhere/aria-code-worker.exe"), "app");
        assert_eq!(cmd.get_args().collect::<Vec<_>>(), ["--aria-worker", "app"]);
        let cmd = command(OsStr::new("python3"), "app");
        assert_eq!(cmd.get_args().next(), Some(OsStr::new("-u")));
    }
}

#[cfg(test)]
mod engine_tests {
    use super::engine;
    use std::ffi::OsStr;

    #[test]
    fn a_native_release_runs_the_go_engine_except_for_cli() {
        let dir = tempfile::tempdir().unwrap();
        let ext = if cfg!(windows) { ".exe" } else { "" };
        let worker = dir.path().join(format!("aria-code-worker{ext}"));
        let native = dir.path().join(format!("aria-runtime{ext}"));
        std::fs::write(&worker, b"").unwrap();
        let w = worker.as_os_str();
        // Without the Go runtime beside it, the worker stays.
        assert_eq!(engine(w, "app", false), w);
        std::fs::write(&native, b"").unwrap();
        for mode in ["app", "stream", "bridge"] {
            assert_eq!(engine(w, mode, false), native.as_os_str(), "{mode}");
        }
        assert_eq!(engine(w, "cli", false), w);
        assert_eq!(engine(w, "app", true), w, "ARIA_ENGINE=python");
        // A plain interpreter (pip installs) is never swapped.
        assert_eq!(
            engine(OsStr::new("python3"), "app", false),
            OsStr::new("python3")
        );
    }
}
