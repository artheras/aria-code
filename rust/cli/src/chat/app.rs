//! UI state/reducer. Protocol events cannot create host approval grants.
use super::editor::Editor;
use serde_json::{json, Value};
use std::collections::VecDeque;

const MAX_TEXT: usize = 262_144;
const MAX_TRANSCRIPT: usize = 4 * 1024 * 1024;

pub fn safe(text: &str) -> String {
    text.chars()
        .filter(|c| {
            (!c.is_control() || matches!(c, '\n' | '\t'))
                && !matches!(c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        })
        .collect()
}
fn limited(text: &str) -> String {
    let mut text = safe(text);
    if text.len() > MAX_TEXT {
        let mut end = MAX_TEXT;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        text.truncate(end);
        text.push_str("\n[display truncated; full session is saved by the runtime]");
    }
    text
}

#[derive(Debug)]
pub struct Entry {
    pub role: String,
    pub text: String,
    pub detail: bool,
}
#[derive(Debug)]
pub struct Dialog {
    pub id: String,
    pub title: String,
    pub choices: Vec<(String, String)>,
    pub selected: usize,
    pub secret: bool,
    pub editor: Editor,
    pub shortcuts: Value,
}
#[derive(Debug, Default)]
pub struct App {
    pub editor: Editor,
    pub entries: VecDeque<Entry>,
    pub dialog: Option<Dialog>,
    pub turn: Option<String>,
    pub ready: bool,
    pub closed: bool,
    pub failed: bool,
    pub session: String,
    pub model: String,
    pub version: String,
    pub provider: String,
    pub workspace: String,
    pub permission: String,
    pub network: bool,
    pub commands: Vec<String>,
    pub status: String,
    pub details: bool,
    pub help: bool,
    pub scroll: usize,
    pub next_turn: u64,
}

impl App {
    pub fn add(&mut self, role: &str, text: &str, detail: bool) {
        if text.is_empty() {
            return;
        }
        self.entries.push_back(Entry {
            role: role.into(),
            text: limited(text),
            detail,
        });
        self.trim();
    }
    fn trim(&mut self) {
        while self.entries.len() > 2000
            || self.entries.iter().map(|e| e.text.len()).sum::<usize>() > MAX_TRANSCRIPT
        {
            self.entries.pop_front();
        }
    }
    fn delta(&mut self, role: &str, text: &str) {
        if role == "assistant" {
            let start = self
                .entries
                .iter()
                .rposition(|e| e.role == "user")
                .map_or(0, |i| i + 1);
            if let Some(entry) = self
                .entries
                .iter_mut()
                .skip(start)
                .find(|e| e.role == "assistant")
            {
                if entry.text.len() < MAX_TEXT {
                    entry.text = limited(&(entry.text.clone() + text));
                }
                self.trim();
                return;
            }
        }
        if self
            .entries
            .back()
            .is_some_and(|e| e.role == role && e.text.len() < MAX_TEXT)
        {
            let entry = self.entries.back_mut().unwrap();
            entry.text = limited(&(entry.text.clone() + text));
            self.trim();
        } else if !self.entries.back().is_some_and(|e| e.role == role) {
            self.add(role, text, false);
        }
    }
    pub fn event(&mut self, v: Value) -> Result<(), String> {
        if v["protocol"] != 1 || !v["type"].is_string() {
            return Err("Unsupported runtime protocol".into());
        }
        let kind = v["type"].as_str().unwrap();
        let text = |key: &str| v[key].as_str().unwrap_or("");
        if (kind.starts_with("answer.")
            || kind.starts_with("tool.")
            || kind.starts_with("turn.")
            || kind.starts_with("approval.")
            || kind.starts_with("input.")
            || kind == "dialog.closed")
            && (v["turn_id"].as_str() != self.turn.as_deref() || self.turn.is_none())
        {
            return Err("Runtime event belongs to an unknown turn".into());
        }
        match kind {
            "session.ready" | "session.state" => {
                crate::sessions::validate_id(text("session_id"))?;
                let changed_session = self.session != text("session_id");
                self.session = safe(text("session_id"));
                self.model = safe(text("model"));
                self.version = safe(text("version"));
                self.provider = safe(text("provider"));
                self.workspace = safe(text("workspace"));
                self.permission = safe(text("permission"));
                self.network = v["network"].as_bool().unwrap_or(false);
                self.commands = v["commands"]
                    .as_array()
                    .ok_or("Missing command list")?
                    .iter()
                    .take(1000)
                    .filter_map(|s| s.as_str().map(safe))
                    .collect();
                for command in ["/new", "/resume", "/help-native"] {
                    if !self.commands.iter().any(|c| c == command) {
                        self.commands.push(command.into());
                    }
                }
                if kind == "session.ready" || changed_session {
                    self.entries.clear();
                    self.scroll = 0;
                    if let Some(messages) = v["messages"].as_array() {
                        for m in messages.iter().rev().take(40).rev() {
                            self.add(
                                m["role"].as_str().unwrap_or("system"),
                                m["content"].as_str().unwrap_or("[multimodal message]"),
                                false,
                            );
                        }
                    }
                    self.status = "Ready".into();
                }
                self.ready = true;
            }
            "turn.started" => self.status = "Thinking · Esc cancels".into(),
            "answer.delta" => {
                self.status = "Responding · Esc cancels".into();
                self.delta("assistant", text("text"));
            }
            "answer.replace" => {
                if let Some(entry) = self
                    .entries
                    .iter_mut()
                    .rev()
                    .find(|e| e.role == "assistant")
                {
                    // Only replace an answer from THIS turn (after the last user).
                    entry.text = limited(text("text"));
                    self.trim();
                } else {
                    self.add("assistant", text("text"), false);
                }
            }
            "output.delta" => self.delta("output", text("text")),
            "tool.started" => {
                self.status = format!("Running {} · Esc cancels", safe(text("tool")));
                self.add(
                    "action",
                    &format!(
                        "{}\n{}",
                        text("tool"),
                        serde_json::to_string_pretty(&v["params"]).unwrap_or_default()
                    ),
                    true,
                );
            }
            "tool.completed" => {
                let ok = v["success"]
                    .as_bool()
                    .ok_or("Tool result requires success")?;
                self.add(
                    "action",
                    &format!(
                        "{} {} {}",
                        if ok { "✓" } else { "✗" },
                        text("tool"),
                        text("error")
                    ),
                    false,
                );
            }
            "turn.status" => {
                self.status = safe(&format!("{}: {}", text("state"), text("message")));
                self.add("status", &self.status.clone(), false);
            }
            "approval.requested" | "input.requested" => {
                if self.dialog.is_some() || !v["request_id"].is_string() {
                    return Err("Invalid nested dialog".into());
                }
                let choices = if kind == "approval.requested" {
                    let rows = v["choices"].as_array().ok_or("Missing choices")?;
                    if rows.is_empty() || rows.len() > 100 {
                        return Err("Invalid number of choices".into());
                    }
                    rows.iter()
                        .map(|row| {
                            let row = row.as_array().ok_or("Invalid choice")?;
                            if row.len() != 2 {
                                return Err("Invalid choice".into());
                            }
                            Ok((
                                safe(row[0].as_str().ok_or("Invalid label")?),
                                safe(row[1].as_str().ok_or("Invalid help")?),
                            ))
                        })
                        .collect::<Result<Vec<_>, String>>()?
                } else {
                    vec![]
                };
                let selected = v["selected"].as_u64().unwrap_or(0) as usize;
                self.dialog = Some(Dialog {
                    id: text("request_id").into(),
                    title: safe(if kind == "approval.requested" {
                        text("title")
                    } else {
                        text("prompt")
                    }),
                    selected: selected.min(choices.len().saturating_sub(1)),
                    choices,
                    editor: Editor::default(),
                    secret: v["secret"].as_bool().unwrap_or(false),
                    shortcuts: v["shortcuts"].clone(),
                });
                self.status = "Waiting for your response".into();
            }
            "dialog.closed" => {
                if self
                    .dialog
                    .as_ref()
                    .is_some_and(|d| d.id == text("request_id"))
                {
                    self.dialog = None;
                }
            }
            "turn.completed" => {
                self.status = safe(text("status"));
                if !text("error").is_empty() {
                    self.add("error", text("error"), false);
                }
                self.turn = None;
                self.dialog = None;
            }
            "protocol.error" => {
                self.add("error", text("error"), false);
                // Failed /resume retains the current session and remains usable.
                if !self.session.is_empty() {
                    self.ready = true;
                }
            }
            "session.failed" => {
                self.add("error", text("error"), false);
                self.failed = true;
                self.closed = true;
            }
            "session.closed" => self.closed = true,
            _ => return Err(format!("Unknown application event: {kind}")),
        }
        Ok(())
    }
    pub fn submit(&mut self) -> Option<Value> {
        if !self.ready || self.turn.is_some() || self.editor.text().trim().is_empty() {
            return None;
        }
        let text = self.editor.take();
        self.scroll = 0;
        match text.trim() {
            "/exit" | "/quit" | "exit" | "quit" => {
                return Some(json!({"type":"shutdown","protocol":1}))
            }
            "/new" => {
                self.ready = false;
                return Some(json!({"type":"session.new","protocol":1}));
            }
            "/help-native" => {
                self.help = true;
                return None;
            }
            _ => (),
        }
        if let Some(id) = text.trim().strip_prefix("/resume ") {
            if let Err(e) = crate::sessions::validate_id(id) {
                self.add("error", &e, false);
                return None;
            }
            self.ready = false;
            return Some(json!({"type":"session.resume","protocol":1,"session_id":id}));
        }
        self.next_turn += 1;
        let id = format!("turn-{}", self.next_turn);
        self.add("user", &text, false);
        // A placeholder prevents replacing an assistant message from a prior turn.
        self.entries.push_back(Entry {
            role: "assistant".into(),
            text: String::new(),
            detail: false,
        });
        self.turn = Some(id.clone());
        self.status = "Submitting…".into();
        Some(json!({"type":"turn.submit","protocol":1,"turn_id":id,"text":text}))
    }
    pub fn cancel(&mut self) -> Option<Value> {
        self.turn.as_ref().map(|id| {
            self.status = "Cancelling…".into();
            json!({"type":"turn.cancel","protocol":1,"turn_id":id})
        })
    }
    pub fn respond(&mut self, choice: Option<i64>) -> Option<Value> {
        let d = self.dialog.as_mut()?;
        let fields = if d.choices.is_empty() {
            json!({"text": d.editor.text()})
        } else {
            json!({"choice":choice.unwrap_or(d.selected as i64)})
        };
        // Secret input is never added to history or the transcript.
        let request = json!({"type":"dialog.respond","protocol":1,"turn_id":self.turn,
                            "request_id":d.id,"choice":fields.get("choice"),"text":fields.get("text")});
        self.dialog = None;
        self.status = "Continuing…".into();
        Some(request)
    }
    pub fn complete(&mut self) {
        let text = self.editor.text();
        let candidates: Vec<_> = self
            .commands
            .iter()
            .filter(|c| c.starts_with(text))
            .collect();
        if candidates.len() == 1 {
            let result = format!("{} ", candidates[0]);
            self.editor.set(&result);
        } else if !candidates.is_empty() {
            self.add(
                "status",
                &candidates
                    .iter()
                    .map(|s| s.as_str())
                    .collect::<Vec<_>>()
                    .join("  "),
                false,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn app() -> App {
        App {
            ready: true,
            ..App::default()
        }
    }
    #[test]
    fn stale_events_and_approvals_are_rejected() {
        let mut a = app();
        a.editor.insert("你好");
        let req = a.submit().unwrap();
        assert_eq!(req["turn_id"], "turn-1");
        assert!(a
            .event(json!({"protocol":1,"type":"approval.requested","turn_id":"old"}))
            .is_err());
        assert!(a
            .event(json!({"protocol":1,"type":"turn.completed","turn_id":"old"}))
            .is_err());
        assert!(a.turn.is_some());
    }
    #[test]
    fn authoritative_answer_does_not_replace_previous_turn() {
        let mut a = app();
        a.add("assistant", "prior", false);
        a.editor.insert("next");
        a.submit();
        a.event(json!({"protocol":1,"type":"answer.replace","turn_id":"turn-1","text":"current"}))
            .unwrap();
        assert_eq!(a.entries[0].text, "prior");
        assert_eq!(a.entries[2].text, "current");
    }
    #[test]
    fn secret_dialog_does_not_leak_or_persist() {
        let mut a = app();
        a.editor.insert("/login");
        a.submit();
        a.event(json!({"protocol":1,"type":"input.requested","turn_id":"turn-1","request_id":"secret","prompt":"Token","secret":true})).unwrap();
        a.dialog.as_mut().unwrap().editor.insert("SECRET");
        let req = a.respond(None).unwrap();
        assert_eq!(req["text"], "SECRET");
        assert!(!format!("{:?}", a.entries).contains("SECRET"));
        assert_eq!(a.editor.text(), "");
    }
    #[test]
    fn hostile_terminal_controls_are_removed() {
        assert_eq!(safe("a\u{1b}\u{202e}b\n"), "ab\n");
        let mut a = app();
        for _ in 0..40 {
            a.add("output", &"界".repeat(MAX_TEXT), false);
        }
        assert!(a.entries.iter().map(|e| e.text.len()).sum::<usize>() <= MAX_TRANSCRIPT);
    }
}
