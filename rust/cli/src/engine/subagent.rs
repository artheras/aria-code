//! Native bounded subagent delegation for isolated coding and research subtasks.
//!
//! Replaces Python `runtime/subagent.py` with a structured Rust task spec & worktree runner.

#![allow(dead_code)]

use super::{agent, tools};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

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
        let branch = format!("aria-subagent-{}", std::process::id());
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
}
