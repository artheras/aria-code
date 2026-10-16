//! UI state/reducer. Protocol events cannot create host approval grants.
use super::editor::Editor;
use serde_json::{json, Value};
use std::cell::Cell;
use std::collections::VecDeque;
use std::time::{Duration, Instant};

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
    pub action: Option<Action>,
}
#[derive(Debug)]
pub struct Action {
    pub tool: String,
    pub target: String,
    pub done: bool,
    pub success: bool,
    pub count: Option<u64>,
    pub exit_code: Option<i64>,
    pub elapsed: Option<f64>,
    pub reused: bool,
    pub staged: bool,
    pub cancelled: bool,
    pub error: String,
    /// How the host approved the call when it did not ask, e.g.
    /// "auto-approved · L1 low · Edit files" under approval_mode=risk.
    pub approval: String,
}
#[derive(Debug)]
pub struct Dialog {
    pub id: String,
    pub title: String,
    pub context: String,
    pub context_scroll: u16,
    pub context_max_scroll: Cell<u16>,
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
    /// Messages sent while a turn was running, in order. Each goes out when
    /// the turn before it completes.
    pub queued: VecDeque<String>,
    pub dialog: Option<Dialog>,
    pub turn: Option<String>,
    pub ready: bool,
    pub closed: bool,
    pub failed: bool,
    pub session: String,
    pub model: String,
    pub version: String,
    pub update_notice: String,
    pub provider: String,
    pub workspace: String,
    pub permission: String,
    pub network: bool,
    pub robot: super::robot::Robot,
    pub commands: Vec<String>,
    pub reference_completion: bool,
    pub status: String,
    pub details: bool,
    pub help: bool,
    pub scroll: usize,
    pub next_turn: u64,
    pub approval_context: String,
    pub lang: String,
    pub theme: String,
    pub home_workspace: bool,
    pub started: Option<Instant>,
    pub elapsed: Option<Duration>,
    pub viewport_top: Cell<Option<usize>>,
    pub viewport_scroll: Cell<usize>,
}

impl App {
    pub fn tr<'a>(&self, en: &'a str, zh: &'a str) -> &'a str {
        if self.lang.starts_with("zh") {
            zh
        } else {
            en
        }
    }
    fn current_start(&self) -> usize {
        self.entries
            .iter()
            .rposition(|e| e.role == "user")
            .map_or(0, |i| i + 1)
    }
    pub fn add(&mut self, role: &str, text: &str, detail: bool) {
        if text.is_empty() {
            return;
        }
        self.entries.push_back(Entry {
            role: role.into(),
            text: limited(text),
            detail,
            action: None,
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
            "completion.result" => {
                // Discard responses after typing, sending, or changing sessions.
                if self.turn.is_none()
                    && self.editor.at_end()
                    && self.session == text("session_id")
                    && self.editor.text() == text("text")
                {
                    let candidates: Vec<String> = v["candidates"]
                        .as_array()
                        .map(|items| {
                            items
                                .iter()
                                .filter_map(|x| x.as_str())
                                .take(50)
                                .map(safe)
                                .collect()
                        })
                        .unwrap_or_default();
                    if candidates.len() == 1 {
                        self.editor.set(&candidates[0]);
                    } else if !candidates.is_empty() {
                        self.add("status", &candidates.join("\n"), false);
                    }
                }
            }
            "update.available" => {
                self.update_notice = safe(text("text"))
                    .chars()
                    .filter(|c| !c.is_control())
                    .take(256)
                    .collect();
            }
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
                self.reference_completion = v["reference_completion"].as_bool().unwrap_or(false);
                self.lang = safe(text("ui_lang"));
                self.theme = super::theme::resolve(text("ui_theme"));
                self.home_workspace = v["home_workspace"].as_bool().unwrap_or(false);
                let mut robot = v["robot"].clone();
                if v["robot_design"] == "cream-block-v2"
                    && robot.as_array().is_some_and(|rows| !rows.is_empty())
                {
                    // Python retains legacy spans for older frontends. The
                    // current frontend owns the same artwork for both workers.
                    robot = json!({"design": "cream-block-v2", "theme": self.theme});
                } else if robot["design"] == "cream-block-v2" {
                    robot["theme"] = self.theme.clone().into();
                }
                self.robot = super::robot::Robot::from_event(&robot);
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
                let history_reset = v["history_reset"].as_bool().unwrap_or(false);
                if kind == "session.ready" || changed_session || history_reset {
                    self.approval_context.clear();
                    self.entries.clear();
                    self.scroll = 0;
                    self.viewport_top.set(None);
                    self.viewport_scroll.set(0);
                    if let Some(messages) = v["messages"].as_array() {
                        for m in messages.iter().rev().take(40).rev() {
                            self.add(
                                m["role"].as_str().unwrap_or("system"),
                                m["content"].as_str().unwrap_or("[multimodal message]"),
                                false,
                            );
                        }
                    }
                    if history_reset && !text("history_notice").is_empty() {
                        self.add("output", &limited(text("history_notice")), false);
                    }
                    self.status = self.tr("Ready", "就绪").into();
                }
                self.ready = true;
            }
            "turn.started" => self.status = self.tr("Working", "正在处理").into(),
            "answer.delta" => {
                self.status = self.tr("Responding", "正在回复").into();
                self.delta("assistant", text("text"));
            }
            "answer.replace" => {
                let start = self.current_start();
                let last = self
                    .entries
                    .iter()
                    .enumerate()
                    .skip(start)
                    .filter(|(_, e)| e.role == "assistant")
                    .map(|(i, _)| i)
                    .next_back();
                // The runtime's authoritative answer concatenates streamed rounds.
                // Keep narration before tools and the final segment after tools.
                if let Some(last) = last.filter(|i| *i + 1 == self.entries.len()) {
                    let prefix: String = self
                        .entries
                        .iter()
                        .skip(start)
                        .take(last - start)
                        .filter(|e| e.role == "assistant")
                        .map(|e| e.text.as_str())
                        .collect();
                    if let Some(tail) = text("text").strip_prefix(&prefix) {
                        self.entries[last].text = limited(tail);
                    } else {
                        for i in (start..self.entries.len()).rev() {
                            if self.entries[i].role == "assistant" {
                                self.entries.remove(i);
                            }
                        }
                        self.add("assistant", text("text"), false);
                    }
                } else {
                    for i in (start..self.entries.len()).rev() {
                        if self.entries[i].role == "assistant" {
                            self.entries.remove(i);
                        }
                    }
                    self.add("assistant", text("text"), false);
                }
                self.trim();
            }
            "output.delta" => self.delta("output", text("text")),
            "tool.started" => {
                self.status = format!("{} {}", self.tr("Running", "正在执行"), safe(text("tool")));
                let params = &v["params"];
                self.approval_context =
                    format!("{}: {}", self.tr("Tool", "工具"), safe(text("tool")));
                for (key, label) in [
                    ("path", self.tr("File", "文件")),
                    ("command", self.tr("Command", "命令")),
                    ("directory", self.tr("Directory", "目录")),
                ] {
                    if let Some(target) = params[key].as_str() {
                        let target: String = safe(target).chars().take(600).collect();
                        self.approval_context
                            .push_str(&format!("\n{label}: {target}"));
                    }
                }
                let target = ["path", "directory", "command", "pattern", "query"]
                    .iter()
                    .find_map(|key| params[*key].as_str())
                    .unwrap_or("");
                self.add(
                    "action",
                    &serde_json::to_string_pretty(params).unwrap_or_default(),
                    false,
                );
                self.entries.back_mut().unwrap().action = Some(Action {
                    tool: safe(text("tool")),
                    target: safe(target).replace('\n', " ").chars().take(600).collect(),
                    done: false,
                    success: false,
                    count: None,
                    exit_code: None,
                    elapsed: None,
                    reused: false,
                    staged: false,
                    cancelled: false,
                    error: String::new(),
                    approval: String::new(),
                });
            }
            "tool.completed" => {
                self.approval_context.clear();
                let ok = v["success"]
                    .as_bool()
                    .ok_or("Tool result requires success")?;
                let start = self.current_start();
                let index = self
                    .entries
                    .iter()
                    .enumerate()
                    .skip(start)
                    .find(|(_, e)| {
                        e.action
                            .as_ref()
                            .is_some_and(|a| !a.done && a.tool == text("tool"))
                    })
                    .map(|(i, _)| i);
                let index = if let Some(index) = index {
                    index
                } else {
                    self.add("action", "{}", false);
                    self.entries.back_mut().unwrap().action = Some(Action {
                        tool: safe(text("tool")),
                        target: safe(text("path")),
                        done: false,
                        success: false,
                        count: None,
                        exit_code: None,
                        elapsed: None,
                        reused: false,
                        staged: false,
                        cancelled: false,
                        error: String::new(),
                        approval: String::new(),
                    });
                    self.entries.len() - 1
                };
                let action = self.entries[index].action.as_mut().unwrap();
                action.done = true;
                action.success = ok;
                action.count = v["item_count"].as_u64();
                action.exit_code = v["exit_code"].as_i64();
                action.success = ok && action.exit_code.is_none_or(|code| code == 0);
                action.elapsed = v["elapsed_s"]
                    .as_f64()
                    .filter(|s| s.is_finite() && *s >= 0.0);
                action.reused = v["reused"].as_bool().unwrap_or(false);
                action.staged = v["staged"].as_bool().unwrap_or(false);
                action.error = limited(text("error"));
                action.approval = safe(text("approval"))
                    .replace(['\n', '\t'], " ")
                    .chars()
                    .take(200)
                    .collect();
                if !text("output").is_empty() {
                    let output = format!(
                        "\n{}\n{}",
                        self.tr("Actual output:", "实际输出:"),
                        limited(text("output"))
                    );
                    self.entries[index].text.push_str(&output);
                }
                self.status = self.tr("Working", "正在处理").into();
            }
            "turn.status" => {
                self.status = safe(&format!("{}: {}", text("state"), text("message")));
                self.add("status", &self.status.clone(), false);
            }
            "approval.requested" | "input.requested" => {
                if self.dialog.is_some() || !v["request_id"].is_string() {
                    return Err("Invalid nested dialog".into());
                }
                let choices = if kind == "approval.requested" || v["choices"].is_array() {
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
                    title: safe(if kind == "approval.requested" || v["choices"].is_array() {
                        text("title")
                    } else {
                        text("prompt")
                    }),
                    context: if kind == "approval.requested" {
                        v["context"]
                            .as_str()
                            .map(safe)
                            .unwrap_or_else(|| self.approval_context.clone())
                    } else {
                        safe(text("context"))
                    },
                    selected: selected.min(choices.len().saturating_sub(1)),
                    context_scroll: 0,
                    context_max_scroll: Cell::new(0),
                    choices,
                    editor: Editor::default(),
                    secret: v["secret"].as_bool().unwrap_or(false),
                    shortcuts: v["shortcuts"].clone(),
                });
                self.status = self.tr("Waiting for your response", "等待你的选择").into();
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
                self.approval_context.clear();
                self.elapsed = self.started.take().map(|start| start.elapsed());
                for entry in self
                    .entries
                    .iter_mut()
                    .filter(|e| e.action.as_ref().is_some_and(|a| !a.done))
                {
                    let action = entry.action.as_mut().unwrap();
                    action.done = true;
                    action.cancelled = text("status") == "cancelled";
                    if !action.cancelled {
                        action.error = "No completion result was received".into();
                    }
                }
                self.status = match text("status") {
                    "ok" | "completed" => self.tr("Completed", "已完成").into(),
                    "cancelled" => self.tr("Cancelled", "已取消").into(),
                    "failed" => self.tr("Failed", "失败").into(),
                    other => safe(other),
                };
                // The runtime's delivery report, written from what the turn
                // did; delivery_report=false hides it here.
                if let Some(report) = v["delivery"]["text"].as_str() {
                    if v["delivery"]["show"].as_bool().unwrap_or(true) {
                        self.add("delivery", &limited(report), false);
                    }
                }
                // The worker says when it has already told the user what went
                // wrong, in their language. Printing the raw text again, with
                // its internal code, under a heading of its own showed one
                // failure three times. Absent means "not shown yet" — the Go
                // runtime never sets it — so the default is to show it.
                let presented = v["presented"].as_bool().unwrap_or(false);
                if !text("error").is_empty() && !presented {
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
        if self.editor.text().trim().is_empty() {
            return None;
        }
        // Enter while a turn runs used to do nothing: the text sat in the box
        // until the turn ended and Enter was pressed again. Codex and Claude
        // Code both queue it and send it when the turn completes.
        if self.ready && self.turn.is_some() {
            let text = self.editor.take();
            self.queued.push_back(text);
            self.scroll = 0;
            return None;
        }
        if !self.ready || self.turn.is_some() {
            return None;
        }
        let text = self.editor.take();
        self.approval_context.clear();
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
        self.turn = Some(id.clone());
        self.started = Some(Instant::now());
        self.elapsed = None;
        self.status = self.tr("Submitting…", "正在提交…").into();
        Some(json!({"type":"turn.submit","protocol":1,"turn_id":id,"text":text}))
    }
    /// The next queued message, submitted, once the session is free for it.
    pub fn next_queued(&mut self) -> Option<Value> {
        if !self.ready || self.turn.is_some() || self.dialog.is_some() {
            return None;
        }
        let text = self.queued.pop_front()?;
        // Whatever is being typed now stays where it is.
        let typing = self.editor.take();
        self.editor.insert(&text);
        let request = self.submit();
        self.editor.insert(&typing);
        request
    }
    pub fn cancel(&mut self) -> Option<Value> {
        // Stopping a turn must not start the next one. Messages still waiting
        // go back into the input box, ahead of anything being typed, so the
        // user can edit or resend them: nothing is sent without another Enter.
        if !self.queued.is_empty() {
            let waiting: Vec<String> = self.queued.drain(..).collect();
            let typing = self.editor.take();
            let mut restored = waiting.join("\n");
            if !typing.is_empty() {
                restored.push('\n');
                restored.push_str(&typing);
            }
            self.editor.insert(&restored);
        }
        self.turn.clone().map(|id| {
            self.status = self.tr("Cancelling…", "正在取消…").into();
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
        self.status = self.tr("Continuing…", "正在继续…").into();
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
    #[test]
    fn model_choices_are_not_treated_as_free_text_or_approvals() {
        let mut a = app();
        a.turn = Some("turn-1".into());
        a.event(json!({"protocol":1,"type":"input.requested","turn_id":"turn-1","request_id":"models","title":"Ollama models","context":"Choose local model","choices":[["qwen:test","local"],["Cancel","Keep current"]]})).unwrap();
        let d = a.dialog.as_ref().unwrap();
        assert_eq!(d.title, "Ollama models");
        assert_eq!(d.context, "Choose local model");
        assert_eq!(d.choices.len(), 2);
        assert_eq!(a.respond(Some(0)).unwrap()["choice"], 0);
    }
    #[test]
    fn reference_completion_does_not_replace_a_new_draft_or_session() {
        let mut a = app();
        a.session = "one".into();
        a.editor.set("read @fi");
        let response = json!({"protocol":1,"type":"completion.result","session_id":"one","text":"read @fi","candidates":["read @file:file.txt"]});
        a.event(response.clone()).unwrap();
        assert_eq!(a.editor.text(), "read @file:file.txt");
        a.editor.set("new draft");
        a.event(response.clone()).unwrap();
        assert_eq!(a.editor.text(), "new draft");
        a.editor.set("read @fi");
        a.session = "two".into();
        a.event(response).unwrap();
        assert_eq!(a.editor.text(), "read @fi");
    }
    fn app() -> App {
        App {
            ready: true,
            ..App::default()
        }
    }
    // ── Sending while a turn runs ────────────────────────────────────────
    // Enter while a turn runs used to do nothing: the text sat in the box and
    // had to be sent by hand once the task ended. It is queued now, and goes
    // out when the turn before it completes — as in Codex and Claude Code.

    fn running(first: &str) -> App {
        let mut a = app();
        a.editor.insert(first);
        assert!(a.submit().is_some());
        assert!(a.turn.is_some());
        a
    }
    fn complete(a: &mut App, id: &str) {
        a.event(json!({"protocol":1,"type":"turn.completed","turn_id":id,"status":"ok"}))
            .unwrap();
    }
    fn sent_text(request: &Value) -> &str {
        request["text"].as_str().unwrap()
    }

    #[test]
    fn a_message_sent_during_a_turn_waits_instead_of_being_lost() {
        let mut a = running("first");
        a.editor.insert("second");
        assert!(a.submit().is_none(), "nothing goes out while the turn runs");
        assert_eq!(a.queued.len(), 1);
        assert!(a.editor.is_empty());
    }

    #[test]
    fn a_waiting_message_is_sent_once_the_turn_completes_not_before() {
        let mut a = running("first");
        a.editor.insert("second");
        a.submit();
        assert!(a.next_queued().is_none(), "the turn is still running");
        complete(&mut a, "turn-1");
        let request = a.next_queued().expect("the turn is done");
        assert_eq!(request["type"], "turn.submit");
        assert_eq!(sent_text(&request), "second");
        assert_eq!(a.turn.as_deref(), Some("turn-2"));
        assert!(a.queued.is_empty());
    }

    #[test]
    fn several_waiting_messages_go_out_one_at_a_time_in_order() {
        let mut a = running("first");
        for text in ["second", "third"] {
            a.editor.insert(text);
            a.submit();
        }
        complete(&mut a, "turn-1");
        assert_eq!(sent_text(&a.next_queued().unwrap()), "second");
        assert!(a.next_queued().is_none(), "second is running; third waits");
        complete(&mut a, "turn-2");
        assert_eq!(sent_text(&a.next_queued().unwrap()), "third");
    }

    #[test]
    fn what_is_being_typed_survives_a_queued_message_going_out() {
        let mut a = running("first");
        a.editor.insert("second");
        a.submit();
        complete(&mut a, "turn-1");
        a.editor.insert("half a thought");
        a.next_queued().unwrap();
        assert_eq!(a.editor.text(), "half a thought");
    }

    #[test]
    fn stopping_a_turn_does_not_start_the_next_one() {
        // Esc means stop. If it also fired off what was waiting, stopping one
        // thing would immediately begin another.
        let mut a = running("first");
        for text in ["second", "third"] {
            a.editor.insert(text);
            a.submit();
        }
        a.editor.insert("typing");
        let request = a.cancel().expect("a turn is running");
        assert_eq!(request["type"], "turn.cancel");
        assert!(a.queued.is_empty());
        assert_eq!(a.editor.text(), "second\nthird\ntyping");
        complete(&mut a, "turn-1");
        assert!(
            a.next_queued().is_none(),
            "nothing is sent without another Enter"
        );
    }

    #[test]
    fn a_question_opened_mid_turn_holds_the_queue_until_the_turn_ends() {
        // The runtime only raises a question for a turn that is running, and
        // completing the turn closes it — so a waiting message never goes out
        // underneath an approval, and is released once the question is gone.
        let mut a = running("first");
        a.editor.insert("second");
        a.submit();
        a.event(json!({
            "protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"approval","title":"Approval",
            "choices":[["Allow once",""],["Decline",""]]
        }))
        .unwrap();
        assert!(a.dialog.is_some(), "the question is open");
        assert!(
            a.next_queued().is_none(),
            "a message must not go out under it"
        );
        assert_eq!(a.queued.len(), 1, "and it is still waiting");

        complete(&mut a, "turn-1");
        assert!(a.dialog.is_none(), "the turn ending closes the question");
        assert_eq!(sent_text(&a.next_queued().unwrap()), "second");
    }

    #[test]
    fn a_blank_message_is_not_queued() {
        let mut a = running("first");
        a.editor.insert("   ");
        assert!(a.submit().is_none());
        assert!(a.queued.is_empty());
    }

    #[test]
    fn idle_enter_still_sends_straight_away() {
        let mut a = app();
        a.editor.insert("hello");
        let request = a.submit().expect("nothing is running");
        assert_eq!(sent_text(&request), "hello");
        assert!(a.queued.is_empty());
    }

    // One failed check reached the screen three times: a status line, the
    // worker's explanation in the user's language, and the raw error with its
    // internal code under a red heading. The worker says when it has already
    // explained the failure; the front end then does not repeat it.

    fn failed_turn(presented: Option<bool>) -> App {
        let mut a = app();
        a.editor.insert("hi");
        a.submit();
        let mut event = json!({
            "protocol":1,"type":"turn.completed","turn_id":"turn-1","status":"failed",
            "error":"[ARIA-4223] Current verified financial data is required",
        });
        if let Some(p) = presented {
            event["presented"] = json!(p);
        }
        a.event(event).unwrap();
        a
    }
    fn errors(a: &App) -> usize {
        a.entries.iter().filter(|e| e.role == "error").count()
    }

    #[test]
    fn an_error_the_worker_already_explained_is_not_printed_again() {
        assert_eq!(errors(&failed_turn(Some(true))), 0);
    }

    #[test]
    fn the_turn_still_counts_as_failed_when_the_error_text_is_not_repeated() {
        let a = failed_turn(Some(true));
        assert!(a.turn.is_none());
        assert_eq!(a.status, "Failed");
    }

    #[test]
    fn an_error_nobody_explained_is_still_shown() {
        assert_eq!(errors(&failed_turn(Some(false))), 1);
    }

    #[test]
    fn a_runtime_that_does_not_say_shows_the_error() {
        // The Go runtime emits turn.completed without the field. Silence must
        // never hide a failure.
        assert_eq!(errors(&failed_turn(None)), 1);
    }

    #[test]
    fn both_runtime_packets_draw_the_same_mascot_and_respect_hidden_banners() {
        let legacy = json!([
            [{"text": "▗▛▀▀▀▀▀▜▖"}], [{"text": "▌▌▗▖ ▗▖▐▐"}],
            [{"text": "▐▙▄▄▄▄▄▟▌"}], [{"text": "▝▀▀▀▀▀▀▀▘"}]
        ]);
        let packet = |robot: Value, design: &str| {
            json!({
                "protocol": 1, "type": "session.ready", "session_id": "robot-test",
                "commands": [], "ui_theme": "light", "robot": robot, "robot_design": design
            })
        };
        let mut python = app();
        python
            .event(packet(legacy.clone(), "cream-block-v2"))
            .unwrap();
        let mut go = app();
        go.event(packet(json!({"design": "cream-block-v2"}), ""))
            .unwrap();
        assert_eq!(python.robot.0, go.robot.0);
        assert_eq!(python.robot.0.len(), 4);
        python.event(packet(json!([]), "cream-block-v2")).unwrap();
        assert!(python.robot.0.is_empty());
        go.event(packet(json!([]), "")).unwrap();
        assert!(go.robot.0.is_empty());
        // A legacy worker without the optional field remains supported.
        python.event(packet(legacy, "")).unwrap();
        assert_eq!(python.robot.0.len(), 4);
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
        a.approval_context = "File: unrelated.txt".into();
        a.event(json!({"protocol":1,"type":"input.requested","turn_id":"turn-1","request_id":"secret","prompt":"Token","secret":true})).unwrap();
        assert!(a.dialog.as_ref().unwrap().context.is_empty());
        a.dialog.as_mut().unwrap().editor.insert("SECRET");
        let req = a.respond(None).unwrap();
        assert_eq!(req["text"], "SECRET");
        assert!(!format!("{:?}", a.entries).contains("SECRET"));
        assert_eq!(a.editor.text(), "");
    }
    #[test]
    fn streamed_narration_tools_and_final_answer_keep_event_order() {
        let mut a = app();
        a.add("assistant", "older answer", false);
        a.editor.insert("Read README.md");
        a.submit();
        a.event(json!({"protocol":1,"type":"answer.delta","turn_id":"turn-1","text":"Reading.\n"}))
            .unwrap();
        a.event(json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"read_file","params":{"path":"README.md"}})).unwrap();
        a.event(json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"read_file","success":true})).unwrap();
        a.event(
            json!({"protocol":1,"type":"answer.delta","turn_id":"turn-1","text":"The result."}),
        )
        .unwrap();
        a.event(json!({"protocol":1,"type":"answer.replace","turn_id":"turn-1","text":"Reading.\nThe result."})).unwrap();
        assert_eq!(
            a.entries
                .iter()
                .map(|e| e.role.as_str())
                .collect::<Vec<_>>(),
            vec!["assistant", "user", "assistant", "action", "assistant"]
        );
        assert_eq!(a.entries.back().unwrap().text, "The result.");
        assert_eq!(a.entries.front().unwrap().text, "older answer");
    }
    #[test]
    fn parallel_targets_finish_in_place_and_authoritative_answer_follows_tools() {
        let mut a = app();
        a.editor.insert("Read two files");
        a.submit();
        for path in ["one.rs", "two.rs"] {
            a.event(json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"read_file","params":{"path":path}})).unwrap();
        }
        for ok in [true, false] {
            a.event(json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"read_file","success":ok,"error":if ok {""} else {"not found"}})).unwrap();
        }
        a.event(json!({"protocol":1,"type":"answer.replace","turn_id":"turn-1","text":"Only one exists."})).unwrap();
        assert_eq!(a.entries.len(), 4);
        assert_eq!(a.entries[1].action.as_ref().unwrap().target, "one.rs");
        assert!(a.entries[1].action.as_ref().unwrap().success);
        assert_eq!(a.entries[2].action.as_ref().unwrap().target, "two.rs");
        assert!(!a.entries[2].action.as_ref().unwrap().success);
        assert_eq!(a.entries[3].text, "Only one exists.");
    }
    #[test]
    fn approval_uses_actual_operation_context_and_cancel_settles_pending_actions() {
        let mut a = app();
        a.editor.insert("Write two files");
        a.submit();
        a.approval_context = "File: two.rs".into();
        a.event(json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1","request_id":"approve","title":"Approve","choices":[["Yes", ""]],"context":"File: one.rs"})).unwrap();
        assert_eq!(a.dialog.as_ref().unwrap().context, "File: one.rs");
        a.event(json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"read_file","params":{"path":"one.rs"}})).unwrap();
        a.event(
            json!({"protocol":1,"type":"turn.completed","turn_id":"turn-1","status":"cancelled"}),
        )
        .unwrap();
        assert!(a.entries.back().unwrap().action.as_ref().unwrap().cancelled);
        assert!(a.started.is_none() && a.elapsed.is_some());
        assert_eq!(a.status, "Cancelled");
    }
    #[test]
    fn approvals_keep_the_target_without_reusing_completed_tool_context() {
        let mut a = app();
        a.editor.insert("Create a note");
        a.submit();
        a.event(json!({"protocol":1,"type":"tool.started","turn_id":"turn-1",
            "tool":"write_file","params":{"path":"/project/native-note.txt","content":"PRIVATE_BODY"}})).unwrap();
        a.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"write","title":"Approval","choices":[["Allow",""],["Decline",""]]}),
        )
        .unwrap();
        assert_eq!(
            a.dialog.as_ref().unwrap().context,
            "Tool: write_file\nFile: /project/native-note.txt"
        );
        assert!(!a.dialog.as_ref().unwrap().context.contains("PRIVATE_BODY"));
        a.respond(Some(0));
        a.event(json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"write_file","success":true})).unwrap();
        a.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"model","title":"Choose model","choices":[["Google",""]]}),
        )
        .unwrap();
        assert!(a.dialog.as_ref().unwrap().context.is_empty());
        a.respond(Some(0));
        a.event(
            json!({"protocol":1,"type":"tool.started","turn_id":"turn-1",
            "tool":"shell","params":{"command":"git status","directory":"/project"}}),
        )
        .unwrap();
        a.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"shell","title":"Approval","choices":[["Allow",""]]}),
        )
        .unwrap();
        assert_eq!(
            a.dialog.as_ref().unwrap().context,
            "Tool: shell\nCommand: git status\nDirectory: /project"
        );
        a.event(
            json!({"protocol":1,"type":"turn.completed","turn_id":"turn-1","status":"cancelled"}),
        )
        .unwrap();
        assert!(a.approval_context.is_empty());
        assert!(a.dialog.is_none());
    }
    #[test]
    fn auto_approval_is_shown_on_the_action() {
        let mut a = app();
        a.editor.insert("Edit a.txt");
        a.submit();
        a.event(json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"write_file","params":{"path":"a.txt"}})).unwrap();
        a.event(json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"write_file","success":true,
            "approval":"auto-approved · L1 low · Edit files\u{1b}[2J"})).unwrap();
        let action = a.entries.back().unwrap().action.as_ref().unwrap();
        assert_eq!(action.approval, "auto-approved · L1 low · Edit files[2J");
    }

    #[test]
    fn delivery_report_follows_the_answer_unless_hidden() {
        let mut a = app();
        a.editor.insert("Fix it");
        a.submit();
        a.event(json!({"protocol":1,"type":"answer.replace","turn_id":"turn-1","text":"Fixed."}))
            .unwrap();
        a.event(
            json!({"protocol":1,"type":"turn.completed","turn_id":"turn-1","status":"ok",
            "delivery":{"text":"DONE\n\nChanged\n  M a.py  +1 -0","show":true}}),
        )
        .unwrap();
        let last = a.entries.back().unwrap();
        assert_eq!(
            (last.role.as_str(), last.text.as_str()),
            ("delivery", "DONE\n\nChanged\n  M a.py  +1 -0")
        );
        a.editor.insert("Again");
        a.submit();
        a.event(
            json!({"protocol":1,"type":"turn.completed","turn_id":"turn-2","status":"ok",
            "delivery":{"text":"DONE","show":false}}),
        )
        .unwrap();
        assert_ne!(a.entries.back().unwrap().role, "delivery");
    }

    #[test]
    fn command_exit_and_output_are_kept_even_when_invocation_succeeded() {
        let mut a = app();
        a.editor.insert("Run tests");
        a.submit();
        a.event(
            json!({"protocol":1,"type":"tool.started","turn_id":"turn-1",
            "tool":"run_command","params":{"command":"cargo test"}}),
        )
        .unwrap();
        a.event(
            json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1",
            "tool":"run_command","success":true,"exit_code":7,
            "output":"stderr:\nassertion failed\u{1b}"}),
        )
        .unwrap();
        let row = a.entries.back().unwrap();
        let action = row.action.as_ref().unwrap();
        assert_eq!(action.exit_code, Some(7));
        assert!(!action.success);
        assert!(row.text.contains("assertion failed"));
        assert!(!row.text.contains('\u{1b}'));
    }
    #[test]
    fn explicit_history_reset_removes_rewound_turns_and_keeps_the_draft() {
        let mut a = app();
        a.session = "history-reset-test".into();
        a.add("user", "Old request", false);
        a.add("assistant", "Old answer", false);
        a.editor.insert("Retained draft");
        a.event(
            json!({"protocol":1,"type":"session.state","session_id":a.session,
            "commands":[],"messages":[],"history_reset":true,"history_notice":"Undone."}),
        )
        .unwrap();
        assert_eq!(a.entries.len(), 1);
        assert_eq!(a.entries[0].text, "Undone.");
        assert_eq!(a.editor.text(), "Retained draft");
    }
    #[test]
    fn update_notice_does_not_change_the_active_turn_or_permissions() {
        let mut a = app();
        a.editor.insert("Inspect my project");
        a.submit();
        a.event(
            json!({"protocol":1,"type":"update.available","turn_id":null,
            "text":"Update available\n v0.138.0 · aria update\u{1b}"}),
        )
        .unwrap();
        assert_eq!(a.turn.as_deref(), Some("turn-1"));
        assert!(a.dialog.is_none());
        assert_eq!(a.update_notice, "Update available v0.138.0 · aria update");
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
