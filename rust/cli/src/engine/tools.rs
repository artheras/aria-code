//! Native Rust workspace tools with strict path confinement and write gating.

use serde_json::{json, Value};
use std::{
    fs,
    path::{Component, Path, PathBuf},
    process::Command,
};

const SKIP_DIRS: &[&str] = &[
    ".git",
    ".aria",
    ".claude",
    ".worktrees",
    "node_modules",
    "target",
    "__pycache__",
    ".venv",
    ".venv-adk",
    "dist",
    "build",
];

/// Confine a relative or workspace-scoped path strictly within `workspace`.
pub fn resolve_workspace_path(workspace: &Path, raw: &str) -> Result<PathBuf, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed == "." {
        return Ok(base);
    }
    let req = Path::new(trimmed);
    let candidate = if req.is_absolute() {
        req.to_path_buf()
    } else {
        let mut clean = base.clone();
        for comp in req.components() {
            match comp {
                Component::CurDir => {}
                Component::Normal(seg) => clean.push(seg),
                Component::ParentDir => {
                    if !clean.pop() || !clean.starts_with(&base) {
                        return Err("Path escapes workspace".into());
                    }
                }
                Component::RootDir | Component::Prefix(_) => {
                    return Err("Invalid path component".into());
                }
            }
        }
        clean
    };

    if let Ok(resolved) = candidate.canonicalize() {
        if !resolved.starts_with(&base) {
            return Err("Path escapes workspace".into());
        }
        return Ok(resolved);
    }

    // New file path: verify nearest existing ancestor is inside `base`.
    let mut probe = candidate.as_path();
    while let Some(parent) = probe.parent() {
        if parent.exists() {
            let resolved_parent = parent
                .canonicalize()
                .map_err(|e| format!("Invalid parent path: {e}"))?;
            if !resolved_parent.starts_with(&base) {
                return Err("Path escapes workspace".into());
            }
            return Ok(candidate);
        }
        probe = parent;
    }
    Err("Path has no valid ancestor inside workspace".into())
}

pub fn list_files(workspace: &Path, rel: &str, max_entries: usize) -> Result<Vec<String>, String> {
    let normalized = match rel.trim() {
        "" | "/" | "~" | "./" => ".",
        "current" if !workspace.join("current").exists() => ".",
        other => other,
    };
    let target = resolve_workspace_path(workspace, normalized)?;
    let base = workspace.canonicalize().map_err(|e| e.to_string())?;
    if target.is_file() {
        let rel_path = target
            .strip_prefix(&base)
            .unwrap_or(&target)
            .to_string_lossy()
            .into_owned();
        return Ok(vec![rel_path]);
    }
    if !target.is_dir() {
        return Err("Target path does not exist".into());
    }
    let mut out = Vec::new();
    collect_files(&base, &target, max_entries.clamp(1, 500), &mut out);
    Ok(out)
}

fn collect_files(base: &Path, dir: &Path, limit: usize, out: &mut Vec<String>) {
    if out.len() >= limit {
        return;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    let mut sorted: Vec<_> = entries.filter_map(Result::ok).collect();
    sorted.sort_by_key(|e| e.file_name());
    let mut subdirs = Vec::new();
    for entry in sorted {
        if out.len() >= limit {
            break;
        }
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if path.is_dir() {
            if !SKIP_DIRS.contains(&name.as_str()) {
                subdirs.push(path);
            }
        } else if let Ok(rel) = path.strip_prefix(base) {
            out.push(rel.to_string_lossy().into_owned());
        }
    }
    for sub in subdirs {
        if out.len() >= limit {
            break;
        }
        collect_files(base, &sub, limit, out);
    }
}

pub fn read_file(
    workspace: &Path,
    rel: &str,
    start_line: usize,
    end_line: usize,
) -> Result<String, String> {
    let path = resolve_workspace_path(workspace, rel)?;
    let content = fs::read_to_string(&path).map_err(|e| format!("Read failed: {e}"))?;
    let lines: Vec<&str> = content.lines().collect();
    let start = start_line.saturating_sub(1).min(lines.len());
    let end = if end_line == 0 {
        (start + 200).min(lines.len())
    } else {
        end_line.min(lines.len())
    };
    let numbered: Vec<String> = lines[start..end]
        .iter()
        .enumerate()
        .map(|(idx, line)| format!("{}: {}", start + idx + 1, line))
        .collect();
    Ok(numbered.join("\n"))
}

pub fn search_code(workspace: &Path, query: &str, max_hits: usize) -> Result<Vec<String>, String> {
    if query.trim().is_empty() {
        return Err("Search query cannot be empty".into());
    }
    let files = list_files(workspace, ".", 500)?;
    let base = workspace.canonicalize().map_err(|e| e.to_string())?;
    let limit = max_hits.clamp(1, 100);
    let mut hits = Vec::new();
    for rel in files {
        if hits.len() >= limit {
            break;
        }
        let Ok(content) = fs::read_to_string(base.join(&rel)) else {
            continue;
        };
        for (idx, line) in content.lines().enumerate() {
            if line.contains(query) {
                hits.push(format!("{}:{}: {}", rel, idx + 1, line.trim()));
                if hits.len() >= limit {
                    break;
                }
            }
        }
    }
    Ok(hits)
}

fn save_checkpoint(workspace: &Path, target: &Path, rel: &str) {
    let Ok(base) = workspace.canonicalize() else {
        return;
    };
    let ckpt_dir = base.join(".aria").join("checkpoints");
    if fs::create_dir_all(&ckpt_dir).is_err() {
        return;
    }
    let history_path = ckpt_dir.join("history.json");
    let mut stack: Vec<Value> = fs::read_to_string(&history_path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    let existed = target.exists();
    let prev = if existed {
        fs::read_to_string(target).ok()
    } else {
        None
    };
    stack.push(json!({
        "path": rel,
        "existed": existed,
        "content": prev,
    }));
    if stack.len() > 32 {
        stack.drain(..stack.len() - 32);
    }
    let _ = fs::write(
        &history_path,
        serde_json::to_string(&stack).unwrap_or_else(|_| "[]".into()),
    );
}

/// Restore the most recent file edit/write saved in `.aria/checkpoints/history.json`.
pub fn restore_checkpoint(workspace: &Path) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let history_path = base.join(".aria").join("checkpoints").join("history.json");
    let raw = fs::read_to_string(&history_path)
        .map_err(|_| "No checkpoints available to rewind.".to_owned())?;
    let mut stack: Vec<Value> = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    let entry = stack
        .pop()
        .ok_or_else(|| "No checkpoints available to rewind.".to_owned())?;
    let rel = entry["path"]
        .as_str()
        .ok_or("Corrupt checkpoint entry: missing path")?;
    let target = resolve_workspace_path(&base, rel)?;
    let existed = entry["existed"].as_bool().unwrap_or(false);
    if existed {
        let content = entry["content"]
            .as_str()
            .ok_or("Corrupt checkpoint entry: missing content")?;
        if let Some(parent) = target.parent() {
            let _ = fs::create_dir_all(parent);
        }
        fs::write(&target, content).map_err(|e| format!("Restore failed: {e}"))?;
    } else if target.exists() {
        fs::remove_file(&target).map_err(|e| format!("Remove failed: {e}"))?;
    }
    let _ = fs::write(
        &history_path,
        serde_json::to_string(&stack).unwrap_or_else(|_| "[]".into()),
    );
    Ok(json!({ "restored": rel, "existed": existed, "remaining": stack.len() }))
}

pub fn write_file(
    workspace: &Path,
    rel: &str,
    content: &str,
    allow_write: bool,
) -> Result<String, String> {
    if !allow_write {
        return Err("Write denied: --allow-write is required for file modifications".into());
    }
    let path = resolve_workspace_path(workspace, rel)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Create parent dir failed: {e}"))?;
    }
    save_checkpoint(workspace, &path, rel);
    fs::write(&path, content).map_err(|e| format!("Write failed: {e}"))?;
    Ok(format!("Wrote {} bytes to {}", content.len(), rel))
}

pub fn edit_file(
    workspace: &Path,
    rel: &str,
    old_text: &str,
    new_text: &str,
    allow_write: bool,
) -> Result<String, String> {
    if !allow_write {
        return Err("Edit denied: --allow-write is required for file modifications".into());
    }
    let path = resolve_workspace_path(workspace, rel)?;
    let content = fs::read_to_string(&path).map_err(|e| format!("Read failed: {e}"))?;
    if !content.contains(old_text) {
        return Err("Target text not found in file".into());
    }
    save_checkpoint(workspace, &path, rel);
    let updated = content.replacen(old_text, new_text, 1);
    fs::write(&path, &updated).map_err(|e| format!("Write failed: {e}"))?;
    Ok(format!("Updated {}", rel))
}

/// Reject slash commands and high-risk destructive shell patterns before execution.
pub fn validate_command(cmd: &str) -> Result<(), String> {
    let trimmed = cmd.trim();
    if trimmed.is_empty() {
        return Err("Command cannot be empty".into());
    }
    if trimmed.starts_with('/') {
        return Err("Slash commands cannot be passed to run_command".into());
    }
    let low = trimmed.to_ascii_lowercase();
    let blocked = [
        "rm -rf /",
        "rm -fr /",
        "mkfs",
        "dd if=",
        ":(){:|:&};:",
        "shutdown",
        "reboot",
        "chmod -r 777 /",
        "sudo ",
    ];
    if blocked.iter().any(|p| low.contains(p)) {
        return Err("Command blocked by native safety policy".into());
    }
    Ok(())
}

fn is_readonly_command(cmd: &str) -> bool {
    let low = cmd.trim().to_ascii_lowercase();
    let allowed_prefixes = [
        "pwd",
        "ls",
        "git status",
        "git diff",
        "git log",
        "git branch",
        "cargo test",
        "cargo check",
        "pytest",
    ];
    allowed_prefixes
        .iter()
        .any(|p| low == *p || low.starts_with(&format!("{p} ")))
}

pub fn run_command(workspace: &Path, cmd: &str, allow_write: bool) -> Result<Value, String> {
    validate_command(cmd)?;
    if !allow_write && !is_readonly_command(cmd) {
        return Err(
            "Command execution denied: --allow-write is required for non-readonly commands".into(),
        );
    }
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    #[cfg(unix)]
    let output = Command::new("sh")
        .arg("-c")
        .arg(cmd)
        .current_dir(&base)
        .output()
        .map_err(|e| format!("Spawn failed: {e}"))?;
    #[cfg(windows)]
    let output = Command::new("cmd")
        .args(["/C", cmd])
        .current_dir(&base)
        .output()
        .map_err(|e| format!("Spawn failed: {e}"))?;

    let cap = |bytes: &[u8]| -> String {
        let s = String::from_utf8_lossy(bytes);
        if s.len() > 16_384 {
            format!("{}…\n[truncated]", &s[..16_384])
        } else {
            s.into_owned()
        }
    };
    Ok(json!({
        "exit_code": output.status.code().unwrap_or(-1),
        "stdout": cap(&output.stdout),
        "stderr": cap(&output.stderr),
    }))
}

fn truncate_utf8(bytes: &[u8], limit: usize) -> String {
    let s = String::from_utf8_lossy(bytes);
    if s.len() <= limit {
        return s.into_owned();
    }
    let end = s
        .char_indices()
        .take_while(|(idx, _)| *idx <= limit)
        .last()
        .map(|(idx, _)| idx)
        .unwrap_or(0);
    format!("{}…\n[truncated]", &s[..end])
}

/// Read-only `git status --short --branch` inside the workspace.
pub fn git_status(workspace: &Path) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let output = Command::new("git")
        .args(["status", "--short", "--branch"])
        .current_dir(&base)
        .output()
        .map_err(|e| format!("Failed to run git status: {e}"))?;
    if !output.status.success() {
        return Err(truncate_utf8(&output.stderr, 4096));
    }
    Ok(json!({ "status": truncate_utf8(&output.stdout, 16_384) }))
}

/// Read-only `git diff` inside the workspace, optionally scoped to `path` and `staged`.
pub fn git_diff(workspace: &Path, rel: Option<&str>, staged: bool) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let mut cmd = Command::new("git");
    cmd.arg("diff");
    if staged {
        cmd.arg("--cached");
    }
    if let Some(raw_path) = rel.map(str::trim).filter(|s| !s.is_empty()) {
        let confined = resolve_workspace_path(&base, raw_path)?;
        let rel_path = confined
            .strip_prefix(&base)
            .unwrap_or(&confined)
            .to_path_buf();
        cmd.arg("--").arg(rel_path);
    }
    let output = cmd
        .current_dir(&base)
        .output()
        .map_err(|e| format!("Failed to run git diff: {e}"))?;
    if !output.status.success() {
        return Err(truncate_utf8(&output.stderr, 4096));
    }
    Ok(json!({ "diff": truncate_utf8(&output.stdout, 24_576) }))
}

/// Read-only `git log --oneline` inside the workspace, optionally scoped to `path` and `limit`.
pub fn git_log(workspace: &Path, rel: Option<&str>, limit: usize) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let n = limit.clamp(1, 50).to_string();
    let mut cmd = Command::new("git");
    cmd.args(["log", "--oneline", "-n", &n]);
    if let Some(raw_path) = rel.map(str::trim).filter(|s| !s.is_empty()) {
        let confined = resolve_workspace_path(&base, raw_path)?;
        let rel_path = confined
            .strip_prefix(&base)
            .unwrap_or(&confined)
            .to_path_buf();
        cmd.arg("--").arg(rel_path);
    }
    let output = cmd
        .current_dir(&base)
        .output()
        .map_err(|e| format!("Failed to run git log: {e}"))?;
    if !output.status.success() {
        return Err(truncate_utf8(&output.stderr, 4096));
    }
    Ok(json!({ "log": truncate_utf8(&output.stdout, 16_384) }))
}

fn is_ident_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}

fn line_defines_symbol(line: &str, symbol: &str) -> bool {
    const KEYWORDS: &[&str] = &[
        "fn ",
        "struct ",
        "enum ",
        "trait ",
        "type ",
        "def ",
        "class ",
        "function ",
        "interface ",
        "func ",
    ];
    for kw in KEYWORDS {
        let mut rest = line;
        while let Some(pos) = rest.find(kw) {
            let before_ok = pos == 0
                || rest[..pos]
                    .chars()
                    .next_back()
                    .is_none_or(|c| !is_ident_char(c));
            let after = &rest[pos + kw.len()..];
            let trimmed = after.trim_start();
            if before_ok {
                if let Some(tail) = trimmed.strip_prefix(symbol) {
                    if tail.chars().next().is_none_or(|c| !is_ident_char(c)) {
                        return true;
                    }
                }
            }
            rest = after;
        }
    }
    false
}

/// Locate symbol definitions (`fn`, `struct`, `enum`, `trait`, `def`, `class`, `function`, `interface`, `func`) in workspace source files.
pub fn find_symbol(workspace: &Path, symbol: &str, max_hits: usize) -> Result<Vec<String>, String> {
    let sym = symbol.trim();
    if sym.is_empty() {
        return Err("Symbol name cannot be empty".into());
    }
    let files = list_files(workspace, ".", 500)?;
    let base = workspace.canonicalize().map_err(|e| e.to_string())?;
    let limit = max_hits.clamp(1, 100);
    let mut hits = Vec::new();
    for rel in files {
        if hits.len() >= limit {
            break;
        }
        let Ok(content) = fs::read_to_string(base.join(&rel)) else {
            continue;
        };
        for (idx, line) in content.lines().enumerate() {
            if line_defines_symbol(line, sym) {
                hits.push(format!("{}:{}: {}", rel, idx + 1, line.trim()));
                if hits.len() >= limit {
                    break;
                }
            }
        }
    }
    Ok(hits)
}

/// Run the workspace's detected test/verification command (Cargo, pytest, npm, or Go).
pub fn verify_workspace(workspace: &Path, target: Option<&str>) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let rel = target.map(str::trim).filter(|s| !s.is_empty());
    let (runner, program, args): (&str, &str, Vec<String>) = if base.join("Cargo.toml").is_file() {
        let mut a = vec!["test".into(), "--quiet".into()];
        if let Some(t) = rel {
            a.push(t.to_owned());
        }
        ("cargo", "cargo", a)
    } else if base.join("rust/Cargo.toml").is_file() {
        let mut a = vec![
            "test".into(),
            "--manifest-path".into(),
            "rust/Cargo.toml".into(),
            "--quiet".into(),
        ];
        if let Some(t) = rel {
            a.push(t.to_owned());
        }
        ("cargo", "cargo", a)
    } else if base.join("pyproject.toml").is_file()
        || base.join("pytest.ini").is_file()
        || base.join("tests").is_dir()
    {
        let mut a = vec!["-m".into(), "pytest".into(), "-q".into()];
        if let Some(t) = rel {
            let confined = resolve_workspace_path(&base, t)?;
            a.push(
                confined
                    .strip_prefix(&base)
                    .unwrap_or(&confined)
                    .to_string_lossy()
                    .into_owned(),
            );
        }
        let py = if base.join(".venv/bin/python").is_file() {
            ".venv/bin/python"
        } else {
            "python3"
        };
        ("pytest", py, a)
    } else if base.join("package.json").is_file() {
        ("npm", "npm", vec!["test".into(), "--silent".into()])
    } else if base.join("go.mod").is_file() {
        ("go", "go", vec!["test".into(), "./...".into()])
    } else {
        return Err(
            "No supported verification runner detected (Cargo.toml, pyproject.toml/tests, package.json, go.mod)".into(),
        );
    };

    let output = Command::new(program)
        .args(&args)
        .current_dir(&base)
        .output()
        .map_err(|e| format!("Failed to run `{program}`: {e}"))?;
    Ok(json!({
        "runner": runner,
        "command": format!("{program} {}", args.join(" ")),
        "exit_code": output.status.code().unwrap_or(-1),
        "passed": output.status.success(),
        "stdout": truncate_utf8(&output.stdout, 8192),
        "stderr": truncate_utf8(&output.stderr, 8192),
    }))
}

fn extract_top_symbols(content: &str) -> Vec<String> {
    const PREFIXES: &[&str] = &[
        "pub fn ",
        "pub async fn ",
        "fn ",
        "async fn ",
        "pub struct ",
        "struct ",
        "pub enum ",
        "enum ",
        "pub trait ",
        "trait ",
        "def ",
        "async def ",
        "class ",
        "export function ",
        "function ",
        "export interface ",
        "interface ",
        "func ",
    ];
    let mut syms = Vec::new();
    for line in content.lines() {
        let trimmed = line.trim_start();
        if trimmed.len() != line.len() && !trimmed.starts_with("def ") {
            continue;
        }
        if PREFIXES.iter().any(|p| trimmed.starts_with(p)) {
            let sig = trimmed
                .split(['{', ':'])
                .next()
                .unwrap_or(trimmed)
                .trim()
                .to_owned();
            if !sig.is_empty() {
                syms.push(sig);
                if syms.len() >= 25 {
                    break;
                }
            }
        }
    }
    syms
}

/// Build a compact symbol map across workspace source files (`.rs`, `.py`, `.ts`, `.tsx`, `.js`, `.go`).
pub fn repo_map(workspace: &Path, max_files: usize) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let files = list_files(&base, ".", 500)?;
    let limit = max_files.clamp(1, 200);
    let mut mapped = Vec::new();
    for rel in files {
        if mapped.len() >= limit {
            break;
        }
        let is_source = [".rs", ".py", ".ts", ".tsx", ".js", ".go"]
            .iter()
            .any(|ext| rel.ends_with(ext));
        if !is_source {
            continue;
        }
        let Ok(content) = fs::read_to_string(base.join(&rel)) else {
            continue;
        };
        let symbols = extract_top_symbols(&content);
        if !symbols.is_empty() {
            mapped.push(json!({ "path": rel, "symbols": symbols }));
        }
    }
    Ok(json!({ "files": mapped }))
}

fn sanitize_worktree_name(raw: &str) -> Result<String, String> {
    let cleaned: String = raw
        .trim()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .collect();
    if cleaned.is_empty() {
        return Err("Worktree name must contain alphanumeric, '-' or '_' characters".into());
    }
    Ok(cleaned)
}

/// Manage isolated git worktrees under `.aria/worktrees/<name>` (`list`, `create`, `remove`).
pub fn git_worktree(
    workspace: &Path,
    action: &str,
    name: Option<&str>,
    allow_write: bool,
) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    match action.trim().to_ascii_lowercase().as_str() {
        "" | "list" => {
            let output = Command::new("git")
                .args(["worktree", "list", "--porcelain"])
                .current_dir(&base)
                .output()
                .map_err(|e| format!("Failed to list git worktrees: {e}"))?;
            if !output.status.success() {
                return Err(truncate_utf8(&output.stderr, 4096));
            }
            Ok(json!({ "worktrees": truncate_utf8(&output.stdout, 16_384) }))
        }
        "create" => {
            if !allow_write {
                return Err(
                    "Worktree create denied: --allow-write or /write-mode on required".into(),
                );
            }
            let wt_name = sanitize_worktree_name(name.unwrap_or(""))?;
            let wt_dir = base.join(".aria").join("worktrees").join(&wt_name);
            if let Some(parent) = wt_dir.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let branch = format!("aria-wt/{wt_name}");
            let output = Command::new("git")
                .args(["worktree", "add", "-b", &branch])
                .arg(&wt_dir)
                .current_dir(&base)
                .output()
                .map_err(|e| format!("Failed to create git worktree: {e}"))?;
            if !output.status.success() {
                return Err(truncate_utf8(&output.stderr, 4096));
            }
            Ok(json!({
                "created": wt_dir.to_string_lossy(),
                "branch": branch,
            }))
        }
        "remove" => {
            if !allow_write {
                return Err(
                    "Worktree remove denied: --allow-write or /write-mode on required".into(),
                );
            }
            let wt_name = sanitize_worktree_name(name.unwrap_or(""))?;
            let wt_dir = base.join(".aria").join("worktrees").join(&wt_name);
            let output = Command::new("git")
                .args(["worktree", "remove", "--force"])
                .arg(&wt_dir)
                .current_dir(&base)
                .output()
                .map_err(|e| format!("Failed to remove git worktree: {e}"))?;
            if !output.status.success() {
                return Err(truncate_utf8(&output.stderr, 4096));
            }
            Ok(json!({ "removed": wt_dir.to_string_lossy() }))
        }
        other => Err(format!(
            "Unknown worktree action `{other}` (expected list, create, remove)"
        )),
    }
}

fn glob_match(pattern: &str, text: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let t: Vec<char> = text.chars().collect();
    fn rec(p: &[char], t: &[char]) -> bool {
        match (p.first(), t.first()) {
            (None, None) => true,
            (Some('*'), _) => rec(&p[1..], t) || (!t.is_empty() && rec(p, &t[1..])),
            (Some('?'), Some(_)) => rec(&p[1..], &t[1..]),
            (Some(pc), Some(tc)) if pc == tc => rec(&p[1..], &t[1..]),
            _ => false,
        }
    }
    rec(&p, &t)
}

/// Find workspace files matching a glob pattern (`*`, `?`, `**`), bounded by `limit`.
pub fn glob_files(workspace: &Path, pattern: &str, limit: usize) -> Result<Vec<String>, String> {
    let pat = pattern.trim().replace('\\', "/");
    if pat.is_empty() {
        return Err("Empty glob pattern".into());
    }
    let all = list_files(workspace, ".", limit.saturating_mul(10).max(500))?;
    let mut matches: Vec<String> = all
        .into_iter()
        .filter_map(|rel| {
            let norm = rel.replace('\\', "/");
            let matched = glob_match(&pat, &norm)
                || (!pat.contains('/')
                    && Path::new(&norm)
                        .file_name()
                        .and_then(|n| n.to_str())
                        .is_some_and(|n| glob_match(&pat, n)));
            matched.then_some(norm)
        })
        .take(limit)
        .collect();
    matches.sort();
    Ok(matches)
}

/// Detect project languages/manifests and summarize workspace structure natively in Rust.
pub fn project_scan(workspace: &Path) -> Result<Value, String> {
    let base = workspace
        .canonicalize()
        .map_err(|e| format!("Invalid workspace: {e}"))?;
    let files = list_files(&base, ".", 500)?;
    let manifests: &[(&str, &str)] = &[
        ("Cargo.toml", "Rust"),
        ("pyproject.toml", "Python"),
        ("setup.py", "Python"),
        ("package.json", "Node.js"),
        ("go.mod", "Go"),
        ("pom.xml", "Java"),
        ("build.gradle", "Java"),
    ];
    let mut detected_types: Vec<&str> = Vec::new();
    let mut key_files: Vec<String> = Vec::new();
    for (file, lang) in manifests {
        if base.join(file).is_file() {
            if !detected_types.contains(lang) {
                detected_types.push(lang);
            }
            key_files.push((*file).to_owned());
        }
    }
    let git = git_status(&base).ok();
    Ok(json!({
        "workspace": base.to_string_lossy(),
        "project_types": detected_types,
        "key_files": key_files,
        "sampled_files": files.len(),
        "top_files": files.into_iter().take(25).collect::<Vec<_>>(),
        "git_status": git.and_then(|v| v.get("status").cloned()),
    }))
}

pub fn tool_schemas() -> Value {
    json!([
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in the workspace directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string", "description": "Relative directory path (default '.')" }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a text file in the workspace with line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "start_line": { "type": "integer" },
                        "end_line": { "type": "integer" }
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_code",
                "description": "Search for literal text across workspace files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string" }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "content": { "type": "string" }
                    },
                    "required": ["path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace an exact string in a workspace file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "old_string": { "type": "string" },
                        "new_string": { "type": "string" }
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Execute a shell command inside the workspace (readonly checks allowed; mutations require --allow-write).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": { "type": "string" }
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Show concise git status and current branch for the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": "Show git diff for the workspace or a specific path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "staged": { "type": "boolean" }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Find workspace files matching a glob pattern (e.g. '*.rs', 'src/**/*.py').",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": { "type": "string" }
                    },
                    "required": ["pattern"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "project_scan",
                "description": "Scan the workspace to detect project languages, manifests, key files, and git status.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "git_log",
                "description": "Show recent git commit history (`git log --oneline`) for the workspace or a path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "limit": { "type": "integer" }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "find_symbol",
                "description": "Locate function, struct, class, trait, or type definitions by symbol name across the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": { "type": "string" }
                    },
                    "required": ["symbol"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "verify_workspace",
                "description": "Run the project's test/verification suite (cargo test, pytest, npm test, or go test) after making edits.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": { "type": "string", "description": "Optional test file or filter." }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "repo_map",
                "description": "Build a compact symbol outline (functions, structs, classes, interfaces) across workspace source files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_files": { "type": "integer" }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "restore_checkpoint",
                "description": "Rewind the most recent file edit or write made in this workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "git_worktree",
                "description": "List, create, or remove isolated git worktrees under `.aria/worktrees/`.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": { "type": "string", "enum": ["list", "create", "remove"] },
                        "name": { "type": "string" }
                    },
                    "required": ["action"]
                }
            }
        }
    ])
}

pub fn execute_tool(workspace: &Path, name: &str, args: &Value, allow_write: bool) -> Value {
    let result = match name {
        "list_files" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or(".");
            list_files(workspace, rel, 200).map(|files| json!({ "files": files }))
        }
        "read_file" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
            let start = args.get("start_line").and_then(Value::as_u64).unwrap_or(1) as usize;
            let end = args.get("end_line").and_then(Value::as_u64).unwrap_or(0) as usize;
            read_file(workspace, rel, start, end).map(|content| json!({ "content": content }))
        }
        "search_code" => {
            let query = args.get("query").and_then(Value::as_str).unwrap_or("");
            search_code(workspace, query, 50).map(|hits| json!({ "hits": hits }))
        }
        "write_file" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
            let content = args.get("content").and_then(Value::as_str).unwrap_or("");
            write_file(workspace, rel, content, allow_write).map(|msg| json!({ "message": msg }))
        }
        "edit_file" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
            let old_s = args.get("old_string").and_then(Value::as_str).unwrap_or("");
            let new_s = args.get("new_string").and_then(Value::as_str).unwrap_or("");
            edit_file(workspace, rel, old_s, new_s, allow_write)
                .map(|msg| json!({ "message": msg }))
        }
        "run_command" => {
            let cmd = args.get("command").and_then(Value::as_str).unwrap_or("");
            run_command(workspace, cmd, allow_write)
        }
        "git_status" => git_status(workspace),
        "git_diff" => {
            let rel = args.get("path").and_then(Value::as_str);
            let staged = args.get("staged").and_then(Value::as_bool).unwrap_or(false);
            git_diff(workspace, rel, staged)
        }
        "glob" => {
            let pat = args.get("pattern").and_then(Value::as_str).unwrap_or("");
            glob_files(workspace, pat, 200).map(|files| json!({ "files": files }))
        }
        "project_scan" => project_scan(workspace),
        "git_log" => {
            let rel = args.get("path").and_then(Value::as_str);
            let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(15) as usize;
            git_log(workspace, rel, limit)
        }
        "find_symbol" => {
            let sym = args.get("symbol").and_then(Value::as_str).unwrap_or("");
            find_symbol(workspace, sym, 50).map(|hits| json!({ "hits": hits }))
        }
        "verify_workspace" => {
            let target = args.get("target").and_then(Value::as_str);
            verify_workspace(workspace, target)
        }
        "repo_map" => {
            let max_files = args.get("max_files").and_then(Value::as_u64).unwrap_or(60) as usize;
            repo_map(workspace, max_files)
        }
        "restore_checkpoint" => restore_checkpoint(workspace),
        "git_worktree" => {
            let action = args.get("action").and_then(Value::as_str).unwrap_or("list");
            let name = args.get("name").and_then(Value::as_str);
            git_worktree(workspace, action, name, allow_write)
        }
        other => Err(format!("Unknown native tool: {other}")),
    };
    match result {
        Ok(data) => json!({ "ok": true, "data": data }),
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn confines_paths_inside_workspace() {
        let dir = tempdir().unwrap();
        assert!(resolve_workspace_path(dir.path(), "../../etc/passwd").is_err());
        assert!(resolve_workspace_path(dir.path(), "/etc/passwd").is_err());
        assert!(resolve_workspace_path(dir.path(), "src/main.rs").is_ok());
    }

    #[test]
    fn gates_writes_and_supports_read_search_edit() {
        let dir = tempdir().unwrap();
        let denied = write_file(dir.path(), "note.txt", "hello world", false);
        assert!(denied.is_err());

        assert!(write_file(dir.path(), "note.txt", "hello world", true).is_ok());
        assert!(edit_file(dir.path(), "note.txt", "world", "Aria", true).is_ok());
        let text = read_file(dir.path(), "note.txt", 1, 10).unwrap();
        assert!(text.contains("1: hello Aria"));

        let hits = search_code(dir.path(), "Aria", 10).unwrap();
        assert_eq!(hits, vec!["note.txt:1: hello Aria"]);
    }

    #[test]
    fn enforces_command_policy_and_readonly_gating() {
        let dir = tempdir().unwrap();
        assert!(validate_command("/health").is_err());
        assert!(validate_command("rm -rf /").is_err());
        assert!(run_command(dir.path(), "echo hi", false).is_err());
        let ro = run_command(dir.path(), "pwd", false).unwrap();
        assert_eq!(ro["exit_code"], 0);
        let rw = run_command(dir.path(), "echo ok", true).unwrap();
        assert_eq!(rw["exit_code"], 0);
        assert_eq!(rw["stdout"].as_str().unwrap().trim(), "ok");
    }

    #[test]
    fn git_status_and_diff_work_in_git_repo() {
        let dir = tempdir().unwrap();
        assert!(Command::new("git")
            .args(["init"])
            .current_dir(dir.path())
            .output()
            .unwrap()
            .status
            .success());
        write_file(dir.path(), "a.txt", "hello\n", true).unwrap();
        let st = git_status(dir.path()).unwrap();
        assert!(st["status"].as_str().unwrap().contains("a.txt"));
        let df = git_diff(dir.path(), None, false).unwrap();
        assert!(df["diff"].as_str().is_some());
    }

    #[test]
    fn glob_matches_nested_and_basename_patterns() {
        let dir = tempdir().unwrap();
        write_file(dir.path(), "src/lib.rs", "pub fn x() {}", true).unwrap();
        write_file(dir.path(), "README.md", "# hi", true).unwrap();
        let rs = glob_files(dir.path(), "*.rs", 20).unwrap();
        assert_eq!(rs, vec!["src/lib.rs"]);
        let nested = glob_files(dir.path(), "src/*.rs", 20).unwrap();
        assert_eq!(nested, vec!["src/lib.rs"]);
    }

    #[test]
    fn project_scan_detects_manifests_and_counts_files() {
        let dir = tempdir().unwrap();
        write_file(
            dir.path(),
            "Cargo.toml",
            "[package]\nname = \"demo\"\n",
            true,
        )
        .unwrap();
        write_file(dir.path(), "src/main.rs", "fn main() {}\n", true).unwrap();
        let scan = project_scan(dir.path()).unwrap();
        assert_eq!(scan["project_types"], json!(["Rust"]));
        assert_eq!(scan["key_files"], json!(["Cargo.toml"]));
        assert_eq!(scan["sampled_files"], 2);
    }

    #[test]
    fn find_symbol_locates_rust_and_python_definitions() {
        let dir = tempdir().unwrap();
        write_file(
            dir.path(),
            "src/lib.rs",
            "pub struct AgentLoop;\npub fn run_agent() {}\n",
            true,
        )
        .unwrap();
        write_file(
            dir.path(),
            "app.py",
            "class AgentLoop:\n    pass\ndef run_agent_helper():\n    return run_agent()\n",
            true,
        )
        .unwrap();
        let structs = find_symbol(dir.path(), "AgentLoop", 10).unwrap();
        assert_eq!(structs.len(), 2);
        let funcs = find_symbol(dir.path(), "run_agent", 10).unwrap();
        assert_eq!(funcs.len(), 1);
        assert_eq!(
            funcs[0].replace('\\', "/"),
            "src/lib.rs:2: pub fn run_agent() {}"
        );
    }

    #[test]
    fn verify_workspace_reports_when_no_runner_exists() {
        let dir = tempdir().unwrap();
        let err = verify_workspace(dir.path(), None).unwrap_err();
        assert!(err.contains("No supported verification runner"));
    }

    #[test]
    fn checkpoint_and_repo_map_work_together() {
        let dir = tempdir().unwrap();
        write_file(dir.path(), "src/app.rs", "pub fn alpha() {}\n", true).unwrap();
        edit_file(dir.path(), "src/app.rs", "alpha", "beta", true).unwrap();
        assert!(read_file(dir.path(), "src/app.rs", 1, 5)
            .unwrap()
            .contains("beta"));

        let map = repo_map(dir.path(), 10).unwrap();
        let files = map["files"].as_array().unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0]["symbols"][0], "pub fn beta()");

        let restored = restore_checkpoint(dir.path()).unwrap();
        assert_eq!(restored["restored"], "src/app.rs");
        assert!(read_file(dir.path(), "src/app.rs", 1, 5)
            .unwrap()
            .contains("alpha"));
    }

    #[test]
    fn git_worktree_lists_creates_and_removes() {
        let dir = tempdir().unwrap();
        assert!(Command::new("git")
            .args(["init"])
            .current_dir(dir.path())
            .output()
            .unwrap()
            .status
            .success());
        assert!(Command::new("git")
            .args(["config", "user.email", "test@example.com"])
            .current_dir(dir.path())
            .output()
            .unwrap()
            .status
            .success());
        assert!(Command::new("git")
            .args(["config", "user.name", "Tester"])
            .current_dir(dir.path())
            .output()
            .unwrap()
            .status
            .success());
        write_file(dir.path(), "README.md", "# demo\n", true).unwrap();
        assert!(Command::new("git")
            .args(["add", "."])
            .current_dir(dir.path())
            .output()
            .unwrap()
            .status
            .success());
        assert!(Command::new("git")
            .args(["commit", "-m", "init"])
            .current_dir(dir.path())
            .output()
            .unwrap()
            .status
            .success());

        assert!(git_worktree(dir.path(), "create", Some("feat1"), false).is_err());
        let created = git_worktree(dir.path(), "create", Some("feat1"), true).unwrap();
        assert_eq!(created["branch"], "aria-wt/feat1");
        let listed = git_worktree(dir.path(), "list", None, false).unwrap();
        assert!(listed["worktrees"].as_str().unwrap().contains("feat1"));
        let removed = git_worktree(dir.path(), "remove", Some("feat1"), true).unwrap();
        assert!(removed["removed"].as_str().unwrap().contains("feat1"));
    }
}
