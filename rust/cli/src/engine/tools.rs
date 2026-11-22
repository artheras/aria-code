//! Native Rust workspace tools with strict path confinement and write gating.

use serde_json::{json, Value};
use std::{
    fs,
    io::Read,
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
    walk_files(workspace, rel, max_entries.clamp(1, 500))
}

/// Walk workspace files for search and glob. The listing tool stays capped at 500
/// so a model turn is not flooded; search must see past that cap.
fn walk_files(workspace: &Path, rel: &str, max_entries: usize) -> Result<Vec<String>, String> {
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
    collect_files(&base, &target, max_entries.clamp(1, 8_000), &mut out);
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileSlice {
    pub content: String,
    pub total_lines: usize,
    pub truncated: bool,
}

#[cfg(test)]
pub fn read_file(
    workspace: &Path,
    rel: &str,
    start_line: usize,
    end_line: usize,
) -> Result<String, String> {
    read_file_slice(workspace, rel, start_line, end_line).map(|slice| slice.content)
}

pub fn read_file_slice(
    workspace: &Path,
    rel: &str,
    start_line: usize,
    end_line: usize,
) -> Result<FileSlice, String> {
    let path = resolve_workspace_path(workspace, rel)?;
    let content = fs::read_to_string(&path).map_err(|e| format!("Read failed: {e}"))?;
    let lines: Vec<&str> = content.lines().collect();
    let total_lines = lines.len();
    let start = start_line.saturating_sub(1).min(total_lines);
    let end = if end_line == 0 {
        (start + 200).min(total_lines)
    } else {
        end_line.max(start).min(total_lines)
    };
    let numbered: Vec<String> = lines[start..end]
        .iter()
        .enumerate()
        .map(|(idx, line)| format!("{}: {}", start + idx + 1, line))
        .collect();
    Ok(FileSlice {
        content: numbered.join("\n"),
        total_lines,
        truncated: end < total_lines,
    })
}

fn is_probably_text(path: &Path) -> bool {
    let mut file = match fs::File::open(path) {
        Ok(file) => file,
        Err(_) => return false,
    };
    let mut buf = [0u8; 512];
    let n = file.read(&mut buf).unwrap_or(0);
    !buf[..n].contains(&0)
}

pub fn search_code(
    workspace: &Path,
    query: &str,
    max_hits: usize,
    scope: Option<&str>,
    file_glob: Option<&str>,
) -> Result<Vec<String>, String> {
    if query.trim().is_empty() {
        return Err("Search query cannot be empty".into());
    }
    let root = scope
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(".");
    let files = walk_files(workspace, root, 8_000)?;
    let base = workspace.canonicalize().map_err(|e| e.to_string())?;
    let limit = max_hits.clamp(1, 100);
    let glob = file_glob.map(str::trim).filter(|s| !s.is_empty());
    let mut hits = Vec::new();
    for rel in files {
        if hits.len() >= limit {
            break;
        }
        let norm = rel.replace('\\', "/");
        if let Some(pat) = glob {
            let base_name = Path::new(&norm)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("");
            let matched =
                glob_match(pat, &norm) || (!pat.contains('/') && glob_match(pat, base_name));
            if !matched {
                continue;
            }
        }
        let path = base.join(&rel);
        if path.metadata().map(|m| m.len() > 1_000_000).unwrap_or(true) || !is_probably_text(&path)
        {
            continue;
        }
        let Ok(content) = fs::read_to_string(&path) else {
            continue;
        };
        for (idx, line) in content.lines().enumerate() {
            if line.contains(query) {
                hits.push(format!("{norm}:{}: {}", idx + 1, line.trim()));
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

fn match_count(haystack: &str, needle: &str) -> usize {
    if needle.is_empty() {
        return 0;
    }
    let mut count = 0;
    let mut start = 0;
    while let Some(pos) = haystack[start..].find(needle) {
        count += 1;
        start += pos + needle.len();
    }
    count
}

fn change_preview(before: &str, after: &str) -> String {
    let before_lines: Vec<&str> = before.lines().collect();
    let after_lines: Vec<&str> = after.lines().collect();
    let mut start = 0;
    while start < before_lines.len()
        && start < after_lines.len()
        && before_lines[start] == after_lines[start]
    {
        start += 1;
    }
    let mut before_end = before_lines.len();
    let mut after_end = after_lines.len();
    while before_end > start
        && after_end > start
        && before_lines[before_end - 1] == after_lines[after_end - 1]
    {
        before_end -= 1;
        after_end -= 1;
    }
    let mut out = String::new();
    const CAP: usize = 12;
    for (idx, line) in before_lines[start..before_end].iter().take(CAP).enumerate() {
        out.push_str(&format!("-{}: {line}\n", start + idx + 1));
    }
    if before_end.saturating_sub(start) > CAP {
        out.push_str(&format!(
            "... {} more removed lines\n",
            before_end - start - CAP
        ));
    }
    for (idx, line) in after_lines[start..after_end].iter().take(CAP).enumerate() {
        out.push_str(&format!("+{}: {line}\n", start + idx + 1));
    }
    if after_end.saturating_sub(start) > CAP {
        out.push_str(&format!(
            "... {} more added lines\n",
            after_end - start - CAP
        ));
    }
    if out.is_empty() {
        out.push_str("(no line changes)\n");
    }
    out
}

pub fn write_file(
    workspace: &Path,
    rel: &str,
    content: &str,
    allow_write: bool,
) -> Result<Value, String> {
    if !allow_write {
        return Err("Write denied: --allow-write is required for file modifications".into());
    }
    let path = resolve_workspace_path(workspace, rel)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Create parent dir failed: {e}"))?;
    }
    let before = if path.exists() {
        fs::read_to_string(&path).unwrap_or_default()
    } else {
        String::new()
    };
    save_checkpoint(workspace, &path, rel);
    fs::write(&path, content).map_err(|e| format!("Write failed: {e}"))?;
    Ok(json!({
        "message": format!("Wrote {} bytes to {}", content.len(), rel),
        "diff": change_preview(&before, content),
    }))
}

pub fn edit_file(
    workspace: &Path,
    rel: &str,
    old_text: &str,
    new_text: &str,
    allow_write: bool,
    replace_all: bool,
) -> Result<Value, String> {
    if !allow_write {
        return Err("Edit denied: --allow-write is required for file modifications".into());
    }
    if old_text.is_empty() {
        return Err("old_string cannot be empty".into());
    }
    let path = resolve_workspace_path(workspace, rel)?;
    let content = fs::read_to_string(&path).map_err(|e| format!("Read failed: {e}"))?;
    let matches = match_count(&content, old_text);
    if matches == 0 {
        return Err("Target text not found in file".into());
    }
    if matches > 1 && !replace_all {
        return Err(format!(
            "old_string matched {matches} times in {rel}; include more surrounding context, or set replace_all"
        ));
    }
    let updated = if replace_all {
        content.replace(old_text, new_text)
    } else {
        content.replacen(old_text, new_text, 1)
    };
    save_checkpoint(workspace, &path, rel);
    fs::write(&path, &updated).map_err(|e| format!("Write failed: {e}"))?;
    Ok(json!({
        "message": format!("Updated {rel}"),
        "replacements": if replace_all { matches } else { 1 },
        "diff": change_preview(&content, &updated),
    }))
}

/// Apply several unique replacements to one file, or leave the file unchanged.
pub fn multi_edit(
    workspace: &Path,
    rel: &str,
    edits: &[(String, String)],
    allow_write: bool,
) -> Result<Value, String> {
    if !allow_write {
        return Err("Edit denied: --allow-write is required for file modifications".into());
    }
    if edits.is_empty() {
        return Err("multi_edit requires at least one edit".into());
    }
    let path = resolve_workspace_path(workspace, rel)?;
    let original = fs::read_to_string(&path).map_err(|e| format!("Read failed: {e}"))?;
    let mut updated = original.clone();
    for (idx, (old_text, new_text)) in edits.iter().enumerate() {
        if old_text.is_empty() {
            return Err(format!("edit {idx} old_string cannot be empty"));
        }
        let matches = match_count(&updated, old_text);
        if matches == 0 {
            return Err(format!(
                "edit {idx} target text not found in {rel}; no changes written"
            ));
        }
        if matches > 1 {
            return Err(format!(
                "edit {idx} old_string matched {matches} times in {rel}; include more surrounding context. No changes written"
            ));
        }
        updated = updated.replacen(old_text, new_text, 1);
    }
    save_checkpoint(workspace, &path, rel);
    fs::write(&path, &updated).map_err(|e| format!("Write failed: {e}"))?;
    Ok(json!({
        "message": format!("Applied {} edits to {rel}", edits.len()),
        "replacements": edits.len(),
        "diff": change_preview(&original, &updated),
    }))
}

/// True when the workspace has a runner `verify_workspace` knows how to start.
pub fn has_verifier(workspace: &Path) -> bool {
    let Ok(base) = workspace.canonicalize() else {
        return false;
    };
    base.join("Cargo.toml").is_file()
        || base.join("rust/Cargo.toml").is_file()
        || base.join("pyproject.toml").is_file()
        || base.join("pytest.ini").is_file()
        || base.join("tests").is_dir()
        || base.join("package.json").is_file()
        || base.join("go.mod").is_file()
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
    let mode = super::sandbox::SandboxMode::from_env();
    let output = super::sandbox::build_sandboxed_shell_command(&base, cmd, mode)
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
            let rel_wt = std::path::PathBuf::from(".aria")
                .join("worktrees")
                .join(&wt_name);
            let _ = fs::create_dir_all(base.join(".aria").join("worktrees"));
            let branch = format!("aria-wt/{wt_name}");
            let output = Command::new("git")
                .args(["worktree", "add", "-b", &branch])
                .arg(&rel_wt)
                .current_dir(&base)
                .output()
                .map_err(|e| format!("Failed to create git worktree: {e}"))?;
            if !output.status.success() {
                return Err(truncate_utf8(&output.stderr, 4096));
            }
            Ok(json!({
                "created": rel_wt.to_string_lossy(),
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
            let rel_wt = std::path::PathBuf::from(".aria")
                .join("worktrees")
                .join(&wt_name);
            let output = Command::new("git")
                .args(["worktree", "remove", "--force"])
                .arg(&rel_wt)
                .current_dir(&base)
                .output()
                .map_err(|e| format!("Failed to remove git worktree: {e}"))?;
            if !output.status.success() {
                return Err(truncate_utf8(&output.stderr, 4096));
            }
            Ok(json!({ "removed": rel_wt.to_string_lossy() }))
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
    let all = walk_files(workspace, ".", limit.saturating_mul(20).clamp(500, 8_000))?;
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
                "description": "Search for literal text across workspace files. Optionally restrict to a directory and a glob.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string" },
                        "path": { "type": "string", "description": "Directory or file to search (default workspace root)" },
                        "glob": { "type": "string", "description": "Optional glob such as '*.rs' or 'src/**/*.py'" }
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
                "description": "Replace an exact string in a workspace file. Fails when old_string matches more than once unless replace_all is true.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "old_string": { "type": "string" },
                        "new_string": { "type": "string" },
                        "replace_all": { "type": "boolean" }
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "multi_edit",
                "description": "Apply several exact replacements to one file. Every old_string must match once in sequence; if any edit fails, the file is left unchanged.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_string": { "type": "string" },
                                    "new_string": { "type": "string" }
                                },
                                "required": ["old_string", "new_string"]
                            }
                        }
                    },
                    "required": ["path", "edits"]
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
        },
        {
            "type": "function",
            "function": {
                "name": "process",
                "description": "Manage background shell processes (start, read, write, stop, list).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": { "type": "string", "enum": ["start", "read", "write", "stop", "list"] },
                        "command": { "type": "string" },
                        "id": { "type": "string" },
                        "input": { "type": "string" }
                    },
                    "required": ["action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "lsp_hover",
                "description": "Query type/doc hover info at a 0-based (line, character) position via LSP.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "line": { "type": "integer" },
                        "character": { "type": "integer" }
                    },
                    "required": ["path", "line", "character"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "lsp_definition",
                "description": "Go to symbol definition at a 0-based (line, character) position via LSP.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "line": { "type": "integer" },
                        "character": { "type": "integer" }
                    },
                    "required": ["path", "line", "character"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "lsp_references",
                "description": "Find symbol references at a 0-based (line, character) position via LSP.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": { "type": "string" },
                        "line": { "type": "integer" },
                        "character": { "type": "integer" }
                    },
                    "required": ["path", "line", "character"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "spawn_subagent",
                "description": "Delegate a bounded coding or research subtask to a native subagent (optionally inside an isolated git worktree).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "role": { "type": "string", "enum": ["research", "coder", "reviewer"] },
                        "prompt": { "type": "string" },
                        "max_rounds": { "type": "integer" },
                        "isolated_worktree": { "type": "boolean" }
                    },
                    "required": ["prompt"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web via Brave, Tavily, or Google Programmable Search.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "Search query" },
                        "num_results": { "type": "integer", "description": "Max results (1-10)" }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch a web page over HTTP/HTTPS and return extracted plain text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": { "type": "string", "description": "http:// or https:// URL" },
                        "max_chars": { "type": "integer", "description": "Max characters to return (default 8000)" }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_skill",
                "description": "List available workspace/global skills (when name is omitted) or load a skill's full SKILL.md instructions by name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": { "type": "string", "description": "Optional skill name to load" }
                    }
                }
            }
        }
    ])
}

fn global_process_manager() -> &'static std::sync::Mutex<super::processes::ProcessManager> {
    static MGR: std::sync::OnceLock<std::sync::Mutex<super::processes::ProcessManager>> =
        std::sync::OnceLock::new();
    MGR.get_or_init(|| std::sync::Mutex::new(super::processes::ProcessManager::new()))
}

fn execute_process_tool(
    workspace: &Path,
    args: &Value,
    allow_write: bool,
) -> Result<Value, String> {
    let action = args.get("action").and_then(Value::as_str).unwrap_or("list");
    let mut mgr = global_process_manager()
        .lock()
        .map_err(|_| "Process manager lock poisoned".to_owned())?;
    match action {
        "list" => Ok(json!({ "processes": mgr.list() })),
        "read" => {
            let id = args.get("id").and_then(Value::as_str).unwrap_or("");
            mgr.read_output(id)
        }
        "start" => {
            if !allow_write {
                return Err("Background process start denied: --allow-write is required".into());
            }
            let cmd = args.get("command").and_then(Value::as_str).unwrap_or("");
            validate_command(cmd)?;
            let id = mgr.start(workspace, cmd)?;
            Ok(json!({ "id": id, "started": true }))
        }
        "write" => {
            if !allow_write {
                return Err("Background process write denied: --allow-write is required".into());
            }
            let id = args.get("id").and_then(Value::as_str).unwrap_or("");
            let input = args.get("input").and_then(Value::as_str).unwrap_or("");
            mgr.write_input(id, input)?;
            Ok(json!({ "id": id, "written": true }))
        }
        "stop" => {
            if !allow_write {
                return Err("Background process stop denied: --allow-write is required".into());
            }
            let id = args.get("id").and_then(Value::as_str).unwrap_or("");
            mgr.stop(id)
        }
        other => Err(format!("Unknown process action: {other}")),
    }
}

fn execute_lsp_tool(workspace: &Path, kind: &str, args: &Value) -> Result<Value, String> {
    let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
    let line = args.get("line").and_then(Value::as_u64).unwrap_or(0) as u32;
    let character = args.get("character").and_then(Value::as_u64).unwrap_or(0) as u32;
    let path = resolve_workspace_path(workspace, rel)?;
    let spec = super::lsp::server_for_path(&path)
        .ok_or_else(|| format!("No LSP server configured for `{rel}`"))?;
    let text = fs::read_to_string(&path).map_err(|e| format!("Failed to read `{rel}`: {e}"))?;
    let mut client = super::lsp::LspClient::start(&spec, workspace)?;
    client.did_open(&path, spec.language_id, &text)?;
    match kind {
        "hover" => client.hover(&path, line, character),
        "definition" => client.definition(&path, line, character),
        "references" => client.references(&path, line, character),
        _ => Err(format!("Unknown LSP query: {kind}")),
    }
}

fn execute_subagent_tool(
    workspace: &Path,
    args: &Value,
    allow_write: bool,
) -> Result<Value, String> {
    let prompt = args
        .get("prompt")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if prompt.is_empty() {
        return Err("spawn_subagent requires a non-empty prompt".into());
    }
    let role = super::subagent::SubagentRole::from_str(
        args.get("role")
            .and_then(Value::as_str)
            .unwrap_or("research"),
    );
    let isolated_worktree = args
        .get("isolated_worktree")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if (role.allow_write() || isolated_worktree) && !allow_write {
        return Err("Subagent write/worktree mode denied: --allow-write is required".into());
    }
    let max_rounds = args.get("max_rounds").and_then(Value::as_u64).unwrap_or(4) as usize;
    let base_url = std::env::var("OLLAMA_HOST")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "http://localhost:11434".to_owned());
    let model = std::env::var("ARIA_MODEL")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "qwen2.5-coder:7b".to_owned());
    let req = super::subagent::SubagentRequest {
        role,
        prompt: prompt.to_owned(),
        max_rounds,
        isolated_worktree,
    };
    let out = super::subagent::run_local_subagent(
        &base_url,
        &model,
        workspace,
        &req,
        std::time::Duration::from_secs(60),
    );
    Ok(out.to_json())
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
            read_file_slice(workspace, rel, start, end).map(|slice| {
                json!({
                    "content": slice.content,
                    "total_lines": slice.total_lines,
                    "truncated": slice.truncated,
                })
            })
        }
        "search_code" => {
            let query = args.get("query").and_then(Value::as_str).unwrap_or("");
            let scope = args.get("path").and_then(Value::as_str);
            let glob = args.get("glob").and_then(Value::as_str);
            search_code(workspace, query, 50, scope, glob).map(|hits| json!({ "hits": hits }))
        }
        "write_file" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
            let content = args.get("content").and_then(Value::as_str).unwrap_or("");
            write_file(workspace, rel, content, allow_write)
        }
        "edit_file" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
            let old_s = args.get("old_string").and_then(Value::as_str).unwrap_or("");
            let new_s = args.get("new_string").and_then(Value::as_str).unwrap_or("");
            let replace_all = args
                .get("replace_all")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            edit_file(workspace, rel, old_s, new_s, allow_write, replace_all)
        }
        "multi_edit" => {
            let rel = args.get("path").and_then(Value::as_str).unwrap_or("");
            let edits = args
                .get("edits")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .map(|item| {
                            (
                                item.get("old_string")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_owned(),
                                item.get("new_string")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_owned(),
                            )
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            multi_edit(workspace, rel, &edits, allow_write)
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
        "process" => execute_process_tool(workspace, args, allow_write),
        "lsp_hover" => execute_lsp_tool(workspace, "hover", args),
        "lsp_definition" => execute_lsp_tool(workspace, "definition", args),
        "lsp_references" => execute_lsp_tool(workspace, "references", args),
        "spawn_subagent" => execute_subagent_tool(workspace, args, allow_write),
        "web_search" => execute_web_search(args),
        "web_fetch" => execute_web_fetch(args),
        "read_skill" => execute_read_skill(workspace, args),
        other => Err(format!("Unknown native tool: {other}")),
    };
    match result {
        Ok(data) => json!({ "ok": true, "data": data }),
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

fn execute_read_skill(workspace: &Path, args: &Value) -> Result<Value, String> {
    let name = args
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if name.is_empty() {
        let mut all = super::skills::discover_skills(workspace);
        for (builtin, _) in super::skills::builtin_ecosystem_skills() {
            if !all
                .iter()
                .any(|s| s.name.eq_ignore_ascii_case(&builtin.name))
            {
                all.push(builtin);
            }
        }
        let list: Vec<Value> = all
            .into_iter()
            .map(|s| {
                json!({
                    "name": s.name,
                    "description": s.description,
                    "scope": s.scope,
                    "path": s.path.to_string_lossy(),
                })
            })
            .collect();
        return Ok(json!({ "skills": list }));
    }
    let (info, content) = super::skills::load_skill_content(workspace, name)?;
    Ok(json!({
        "name": info.name,
        "description": info.description,
        "scope": info.scope,
        "path": info.path.to_string_lossy(),
        "content": content,
    }))
}

fn resolve_search_key(env_var: &str, provider: &str) -> Option<String> {
    if let Ok(v) = std::env::var(env_var) {
        let trimmed = v.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_owned());
        }
    }
    if cfg!(test) {
        return None;
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    for dir in [".arthera", ".aria"] {
        let path = Path::new(&home).join(dir).join("providers.json");
        let Ok(raw) = fs::read_to_string(path) else {
            continue;
        };
        let Ok(val) = serde_json::from_str::<Value>(&raw) else {
            continue;
        };
        for section in ["data", "llm"] {
            if let Some(entry) = val.get(section).and_then(|s| s.get(provider)) {
                if let Some(key) = entry
                    .get("api_key")
                    .and_then(Value::as_str)
                    .or_else(|| entry.as_str())
                {
                    if !key.trim().is_empty() {
                        return Some(key.trim().to_owned());
                    }
                }
            }
        }
    }
    None
}

fn http_agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(std::time::Duration::from_secs(12)))
        .build()
        .new_agent()
}

fn execute_web_search(args: &Value) -> Result<Value, String> {
    let query = args
        .get("query")
        .or_else(|| args.get("q"))
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if query.is_empty() {
        return Err("web_search requires a non-empty query".into());
    }
    let num = args
        .get("num_results")
        .or_else(|| args.get("max_results"))
        .and_then(Value::as_u64)
        .unwrap_or(5)
        .clamp(1, 10) as usize;
    let agent = http_agent();

    if let Some(key) = resolve_search_key("BRAVE_SEARCH_API_KEY", "brave") {
        let num_str = num.to_string();
        if let Ok(mut resp) = agent
            .get("https://api.search.brave.com/res/v1/web/search")
            .query("q", query)
            .query("count", &num_str)
            .header("Accept", "application/json")
            .header("X-Subscription-Token", &key)
            .call()
        {
            if let Ok(body) = resp.body_mut().read_to_string() {
                if let Ok(data) = serde_json::from_str::<Value>(&body) {
                    let results: Vec<Value> = data["web"]["results"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .take(num)
                                .map(|item| {
                                    json!({
                                        "title": item["title"].as_str().unwrap_or(""),
                                        "url": item["url"].as_str().unwrap_or(""),
                                        "snippet": item["description"].as_str().unwrap_or(""),
                                    })
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    if !results.is_empty() {
                        return Ok(
                            json!({ "query": query, "provider": "brave", "results": results }),
                        );
                    }
                }
            }
        }
    }

    if let Some(key) = resolve_search_key("TAVILY_API_KEY", "tavily") {
        let payload = json!({
            "api_key": key,
            "query": query,
            "max_results": num,
            "search_depth": "basic",
        })
        .to_string();
        if let Ok(mut resp) = agent
            .post("https://api.tavily.com/search")
            .header("Content-Type", "application/json")
            .send(payload)
        {
            if let Ok(body) = resp.body_mut().read_to_string() {
                if let Ok(data) = serde_json::from_str::<Value>(&body) {
                    let results: Vec<Value> = data["results"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .take(num)
                                .map(|item| {
                                    json!({
                                        "title": item["title"].as_str().unwrap_or(""),
                                        "url": item["url"].as_str().unwrap_or(""),
                                        "snippet": item["content"].as_str().unwrap_or("").chars().take(300).collect::<String>(),
                                    })
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    if !results.is_empty() {
                        return Ok(
                            json!({ "query": query, "provider": "tavily", "results": results }),
                        );
                    }
                }
            }
        }
    }

    if let (Some(key), Some(cx)) = (
        resolve_search_key("GOOGLE_SEARCH_API_KEY", "google"),
        resolve_search_key("GOOGLE_SEARCH_ENGINE_ID", "google_cx"),
    ) {
        let num_str = num.to_string();
        if let Ok(mut resp) = agent
            .get("https://www.googleapis.com/customsearch/v1")
            .query("key", &key)
            .query("cx", &cx)
            .query("q", query)
            .query("num", &num_str)
            .call()
        {
            if let Ok(body) = resp.body_mut().read_to_string() {
                if let Ok(data) = serde_json::from_str::<Value>(&body) {
                    let results: Vec<Value> = data["items"]
                        .as_array()
                        .map(|arr| {
                            arr.iter()
                                .take(num)
                                .map(|item| {
                                    json!({
                                        "title": item["title"].as_str().unwrap_or(""),
                                        "url": item["link"].as_str().unwrap_or(""),
                                        "snippet": item["snippet"].as_str().unwrap_or(""),
                                    })
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    if !results.is_empty() {
                        return Ok(
                            json!({ "query": query, "provider": "google", "results": results }),
                        );
                    }
                }
            }
        }
    }

    Err("No web search API key configured (set BRAVE_SEARCH_API_KEY, TAVILY_API_KEY, or GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_ENGINE_ID).".into())
}

fn strip_html_to_text(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    let mut in_tag = false;
    let mut skip_depth = 0usize;
    let lower = raw.to_ascii_lowercase();
    let bytes = raw.as_bytes();
    let lower_bytes = lower.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if lower_bytes[i..].starts_with(b"<script") || lower_bytes[i..].starts_with(b"<style") {
            skip_depth = skip_depth.saturating_add(1);
            in_tag = true;
            i += 1;
            continue;
        }
        if lower_bytes[i..].starts_with(b"</script>") || lower_bytes[i..].starts_with(b"</style>") {
            skip_depth = skip_depth.saturating_sub(1);
        }
        let ch = raw[i..].chars().next().unwrap_or(' ');
        if ch == '<' {
            in_tag = true;
        } else if ch == '>' {
            in_tag = false;
            if skip_depth == 0 {
                out.push(' ');
            }
        } else if !in_tag && skip_depth == 0 {
            out.push(ch);
        }
        i += ch.len_utf8();
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn execute_web_fetch(args: &Value) -> Result<Value, String> {
    let url = args
        .get("url")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("web_fetch requires an http:// or https:// URL".into());
    }
    let max_chars = args
        .get("max_chars")
        .and_then(Value::as_u64)
        .unwrap_or(8000)
        .clamp(200, 32000) as usize;
    let agent = http_agent();
    let mut resp = agent
        .get(url)
        .header("User-Agent", "AriaCode/0.5")
        .call()
        .map_err(|e| format!("web_fetch failed: {e}"))?;
    let status = resp.status().as_u16();
    let content_type = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned();
    let body = resp
        .body_mut()
        .read_to_string()
        .map_err(|e| format!("Read body failed: {e}"))?;
    let text = if content_type.contains("html") || body.trim_start().starts_with('<') {
        strip_html_to_text(&body)
    } else {
        body
    };
    let truncated = text.chars().count() > max_chars;
    let snippet: String = text.chars().take(max_chars).collect();
    Ok(json!({
        "url": url,
        "status": status,
        "content_type": content_type,
        "truncated": truncated,
        "content": snippet,
    }))
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
        assert!(edit_file(dir.path(), "note.txt", "world", "Aria", true, false).is_ok());
        let text = read_file(dir.path(), "note.txt", 1, 10).unwrap();
        assert!(text.contains("1: hello Aria"));

        let hits = search_code(dir.path(), "Aria", 10, None, None).unwrap();
        assert_eq!(hits, vec!["note.txt:1: hello Aria"]);
    }

    #[test]
    fn edit_requires_a_unique_match_and_multi_edit_is_atomic() {
        let dir = tempdir().unwrap();
        write_file(dir.path(), "note.txt", "alpha alpha\nbeta\n", true).unwrap();
        let ambiguous = edit_file(dir.path(), "note.txt", "alpha", "ALPHA", true, false);
        assert!(ambiguous.unwrap_err().contains("matched 2 times"));
        assert!(read_file(dir.path(), "note.txt", 1, 5)
            .unwrap()
            .contains("alpha alpha"));

        let replaced = edit_file(dir.path(), "note.txt", "alpha", "ALPHA", true, true).unwrap();
        assert_eq!(replaced["replacements"], 2);
        assert!(read_file(dir.path(), "note.txt", 1, 5)
            .unwrap()
            .contains("ALPHA ALPHA"));

        write_file(dir.path(), "note.txt", "alpha\nbeta\n", true).unwrap();
        let failed = multi_edit(
            dir.path(),
            "note.txt",
            &[
                ("alpha".into(), "ALPHA".into()),
                ("missing".into(), "x".into()),
            ],
            true,
        );
        assert!(failed.unwrap_err().contains("no changes written"));
        assert!(read_file(dir.path(), "note.txt", 1, 5)
            .unwrap()
            .contains("alpha"));

        let applied = multi_edit(
            dir.path(),
            "note.txt",
            &[
                ("alpha".into(), "ALPHA".into()),
                ("beta".into(), "BETA".into()),
            ],
            true,
        )
        .unwrap();
        assert_eq!(applied["replacements"], 2);
        let text = read_file(dir.path(), "note.txt", 1, 5).unwrap();
        assert!(text.contains("ALPHA"));
        assert!(text.contains("BETA"));
    }

    #[test]
    fn read_reports_truncation_and_search_is_scoped() {
        let dir = tempdir().unwrap();
        let body = (1..=250)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        write_file(dir.path(), "long.txt", &body, true).unwrap();
        let slice = read_file_slice(dir.path(), "long.txt", 1, 0).unwrap();
        assert!(slice.truncated);
        assert_eq!(slice.total_lines, 250);
        assert!(slice.content.contains("1: line 1"));
        assert!(!slice.content.contains("250: line 250"));

        for i in 0..520 {
            write_file(dir.path(), &format!("bulk/f{i:04}.txt"), "plain\n", true).unwrap();
        }
        write_file(
            dir.path(),
            "nested/hidden.rs",
            "fn unique_marker() {}\n",
            true,
        )
        .unwrap();
        let hits = search_code(dir.path(), "unique_marker", 10, None, None).unwrap();
        assert_eq!(hits.len(), 1);
        let missed = search_code(dir.path(), "unique_marker", 10, Some("bulk"), None).unwrap();
        assert!(missed.is_empty());
        let rust_only = search_code(dir.path(), "plain", 5, None, Some("*.rs")).unwrap();
        assert!(rust_only.is_empty());
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
        edit_file(dir.path(), "src/app.rs", "alpha", "beta", true, false).unwrap();
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

    #[test]
    fn exposes_process_and_lsp_tools_and_gates_process_start() {
        let schemas = tool_schemas();
        let names: Vec<&str> = schemas
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|v| v["function"]["name"].as_str())
            .collect();
        for expected in [
            "process",
            "lsp_hover",
            "lsp_definition",
            "lsp_references",
            "spawn_subagent",
            "web_search",
            "web_fetch",
        ] {
            assert!(names.contains(&expected), "missing schema for {expected}");
        }

        let dir = tempdir().unwrap();
        let ws_empty = execute_tool(dir.path(), "web_search", &json!({"query": ""}), false);
        assert_eq!(ws_empty["ok"], false);
        let wf_bad = execute_tool(
            dir.path(),
            "web_fetch",
            &json!({"url": "file:///etc/passwd"}),
            false,
        );
        assert_eq!(wf_bad["ok"], false);
        assert_eq!(
            strip_html_to_text("<html><style>body{}</style><body><h1>Hello</h1><script>x()</script><p>World</p></body></html>"),
            "Hello World"
        );
        let sub_denied = execute_tool(
            dir.path(),
            "spawn_subagent",
            &json!({"role": "coder", "prompt": "edit file"}),
            false,
        );
        assert_eq!(sub_denied["ok"], false);
        let denied = execute_tool(
            dir.path(),
            "process",
            &json!({"action": "start", "command": "echo hi"}),
            false,
        );
        assert_eq!(denied["ok"], false);

        let started = execute_tool(
            dir.path(),
            "process",
            &json!({"action": "start", "command": "echo hi_tool"}),
            true,
        );
        assert_eq!(started["ok"], true);
        let id = started["data"]["id"].as_str().unwrap().to_owned();
        let stopped = execute_tool(
            dir.path(),
            "process",
            &json!({"action": "stop", "id": id}),
            true,
        );
        assert_eq!(stopped["ok"], true);
    }

    #[test]
    fn read_skill_lists_and_loads_workspace_skills() {
        let dir = tempfile::tempdir().unwrap();
        let skill_dir = dir.path().join(".claude").join("skills").join("release");
        fs::create_dir_all(&skill_dir).unwrap();
        fs::write(
            skill_dir.join("SKILL.md"),
            "---\nname: release\ndescription: Release workflow\n---\nRun cargo test first.\n",
        )
        .unwrap();

        let listed = execute_tool(dir.path(), "read_skill", &json!({}), false);
        assert_eq!(listed["ok"], true);
        assert_eq!(listed["data"]["skills"][0]["name"], "release");

        let loaded = execute_tool(dir.path(), "read_skill", &json!({"name": "release"}), false);
        assert_eq!(loaded["ok"], true);
        assert!(loaded["data"]["content"]
            .as_str()
            .unwrap()
            .contains("Run cargo test first."));
    }
}
