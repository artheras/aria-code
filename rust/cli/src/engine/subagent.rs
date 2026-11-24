//! Native bounded subagent delegation for isolated coding and research subtasks.
//!
//! Replaces Python `runtime/subagent.py` with a structured Rust task spec & worktree runner.

#![allow(dead_code)]

use super::{agent, tools};
use serde_json::{json, Value};
use std::{
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

fn next_worktree_branch_name() -> String {
    static SEQ: AtomicU64 = AtomicU64::new(1);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    format!("aria-subagent-{}-{seq}", std::process::id())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SubagentRole {
    Research,
    Coder,
    Reviewer,
}

impl SubagentRole {
    pub fn allow_write(&self) -> bool {
        matches!(self, Self::Coder)
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Research => "research",
            Self::Coder => "coder",
            Self::Reviewer => "reviewer",
        }
    }

    pub fn from_str(raw: &str) -> Self {
        match raw.trim().to_ascii_lowercase().as_str() {
            "coder" | "implement" | "edit" => Self::Coder,
            "reviewer" | "review" => Self::Reviewer,
            _ => Self::Research,
        }
    }
}

#[derive(Debug, Clone)]
pub struct SubagentRequest {
    pub role: SubagentRole,
    pub prompt: String,
    pub max_rounds: usize,
    pub isolated_worktree: bool,
}

#[derive(Debug, Clone)]
pub struct SubagentOutcome {
    pub role: String,
    pub ok: bool,
    pub answer: String,
    pub tool_calls: usize,
    pub worktree_path: Option<String>,
}

impl SubagentOutcome {
    pub fn to_json(&self) -> Value {
        json!({
            "role": self.role,
            "ok": self.ok,
            "answer": self.answer,
            "tool_calls": self.tool_calls,
            "worktree_path": self.worktree_path,
        })
    }
}

/// Execute a bounded subagent turn against an Ollama endpoint, optionally inside a fresh git worktree.
pub fn run_local_subagent(
    base_url: &str,
    model: &str,
    workspace: &Path,
    req: &SubagentRequest,
    timeout: Duration,
) -> SubagentOutcome {
    let mut worktree_dir: Option<std::path::PathBuf> = None;
    let effective_ws = if req.isolated_worktree && req.role.allow_write() {
        let branch = next_worktree_branch_name();
        let target = workspace.join(".aria").join("worktrees").join(&branch);
        let created = tools::execute_tool(
            workspace,
            "git_worktree",
            &json!({
                "action": "add",
                "path": target.to_string_lossy(),
                "branch": branch,
            }),
            true,
        );
        if created.get("ok").and_then(Value::as_bool) == Some(true) {
            worktree_dir = Some(target.clone());
            target
        } else {
            workspace.to_path_buf()
        }
    } else {
        workspace.to_path_buf()
    };

    let prefixed_prompt = format!(
        "[Subagent role: {}]\n{}",
        req.role.as_str(),
        req.prompt.trim()
    );
    let mut count = 0usize;
    let res = agent::run_native_agent(
        base_url,
        model,
        &effective_ws,
        &prefixed_prompt,
        req.role.allow_write(),
        req.max_rounds.clamp(1, 8),
        timeout,
        |_, _, _| count += 1,
    );

    match res {
        Ok(answer) => SubagentOutcome {
            role: req.role.as_str().to_owned(),
            ok: true,
            answer,
            tool_calls: count,
            worktree_path: worktree_dir.map(|p| p.to_string_lossy().into_owned()),
        },
        Err(err) => SubagentOutcome {
            role: req.role.as_str().to_owned(),
            ok: false,
            answer: err,
            tool_calls: count,
            worktree_path: worktree_dir.map(|p| p.to_string_lossy().into_owned()),
        },
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExternalAgentBackend {
    Claude,
    Codex,
}

impl ExternalAgentBackend {
    pub fn from_str(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "claude" | "claude-code" => Some(Self::Claude),
            "codex" => Some(Self::Codex),
            _ => None,
        }
    }

    pub fn binary(self) -> &'static str {
        match self {
            Self::Claude => "claude",
            Self::Codex => "codex",
        }
    }

    pub fn args(self, prompt: &str) -> Vec<String> {
        match self {
            Self::Claude => vec![
                "-p".to_owned(),
                prompt.to_owned(),
                "--output-format".to_owned(),
                "json".to_owned(),
            ],
            Self::Codex => vec!["exec".to_owned(), prompt.to_owned(), "--json".to_owned()],
        }
    }
}

pub fn extract_json_result(raw: &str, keys: &[&str]) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return "(no output)".to_owned();
    }
    if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(trimmed) {
        for k in keys {
            if let Some(s) = map
                .get(*k)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                return s.to_owned();
            }
        }
    }
    trimmed.to_owned()
}

/// Delegate a prompt to an external headless coding CLI (`claude -p` or `codex exec`).
/// Replaces legacy Python `external_agent_runner.py`.
pub fn run_external_agent(
    backend: ExternalAgentBackend,
    prompt: &str,
    workspace: &Path,
) -> Result<String, String> {
    let bin = backend.binary();
    let args = backend.args(prompt);
    let out = std::process::Command::new(bin)
        .args(&args)
        .current_dir(workspace)
        .output()
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                format!("{bin} CLI not found on PATH. Install it first.")
            } else {
                format!("Failed to launch {bin}: {e}")
            }
        })?;
    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.chars().take(500).collect::<String>()
        } else {
            stderr.chars().take(500).collect::<String>()
        };
        return Err(format!("{bin} CLI exited {}: {detail}", out.status));
    }
    let keys: &[&str] = match backend {
        ExternalAgentBackend::Claude => &["result", "response"],
        ExternalAgentBackend::Codex => &["result", "message", "output"],
    };
    Ok(extract_json_result(&stdout, keys))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_roles_and_write_permissions() {
        assert!(!SubagentRole::from_str("research").allow_write());
        assert!(!SubagentRole::from_str("reviewer").allow_write());
        assert!(SubagentRole::from_str("coder").allow_write());
        assert_eq!(SubagentRole::from_str("implement"), SubagentRole::Coder);
    }

    #[test]
    fn serializes_subagent_outcome() {
        let out = SubagentOutcome {
            role: "research".into(),
            ok: true,
            answer: "found 3 callers".into(),
            tool_calls: 2,
            worktree_path: None,
        };
        let v = out.to_json();
        assert_eq!(v["role"], "research");
        assert_eq!(v["ok"], true);
        assert_eq!(v["tool_calls"], 2);
    }

    #[test]
    fn generates_unique_worktree_branch_names_within_same_process() {
        let a = next_worktree_branch_name();
        let b = next_worktree_branch_name();
        assert_ne!(a, b);
        assert!(a.starts_with("aria-subagent-"));
        assert!(b.starts_with("aria-subagent-"));
    }

    #[test]
    fn external_agent_parses_backends_and_json_results() {
        assert_eq!(
            ExternalAgentBackend::from_str("claude"),
            Some(ExternalAgentBackend::Claude)
        );
        assert_eq!(
            ExternalAgentBackend::from_str("codex"),
            Some(ExternalAgentBackend::Codex)
        );
        assert_eq!(ExternalAgentBackend::from_str("unknown"), None);
        assert_eq!(
            extract_json_result(r#"{"result":"done"}"#, &["result", "response"]),
            "done"
        );
        assert_eq!(extract_json_result("plain text", &["result"]), "plain text");
    }
}
