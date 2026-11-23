//! Native Rust persistent project memory (`/memory`), notes (`/note`), and task tracker (`/todo`).
//!
//! Replaces the Python `memory_manager.py`, `/memory`, `/note`, and `/todo` handlers with
//! zero-dependency JSON persistence under `<workspace>/.aria/`.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryEntry {
    pub text: String,
    pub scope: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TodoItem {
    pub text: String,
    pub done: bool,
}

fn memory_path(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("memory.json")
}

fn todos_path(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("todos.json")
}

pub fn load_memories(workspace: &Path) -> Vec<MemoryEntry> {
    let path = memory_path(workspace);
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| v.as_array().cloned())
        .map(|arr| {
            arr.into_iter()
                .filter_map(|item| {
                    let text = item.get("text")?.as_str()?.to_owned();
                    let scope = item
                        .get("scope")
                        .and_then(Value::as_str)
                        .unwrap_or("project")
                        .to_owned();
                    Some(MemoryEntry { text, scope })
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn save_memories(workspace: &Path, items: &[MemoryEntry]) -> Result<(), String> {
    let path = memory_path(workspace);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let arr: Vec<Value> = items
        .iter()
        .map(|e| json!({ "text": e.text, "scope": e.scope }))
        .collect();
    let body = serde_json::to_string_pretty(&arr).map_err(|e| e.to_string())?;
    fs::write(path, body).map_err(|e| e.to_string())
}

pub fn load_todos(workspace: &Path) -> Vec<TodoItem> {
    let path = todos_path(workspace);
    fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| v.as_array().cloned())
        .map(|arr| {
            arr.into_iter()
                .filter_map(|item| {
                    let text = item.get("text")?.as_str()?.to_owned();
                    let done = item.get("done").and_then(Value::as_bool).unwrap_or(false);
                    Some(TodoItem { text, done })
                })
                .collect()
        })
        .unwrap_or_default()
}

pub fn save_todos(workspace: &Path, items: &[TodoItem]) -> Result<(), String> {
    let path = todos_path(workspace);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let arr: Vec<Value> = items
        .iter()
        .map(|t| json!({ "text": t.text, "done": t.done }))
        .collect();
    let body = serde_json::to_string_pretty(&arr).map_err(|e| e.to_string())?;
    fs::write(path, body).map_err(|e| e.to_string())
}

/// Format saved project memories for injection into the native agent system prompt.
pub fn format_memory_prompt_section(workspace: &Path) -> Option<String> {
    let items = load_memories(workspace);
    if items.is_empty() {
        return None;
    }
    let mut lines = vec!["## Project Memory (`.aria/memory.json`)".to_owned()];
    for entry in items.iter().take(20) {
        lines.push(format!("- {}", entry.text));
    }
    Some(lines.join("\n"))
}

/// Append a concise note line to `<workspace>/ARIA.md` under `## Notes`.
pub fn append_aria_note(workspace: &Path, text: &str) -> Result<PathBuf, String> {
    let clean = text.trim();
    if clean.is_empty() {
        return Err("Usage: /note <text>".into());
    }
    let path = workspace.join("ARIA.md");
    let mut content = fs::read_to_string(&path).unwrap_or_else(|_| "# Aria Project Notes\n".into());
    if !content.contains("## Notes") {
        if !content.ends_with('\n') {
            content.push('\n');
        }
        content.push_str("\n## Notes\n");
    }
    if !content.ends_with('\n') {
        content.push('\n');
    }
    content.push_str(&format!("- {clean}\n"));
    fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(path)
}

/// Handle `/memory [list|add <text>|rm <idx>|clear]` natively.
pub fn handle_memory_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let trimmed = arg.trim();
    let mut parts = trimmed.splitn(2, char::is_whitespace);
    let sub = parts.next().unwrap_or("").to_ascii_lowercase();
    let rest = parts.next().unwrap_or("").trim();

    let mut items = load_memories(workspace);
    match sub.as_str() {
        "" | "list" | "ls" => {
            if items.is_empty() {
                let msg = if zh {
                    "暂无项目记忆。使用 `/memory add <内容>` 添加。"
                } else {
                    "No project memories saved. Add one with `/memory add <text>`."
                };
                (msg.to_owned(), "ok")
            } else {
                let mut lines = vec![if zh {
                    format!("**项目记忆（{} 条）**", items.len())
                } else {
                    format!("**Project Memories ({})**", items.len())
                }];
                for (i, item) in items.iter().enumerate() {
                    lines.push(format!("- `{i}` {}", item.text));
                }
                (lines.join("\n"), "ok")
            }
        }
        "add" => {
            if rest.is_empty() {
                return ("Usage: /memory add <text>".to_owned(), "ok");
            }
            items.push(MemoryEntry {
                text: rest.to_owned(),
                scope: "project".to_owned(),
            });
            match save_memories(workspace, &items) {
                Ok(()) => (format!("Saved memory `{}`: {rest}", items.len() - 1), "ok"),
                Err(e) => (format!("[memory save error: {e}]"), "failed"),
            }
        }
        "rm" | "remove" | "del" | "delete" => {
            let Ok(idx) = rest.parse::<usize>() else {
                return ("Usage: /memory rm <index>".to_owned(), "ok");
            };
            if idx >= items.len() {
                return (format!("Memory index `{idx}` out of range."), "failed");
            }
            let removed = items.remove(idx);
            match save_memories(workspace, &items) {
                Ok(()) => (format!("Removed memory `{idx}`: {}", removed.text), "ok"),
                Err(e) => (format!("[memory save error: {e}]"), "failed"),
            }
        }
        "clear" => match save_memories(workspace, &[]) {
            Ok(()) => ("Cleared all project memories.".to_owned(), "ok"),
            Err(e) => (format!("[memory clear error: {e}]"), "failed"),
        },
        _ => {
            // Treat bare `/memory <text>` as add for convenience.
            items.push(MemoryEntry {
                text: trimmed.to_owned(),
                scope: "project".to_owned(),
            });
            match save_memories(workspace, &items) {
                Ok(()) => (
                    format!("Saved memory `{}`: {trimmed}", items.len() - 1),
                    "ok",
                ),
                Err(e) => (format!("[memory save error: {e}]"), "failed"),
            }
        }
    }
}

/// Handle `/todo [list|add <text>|done <idx>|rm <idx>|clear]` natively.
pub fn handle_todo_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let trimmed = arg.trim();
    let mut parts = trimmed.splitn(2, char::is_whitespace);
    let sub = parts.next().unwrap_or("").to_ascii_lowercase();
    let rest = parts.next().unwrap_or("").trim();

    let mut items = load_todos(workspace);
    match sub.as_str() {
        "" | "list" | "ls" => {
            if items.is_empty() {
                let msg = if zh {
                    "暂无待办任务。使用 `/todo add <任务>` 添加。"
                } else {
                    "No tasks. Add one with `/todo add <task>`."
                };
                (msg.to_owned(), "ok")
            } else {
                let pending = items.iter().filter(|t| !t.done).count();
                let mut lines = vec![if zh {
                    format!("**待办任务（{pending}/{} 未完成）**", items.len())
                } else {
                    format!("**Tasks ({pending}/{} pending)**", items.len())
                }];
                for (i, item) in items.iter().enumerate() {
                    let mark = if item.done { "[x]" } else { "[ ]" };
                    lines.push(format!("- {mark} `{i}` {}", item.text));
                }
                (lines.join("\n"), "ok")
            }
        }
        "add" => {
            if rest.is_empty() {
                return ("Usage: /todo add <task>".to_owned(), "ok");
            }
            items.push(TodoItem {
                text: rest.to_owned(),
                done: false,
            });
            match save_todos(workspace, &items) {
                Ok(()) => (format!("Added task `{}`: {rest}", items.len() - 1), "ok"),
                Err(e) => (format!("[todo save error: {e}]"), "failed"),
            }
        }
        "done" | "check" | "complete" => {
            let Ok(idx) = rest.parse::<usize>() else {
                return ("Usage: /todo done <index>".to_owned(), "ok");
            };
            let Some(item) = items.get_mut(idx) else {
                return (format!("Task index `{idx}` out of range."), "failed");
            };
            item.done = true;
            let label = item.text.clone();
            match save_todos(workspace, &items) {
                Ok(()) => (format!("Completed task `{idx}`: {label}"), "ok"),
                Err(e) => (format!("[todo save error: {e}]"), "failed"),
            }
        }
        "rm" | "remove" | "del" | "delete" => {
            let Ok(idx) = rest.parse::<usize>() else {
                return ("Usage: /todo rm <index>".to_owned(), "ok");
            };
            if idx >= items.len() {
                return (format!("Task index `{idx}` out of range."), "failed");
            }
            let removed = items.remove(idx);
            match save_todos(workspace, &items) {
                Ok(()) => (format!("Removed task `{idx}`: {}", removed.text), "ok"),
                Err(e) => (format!("[todo save error: {e}]"), "failed"),
            }
        }
        "clear" => match save_todos(workspace, &[]) {
            Ok(()) => ("Cleared all tasks.".to_owned(), "ok"),
            Err(e) => (format!("[todo clear error: {e}]"), "failed"),
        },
        _ => {
            items.push(TodoItem {
                text: trimmed.to_owned(),
                done: false,
            });
            match save_todos(workspace, &items) {
                Ok(()) => (format!("Added task `{}`: {trimmed}", items.len() - 1), "ok"),
                Err(e) => (format!("[todo save error: {e}]"), "failed"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_todo_and_note_roundtrip() {
        let dir = tempfile::tempdir().expect("tempdir");

        let (msg, st) = handle_memory_command(dir.path(), "add Prefer Rust over Python", false);
        assert_eq!(st, "ok");
        assert!(msg.contains("Prefer Rust over Python"));
        let prompt = format_memory_prompt_section(dir.path()).expect("memory prompt");
        assert!(prompt.contains("Prefer Rust over Python"));

        let (list_msg, _) = handle_memory_command(dir.path(), "list", false);
        assert!(list_msg.contains("Prefer Rust over Python"));
        let (_, st) = handle_memory_command(dir.path(), "rm 0", false);
        assert_eq!(st, "ok");
        assert!(format_memory_prompt_section(dir.path()).is_none());

        let (_, st) = handle_todo_command(dir.path(), "add Ship Batch 61", false);
        assert_eq!(st, "ok");
        let (_, st) = handle_todo_command(dir.path(), "done 0", false);
        assert_eq!(st, "ok");
        let (todo_list, _) = handle_todo_command(dir.path(), "list", false);
        assert!(todo_list.contains("[x] `0` Ship Batch 61"));

        let note_path =
            append_aria_note(dir.path(), "Always verify with cargo test").expect("note");
        let note_body = fs::read_to_string(note_path).expect("read ARIA.md");
        assert!(note_body.contains("## Notes"));
        assert!(note_body.contains("- Always verify with cargo test"));
    }
}
