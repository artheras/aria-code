//! Native Rust `/review` code review engine.
//!
//! Replaces legacy Python `review_service.py` + `review_runner.py` with a fast,
//! zero-dependency git diff collector and static/security heuristic reviewer.
//!
//! Supported targets:
//! - `/review`                  — uncommitted changes (staged, unstaged, untracked)
//! - `/review --staged`         — staged changes only
//! - `/review --base <branch>`  — changes since merge-base with `<branch>`
//! - `/review --commit <sha>`   — single commit diff
//! - `/review <path>`           — single file diff
//! - `--json`                   — structured JSON output

use std::path::Path;
use std::process::Command;

use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReviewTarget {
    Uncommitted,
    Staged,
    Base(String),
    Commit(String),
    File(String),
}

impl ReviewTarget {
    pub fn describe(&self) -> String {
        match self {
            Self::Uncommitted => "uncommitted changes (staged, unstaged and untracked)".into(),
            Self::Staged => "staged changes".into(),
            Self::Base(b) => format!("changes against {b}"),
            Self::Commit(c) => format!("commit {c}"),
            Self::File(f) => format!("file {f}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ReviewFinding {
    pub priority: &'static str, // P0, P1, P2, P3
    pub title: String,
    pub file: String,
    pub line: usize,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct ReviewReport {
    pub target: String,
    pub files: Vec<String>,
    pub added: usize,
    pub removed: usize,
    pub findings: Vec<ReviewFinding>,
}

impl ReviewReport {
    pub fn verdict(&self) -> &'static str {
        if self
            .findings
            .iter()
            .any(|f| f.priority == "P0" || f.priority == "P1")
        {
            "changes_requested"
        } else {
            "approved"
        }
    }

    pub fn to_json(&self) -> Value {
        let items: Vec<Value> = self
            .findings
            .iter()
            .map(|f| {
                json!({
                    "priority": f.priority,
                    "title": f.title,
                    "file": f.file,
                    "line": f.line,
                    "detail": f.detail,
                })
            })
            .collect();
        json!({
            "target": self.target,
            "verdict": self.verdict(),
            "files": self.files,
            "added": self.added,
            "removed": self.removed,
            "findings": items,
        })
    }
}

pub fn parse_review_args(arg: &str) -> Result<(ReviewTarget, bool), String> {
    let tokens: Vec<&str> = arg.split_whitespace().collect();
    let mut as_json = false;
    let mut rest: Vec<&str> = Vec::new();
    for t in tokens {
        if t == "--json" {
            as_json = true;
        } else {
            rest.push(t);
        }
    }
    if rest.is_empty() {
        return Ok((ReviewTarget::Uncommitted, as_json));
    }
    match rest.as_slice() {
        ["--staged"] => Ok((ReviewTarget::Staged, as_json)),
        ["--base", branch] => Ok((ReviewTarget::Base((*branch).to_owned()), as_json)),
        ["--commit", sha] => Ok((ReviewTarget::Commit((*sha).to_owned()), as_json)),
        [path] if !path.starts_with('-') => Ok((ReviewTarget::File((*path).to_owned()), as_json)),
        _ => Err(
            "Usage: /review [--staged | --base BRANCH | --commit SHA | PATH] [--json]".to_owned(),
        ),
    }
}

fn run_git(workspace: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new("git")
        .args(args)
        .current_dir(workspace)
        .output()
        .map_err(|e| format!("failed to run git: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr).trim().to_owned();
        return Err(if err.is_empty() {
            format!("git {:?} exited with {}", args, out.status)
        } else {
            err
        });
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

fn collect_diff(workspace: &Path, target: &ReviewTarget) -> Result<String, String> {
    match target {
        ReviewTarget::Staged => run_git(workspace, &["diff", "--cached", "--unified=3"]),
        ReviewTarget::Base(branch) => {
            let range = format!("{branch}...HEAD");
            run_git(workspace, &["diff", "--unified=3", &range])
        }
        ReviewTarget::Commit(sha) => run_git(workspace, &["show", "--format=", "--unified=3", sha]),
        ReviewTarget::File(path) => {
            run_git(workspace, &["diff", "HEAD", "--unified=3", "--", path])
        }
        ReviewTarget::Uncommitted => {
            let mut combined = run_git(workspace, &["diff", "HEAD", "--unified=3"])
                .or_else(|_| run_git(workspace, &["diff", "--unified=3"]))?;
            if let Ok(untracked) =
                run_git(workspace, &["ls-files", "--others", "--exclude-standard"])
            {
                for rel in untracked
                    .lines()
                    .map(str::trim)
                    .filter(|l| !l.is_empty())
                    .take(30)
                {
                    let full = workspace.join(rel);
                    if let Ok(text) = std::fs::read_to_string(&full) {
                        if text.len() <= 64_000 {
                            combined.push_str(&format!(
                                "\ndiff --git a/{rel} b/{rel}\n+++ b/{rel}\n@@ -0,0 +1,{} @@\n",
                                text.lines().count()
                            ));
                            for line in text.lines() {
                                combined.push('+');
                                combined.push_str(line);
                                combined.push('\n');
                            }
                        }
                    }
                }
            }
            Ok(combined)
        }
    }
}

fn is_sensitive_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    let name = Path::new(&lower)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("");
    name == ".env"
        || name.starts_with(".env.") && !name.ends_with(".example") && !name.ends_with(".template")
        || name.ends_with(".pem")
        || name.ends_with(".key")
        || name == "id_rsa"
        || name == "id_ed25519"
        || name == "credentials.json"
}

fn is_test_path(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    lower.contains("/tests/")
        || lower.starts_with("tests/")
        || lower.ends_with("_test.rs")
        || lower.ends_with(".test.ts")
        || lower.ends_with("_test.py")
        || lower.contains("test_")
}

pub fn analyze_diff(target_desc: String, diff: &str) -> ReviewReport {
    let mut files: Vec<String> = Vec::new();
    let mut findings: Vec<ReviewFinding> = Vec::new();
    let mut current_file = String::new();
    let mut current_line: usize = 0;
    let mut added: usize = 0;
    let mut removed: usize = 0;
    let mut file_added: usize = 0;

    let flush_file_size = |file: &str, f_added: usize, out: &mut Vec<ReviewFinding>| {
        if !file.is_empty() && f_added > 400 {
            out.push(ReviewFinding {
                priority: "P2",
                title: "Oversized single-file change".into(),
                file: file.to_owned(),
                line: 1,
                detail: format!(
                    "{f_added} lines added in one file; consider splitting into smaller modules."
                ),
            });
        }
    };

    for raw in diff.lines() {
        if let Some(rest) = raw.strip_prefix("+++ b/") {
            flush_file_size(&current_file, file_added, &mut findings);
            current_file = rest.trim().to_owned();
            file_added = 0;
            if !files.contains(&current_file) {
                files.push(current_file.clone());
            }
            if is_sensitive_path(&current_file) {
                findings.push(ReviewFinding {
                    priority: "P0",
                    title: "Sensitive secret/credential file in diff".into(),
                    file: current_file.clone(),
                    line: 1,
                    detail: "Never commit .env, private keys, or credential files.".into(),
                });
            }
            continue;
        }
        if let Some(hunk) = raw.strip_prefix("@@ ") {
            // Parse "+<start>" from "@@ -a,b +c,d @@"
            if let Some(plus_part) = hunk.split_whitespace().find(|s| s.starts_with('+')) {
                let start_str = plus_part
                    .trim_start_matches('+')
                    .split(',')
                    .next()
                    .unwrap_or("1");
                current_line = start_str.parse::<usize>().unwrap_or(1).saturating_sub(1);
            }
            continue;
        }
        if raw.starts_with('+') && !raw.starts_with("+++") {
            added += 1;
            file_added += 1;
            current_line += 1;
            let body = &raw[1..];
            let trimmed = body.trim();
            let in_test = is_test_path(&current_file);

            if body.contains("-----BEGIN PRIVATE KEY-----")
                || body.contains("-----BEGIN RSA PRIVATE KEY-----")
                || body.contains("AKIA") && body.len() >= 20
            {
                findings.push(ReviewFinding {
                    priority: "P0",
                    title: "Potential hardcoded secret or private key".into(),
                    file: current_file.clone(),
                    line: current_line,
                    detail: "Move secrets to environment variables or a secret manager.".into(),
                });
            }
            if !in_test && current_file.ends_with(".py") {
                if trimmed == "except:" || trimmed.starts_with("except: ") {
                    findings.push(ReviewFinding {
                        priority: "P1",
                        title: "Bare `except:` swallows KeyboardInterrupt/SystemExit".into(),
                        file: current_file.clone(),
                        line: current_line,
                        detail: "Catch a specific exception type (e.g. `except ValueError:`)."
                            .into(),
                    });
                }
                if body.contains("shell=True") {
                    findings.push(ReviewFinding {
                        priority: "P1",
                        title: "Subprocess call with `shell=True`".into(),
                        file: current_file.clone(),
                        line: current_line,
                        detail:
                            "Pass an argv list without `shell=True` to prevent command injection."
                                .into(),
                    });
                }
                if body.contains("pdb.set_trace()") || body.contains("breakpoint()") {
                    findings.push(ReviewFinding {
                        priority: "P2",
                        title: "Debug breakpoint left in Python code".into(),
                        file: current_file.clone(),
                        line: current_line,
                        detail: "Remove interactive debugger call before merging.".into(),
                    });
                }
            }
            if !in_test && current_file.ends_with(".rs") && body.contains("dbg!(") {
                findings.push(ReviewFinding {
                    priority: "P2",
                    title: "Debug `dbg!()` macro left in Rust code".into(),
                    file: current_file.clone(),
                    line: current_line,
                    detail: "Remove `dbg!()` or replace with structured logging.".into(),
                });
            }
            if trimmed.contains("TODO:") || trimmed.contains("FIXME:") {
                findings.push(ReviewFinding {
                    priority: "P3",
                    title: "Unresolved TODO/FIXME added".into(),
                    file: current_file.clone(),
                    line: current_line,
                    detail: trimmed.chars().take(100).collect(),
                });
            }
        } else if raw.starts_with('-') && !raw.starts_with("---") {
            removed += 1;
        } else if !raw.starts_with('\\') {
            current_line += 1;
        }
    }
    flush_file_size(&current_file, file_added, &mut findings);

    ReviewReport {
        target: target_desc,
        files,
        added,
        removed,
        findings,
    }
}

pub fn format_review_report(report: &ReviewReport, zh: bool) -> String {
    if report.files.is_empty() && report.added == 0 && report.removed == 0 {
        return if zh {
            "没有可审查的改动。".to_owned()
        } else {
            "No changes to review.".to_owned()
        };
    }
    let mut lines = Vec::new();
    let verdict_label = match (report.verdict(), zh) {
        ("approved", true) => "✓ 通过 (approved)",
        ("approved", false) => "✓ Approved",
        (_, true) => "✗ 建议修改 (changes_requested)",
        (_, false) => "✗ Changes requested",
    };
    lines.push(format!(
        "### Native Rust `/review` — {verdict_label}\n- Target: {} · {} files · +{} −{}",
        report.target,
        report.files.len(),
        report.added,
        report.removed
    ));
    if report.findings.is_empty() {
        lines.push(if zh {
            "- 未发现静态/安全问题。".to_owned()
        } else {
            "- No static or security findings.".to_owned()
        });
    } else {
        for (i, f) in report.findings.iter().enumerate() {
            lines.push(format!(
                "{}. `[{}]` **{}** (`{}:{}`)\n   - {}",
                i + 1,
                f.priority,
                f.title,
                f.file,
                f.line,
                f.detail
            ));
        }
    }
    lines.join("\n")
}

pub fn handle_review_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let (target, as_json) = match parse_review_args(arg) {
        Ok(parsed) => parsed,
        Err(usage) => return (usage, "failed"),
    };
    let diff = match collect_diff(workspace, &target) {
        Ok(d) => d,
        Err(err) => return (format!("Review error: {err}"), "failed"),
    };
    let report = analyze_diff(target.describe(), &diff);
    let text = if as_json {
        serde_json::to_string_pretty(&report.to_json()).unwrap_or_else(|_| "{}".to_owned())
    } else {
        format_review_report(&report, zh)
    };
    (text, "completed")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_review_targets_and_flags() {
        assert_eq!(
            parse_review_args("").unwrap(),
            (ReviewTarget::Uncommitted, false)
        );
        assert_eq!(
            parse_review_args("--staged --json").unwrap(),
            (ReviewTarget::Staged, true)
        );
        assert_eq!(
            parse_review_args("--base main").unwrap(),
            (ReviewTarget::Base("main".into()), false)
        );
        assert_eq!(
            parse_review_args("--commit abc123").unwrap(),
            (ReviewTarget::Commit("abc123".into()), false)
        );
    }

    #[test]
    fn analyzes_diff_for_secrets_shell_true_and_clean_verdict() {
        let risky = "\
diff --git a/app.py b/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 import subprocess
+subprocess.run(cmd, shell=True)
+except:
+    pass
";
        let report = analyze_diff("staged".into(), risky);
        assert_eq!(report.verdict(), "changes_requested");
        assert_eq!(report.files, vec!["app.py"]);
        assert_eq!(report.added, 3);
        assert!(report
            .findings
            .iter()
            .any(|f| f.priority == "P1" && f.title.contains("shell=True")));
        assert!(report
            .findings
            .iter()
            .any(|f| f.priority == "P1" && f.title.contains("Bare `except:`")));

        let clean = "\
diff --git a/lib.rs b/lib.rs
+++ b/lib.rs
@@ -1,1 +1,2 @@
 pub fn add(a: i32, b: i32) -> i32 { a + b }
+pub fn sub(a: i32, b: i32) -> i32 { a - b }
";
        let ok_report = analyze_diff("staged".into(), clean);
        assert_eq!(ok_report.verdict(), "approved");
        assert!(ok_report.findings.is_empty());
    }
}
