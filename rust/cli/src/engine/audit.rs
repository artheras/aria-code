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
    }
}
