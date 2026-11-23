//! Native Rust workflow runner for `.aria/workflows/*.{yaml,yml,json}` files.

use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkflowStep {
    pub name: String,
    pub command: String,
    pub allow_failure: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkflowSpec {
    pub name: String,
    pub description: String,
    pub source_path: PathBuf,
    pub steps: Vec<WorkflowStep>,
}

fn unquote(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.len() >= 2
        && ((trimmed.starts_with('"') && trimmed.ends_with('"'))
            || (trimmed.starts_with('\'') && trimmed.ends_with('\'')))
    {
        return trimmed[1..trimmed.len() - 1].to_owned();
    }
    trimmed.to_owned()
}

fn strip_yaml_comment(line: &str) -> &str {
    let mut in_single = false;
    let mut in_double = false;
    for (idx, ch) in line.char_indices() {
        match ch {
            '\'' if !in_double => in_single = !in_single,
            '"' if !in_single => in_double = !in_double,
            '#' if !in_single
                && !in_double
                && (idx == 0 || line[..idx].ends_with(char::is_whitespace)) =>
            {
                return &line[..idx];
            }
            _ => {}
        }
    }
    line
}

fn parse_json_workflow(path: &Path, raw: &str) -> Result<WorkflowSpec, String> {
    let val: Value =
        serde_json::from_str(raw).map_err(|e| format!("Invalid JSON workflow: {e}"))?;
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("workflow")
        .to_owned();
    let name = val
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(&stem)
        .to_owned();
    let description = val
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_owned();
    let mut steps = Vec::new();
    if let Some(arr) = val.get("steps").and_then(Value::as_array) {
        for (idx, item) in arr.iter().enumerate() {
            let cmd = item
                .get("command")
                .or_else(|| item.get("cmd"))
                .or_else(|| item.get("run"))
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if cmd.is_empty() {
                continue;
            }
            let step_name = item
                .get("name")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| format!("step-{}", idx + 1));
            let allow_failure = item
                .get("allow_failure")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            steps.push(WorkflowStep {
                name: step_name,
                command: cmd.to_owned(),
                allow_failure,
            });
        }
    }
    Ok(WorkflowSpec {
        name,
        description,
        source_path: path.to_path_buf(),
        steps,
    })
}

fn parse_yaml_workflow(path: &Path, raw: &str) -> Result<WorkflowSpec, String> {
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("workflow")
        .to_owned();
    let mut name = stem;
    let mut description = String::new();
    let mut steps: Vec<WorkflowStep> = Vec::new();
    let mut in_steps = false;
    let mut current: Option<WorkflowStep> = None;

    let flush_step = |cur: &mut Option<WorkflowStep>, out: &mut Vec<WorkflowStep>| {
        if let Some(mut step) = cur.take() {
            if !step.command.trim().is_empty() {
                if step.name.trim().is_empty() {
                    step.name = format!("step-{}", out.len() + 1);
                }
                out.push(step);
            }
        }
    };

    for raw_line in raw.lines() {
        let cleaned = strip_yaml_comment(raw_line);
        let trimmed = cleaned.trim();
        if trimmed.is_empty() || trimmed == "---" {
            continue;
        }
        let indent = cleaned.len() - cleaned.trim_start().len();
        if indent == 0 {
            if let Some((k, v)) = trimmed.split_once(':') {
                let key = k.trim();
                let val = unquote(v);
                match key {
                    "name" if !val.is_empty() => {
                        in_steps = false;
                        name = val;
                    }
                    "description" => {
                        in_steps = false;
                        description = val;
                    }
                    "steps" => {
                        in_steps = true;
                    }
                    _ => {
                        in_steps = false;
                    }
                }
            }
            continue;
        }
        if !in_steps {
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix("- ") {
            flush_step(&mut current, &mut steps);
            let mut step = WorkflowStep {
                name: String::new(),
                command: String::new(),
                allow_failure: false,
            };
            if let Some((k, v)) = rest.split_once(':') {
                apply_step_field(&mut step, k.trim(), &unquote(v));
            } else {
                step.command = unquote(rest);
            }
            current = Some(step);
        } else if let Some((k, v)) = trimmed.split_once(':') {
            if let Some(ref mut step) = current {
                apply_step_field(step, k.trim(), &unquote(v));
            }
        }
    }
    flush_step(&mut current, &mut steps);

    Ok(WorkflowSpec {
        name,
        description,
        source_path: path.to_path_buf(),
        steps,
    })
}

fn apply_step_field(step: &mut WorkflowStep, key: &str, val: &str) {
    match key {
        "name" => step.name = val.to_owned(),
        "command" | "cmd" | "run" => step.command = val.to_owned(),
        "allow_failure" => {
            step.allow_failure = matches!(val.to_ascii_lowercase().as_str(), "true" | "yes" | "1");
        }
        _ => {}
    }
}

pub fn parse_workflow_file(path: &Path) -> Result<WorkflowSpec, String> {
    let raw =
        fs::read_to_string(path).map_err(|e| format!("Cannot read {}: {e}", path.display()))?;
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if ext == "json" {
        parse_json_workflow(path, &raw)
    } else {
        parse_yaml_workflow(path, &raw)
    }
}

pub fn discover_workflows(workspace: &Path) -> Vec<WorkflowSpec> {
    let mut dirs = Vec::new();
    if let Ok(home) = std::env::var("HOME") {
        dirs.push(PathBuf::from(home).join(".arthera").join("workflows"));
    }
    dirs.push(workspace.join(".aria").join("workflows"));

    let mut by_name: BTreeMap<String, WorkflowSpec> = BTreeMap::new();
    for dir in dirs {
        let Ok(entries) = fs::read_dir(&dir) else {
            continue;
        };
        let mut paths: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.is_file()
                    && p.extension().and_then(|e| e.to_str()).is_some_and(|e| {
                        matches!(e.to_ascii_lowercase().as_str(), "yaml" | "yml" | "json")
                    })
            })
            .collect();
        paths.sort();
        for path in paths {
            if let Ok(spec) = parse_workflow_file(&path) {
                by_name.insert(spec.name.clone(), spec);
            }
        }
    }
    by_name.into_values().collect()
}

fn truncate_output(s: &str, max_chars: usize) -> String {
    let trimmed = s.trim();
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_owned();
    }
    let prefix: String = trimmed.chars().take(max_chars).collect();
    format!("{prefix}\n... (truncated)")
}

pub fn handle_workflow_command(
    workspace: &Path,
    arg: &str,
    allow_write: bool,
    zh: bool,
) -> (String, &'static str) {
    let parts: Vec<&str> = arg.split_whitespace().collect();
    let sub = parts.first().copied().unwrap_or("list");
    let workflows = discover_workflows(workspace);

    match sub {
        "" | "list" | "ls" => {
            if workflows.is_empty() {
                let msg = if zh {
                    "未发现工作流。可在 `.aria/workflows/<name>.yaml` 或 `.json` 中定义步骤（`name` / `description` / `steps`）。"
                } else {
                    "No workflows found. Add `.aria/workflows/<name>.yaml` or `.json` with `name`, `description`, and `steps`."
                };
                return (msg.to_owned(), "ok");
            }
            let mut lines = vec![if zh {
                "### 可用工作流".to_owned()
            } else {
                "### Available Workflows".to_owned()
            }];
            for wf in &workflows {
                let desc = if wf.description.is_empty() {
                    String::new()
                } else {
                    format!(" — {}", wf.description)
                };
                lines.push(format!("- `{}` ({} steps){desc}", wf.name, wf.steps.len()));
            }
            lines.push(String::new());
            lines.push(if zh {
                "用法：`/workflow show <name>` 或 `/workflow run <name> [--dry-run]`".to_owned()
            } else {
                "Usage: `/workflow show <name>` or `/workflow run <name> [--dry-run]`".to_owned()
            });
            (lines.join("\n"), "ok")
        }
        "show" => {
            let Some(target) = parts.get(1).copied() else {
                return ("Usage: `/workflow show <name>`".to_owned(), "ok");
            };
            let Some(wf) = workflows.iter().find(|w| w.name == target) else {
                return (format!("Workflow `{target}` not found."), "failed");
            };
            let mut lines = vec![format!("### Workflow `{}`", wf.name)];
            if !wf.description.is_empty() {
                lines.push(wf.description.clone());
            }
            lines.push(format!("Source: `{}`", wf.source_path.display()));
            lines.push(String::new());
            for (idx, step) in wf.steps.iter().enumerate() {
                let flag = if step.allow_failure {
                    " _(allow_failure)_"
                } else {
                    ""
                };
                lines.push(format!(
                    "{}. **{}**: `{}`{flag}",
                    idx + 1,
                    step.name,
                    step.command
                ));
            }
            (lines.join("\n"), "ok")
        }
        "run" => {
            let Some(target) = parts.get(1).copied() else {
                return ("Usage: `/workflow run <name> [--dry-run]`".to_owned(), "ok");
            };
            let Some(wf) = workflows.iter().find(|w| w.name == target) else {
                return (format!("Workflow `{target}` not found."), "failed");
            };
            let dry_run = parts.iter().any(|p| *p == "--dry-run" || *p == "-n");
            if dry_run || !allow_write {
                let mut lines = vec![format!("### Workflow Dry-Run `{}`", wf.name)];
                if !allow_write && !dry_run {
                    lines.push(if zh {
                        "当前处于只读保护模式；已切换为 dry-run 预览。执行前请先运行 `/write-mode on`。"
                            .to_owned()
                    } else {
                        "Write mode is OFF; showing dry-run plan. Run `/write-mode on` to execute commands."
                            .to_owned()
                    });
                }
                for (idx, step) in wf.steps.iter().enumerate() {
                    lines.push(format!(
                        "{}. **{}**: `{}`",
                        idx + 1,
                        step.name,
                        step.command
                    ));
                }
                return (lines.join("\n"), "ok");
            }
            if wf.steps.is_empty() {
                return (
                    format!("Workflow `{}` has no runnable steps.", wf.name),
                    "ok",
                );
            }
            let mut lines = vec![format!("### Workflow Run `{}`", wf.name)];
            let mut all_ok = true;
            for (idx, step) in wf.steps.iter().enumerate() {
                let started = Instant::now();
                #[cfg(windows)]
                let output = Command::new("cmd")
                    .args(["/C", &step.command])
                    .current_dir(workspace)
                    .output();
                #[cfg(not(windows))]
                let output = Command::new("sh")
                    .args(["-c", &step.command])
                    .current_dir(workspace)
                    .output();
                let ms = started.elapsed().as_millis();
                match output {
                    Ok(out) => {
                        let ok = out.status.success();
                        crate::engine::roi::record_tool_execution("workflow.step", ms as u64);
                        let code = out.status.code().unwrap_or(-1);
                        let badge = if ok { "PASS" } else { "FAIL" };
                        lines.push(format!(
                            "{}. **{}** — `{badge}` (exit {code}, {ms}ms)",
                            idx + 1,
                            step.name
                        ));
                        let stdout = String::from_utf8_lossy(&out.stdout);
                        let stderr = String::from_utf8_lossy(&out.stderr);
                        let combined = if !stderr.trim().is_empty() && !ok {
                            stderr.into_owned()
                        } else {
                            stdout.into_owned()
                        };
                        let snippet = truncate_output(&combined, 800);
                        if !snippet.is_empty() {
                            lines.push(format!("```\n{snippet}\n```"));
                        }
                        if !ok && !step.allow_failure {
                            all_ok = false;
                            lines.push(format!("Stopped after step `{}` failed.", step.name));
                            break;
                        }
                    }
                    Err(err) => {
                        crate::engine::roi::record_tool_execution("workflow.step", ms as u64);
                        lines.push(format!("{}. **{}** — `ERROR` ({err})", idx + 1, step.name));
                        if !step.allow_failure {
                            all_ok = false;
                            break;
                        }
                    }
                }
            }
            (lines.join("\n"), if all_ok { "ok" } else { "failed" })
        }
        _ => (
            "Usage: `/workflow [list|show <name>|run <name> [--dry-run]]`".to_owned(),
            "ok",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_yaml_and_json_workflows() {
        let dir = tempfile::tempdir().unwrap();
        let wf_dir = dir.path().join(".aria").join("workflows");
        fs::create_dir_all(&wf_dir).unwrap();

        let yaml_path = wf_dir.join("ci.yaml");
        fs::write(
            &yaml_path,
            r#"
name: ci-check
description: "Run quick checks"
steps:
  - name: greet
    command: "echo hello-workflow"
  - name: soft-fail
    run: "exit 1"
    allow_failure: true
"#,
        )
        .unwrap();

        let json_path = wf_dir.join("lint.json");
        fs::write(
            &json_path,
            r#"{"name":"lint","description":"JSON workflow","steps":[{"name":"check","command":"echo ok"}]}"#,
        )
        .unwrap();

        let found = discover_workflows(dir.path());
        assert!(found
            .iter()
            .any(|w| w.name == "ci-check" && w.steps.len() == 2 && w.steps[1].allow_failure));
        assert!(found.iter().any(|w| w.name == "lint" && w.steps.len() == 1));
    }

    #[test]
    fn workflow_dry_run_and_execute_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let wf_dir = dir.path().join(".aria").join("workflows");
        fs::create_dir_all(&wf_dir).unwrap();
        fs::write(
            wf_dir.join("hello.yaml"),
            "name: hello\ndescription: demo\nsteps:\n  - name: echo\n    command: echo aria-native-ok\n",
        )
        .unwrap();

        let (dry_out, dry_status) = handle_workflow_command(dir.path(), "run hello", false, false);
        assert_eq!(dry_status, "ok");
        assert!(dry_out.contains("dry-run"));

        let (run_out, run_status) = handle_workflow_command(dir.path(), "run hello", true, false);
        assert_eq!(run_status, "ok");
        assert!(run_out.contains("PASS"));
        assert!(run_out.contains("aria-native-ok"));
    }
}
