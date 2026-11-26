//! Enterprise audit trail (`<workspace>/.aria/audit.jsonl`).
//!
//! Appends structured JSON Lines records for packaged agent runs, tool
//! invocations, and approval decisions so enterprise deployments maintain
//! a verifiable compliance trail.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditEvent {
    pub timestamp_secs: u64,
    pub actor: String,
    pub package: String,
    pub action: String,
    pub tool: Option<String>,
    pub approved: bool,
    pub outcome: String,
}

pub fn audit_log_path(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("audit.jsonl")
}

pub fn now_unix_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

pub fn record_audit_event(workspace: &Path, event: &AuditEvent) -> Result<PathBuf, String> {
    let path = audit_log_path(workspace);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Failed to create audit dir: {e}"))?;
    }
    let line = serde_json::to_string(&json!({
        "timestamp_secs": event.timestamp_secs,
        "actor": event.actor,
        "package": event.package,
        "action": event.action,
        "tool": event.tool,
        "approved": event.approved,
        "outcome": event.outcome,
    }))
    .map_err(|e| format!("Failed to serialize audit event: {e}"))?;

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| format!("Failed to open {}: {e}", path.display()))?;
    writeln!(file, "{line}").map_err(|e| format!("Failed to write {}: {e}", path.display()))?;
    Ok(path)
}

pub fn read_audit_events(workspace: &Path, limit: usize) -> Vec<Value> {
    let path = audit_log_path(workspace);
    let Ok(raw) = fs::read_to_string(path) else {
        return Vec::new();
    };
    let mut events: Vec<Value> = raw
        .lines()
        .filter(|l| !l.trim().is_empty())
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .collect();
    if events.len() > limit {
        events = events.split_off(events.len() - limit);
    }
    events
}

pub fn summarize_audit_usage(workspace: &Path) -> Value {
    use std::collections::BTreeMap;

    let path = audit_log_path(workspace);
    let raw = fs::read_to_string(path).unwrap_or_default();
    let mut total_calls: usize = 0;
    let mut approved_calls: usize = 0;
    let mut denied_calls: usize = 0;
    let mut by_actor: BTreeMap<String, usize> = BTreeMap::new();
    let mut by_tool: BTreeMap<String, usize> = BTreeMap::new();
    let mut by_package: BTreeMap<String, usize> = BTreeMap::new();

    for line in raw.lines().filter(|l| !l.trim().is_empty()) {
        let Ok(evt) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        total_calls += 1;
        if evt
            .get("approved")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            approved_calls += 1;
        } else {
            denied_calls += 1;
        }
        let actor = evt
            .get("actor")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();
        *by_actor.entry(actor).or_insert(0) += 1;
        let pkg = evt
            .get("package")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string();
        *by_package.entry(pkg).or_insert(0) += 1;
        if let Some(tool) = evt.get("tool").and_then(Value::as_str) {
            *by_tool.entry(tool.to_string()).or_insert(0) += 1;
        }
    }

    json!({
        "total_calls": total_calls,
        "approved_calls": approved_calls,
        "denied_calls": denied_calls,
        "by_actor": by_actor,
        "by_tool": by_tool,
        "by_package": by_package,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_event_append_and_read() {
        let dir = tempfile::tempdir().expect("tempdir");
        let evt = AuditEvent {
            timestamp_secs: now_unix_secs(),
            actor: "operator@corp".into(),
            package: "freight-auditor".into(),
            action: "pack.validate".into(),
            tool: Some("analyze_logistics_data".into()),
            approved: true,
            outcome: "ok".into(),
        };
        let path = record_audit_event(dir.path(), &evt).expect("record");
        assert!(path.is_file());
        let events = read_audit_events(dir.path(), 10);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["package"], "freight-auditor");
        assert_eq!(events[0]["approved"], true);

        let denied = AuditEvent {
            timestamp_secs: now_unix_secs(),
            actor: "grok-bot".into(),
            package: "freight-auditor".into(),
            action: "mcp_tools_call".into(),
            tool: Some("write_file".into()),
            approved: false,
            outcome: "denied".into(),
        };
        record_audit_event(dir.path(), &denied).expect("record denied");
        let summary = summarize_audit_usage(dir.path());
        assert_eq!(summary["total_calls"], 2);
        assert_eq!(summary["approved_calls"], 1);
        assert_eq!(summary["denied_calls"], 1);
        assert_eq!(summary["by_actor"]["grok-bot"], 1);
    }
}
