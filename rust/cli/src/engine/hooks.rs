//! Lifecycle hooks (`PreToolUse`, `PostToolUse`, `Stop`) for the native Rust harness.
//!
//! Hooks are loaded from `<workspace>/.aria/hooks.json` (and optionally `~/.aria/hooks.json`).
//! Each hook receives a JSON event payload on stdin. For `PreToolUse`, a non-zero exit code
//! blocks the tool call and returns the hook's stderr/stdout as the denial reason.

use serde_json::{json, Value};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HookEvent {
    PreToolUse,
    PostToolUse,
    Stop,
}

impl HookEvent {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::PreToolUse => "PreToolUse",
            Self::PostToolUse => "PostToolUse",
            Self::Stop => "Stop",
        }
    }

    fn from_key(key: &str) -> Option<Self> {
        match key {
            "PreToolUse" | "pre_tool_use" => Some(Self::PreToolUse),
            "PostToolUse" | "post_tool_use" => Some(Self::PostToolUse),
            "Stop" | "stop" => Some(Self::Stop),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookRule {
    pub event: HookEvent,
    /// Tool matcher: `"*"` or empty matches all tools; otherwise exact name or prefix ending with `*`.
    pub matcher: String,
    pub command: String,
}

impl HookRule {
    pub fn matches(&self, tool_name: &str) -> bool {
        let m = self.matcher.trim();
        if m.is_empty() || m == "*" {
            return true;
        }
        if let Some(prefix) = m.strip_suffix('*') {
            return tool_name.starts_with(prefix);
        }
        m == tool_name
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookOutcome {
    pub allowed: bool,
    pub reason: Option<String>,
}

impl HookOutcome {
    pub fn allow() -> Self {
        Self {
            allowed: true,
            reason: None,
        }
    }

    pub fn deny(reason: impl Into<String>) -> Self {
        Self {
            allowed: false,
            reason: Some(reason.into()),
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct HookRegistry {
    rules: Vec<HookRule>,
}

impl HookRegistry {
    pub fn load(workspace: &Path) -> Self {
        let mut rules = Vec::new();
        for path in candidate_hook_files(workspace) {
            if let Ok(raw) = fs::read_to_string(&path) {
                if let Ok(val) = serde_json::from_str::<Value>(&raw) {
                    parse_hooks_json(&val, &mut rules);
                }
            }
        }
        Self { rules }
    }

    #[allow(dead_code)]
    pub fn from_rules(rules: Vec<HookRule>) -> Self {
        Self { rules }
    }

    #[allow(dead_code)]
    pub fn is_empty(&self) -> bool {
        self.rules.is_empty()
    }

    pub fn rules(&self) -> &[HookRule] {
        &self.rules
    }

    /// Run all matching `PreToolUse` hooks. Returns `deny` on the first non-zero exit status.
    pub fn run_pre_tool(&self, workspace: &Path, tool_name: &str, args: &Value) -> HookOutcome {
        let payload = json!({
            "event": HookEvent::PreToolUse.as_str(),
            "tool": tool_name,
            "arguments": args,
            "workspace": workspace.to_string_lossy(),
        });
        for rule in &self.rules {
            if rule.event != HookEvent::PreToolUse || !rule.matches(tool_name) {
                continue;
            }
            if let Err(reason) = run_shell_hook(workspace, &rule.command, &payload) {
                return HookOutcome::deny(reason);
            }
        }
        HookOutcome::allow()
    }

    /// Run all matching `PostToolUse` hooks (advisory; errors are returned as notes).
    pub fn run_post_tool(
        &self,
        workspace: &Path,
        tool_name: &str,
        args: &Value,
        output: &Value,
    ) -> Vec<String> {
        let payload = json!({
            "event": HookEvent::PostToolUse.as_str(),
            "tool": tool_name,
            "arguments": args,
            "output": output,
            "workspace": workspace.to_string_lossy(),
        });
        let mut notes = Vec::new();
        for rule in &self.rules {
            if rule.event != HookEvent::PostToolUse || !rule.matches(tool_name) {
                continue;
            }
            if let Err(err) = run_shell_hook(workspace, &rule.command, &payload) {
                notes.push(err);
            }
        }
        notes
    }

    /// Run all `Stop` hooks at turn completion.
    pub fn run_stop(&self, workspace: &Path, summary: &str) -> Vec<String> {
        let payload = json!({
            "event": HookEvent::Stop.as_str(),
            "summary": summary,
            "workspace": workspace.to_string_lossy(),
        });
        let mut notes = Vec::new();
        for rule in &self.rules {
            if rule.event != HookEvent::Stop {
                continue;
            }
            if let Err(err) = run_shell_hook(workspace, &rule.command, &payload) {
                notes.push(err);
            }
        }
        notes
    }
}

fn candidate_hook_files(workspace: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    if let Ok(home) = std::env::var("HOME") {
        if !home.trim().is_empty() {
            files.push(PathBuf::from(home).join(".aria").join("hooks.json"));
        }
    }
    files.push(workspace.join(".aria").join("hooks.json"));
    files
}

fn parse_hooks_json(val: &Value, out: &mut Vec<HookRule>) {
    let root = val.get("hooks").unwrap_or(val);
    let Some(map) = root.as_object() else {
        return;
    };
    for (key, entries) in map {
        let Some(event) = HookEvent::from_key(key) else {
            continue;
        };
        let Some(arr) = entries.as_array() else {
            continue;
        };
        for item in arr {
            let matcher = item
                .get("matcher")
                .and_then(Value::as_str)
                .unwrap_or("*")
                .to_owned();
            if let Some(cmd) = item.get("command").and_then(Value::as_str) {
                if !cmd.trim().is_empty() {
                    out.push(HookRule {
                        event,
                        matcher,
                        command: cmd.to_owned(),
                    });
                }
            }
        }
    }
}

fn run_shell_hook(workspace: &Path, command: &str, payload: &Value) -> Result<(), String> {
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

    let mut child = cmd
        .current_dir(workspace)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("hook spawn failed: {e}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(payload.to_string().as_bytes());
    }
    let output = child
        .wait_with_output()
        .map_err(|e| format!("hook wait failed: {e}"))?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    let msg = if !stderr.is_empty() {
        stderr
    } else if !stdout.is_empty() {
        stdout
    } else {
        format!("hook exited with status {}", output.status)
    };
    Err(msg)
}

/// Build a stack-aware `.aria/hooks.json` starter template with deterministic `PostToolUse` checks.
#[allow(dead_code)]
pub fn default_hooks_template(workspace: &Path) -> Value {
    let mut post_rules = Vec::new();
    let cargo_toml = if workspace.join("Cargo.toml").is_file() {
        Some("Cargo.toml")
    } else if workspace
        .join("rust")
        .join("cli")
        .join("Cargo.toml")
        .is_file()
    {
        Some("rust/cli/Cargo.toml")
    } else {
        None
    };
    if let Some(manifest) = cargo_toml {
        let cmd = format!("cargo fmt --manifest-path {manifest} --check");
        for tool in ["write_file", "edit_file", "multi_edit"] {
            post_rules.push(json!({ "matcher": tool, "command": cmd }));
        }
    } else if workspace.join("pyproject.toml").is_file() {
        let cmd = "python3 -m compileall -q src tests 2>/dev/null || true";
        for tool in ["write_file", "edit_file", "multi_edit"] {
            post_rules.push(json!({ "matcher": tool, "command": cmd }));
        }
    } else if workspace.join("package.json").is_file() {
        let cmd = "npm run --if-present lint";
        for tool in ["write_file", "edit_file", "multi_edit"] {
            post_rules.push(json!({ "matcher": tool, "command": cmd }));
        }
    }
    json!({
        "hooks": {
            "PreToolUse": [],
            "PostToolUse": post_rules,
            "Stop": []
        }
    })
}

/// Write `.aria/hooks.json` with a starter template if it does not already exist.
/// Returns `(path, created)`.
#[allow(dead_code)]
pub fn write_default_hooks_if_missing(workspace: &Path) -> Result<(PathBuf, bool), String> {
    let dir = workspace.join(".aria");
    let path = dir.join("hooks.json");
    if path.exists() {
        return Ok((path, false));
    }
    fs::create_dir_all(&dir).map_err(|e| format!("Failed to create .aria dir: {e}"))?;
    let template = default_hooks_template(workspace);
    let pretty = serde_json::to_string_pretty(&template).map_err(|e| e.to_string())?;
    fs::write(&path, format!("{pretty}\n"))
        .map_err(|e| format!("Failed to write hooks.json: {e}"))?;
    Ok((path, true))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn loads_hooks_from_workspace_and_blocks_on_nonzero_exit() {
        let tmp = tempfile::tempdir().unwrap();
        let aria_dir = tmp.path().join(".aria");
        fs::create_dir_all(&aria_dir).unwrap();
        #[cfg(windows)]
        let block_cmd = "echo blocked by policy 1>&2 & exit /b 2";
        #[cfg(not(windows))]
        let block_cmd = "echo 'blocked by policy' >&2; exit 2";
        let config = json!({
            "hooks": {
                "PreToolUse": [
                    { "matcher": "run_command", "command": block_cmd }
                ]
            }
        });
        fs::write(aria_dir.join("hooks.json"), config.to_string()).unwrap();

        let reg = HookRegistry::load(tmp.path());
        assert!(!reg.is_empty());

        let ok = reg.run_pre_tool(tmp.path(), "read_file", &json!({"path": "a.rs"}));
        assert!(ok.allowed);

        let blocked = reg.run_pre_tool(tmp.path(), "run_command", &json!({"command": "rm -rf /"}));
        assert!(!blocked.allowed);
        assert!(blocked
            .reason
            .as_deref()
            .unwrap_or("")
            .contains("blocked by policy"));
    }

    #[test]
    fn prefix_matcher_matches_mcp_tools() {
        let rule = HookRule {
            event: HookEvent::PreToolUse,
            matcher: "mcp__arthera*".into(),
            command: "true".into(),
        };
        assert!(rule.matches("mcp__arthera-finance__financial_quote"));
        assert!(!rule.matches("read_file"));
    }

    #[test]
    fn generates_stack_aware_default_hooks_without_overwriting() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("Cargo.toml"), "[package]\nname=\"demo\"\n").unwrap();

        let (path, created) = write_default_hooks_if_missing(tmp.path()).unwrap();
        assert!(created);
        assert!(path.is_file());

        let reg = HookRegistry::load(tmp.path());
        assert_eq!(reg.rules().len(), 3);
        assert!(reg
            .rules()
            .iter()
            .all(|r| r.event == HookEvent::PostToolUse && r.command.contains("cargo fmt")));

        // Second call preserves existing hooks.json.
        fs::write(
            &path,
            "{\"hooks\":{\"Stop\":[{\"matcher\":\"*\",\"command\":\"true\"}]}}\n",
        )
        .unwrap();
        let (_, created_again) = write_default_hooks_if_missing(tmp.path()).unwrap();
        assert!(!created_again);
        let reloaded = HookRegistry::load(tmp.path());
        assert_eq!(reloaded.rules().len(), 1);
        assert_eq!(reloaded.rules()[0].event, HookEvent::Stop);
    }
}
