//! Read Python's JSON snapshots and append-only JSONL without rewriting them.
use crate::state;
use serde_json::{json, Map, Value};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    time::SystemTime,
};

const MAX_FILES: usize = 10_000;

pub fn validate_id(id: &str) -> Result<(), String> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
    {
        return Err(
            "Session ID must be 1..128 ASCII letters, digits, underscores or hyphens".into(),
        );
    }
    Ok(())
}

fn load(path: &Path, id: &str) -> Result<Value, String> {
    let raw = state::read_text(path, state::MAX_STATE)?;
    if path.extension().is_some_and(|v| v == "json") {
        let mut data: Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
        let object = data
            .as_object_mut()
            .ok_or("Session must be a JSON object")?;
        if object.get("id").and_then(Value::as_str) != Some(id) {
            return Err("Session ID does not match its filename".into());
        }
        if !object.get("messages").is_some_and(Value::is_array) {
            return Err("Session messages must be an array".into());
        }
        object.insert("id".into(), id.into());
        return Ok(data);
    }
    let mut meta = Map::new();
    let mut messages = Vec::new();
    for line in raw.lines().filter(|v| !v.trim().is_empty()) {
        // A crash may leave one partial JSONL line; preceding messages survive.
        let Ok(Value::Object(mut entry)) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        match entry
            .remove("type")
            .and_then(|v| v.as_str().map(str::to_owned))
            .as_deref()
        {
            Some("meta") => meta.extend(entry),
            Some("message") => messages
                .push(json!({"role":entry.get("role").unwrap_or(&json!("user")),
                "content":entry.get("content").unwrap_or(&json!(""))})),
            _ => (),
        }
    }
    if meta.is_empty() && messages.is_empty() {
        return Err("No valid session records".into());
    }
    if meta.get("id").is_some_and(|v| v.as_str() != Some(id)) {
        return Err("Session ID does not match its filename".into());
    }
    Ok(
        json!({"id":id,"messages":messages,"updated_at":meta.get("updated_at").unwrap_or(&json!("")),"metadata":meta}),
    )
}

pub fn show(root: &Path, id: &str, json_only: bool) -> Result<Value, String> {
    validate_id(id)?;
    let snapshot = root.join(format!("{id}.json"));
    // JSON is the stable Python resume format and wins if both exist.
    if snapshot.exists() {
        return load(&snapshot, id);
    }
    if json_only {
        return Err(
            "Resume requires a saved .json snapshot; JSONL history is viewable with sessions show"
                .into(),
        );
    }
    load(&root.join(format!("{id}.jsonl")), id)
}

fn summary(id: &str, data: &Value, resumable: bool) -> Value {
    json!({"id":id,"title":data.pointer("/metadata/title").and_then(Value::as_str).unwrap_or("Untitled"),
        "messages":data["messages"].as_array().map_or(0, Vec::len),
        "updated":data.get("updated_at").or_else(||data.pointer("/metadata/updated_at")).unwrap_or(&json!("")),
        "created":data.pointer("/metadata/created_at").unwrap_or(&json!("")),"resumable":resumable})
}

fn hits(data: &Value, query: &str) -> Vec<String> {
    let mut found = Vec::new();
    for message in data["messages"].as_array().into_iter().flatten() {
        let content = &message["content"];
        let texts: Vec<&str> = match content {
            Value::String(text) => vec![text],
            Value::Array(blocks) => blocks
                .iter()
                .filter_map(|b| b.get("text").and_then(Value::as_str))
                .collect(),
            _ => vec![],
        };
        for text in texts {
            if let Some(index) = text.to_lowercase().find(query) {
                // Map the lowercased byte offset back to an original character
                // offset: Unicode lowercasing may expand a character (e.g. İ).
                let mut bytes = 0;
                let mut chars: usize = 0;
                for ch in text.chars() {
                    if bytes >= index {
                        break;
                    }
                    bytes += ch.to_lowercase().map(char::len_utf8).sum::<usize>();
                    chars += 1;
                }
                found.push(
                    text.chars()
                        .skip(chars.saturating_sub(20))
                        .take(120)
                        .collect(),
                );
            }
        }
    }
    found
}

pub fn list(root: &Path, limit: usize, query: Option<&str>) -> Result<Value, String> {
    if !(1..=1000).contains(&limit) {
        return Err("Session limit must be 1..1000".into());
    }
    let query = query.map(|v| v.to_lowercase());
    if query.as_ref().is_some_and(|q| q.trim().is_empty()) {
        return Err("Search query must not be empty".into());
    }
    let mut candidates: BTreeMap<String, (PathBuf, SystemTime)> = BTreeMap::new();
    let mut truncated = false;
    let mut skipped = 0;
    if !root.exists() {
        return Ok(json!({"sessions":[],"skipped":0,"truncated":false}));
    }
    for (count, entry) in fs::read_dir(root).map_err(|e| e.to_string())?.enumerate() {
        if count >= MAX_FILES {
            truncated = true;
            break;
        }
        let Ok(entry) = entry else {
            skipped += 1;
            continue;
        };
        let path = entry.path();
        if !path
            .extension()
            .is_some_and(|v| v == "json" || v == "jsonl")
        {
            continue;
        }
        let Some(id) = path.file_stem().and_then(|v| v.to_str()) else {
            skipped += 1;
            continue;
        };
        let Ok(metadata) = path.symlink_metadata() else {
            skipped += 1;
            continue;
        };
        if validate_id(id).is_err() || !metadata.is_file() {
            skipped += 1;
            continue;
        }
        if let Some((old, _)) = candidates.get(id) {
            if old.extension().is_some_and(|v| v == "json") {
                continue;
            }
        }
        candidates.insert(
            id.into(),
            (
                path.clone(),
                metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH),
            ),
        );
    }
    let mut files: Vec<_> = candidates.into_iter().collect();
    files.sort_by(|(a, (_, at)), (b, (_, bt))| bt.cmp(at).then(a.cmp(b)));
    let mut results = Vec::new();
    for (id, (path, modified)) in files {
        let Ok(data) = load(&path, &id) else {
            skipped += 1;
            continue;
        };
        let resumable = path.extension().is_some_and(|v| v == "json");
        let mut row = summary(&id, &data, resumable);
        let count = if let Some(query) = &query {
            let matches = hits(&data, query);
            if matches.is_empty() {
                continue;
            }
            row["match_count"] = json!(matches.len());
            row["preview"] = json!(matches[0]);
            matches.len()
        } else {
            0
        };
        results.push((count, modified, id, row));
        if query.is_none() && results.len() >= limit {
            break;
        }
    }
    results.sort_by(|a, b| b.0.cmp(&a.0).then(b.1.cmp(&a.1)).then(a.2.cmp(&b.2)));
    Ok(
        json!({"sessions":results.into_iter().take(limit).map(|r|r.3).collect::<Vec<_>>(),
        "skipped":skipped,"truncated":truncated}),
    )
}

/// Append a user + assistant turn to a native JSON session snapshot (`<root>/<id>.json`).
pub fn save_native_turn(
    root: &Path,
    id: &str,
    workspace: &str,
    model: &str,
    user_msg: &str,
    assistant_msg: &str,
) -> Result<(), String> {
    validate_id(id)?;
    fs::create_dir_all(root).map_err(|e| format!("Failed to create sessions dir: {e}"))?;
    let path = root.join(format!("{id}.json"));
    let mut data: Value = if path.exists() {
        load(&path, id).unwrap_or_else(|_| json!({"id": id, "messages": [], "metadata": {}}))
    } else {
        json!({
            "id": id,
            "messages": [],
            "metadata": {
                "title": user_msg.chars().take(60).collect::<String>(),
                "workspace": workspace,
                "model": model,
            }
        })
    };
    let obj = data
        .as_object_mut()
        .ok_or("Session snapshot must be an object")?;
    let messages = obj
        .entry("messages")
        .or_insert_with(|| json!([]))
        .as_array_mut()
        .ok_or("Session messages must be an array")?;
    messages.push(json!({"role": "user", "content": user_msg}));
    messages.push(json!({"role": "assistant", "content": assistant_msg}));
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default();
    obj.insert("updated_at".into(), json!(now));
    fs::write(
        &path,
        serde_json::to_string_pretty(&data).unwrap_or_else(|_| "{}".into()),
    )
    .map_err(|e| format!("Failed to write session snapshot: {e}"))
}

/// Export conversation transcript to `.md` or `.json` inside the workspace.
pub fn export_transcript(
    workspace: &Path,
    model: &str,
    messages: &[(String, String)],
    arg: &str,
) -> Result<PathBuf, String> {
    let parts: Vec<&str> = arg.split_whitespace().collect();
    let mut format = "md";
    let mut custom_path: Option<&str> = None;
    for part in parts {
        match part.to_ascii_lowercase().as_str() {
            "md" | "markdown" => format = "md",
            "json" => format = "json",
            _ => {
                if part.ends_with(".json") {
                    format = "json";
                } else if part.ends_with(".md") {
                    format = "md";
                }
                custom_path = Some(part);
            }
        }
    }

    let ts = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let target = if let Some(rel) = custom_path {
        let candidate = PathBuf::from(rel);
        if candidate.is_absolute()
            || candidate
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
        {
            return Err(
                "Export path must stay inside the workspace (relative path, no '..')".into(),
            );
        }
        workspace.join(candidate)
    } else {
        workspace
            .join(".aria")
            .join("exports")
            .join(format!("session-{ts}.{format}"))
    };
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Failed to create export dir: {e}"))?;
    }

    let body = if format == "json" {
        let items: Vec<Value> = messages
            .iter()
            .map(|(role, content)| json!({"role": role, "content": content}))
            .collect();
        serde_json::to_string_pretty(&json!({
            "exported_at": ts,
            "workspace": workspace.display().to_string(),
            "model": model,
            "message_count": items.len(),
            "messages": items,
        }))
        .map_err(|e| format!("Failed to serialize export JSON: {e}"))?
    } else {
        let mut out = vec![
            "# Aria Session Export".to_owned(),
            String::new(),
            format!("- **Model**: `{model}`"),
            format!("- **Messages**: {}", messages.len()),
            String::new(),
        ];
        for (role, content) in messages {
            let heading = match role.as_str() {
                "user" => "## User",
                "assistant" => "## Assistant",
                _ => "## System",
            };
            out.push(heading.to_owned());
            out.push(String::new());
            out.push(content.clone());
            out.push(String::new());
        }
        out.join("\n")
    };
    fs::write(&target, body).map_err(|e| format!("Failed to write export file: {e}"))?;
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn safe_ids_and_partial_jsonl() {
        for id in ["", "../secret", "/tmp/x", "x.json", "a\\b", "é"] {
            assert!(validate_id(id).is_err());
        }
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("abc.jsonl"), "{\"type\":\"meta\",\"id\":\"abc\",\"title\":\"你好\"}\n{\"type\":\"message\",\"content\":\"世界\"}\n{partial").unwrap();
        let data = show(dir.path(), "abc", false).unwrap();
        assert_eq!(data["metadata"]["title"], "你好");
        assert_eq!(data["messages"][0]["content"], "世界");
        assert!(show(dir.path(), "abc", true).is_err());
    }

    #[test]
    fn save_native_turn_creates_and_appends_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        save_native_turn(
            dir.path(),
            "native_1",
            "/tmp/ws",
            "qwen2.5:7b",
            "hi",
            "hello",
        )
        .unwrap();
        save_native_turn(
            dir.path(),
            "native_1",
            "/tmp/ws",
            "qwen2.5:7b",
            "next",
            "reply",
        )
        .unwrap();
        let data = show(dir.path(), "native_1", true).unwrap();
        assert_eq!(data["messages"].as_array().unwrap().len(), 4);
        assert_eq!(data["metadata"]["workspace"], "/tmp/ws");
    }

    #[test]
    fn export_transcript_md_json_and_blocks_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let msgs = vec![
            ("user".into(), "hello".into()),
            ("assistant".into(), "world".into()),
        ];
        let md = export_transcript(dir.path(), "qwen2.5:7b", &msgs, "md").unwrap();
        assert!(md.is_file());
        assert!(fs::read_to_string(&md).unwrap().contains("## Assistant"));

        let js = export_transcript(dir.path(), "qwen2.5:7b", &msgs, "out.json").unwrap();
        let parsed: Value = serde_json::from_str(&fs::read_to_string(&js).unwrap()).unwrap();
        assert_eq!(parsed["message_count"], 2);

        assert!(export_transcript(dir.path(), "qwen2.5:7b", &msgs, "../escape.md").is_err());
    }
}
