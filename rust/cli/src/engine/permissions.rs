//! Declarative tool permission policy loaded from `.aria/settings.json`,
//! `.aria/settings.local.json`, and `~/.aria-code/settings.json`.
//!
//! Mirrors Claude Code / Codex declarative allow/deny rules:
//! - `"Bash(cargo test*)"` / `"run_command(git status)"`
//! - `"Edit(src/*)"` / `"Write(*)"` / `"Read(*)"`
//! - `"mcp__erp-service__*"` (wildcard tool name matching)

use serde_json::{json, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
#[allow(dead_code)]
pub enum PermissionMode {
    ReadOnly,
    #[default]
    WorkspaceWrite,
    FullAccess,
}

impl PermissionMode {
    #[allow(dead_code)]
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "read-only" | "readonly" | "plan" => Some(Self::ReadOnly),
            "workspace-write" | "default" | "balanced" => Some(Self::WorkspaceWrite),
            "full-access" | "danger-full-access" | "full" | "bypass" => Some(Self::FullAccess),
            _ => None,
        }
    }

    #[allow(dead_code)]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::WorkspaceWrite => "workspace-write",
            Self::FullAccess => "full-access",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(dead_code)]
pub enum PermissionDecision {
    Allow,
    Ask(String),
    Deny(String),
}

#[derive(Debug, Clone, Default)]
#[allow(dead_code)]
pub struct PermissionPolicy {
    pub mode: PermissionMode,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
    pub ask: Vec<String>,
}

impl PermissionPolicy {
    /// Load and merge settings from user (`~/.aria-code/settings.json`) and
    /// workspace (`.aria/settings.json`, `.aria/settings.local.json`).
    #[allow(dead_code)]
    pub fn load(workspace: &Path) -> Self {
        let mut policy = Self::default();
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            let user_cfg = PathBuf::from(home).join(".aria-code").join("settings.json");
            policy.merge_file(&user_cfg);
        }
        policy.merge_file(&workspace.join(".aria").join("settings.json"));
        policy.merge_file(&workspace.join(".aria").join("settings.local.json"));
        policy
    }

    #[allow(dead_code)]
    pub fn merge_file(&mut self, path: &Path) {
        let Ok(raw) = fs::read_to_string(path) else {
            return;
        };
        let Ok(val) = serde_json::from_str::<Value>(&raw) else {
            return;
        };
        self.merge_value(&val);
    }

    #[allow(dead_code)]
    pub fn merge_value(&mut self, val: &Value) {
        let perms = val.get("permissions").unwrap_or(val);
        if let Some(mode) = perms
            .get("defaultMode")
            .or_else(|| perms.get("mode"))
            .and_then(Value::as_str)
            .and_then(PermissionMode::parse)
        {
            self.mode = mode;
        }
        if let Some(arr) = perms.get("allow").and_then(Value::as_array) {
            for item in arr.iter().filter_map(Value::as_str) {
                let s = item.trim();
                if !s.is_empty() && !self.allow.iter().any(|x| x == s) {
                    self.allow.push(s.to_owned());
                }
            }
        }
        if let Some(arr) = perms.get("deny").and_then(Value::as_array) {
            for item in arr.iter().filter_map(Value::as_str) {
                let s = item.trim();
                if !s.is_empty() && !self.deny.iter().any(|x| x == s) {
                    self.deny.push(s.to_owned());
                }
            }
        }
        if let Some(arr) = perms
            .get("ask")
            .or_else(|| perms.get("requiresApproval"))
            .and_then(Value::as_array)
        {
            for item in arr.iter().filter_map(Value::as_str) {
                let s = item.trim();
                if !s.is_empty() && !self.ask.iter().any(|x| x == s) {
                    self.ask.push(s.to_owned());
                }
            }
        }
    }

    /// Evaluate whether `tool_name` with `args` is allowed, denied, or requires confirmation.
    /// Deny rules take precedence, followed by explicit per-tool `ask` rules, then `allow`.
    #[allow(dead_code)]
    pub fn evaluate(
        &self,
        tool_name: &str,
        args: &Value,
        is_read_only: bool,
    ) -> PermissionDecision {
        let specifier = extract_specifier(tool_name, args);
        for rule in &self.deny {
            if rule_matches(rule, tool_name, specifier) {
                return PermissionDecision::Deny(format!("Denied by permission rule `{rule}`"));
            }
        }
        for rule in &self.ask {
            if rule_matches(rule, tool_name, specifier) {
                return PermissionDecision::Ask(format!(
                    "Approval required by per-tool rule `{rule}`"
                ));
            }
        }
        for rule in &self.allow {
            if rule_matches(rule, tool_name, specifier) {
                return PermissionDecision::Allow;
            }
        }
        if is_read_only {
            return PermissionDecision::Allow;
        }
        match self.mode {
            PermissionMode::ReadOnly => PermissionDecision::Deny(format!(
                "`{tool_name}` is blocked while in read-only permission mode"
            )),
            PermissionMode::FullAccess => PermissionDecision::Allow,
            PermissionMode::WorkspaceWrite => {
                PermissionDecision::Ask(format!("Confirm `{tool_name}`"))
            }
        }
    }
}

/// Write a starter `.aria/settings.json` template if none exists yet.
#[allow(dead_code)]
pub fn write_default_settings_if_missing(workspace: &Path) -> Result<PathBuf, String> {
    let dir = workspace.join(".aria");
    fs::create_dir_all(&dir).map_err(|e| format!("Failed to create .aria dir: {e}"))?;
    let path = dir.join("settings.json");
    if path.exists() {
        return Ok(path);
    }
    let template = json!({
        "permissions": {
            "defaultMode": "workspace-write",
            "allow": [
                "Bash(git status*)",
                "Bash(git diff*)",
                "Bash(cargo test*)",
                "Bash(pytest*)"
            ],
            "deny": [
                "Bash(rm -rf /*)",
                "Bash(git push --force*)"
            ]
        }
    });
    let pretty = serde_json::to_string_pretty(&template).map_err(|e| e.to_string())?;
    fs::write(&path, format!("{pretty}\n"))
        .map_err(|e| format!("Failed to write {}: {e}", path.display()))?;
    Ok(path)
}

fn extract_specifier<'a>(tool_name: &str, args: &'a Value) -> &'a str {
    match tool_name {
        "run_command" | "Bash" => args.get("command").and_then(Value::as_str).unwrap_or(""),
        "read_file" | "write_file" | "edit_file" | "multi_edit" | "Read" | "Write" | "Edit" => {
            args.get("path").and_then(Value::as_str).unwrap_or("")
        }
        "web_fetch" => args.get("url").and_then(Value::as_str).unwrap_or(""),
        _ => "",
    }
}

fn tool_matches_pattern(pattern: &str, tool_name: &str) -> bool {
    if wildcard_match(pattern, tool_name) {
        return true;
    }
    // Support Claude Code canonical aliases alongside native Rust tool names.
    matches!(
        (pattern, tool_name),
        ("Bash", "run_command")
            | ("Read", "read_file")
            | ("Write", "write_file")
            | ("Edit", "edit_file" | "multi_edit")
            | ("WebFetch", "web_fetch")
            | ("WebSearch", "web_search")
    )
}

fn rule_matches(rule: &str, tool_name: &str, specifier: &str) -> bool {
    let trimmed = rule.trim();
    if trimmed.is_empty() {
        return false;
    }
    if let Some(open) = trimmed.find('(') {
        if let Some(rest) = trimmed[open + 1..].strip_suffix(')') {
            let tool_pat = trimmed[..open].trim();
            let arg_pat = rest.trim();
            return tool_matches_pattern(tool_pat, tool_name)
                && (arg_pat.is_empty() || wildcard_match(arg_pat, specifier));
        }
    }
    tool_matches_pattern(trimmed, tool_name)
}

/// Simple `*` wildcard matcher (case-sensitive, zero external crate dependencies).
fn wildcard_match(pattern: &str, text: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let t: Vec<char> = text.chars().collect();
    let (mut pi, mut ti) = (0usize, 0usize);
    let mut star_idx: Option<usize> = None;
    let mut match_idx = 0usize;

    while ti < t.len() {
        if pi < p.len() && p[pi] == t[ti] {
            pi += 1;
            ti += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star_idx = Some(pi);
            match_idx = ti;
            pi += 1;
        } else if let Some(star) = star_idx {
            pi = star + 1;
            match_idx += 1;
            ti = match_idx;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deny_rules_override_allow_rules_and_aliases_match() {
        let mut policy = PermissionPolicy::default();
        policy.merge_value(&json!({
            "permissions": {
                "defaultMode": "workspace-write",
                "allow": ["Bash(cargo *)", "Edit(src/*)", "mcp__erp-service__*"],
                "deny": ["Bash(cargo publish*)"]
            }
        }));

        assert_eq!(
            policy.evaluate("run_command", &json!({"command": "cargo test"}), false),
            PermissionDecision::Allow
        );
        assert!(matches!(
            policy.evaluate("run_command", &json!({"command": "cargo publish"}), false),
            PermissionDecision::Deny(_)
        ));
        assert_eq!(
            policy.evaluate("edit_file", &json!({"path": "src/main.rs"}), false),
            PermissionDecision::Allow
        );
        assert!(matches!(
            policy.evaluate("edit_file", &json!({"path": "Cargo.toml"}), false),
            PermissionDecision::Ask(_)
        ));
        assert_eq!(
            policy.evaluate("mcp__erp-service__erp_list_orders", &json!({}), false),
            PermissionDecision::Allow
        );
    }

    #[test]
    fn read_only_tools_auto_allow_while_read_only_mode_blocks_writes() {
        let policy = PermissionPolicy {
            mode: PermissionMode::ReadOnly,
            allow: vec![],
            deny: vec![],
            ask: vec![],
        };
        assert_eq!(
            policy.evaluate("read_file", &json!({"path": "README.md"}), true),
            PermissionDecision::Allow
        );
        assert!(matches!(
            policy.evaluate("write_file", &json!({"path": "README.md"}), false),
            PermissionDecision::Deny(_)
        ));
    }

    #[test]
    fn loads_layered_workspace_settings_and_local_overrides() {
        let dir = tempfile::tempdir().unwrap();
        let path = write_default_settings_if_missing(dir.path()).unwrap();
        assert!(path.is_file());
        fs::write(
            dir.path().join(".aria").join("settings.local.json"),
            r#"{"permissions":{"defaultMode":"full-access","deny":["Write(.env*)"]}}"#,
        )
        .unwrap();

        let policy = PermissionPolicy::load(dir.path());
        assert_eq!(policy.mode, PermissionMode::FullAccess);
        assert_eq!(
            policy.evaluate("run_command", &json!({"command": "git status -sb"}), false),
            PermissionDecision::Allow
        );
        assert!(matches!(
            policy.evaluate("write_file", &json!({"path": ".env.local"}), false),
            PermissionDecision::Deny(_)
        ));
    }

    #[test]
    fn ask_rule_forces_confirmation_even_in_full_access() {
        let mut policy = PermissionPolicy {
            mode: PermissionMode::FullAccess,
            ..Default::default()
        };
        policy.merge_value(&json!({
            "permissions": {
                "ask": ["mcp__erp-service__*", "Bash(git push*)"]
            }
        }));
        assert!(matches!(
            policy.evaluate("mcp__erp-service__score_carriers", &json!({}), true),
            PermissionDecision::Ask(_)
        ));
        assert!(matches!(
            policy.evaluate(
                "run_command",
                &json!({"command": "git push origin main"}),
                false
            ),
            PermissionDecision::Ask(_)
        ));
        assert_eq!(
            policy.evaluate("run_command", &json!({"command": "git status"}), false),
            PermissionDecision::Allow
        );
    }
}
