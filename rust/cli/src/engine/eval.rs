//! Native Rust evaluation harness (`/eval [smoke|list|run <suite>]`).
//!
//! Runs deterministic built-in governance & coding-agent harness checks or
//! workspace-defined JSON suites from `.aria/evals/*.json`, and persists
//! a timestamped report under `.aria/artifacts/eval-<ts>.json`.

use crate::engine::{compact, permissions, policy};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone)]
pub struct EvalCaseResult {
    pub id: String,
    pub passed: bool,
    pub elapsed_ms: u128,
    pub detail: String,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn eval_suites_dir(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("evals")
}

pub fn list_eval_suites(workspace: &Path) -> Vec<String> {
    let mut names = vec!["smoke".to_owned()];
    if let Ok(entries) = fs::read_dir(eval_suites_dir(workspace)) {
        let mut custom: Vec<String> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.extension()
                        .and_then(|e| e.to_str())
                        .is_some_and(|e| e.eq_ignore_ascii_case("json"))
            })
            .filter_map(|p| p.file_stem().and_then(|s| s.to_str()).map(str::to_owned))
            .collect();
        custom.sort();
        for name in custom {
            if !names.contains(&name) {
                names.push(name);
            }
        }
    }
    names
}

/// Deterministic built-in smoke suite verifying core Rust harness invariants.
pub fn run_builtin_smoke_eval() -> Vec<EvalCaseResult> {
    let mut results = Vec::new();

    // 1. Permission deny takes precedence over allow
    let t0 = Instant::now();
    let mut pol = permissions::PermissionPolicy::default();
    pol.merge_value(&json!({
        "permissions": {
            "allow": ["Bash(*)"],
            "deny": ["Bash(rm -rf*)"]
        }
    }));
    let dec = pol.evaluate("run_command", &json!({"command": "rm -rf /"}), false);
    let ok1 = matches!(dec, permissions::PermissionDecision::Deny(_));
    results.push(EvalCaseResult {
        id: "governance.permission_deny_precedence".into(),
        passed: ok1,
        elapsed_ms: t0.elapsed().as_millis(),
        detail: if ok1 {
            "deny rule overrode wildcard allow".into()
        } else {
            format!("unexpected decision: {dec:?}")
        },
    });

    // 2. Context compaction reduces long message history
    let t1 = Instant::now();
    let mut msgs: Vec<Value> = (0..12)
        .map(|i| {
            json!({
                "role": if i % 2 == 0 { "user" } else { "assistant" },
                "content": format!("turn {i}: {}", "x".repeat(400))
            })
        })
        .collect();
    let before = msgs.len();
    let compacted = compact::compact_messages_if_needed(&mut msgs, 1, 4);
    let ok2 = compacted && msgs.len() < before;
    results.push(EvalCaseResult {
        id: "harness.context_compaction".into(),
        passed: ok2,
        elapsed_ms: t1.elapsed().as_millis(),
        detail: format!("{before} -> {} messages", msgs.len()),
    });

    // 3. Policy prompt preserves Aria Code identity
    let t2 = Instant::now();
    let sys_prompt = policy::general_system_prompt("2026-01-01");
    let ok3 = sys_prompt.contains("Aria Code");
    results.push(EvalCaseResult {
        id: "policy.identity_guard".into(),
        passed: ok3,
        elapsed_ms: t2.elapsed().as_millis(),
        detail: if ok3 {
            "Aria Code identity present".into()
        } else {
            "identity missing".into()
        },
    });

    // 4. Read-only mode blocks write commands
    let t3 = Instant::now();
    let ro_pol = permissions::PermissionPolicy {
        mode: permissions::PermissionMode::ReadOnly,
        ..Default::default()
    };
    let ro_dec = ro_pol.evaluate("edit_file", &json!({"path": "a.rs"}), false);
    let ok4 = matches!(ro_dec, permissions::PermissionDecision::Deny(_));
    results.push(EvalCaseResult {
        id: "governance.read_only_blocks_edits".into(),
        passed: ok4,
        elapsed_ms: t3.elapsed().as_millis(),
        detail: if ok4 {
            "edit_file blocked in read-only mode".into()
        } else {
            format!("unexpected decision: {ro_dec:?}")
        },
    });

    results
}

fn run_custom_suite(
    workspace: &Path,
    suite_name: &str,
    allow_write: bool,
) -> Result<Vec<EvalCaseResult>, String> {
    let path = eval_suites_dir(workspace).join(format!("{suite_name}.json"));
    if !path.is_file() {
        return Err(format!(
            "Eval suite `{suite_name}` not found in `.aria/evals/`."
        ));
    }
    let raw =
        fs::read_to_string(&path).map_err(|e| format!("Cannot read {}: {e}", path.display()))?;
    let val: Value = serde_json::from_str(&raw).map_err(|e| format!("Invalid eval JSON: {e}"))?;
    let cases = val
        .get("cases")
        .and_then(Value::as_array)
        .ok_or_else(|| "Eval JSON must contain a `cases` array".to_owned())?;

    if !allow_write {
        return Err(
            "Custom shell eval suites require `/write-mode on` (built-in `/eval smoke` works in read-only mode)."
                .into(),
        );
    }

    let mut results = Vec::new();
    for (idx, case) in cases.iter().enumerate() {
        let id = case
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("case")
            .to_owned();
        let id = if id == "case" {
            format!("case-{}", idx + 1)
        } else {
            id
        };
        let cmd = case
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        let expected = case
            .get("expect_contains")
            .and_then(Value::as_str)
            .unwrap_or("");
        if cmd.is_empty() {
            continue;
        }
        let started = Instant::now();
        #[cfg(windows)]
        let out = Command::new("cmd")
            .args(["/C", cmd])
            .current_dir(workspace)
            .output();
        #[cfg(not(windows))]
        let out = Command::new("sh")
            .args(["-c", cmd])
            .current_dir(workspace)
            .output();
        let elapsed_ms = started.elapsed().as_millis();
        match out {
            Ok(o) => {
                let stdout = String::from_utf8_lossy(&o.stdout);
                let passed =
                    o.status.success() && (expected.is_empty() || stdout.contains(expected));
                results.push(EvalCaseResult {
                    id,
                    passed,
                    elapsed_ms,
                    detail: format!("exit {}", o.status.code().unwrap_or(-1)),
                });
            }
            Err(e) => results.push(EvalCaseResult {
                id,
                passed: false,
                elapsed_ms,
                detail: format!("spawn error: {e}"),
            }),
        }
    }
    Ok(results)
}

fn persist_eval_report(
    workspace: &Path,
    suite: &str,
    results: &[EvalCaseResult],
) -> Option<PathBuf> {
    let dir = workspace.join(".aria").join("artifacts");
    fs::create_dir_all(&dir).ok()?;
    let path = dir.join(format!("eval-{suite}-{}.json", now_secs()));
    let passed = results.iter().filter(|r| r.passed).count();
    let payload = json!({
        "suite": suite,
        "timestamp": now_secs(),
        "total": results.len(),
        "passed": passed,
        "pass_rate": if results.is_empty() { 1.0 } else { passed as f64 / results.len() as f64 },
        "cases": results.iter().map(|r| json!({
            "id": r.id,
            "passed": r.passed,
            "elapsed_ms": r.elapsed_ms,
            "detail": r.detail,
        })).collect::<Vec<_>>(),
    });
    let text = serde_json::to_string_pretty(&payload).ok()?;
    fs::write(&path, text).ok()?;
    Some(path)
}

pub fn handle_eval_command(
    workspace: &Path,
    arg: &str,
    allow_write: bool,
    zh: bool,
) -> (String, &'static str) {
    let tokens: Vec<&str> = arg.split_whitespace().collect();
    let sub = tokens.first().copied().unwrap_or("smoke");

    if sub == "list" || sub == "ls" {
        let suites = list_eval_suites(workspace);
        let mut lines = vec![if zh {
            "### 可用评测套件".to_owned()
        } else {
            "### Available Eval Suites".to_owned()
        }];
        for s in suites {
            lines.push(format!("- `{s}`"));
        }
        lines.push(String::new());
        lines.push("Usage: `/eval smoke` · `/eval run <suite>`".to_owned());
        return (lines.join("\n"), "ok");
    }

    let suite_name = if sub == "run" {
        tokens.get(1).copied().unwrap_or("smoke")
    } else {
        sub
    };

    let results = if suite_name == "smoke" {
        run_builtin_smoke_eval()
    } else {
        match run_custom_suite(workspace, suite_name, allow_write) {
            Ok(r) => r,
            Err(e) => return (format!("[eval error: {e}]"), "failed"),
        }
    };

    let passed = results.iter().filter(|r| r.passed).count();
    let total = results.len();
    let all_ok = passed == total;
    let report_path = persist_eval_report(workspace, suite_name, &results);

    let mut lines = vec![format!(
        "### Eval Suite `{suite_name}` — **{passed}/{total} passed**"
    )];
    lines.push(String::new());
    for r in &results {
        let icon = if r.passed { "✓" } else { "✗" };
        lines.push(format!(
            "- {icon} `{}` ({}ms) — {}",
            r.id, r.elapsed_ms, r.detail
        ));
    }
    if let Some(p) = report_path {
        lines.push(String::new());
        lines.push(format!("Report saved: `{}`", p.display()));
    }
    (lines.join("\n"), if all_ok { "ok" } else { "failed" })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_smoke_eval_passes_and_writes_artifact() {
        let dir = tempfile::tempdir().unwrap();
        let (out, status) = handle_eval_command(dir.path(), "smoke", false, false);
        assert_eq!(status, "ok");
        assert!(out.contains("4/4 passed"));
        let artifacts_dir = dir.path().join(".aria").join("artifacts");
        assert!(artifacts_dir.is_dir());
    }

    #[test]
    fn custom_eval_suite_requires_write_mode_and_executes_cases() {
        let dir = tempfile::tempdir().unwrap();
        let eval_dir = dir.path().join(".aria").join("evals");
        fs::create_dir_all(&eval_dir).unwrap();
        fs::write(
            eval_dir.join("mini.json"),
            r#"{"cases":[{"id":"echo_ok","command":"echo hello-eval","expect_contains":"hello-eval"}]}"#,
        )
        .unwrap();

        let (blocked, st_blocked) = handle_eval_command(dir.path(), "run mini", false, false);
        assert_eq!(st_blocked, "failed");
        assert!(blocked.contains("/write-mode on"));

        let (ok_out, st_ok) = handle_eval_command(dir.path(), "run mini", true, false);
        assert_eq!(st_ok, "ok");
        assert!(ok_out.contains("1/1 passed"));
    }
}
