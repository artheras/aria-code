//! Native Rust workspace tools with strict path confinement and write gating.

use serde_json::{json, Value};
use std::{
    fs,
    path::{Component, Path, PathBuf},
    process::Command,
};

const SKIP_DIRS: &[&str] = &[
    ".git",
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
        assert_eq!(funcs, vec!["src/lib.rs:2: pub fn run_agent() {}"]);
    }
}
