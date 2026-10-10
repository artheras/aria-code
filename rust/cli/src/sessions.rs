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
}
