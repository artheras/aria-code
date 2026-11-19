//! OS-level sandboxing helpers (macOS Seatbelt `sandbox-exec`, Linux `bwrap`)
//! for native shell command execution.

use std::{
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SandboxMode {
    Off,
    ReadOnly,
    WorkspaceWrite,
}

impl SandboxMode {
    pub fn from_env() -> Self {
        match std::env::var("ARIA_SANDBOX_MODE")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "read-only" | "readonly" | "ro" => Self::ReadOnly,
            "workspace-write" | "workspace" | "rw" => Self::WorkspaceWrite,
            _ => Self::Off,
        }
    }
}

/// Generate a macOS Seatbelt (SBPL) profile that restricts writes to `workspace` (and `/tmp`).
#[allow(dead_code)]
pub fn build_macos_seatbelt_profile(workspace: &Path, mode: SandboxMode) -> String {
    let ws = workspace
        .canonicalize()
        .unwrap_or_else(|_| workspace.to_path_buf());
    let ws_str = ws.to_string_lossy().replace('"', "\\\"");
    match mode {
        SandboxMode::Off => "(version 1)\n(allow default)\n".to_owned(),
        SandboxMode::ReadOnly => "(version 1)\n\
(allow default)\n\
(deny file-write*)\n\
(allow file-write* (subpath \"/dev/null\"))\n"
            .to_owned(),
        SandboxMode::WorkspaceWrite => format!(
            "(version 1)\n\
(allow default)\n\
(deny file-write*)\n\
(allow file-write*\n\
  (subpath \"{ws_str}\")\n\
  (subpath \"/tmp\")\n\
  (subpath \"/private/tmp\")\n\
  (subpath \"/dev/null\"))\n"
        ),
    }
}

/// Build a shell `Command` wrapped in an OS sandbox when `mode != SandboxMode::Off`
/// and the platform sandbox binary is available.
pub fn build_sandboxed_shell_command(
    workspace: &Path,
    command: &str,
    mode: SandboxMode,
) -> Command {
    if mode != SandboxMode::Off {
        #[cfg(target_os = "macos")]
        {
            if Path::new("/usr/bin/sandbox-exec").is_file() {
                let profile = build_macos_seatbelt_profile(workspace, mode);
                let mut cmd = Command::new("/usr/bin/sandbox-exec");
                cmd.args(["-p", &profile, "/bin/sh", "-c", command]);
                cmd.current_dir(workspace);
                return cmd;
            }
        }
        #[cfg(target_os = "linux")]
        {
            if which_exists("bwrap") {
                let ws = workspace
                    .canonicalize()
                    .unwrap_or_else(|_| workspace.to_path_buf());
                let ws_str = ws.to_string_lossy().into_owned();
                let mut cmd = Command::new("bwrap");
                cmd.args(["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]);
                if mode == SandboxMode::WorkspaceWrite {
                    cmd.args(["--bind", &ws_str, &ws_str]);
                }
                cmd.args(["--chdir", &ws_str, "/bin/sh", "-c", command]);
                return cmd;
            }
        }
    }

    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("cmd");
        c.args(["/C", command]);
        c
    };
    #[cfg(not(windows))]
    let mut cmd = {
        let mut c = Command::new("sh");
        c.args(["-c", command]);
        c
    };
    cmd.current_dir(workspace);
    cmd
}

#[cfg(target_os = "linux")]
fn which_exists(bin: &str) -> bool {
    std::env::var_os("PATH")
        .map(|paths| std::env::split_paths(&paths).any(|dir| dir.join(bin).is_file()))
        .unwrap_or(false)
}

/// Verify that `candidate` resolves inside `workspace` (prevents `..` traversal escapes).
#[allow(dead_code)]
pub fn ensure_within_workspace(workspace: &Path, candidate: &Path) -> Result<PathBuf, String> {
    let root = workspace
        .canonicalize()
        .unwrap_or_else(|_| workspace.to_path_buf());
    let joined = if candidate.is_absolute() {
        candidate.to_path_buf()
    } else {
        root.join(candidate)
    };
    let resolved = if let Ok(c) = joined.canonicalize() {
        c
    } else {
        // Canonicalize the nearest existing ancestor so symlinks like `/var` -> `/private/var`
        // match `root` even when the target file does not exist yet.
        let mut suffix = Vec::new();
        let mut cursor = joined.as_path();
        while !cursor.exists() {
            if let Some(name) = cursor.file_name() {
                suffix.push(name.to_os_string());
            } else {
                break;
            }
            match cursor.parent() {
                Some(p) => cursor = p,
                None => break,
            }
        }
        let mut base = cursor
            .canonicalize()
            .unwrap_or_else(|_| cursor.to_path_buf());
        for part in suffix.into_iter().rev() {
            base.push(part);
        }
        base
    };
    if resolved.starts_with(&root) {
        Ok(resolved)
    } else {
        Err(format!(
            "Path escapes workspace root: {}",
            candidate.display()
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generates_workspace_write_seatbelt_profile() {
        let tmp = tempfile::tempdir().unwrap();
        let profile = build_macos_seatbelt_profile(tmp.path(), SandboxMode::WorkspaceWrite);
        assert!(profile.contains("(deny file-write*)"));
        assert!(profile.contains("(allow file-write*"));
        assert!(profile.contains("/tmp"));
    }

    #[test]
    fn blocks_workspace_escape_paths() {
        let tmp = tempfile::tempdir().unwrap();
        let inside = tmp.path().join("src").join("main.rs");
        assert!(ensure_within_workspace(tmp.path(), &inside).is_ok());

        let outside = PathBuf::from("/etc/passwd");
        assert!(ensure_within_workspace(tmp.path(), &outside).is_err());
    }
}
