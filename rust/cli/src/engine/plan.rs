//! Native Rust persistent execution plan (`/plan` and `/apply-plan`).
//!
//! Stores ordered shell steps in `.aria/plan.json` and enforces
//! `PermissionPolicy` deny rules + `/write-mode` gating during `/apply-plan`.

use crate::engine::permissions::{PermissionDecision, PermissionPolicy};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanStep {
    pub command: String,
    pub status: String,
    pub last_exit: Option<i32>,
    pub updated_at: u64,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn plan_path(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("plan.json")
}

pub fn load_plan(workspace: &Path) -> Vec<PlanStep> {
    let path = plan_path(workspace);
    let Ok(raw) = fs::read_to_string(&path) else {
        return Vec::new();
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    let Some(arr) = val.get("steps").and_then(Value::as_array) else {
        return Vec::new();
    };
    arr.iter()
        .filter_map(|item| {
            let cmd = item.get("command").and_then(Value::as_str)?.trim();
            if cmd.is_empty() {
                return None;
            }
            let status = item
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("pending")
                .to_owned();
            let last_exit = item
                .get("last_exit")
                .and_then(Value::as_i64)
                .and_then(|v| i32::try_from(v).ok());
            let updated_at = item.get("updated_at").and_then(Value::as_u64).unwrap_or(0);
            Some(PlanStep {
                command: cmd.to_owned(),
                status,
                last_exit,
                updated_at,
            })
        })
        .collect()
}

pub fn save_plan(workspace: &Path, steps: &[PlanStep]) -> Result<(), String> {
    let path = plan_path(workspace);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Failed to create .aria dir: {e}"))?;
    }
    let items: Vec<Value> = steps
        .iter()
        .map(|s| {
            json!({
                "command": s.command,
                "status": s.status,
                "last_exit": s.last_exit,
                "updated_at": s.updated_at,
            })
        })
        .collect();
    let payload = json!({
        "updated_at": now_secs(),
        "steps": items,
    });
    let text = serde_json::to_string_pretty(&payload)
        .map_err(|e| format!("Failed to serialize plan: {e}"))?;
    fs::write(&path, text).map_err(|e| format!("Failed to write {}: {e}", path.display()))
}

fn truncate_snippet(raw: &str, max_chars: usize) -> String {
    let trimmed = raw.trim();
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_owned();
    }
    let prefix: String = trimmed.chars().take(max_chars).collect();
    format!("{prefix}\n... (truncated)")
}

pub fn handle_plan_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let trimmed = arg.trim();
    let mut split = trimmed.splitn(2, char::is_whitespace);
    let sub = split.next().unwrap_or("");
    let rest = split.next().unwrap_or("").trim();

    match sub {
        "" | "list" | "show" => {
            let steps = load_plan(workspace);
            if steps.is_empty() {
                let msg = if zh {
                    "当前没有待执行计划。使用 `/plan add <command>` 添加步骤，然后运行 `/apply-plan`。"
                } else {
                    "No pending plan. Add steps with `/plan add <command>`, then execute with `/apply-plan`."
                };
                return (msg.to_owned(), "ok");
            }
            let policy = PermissionPolicy::load(workspace);
            let mut lines = vec![if zh {
                "### 当前执行计划 (`.aria/plan.json`)".to_owned()
            } else {
                "### Current Execution Plan (`.aria/plan.json`)".to_owned()
            }];
            for (idx, step) in steps.iter().enumerate() {
                let decision =
                    policy.evaluate("run_command", &json!({"command": step.command}), false);
                let risk_tag = match decision {
                    PermissionDecision::Deny(_) => " [DENY]",
                    PermissionDecision::Allow => " [allow]",
                    PermissionDecision::Ask(_) => "",
                };
                let icon = match step.status.as_str() {
                    "done" => "✓",
                    "failed" => "✗",
                    "blocked" => "⊘",
                    _ => "○",
                };
                lines.push(format!(
                    "{}. {icon} `{}` _{}_{risk_tag}",
                    idx + 1,
                    step.command,
                    step.status
                ));
            }
            lines.push(String::new());
            lines.push(if zh {
                "执行：`/apply-plan [--from N] [--dry-run]` · 清空：`/plan clear`".to_owned()
            } else {
                "Run: `/apply-plan [--from N] [--dry-run]` · Clear: `/plan clear`".to_owned()
            });
            (lines.join("\n"), "ok")
        }
        "add" => {
            if rest.is_empty() {
                return ("Usage: `/plan add <command>`".to_owned(), "ok");
            }
            let mut steps = load_plan(workspace);
            steps.push(PlanStep {
                command: rest.to_owned(),
                status: "pending".to_owned(),
                last_exit: None,
                updated_at: now_secs(),
            });
            if let Err(e) = save_plan(workspace, &steps) {
                return (format!("[plan error: {e}]"), "failed");
            }
            (
                format!("Added step #{} to `.aria/plan.json`: `{rest}`", steps.len()),
                "ok",
            )
        }
        "rm" | "remove" | "del" => {
            let Ok(idx) = rest.parse::<usize>() else {
                return ("Usage: `/plan rm <step_number>`".to_owned(), "ok");
            };
            let mut steps = load_plan(workspace);
            if idx == 0 || idx > steps.len() {
                return (
                    format!(
                        "Step #{idx} does not exist (plan has {} steps).",
                        steps.len()
                    ),
                    "failed",
                );
            }
            let removed = steps.remove(idx - 1);
            if let Err(e) = save_plan(workspace, &steps) {
                return (format!("[plan error: {e}]"), "failed");
            }
            (format!("Removed step #{idx}: `{}`", removed.command), "ok")
        }
        "clear" => {
            if let Err(e) = save_plan(workspace, &[]) {
                return (format!("[plan error: {e}]"), "failed");
            }
            (
                if zh {
                    "已清空 `.aria/plan.json` 中的计划步骤。".to_owned()
                } else {
                    "Cleared all steps from `.aria/plan.json`.".to_owned()
                },
                "ok",
            )
        }
        _ => (
            "Usage: `/plan [list|add <command>|rm <N>|clear]`".to_owned(),
            "ok",
        ),
    }
}

pub fn handle_apply_plan_command(
    workspace: &Path,
    arg: &str,
    allow_write: bool,
    zh: bool,
) -> (String, &'static str) {
    let tokens: Vec<&str> = arg.split_whitespace().collect();
    let dry_run = tokens.iter().any(|t| *t == "--dry-run" || *t == "-n");
    let mut start_idx: usize = 0;
    if let Some(pos) = tokens.iter().position(|t| *t == "--from") {
        let Some(num_str) = tokens.get(pos + 1) else {
            return (
                "Usage: `/apply-plan [--from <step_number>] [--dry-run]`".to_owned(),
                "ok",
            );
        };
        match num_str.parse::<usize>() {
            Ok(n) if n >= 1 => start_idx = n - 1,
            _ => return ("Invalid step number for `--from`.".to_owned(), "failed"),
        }
    }

    let mut steps = load_plan(workspace);
    if steps.is_empty() {
        let msg = if zh {
            "当前没有待执行计划。请先用 `/plan add <command>` 添加步骤。"
        } else {
            "No pending plan. Add steps with `/plan add <command>` first."
        };
        return (msg.to_owned(), "ok");
    }
    if start_idx >= steps.len() {
        return (
            format!(
                "`--from {}` exceeds available steps ({}).",
                start_idx + 1,
                steps.len()
            ),
            "failed",
        );
    }

    let policy = PermissionPolicy::load(workspace);
    if dry_run || !allow_write {
        let mut lines = vec!["### Plan Dry-Run".to_owned()];
        if !allow_write && !dry_run {
            lines.push(if zh {
                "当前处于只读保护模式；已切换为 dry-run 预览。执行前请运行 `/write-mode on`。"
                    .to_owned()
            } else {
                "Write mode is OFF; showing dry-run preview. Run `/write-mode on` to execute."
                    .to_owned()
            });
        }
        for (offset, step) in steps[start_idx..].iter().enumerate() {
            let num = start_idx + offset + 1;
            let verdict =
                match policy.evaluate("run_command", &json!({"command": step.command}), false) {
                    PermissionDecision::Deny(reason) => format!("BLOCKED ({reason})"),
                    PermissionDecision::Allow => "ALLOW".to_owned(),
                    PermissionDecision::Ask(_) => "EXEC".to_owned(),
                };
            lines.push(format!("{num}. `{}` — `{verdict}`", step.command));
        }
        return (lines.join("\n"), "ok");
    }

    let mut lines = vec!["### Plan Execution".to_owned()];
    let mut all_ok = true;

    for (idx, step) in steps.iter_mut().enumerate().skip(start_idx) {
        let cmd = step.command.clone();
        if let PermissionDecision::Deny(reason) =
            policy.evaluate("run_command", &json!({"command": cmd}), false)
        {
            crate::engine::roi::record_blocked_action();
            step.status = "blocked".to_owned();
            step.updated_at = now_secs();
            lines.push(format!("{}. `{cmd}` — `BLOCKED` ({reason})", idx + 1));
            all_ok = false;
            break;
        }

        let started = Instant::now();
        #[cfg(windows)]
        let output = Command::new("cmd")
            .args(["/C", &cmd])
            .current_dir(workspace)
            .output();
        #[cfg(not(windows))]
        let output = Command::new("sh")
            .args(["-c", &cmd])
            .current_dir(workspace)
            .output();
        let ms = started.elapsed().as_millis();

        match output {
            Ok(out) => {
                let ok = out.status.success();
                let code = out.status.code().unwrap_or(-1);
                crate::engine::roi::record_tool_execution("plan.step", ms as u64);
                step.status = if ok { "done" } else { "failed" }.to_owned();
                step.last_exit = Some(code);
                step.updated_at = now_secs();
                let badge = if ok { "PASS" } else { "FAIL" };
                lines.push(format!(
                    "{}. `{cmd}` — `{badge}` (exit {code}, {ms}ms)",
                    idx + 1
                ));
                let raw = if !ok && !out.stderr.is_empty() {
                    String::from_utf8_lossy(&out.stderr).into_owned()
                } else {
                    String::from_utf8_lossy(&out.stdout).into_owned()
                };
                let snippet = truncate_snippet(&raw, 600);
                if !snippet.is_empty() {
                    lines.push(format!("```\n{snippet}\n```"));
                }
                if !ok {
                    all_ok = false;
                    lines.push(format!(
                        "Stopped at step #{}. Resume with `/apply-plan --from {}`.",
                        idx + 1,
                        idx + 1
                    ));
                    break;
                }
            }
            Err(err) => {
                crate::engine::roi::record_tool_execution("plan.step", ms as u64);
                step.status = "failed".to_owned();
                step.updated_at = now_secs();
                lines.push(format!("{}. `{cmd}` — `ERROR` ({err})", idx + 1));
                all_ok = false;
                break;
            }
        }
    }

    let _ = save_plan(workspace, &steps);
    (lines.join("\n"), if all_ok { "ok" } else { "failed" })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plan_add_dry_run_execute_and_policy_deny() {
        let dir = tempfile::tempdir().unwrap();
        let (out1, st1) = handle_plan_command(dir.path(), "add echo plan-step-1", false);
        assert_eq!(st1, "ok");
        assert!(out1.contains("step #1"));

        let (dry, st_dry) = handle_apply_plan_command(dir.path(), "", false, false);
        assert_eq!(st_dry, "ok");
        assert!(dry.contains("dry-run"));

        let (run, st_run) = handle_apply_plan_command(dir.path(), "", true, false);
        assert_eq!(st_run, "ok");
        assert!(run.contains("PASS"));
        assert!(run.contains("plan-step-1"));
        assert_eq!(load_plan(dir.path())[0].status, "done");

        // Enforce .aria/settings.json deny rule on a second step.
        fs::write(
            dir.path().join(".aria").join("settings.json"),
            r#"{"permissions":{"deny":["Bash(rm -rf*)"]}}"#,
        )
        .unwrap();
        let _ = handle_plan_command(dir.path(), "add rm -rf /tmp/fake", false);
        let (blocked, st_blocked) = handle_apply_plan_command(dir.path(), "--from 2", true, false);
        assert_eq!(st_blocked, "failed");
        assert!(blocked.contains("BLOCKED"));
        assert_eq!(load_plan(dir.path())[1].status, "blocked");
    }
}
