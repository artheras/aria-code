//! Native Rust local-first `/privacy` settings and `/feedback` journal.
//!
//! Stores privacy preferences in `.aria/privacy.json` (default: local-only,
//! zero upload) and feedback entries in `.aria/feedback.jsonl`.

use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct PrivacySettings {
    pub data_sharing: bool,
    pub feedback_upload: bool,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn privacy_file(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("privacy.json")
}

fn feedback_file(workspace: &Path) -> PathBuf {
    workspace.join(".aria").join("feedback.jsonl")
}

pub fn load_privacy(workspace: &Path) -> PrivacySettings {
    let Ok(raw) = fs::read_to_string(privacy_file(workspace)) else {
        return PrivacySettings::default();
    };
    let Ok(val) = serde_json::from_str::<Value>(&raw) else {
        return PrivacySettings::default();
    };
    PrivacySettings {
        data_sharing: val
            .get("data_sharing")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        feedback_upload: val
            .get("feedback_upload")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    }
}

pub fn save_privacy(workspace: &Path, settings: &PrivacySettings) -> Result<(), String> {
    let path = privacy_file(workspace);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Failed to create .aria dir: {e}"))?;
    }
    let payload = json!({
        "data_sharing": settings.data_sharing,
        "feedback_upload": settings.feedback_upload,
        "updated_at": now_secs(),
    });
    let text = serde_json::to_string_pretty(&payload)
        .map_err(|e| format!("Failed to serialize privacy settings: {e}"))?;
    fs::write(&path, text).map_err(|e| format!("Failed to write {}: {e}", path.display()))
}

pub fn count_feedback(workspace: &Path) -> usize {
    let Ok(raw) = fs::read_to_string(feedback_file(workspace)) else {
        return 0;
    };
    raw.lines().filter(|l| !l.trim().is_empty()).count()
}

pub fn handle_privacy_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let mut parts = arg.trim().splitn(2, char::is_whitespace);
    let sub = parts.next().unwrap_or("status").to_ascii_lowercase();
    let rest = parts.next().unwrap_or("").trim();
    let settings = load_privacy(workspace);

    match sub.as_str() {
        "" | "status" | "show" => {
            let shared = settings.data_sharing && settings.feedback_upload;
            let state_line = if shared {
                if zh {
                    "ON — 允许将 /feedback 记录用于改进产品"
                } else {
                    "ON — /feedback records may be shared"
                }
            } else if zh {
                "OFF — 纯本地模式，不上传任何数据"
            } else {
                "OFF — local-only, nothing leaves this machine"
            };
            let count = count_feedback(workspace);
            let lines = [
                if zh {
                    "### 隐私与数据设置".to_owned()
                } else {
                    "### Privacy Settings".to_owned()
                },
                format!("- Sharing: **{state_line}**"),
                format!("- `data_sharing`: `{}`", settings.data_sharing),
                format!("- `feedback_upload`: `{}`", settings.feedback_upload),
                format!(
                    "- Local feedback records: **{count}** (`{}`)",
                    feedback_file(workspace).display()
                ),
                String::new(),
                "Commands: `/privacy policy` · `/privacy opt-in` · `/privacy opt-out` · `/privacy export [path]` · `/privacy delete`".to_owned(),
            ];
            (lines.join("\n"), "ok")
        }
        "policy" => {
            let text = if zh {
                "Aria Code 默认采用**本地优先**策略：不自动采集或上传对话、凭证、代码或业务数据。只有显式执行 `/privacy opt-in` 后才会允许分享 `/feedback` 评价记录；可随时 `/privacy opt-out` 或 `/privacy delete` 撤销与清除。"
            } else {
                "Aria Code is **local-first**: nothing is collected or uploaded by default. Only `/privacy opt-in` enables sharing `/feedback` records. Credentials, source code, and vertical service data stay local. Withdraw anytime with `/privacy opt-out` or clear records with `/privacy delete`."
            };
            (text.to_owned(), "ok")
        }
        "opt-in" | "on" | "enable" => {
            let next = PrivacySettings {
                data_sharing: true,
                feedback_upload: true,
            };
            if let Err(e) = save_privacy(workspace, &next) {
                return (format!("[privacy error: {e}]"), "failed");
            }
            (
                if zh {
                    "已开启反馈数据分享（本地仍保留副本；随时可 `/privacy opt-out` 关闭）。"
                        .to_owned()
                } else {
                    "Feedback sharing enabled (local copies are kept; run `/privacy opt-out` anytime to disable).".to_owned()
                },
                "ok",
            )
        }
        "opt-out" | "off" | "disable" => {
            let next = PrivacySettings::default();
            if let Err(e) = save_privacy(workspace, &next) {
                return (format!("[privacy error: {e}]"), "failed");
            }
            (
                if zh {
                    "已关闭数据分享，反馈仅保存在本地。".to_owned()
                } else {
                    "Data sharing disabled. Feedback stays local only.".to_owned()
                },
                "ok",
            )
        }
        "export" => {
            let src = feedback_file(workspace);
            if !src.is_file() {
                return ("No local feedback records to export.".to_owned(), "ok");
            }
            let target = if rest.is_empty() {
                workspace
                    .join(".aria")
                    .join("exports")
                    .join(format!("feedback-{}.jsonl", now_secs()))
            } else {
                let candidate = PathBuf::from(rest);
                if candidate.is_absolute()
                    || candidate
                        .components()
                        .any(|c| matches!(c, std::path::Component::ParentDir))
                {
                    return (
                        "Export path must be a relative path inside the workspace.".to_owned(),
                        "failed",
                    );
                }
                workspace.join(candidate)
            };
            if let Some(parent) = target.parent() {
                let _ = fs::create_dir_all(parent);
            }
            match fs::copy(&src, &target) {
                Ok(_) => (
                    format!("Exported feedback records to `{}`.", target.display()),
                    "ok",
                ),
                Err(e) => (format!("[privacy export error: {e}]"), "failed"),
            }
        }
        "delete" | "clear" => {
            let src = feedback_file(workspace);
            if src.exists() {
                if let Err(e) = fs::remove_file(&src) {
                    return (format!("[privacy delete error: {e}]"), "failed");
                }
            }
            (
                if zh {
                    "已删除本地所有 `/feedback` 记录。".to_owned()
                } else {
                    "Deleted all local `/feedback` records.".to_owned()
                },
                "ok",
            )
        }
        _ => (
            "Usage: `/privacy [status|policy|opt-in|opt-out|export [path]|delete]`".to_owned(),
            "ok",
        ),
    }
}

pub fn handle_feedback_command(
    workspace: &Path,
    model: &str,
    history: &[(String, String)],
    arg: &str,
) -> (String, &'static str) {
    let mut parts = arg.trim().splitn(2, char::is_whitespace);
    let vote = parts.next().unwrap_or("").to_ascii_lowercase();
    let comment = parts.next().unwrap_or("").trim();

    let rating = match vote.as_str() {
        "good" | "up" | "1" | "+" => "positive",
        "bad" | "down" | "0" | "-" => "negative",
        "note" => "note",
        _ => {
            return (
                "Usage: `/feedback good|bad [comment]` or `/feedback note <comment>`".to_owned(),
                "ok",
            );
        }
    };
    if rating == "note" && comment.is_empty() {
        return ("Usage: `/feedback note <comment>`".to_owned(), "ok");
    }

    let last_assistant = history
        .iter()
        .rev()
        .find(|(role, _)| role == "assistant")
        .map(|(_, content)| content.chars().take(500).collect::<String>());
    let Some(snippet) = last_assistant else {
        return ("No AI response in history to rate yet.".to_owned(), "ok");
    };

    let settings = load_privacy(workspace);
    let record = json!({
        "timestamp": now_secs(),
        "rating": rating,
        "comment": comment,
        "model": model,
        "message_preview": snippet,
        "shared": settings.data_sharing && settings.feedback_upload,
    });

    let path = feedback_file(workspace);
    if let Some(parent) = path.parent() {
        if let Err(e) = fs::create_dir_all(parent) {
            return (format!("[feedback error: {e}]"), "failed");
        }
    }
    let line = serde_json::to_string(&record).unwrap_or_else(|_| "{}".into());
    match OpenOptions::new().create(true).append(true).open(&path) {
        Ok(mut f) => {
            if let Err(e) = writeln!(f, "{line}") {
                return (format!("[feedback write error: {e}]"), "failed");
            }
        }
        Err(e) => return (format!("[feedback open error: {e}]"), "failed"),
    }

    let icon = match rating {
        "positive" => "↑",
        "negative" => "↓",
        _ => "note",
    };
    let comment_suffix = if comment.is_empty() {
        String::new()
    } else {
        format!(" — {comment}")
    };
    (
        format!(
            "Feedback `{icon}`{comment_suffix} saved locally to `{}` (sharing: {}).",
            path.display(),
            if settings.data_sharing && settings.feedback_upload {
                "on"
            } else {
                "off"
            }
        ),
        "ok",
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn privacy_settings_and_feedback_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let history = vec![
            ("user".into(), "explain rust".into()),
            ("assistant".into(), "Rust guarantees memory safety.".into()),
        ];

        assert!(!load_privacy(dir.path()).data_sharing);
        let (fb_out, fb_st) =
            handle_feedback_command(dir.path(), "qwen2.5:7b", &history, "good very clear");
        assert_eq!(fb_st, "ok");
        assert!(fb_out.contains("sharing: off"));
        assert_eq!(count_feedback(dir.path()), 1);

        let (_, opt_in_st) = handle_privacy_command(dir.path(), "opt-in", false);
        assert_eq!(opt_in_st, "ok");
        assert!(load_privacy(dir.path()).data_sharing);

        let (exp_out, exp_st) = handle_privacy_command(dir.path(), "export fb_backup.jsonl", false);
        assert_eq!(exp_st, "ok");
        assert!(exp_out.contains("fb_backup.jsonl"));
        assert!(dir.path().join("fb_backup.jsonl").is_file());

        let (_, del_st) = handle_privacy_command(dir.path(), "delete", false);
        assert_eq!(del_st, "ok");
        assert_eq!(count_feedback(dir.path()), 0);
    }
}
