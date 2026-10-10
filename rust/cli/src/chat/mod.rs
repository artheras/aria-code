mod app;
mod editor;
mod transport;
mod ui;

use app::App;
use crossterm::{
    event::{
        self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
        Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind,
    },
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{backend::CrosstermBackend, Terminal};
use serde_json::{json, Value};
use std::{
    ffi::{OsStr, OsString},
    io::{self, BufReader, IsTerminal, Write},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};
use transport::Session;

struct Screen;
impl Drop for Screen {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            DisableBracketedPaste,
            DisableMouseCapture,
            LeaveAlternateScreen,
            crossterm::cursor::Show
        );
    }
}

pub fn run(
    python: &OsStr,
    workspace: &Path,
    args: &[OsString],
    timeout: Duration,
    jsonl: bool,
) -> Result<i32, String> {
    if !jsonl && (!io::stdin().is_terminal() || !io::stdout().is_terminal()) {
        return Err(
            "chat requires a terminal; use chat --jsonl for the application protocol".into(),
        );
    }
    let cancelled = Arc::new(AtomicBool::new(false));
    let signal = cancelled.clone();
    ctrlc::set_handler(move || signal.store(true, Ordering::SeqCst)).map_err(|e| e.to_string())?;
    let mut session = Session::spawn(python, workspace, args)?;
    if jsonl {
        return machine(&mut session, timeout, &cancelled);
    }
    let _screen = Screen;
    enable_raw_mode().map_err(|e| e.to_string())?;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableMouseCapture
    )
    .map_err(|e| e.to_string())?;
    let mut terminal =
        Terminal::new(CrosstermBackend::new(io::stdout())).map_err(|e| e.to_string())?;
    let mut app = App {
        status: "Starting Aria runtime…".into(),
        ..App::default()
    };
    let mut deadline = Some(Instant::now() + timeout);
    let mut shutting_down = false;
    let mut external_signal = false;
    let mut dirty = true;
    loop {
        for _ in 0..128 {
            match session.events.try_recv() {
                Ok(Ok(Some(value))) => {
                    dirty = true;
                    let kind = value["type"].as_str().unwrap_or("").to_owned();
                    app.event(value)?;
                    if !shutting_down
                        && matches!(
                            kind.as_str(),
                            "session.ready"
                                | "turn.completed"
                                | "approval.requested"
                                | "input.requested"
                        )
                    {
                        deadline = None;
                    } else if !shutting_down && kind == "dialog.closed" {
                        deadline = Some(Instant::now() + timeout);
                    }
                }
                Ok(Ok(None)) => {
                    if !app.closed {
                        return Err("Application runtime disconnected unexpectedly".into());
                    }
                    break;
                }
                Ok(Err(error)) => return Err(error),
                Err(mpsc::TryRecvError::Empty) => break,
                Err(_) => return Err("Application reader disconnected".into()),
            }
        }
        if dirty {
            terminal
                .draw(|frame| ui::draw(frame, &app))
                .map_err(|e| e.to_string())?;
            dirty = false;
        }
        if app.closed {
            if app.failed {
                return Err(app
                    .entries
                    .iter()
                    .rev()
                    .find(|e| e.role == "error")
                    .map(|e| e.text.clone())
                    .unwrap_or_else(|| "Application runtime failed".into()));
            }
            break;
        }
        if cancelled.swap(false, Ordering::SeqCst) {
            external_signal = true;
            shutting_down = true;
            session.send(json!({"type":"shutdown","protocol":1}))?;
            deadline = Some(Instant::now() + Duration::from_secs(2));
        }
        if deadline.is_some_and(|end| Instant::now() >= end) {
            if shutting_down {
                return Ok(if external_signal { 130 } else { 0 });
            }
            return Err(
                "Application runtime timed out; worker and child processes were stopped".into(),
            );
        }
        if let Some(status) = session.worker.child.try_wait().map_err(|e| e.to_string())? {
            return Err(format!("Application runtime exited with {status}"));
        }
        if event::poll(Duration::from_millis(25)).map_err(|e| e.to_string())? {
            dirty = true;
            let request = match event::read().map_err(|e| e.to_string())? {
                Event::Key(key) if key.kind != KeyEventKind::Release => key_request(&mut app, key),
                Event::Paste(text) => {
                    if let Some(d) = &mut app.dialog {
                        if d.choices.is_empty() {
                            d.editor.insert(&text);
                        }
                    } else {
                        app.editor.insert(&text);
                    }
                    None
                }
                Event::Mouse(mouse) => {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => app.scroll = app.scroll.saturating_add(3),
                        MouseEventKind::ScrollDown => app.scroll = app.scroll.saturating_sub(3),
                        _ => (),
                    }
                    None
                }
                _ => None,
            };
            if let Some(request) = request {
                let kind = request["type"].as_str().unwrap_or("");
                if kind == "shutdown" {
                    shutting_down = true;
                }
                deadline = Some(
                    Instant::now()
                        + if kind == "turn.cancel" || shutting_down {
                            Duration::from_secs(2)
                        } else {
                            timeout
                        },
                );
                session.send(request)?;
            }
        }
    }
    // A closed event is not sufficient to report success if Python exits 1.
    let end = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = session.worker.child.try_wait().map_err(|e| e.to_string())? {
            return Ok(if external_signal {
                130
            } else if app.failed || !status.success() {
                status.code().filter(|c| *c != 0).unwrap_or(1)
            } else {
                0
            });
        }
        if Instant::now() >= end {
            return Err("Application did not exit after session.closed".into());
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn key_request(app: &mut App, key: KeyEvent) -> Option<Value> {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    let alt = key.modifiers.contains(KeyModifiers::ALT);
    if key.code == KeyCode::F(1) {
        app.help = !app.help;
        return None;
    }
    if app.help {
        if key.code == KeyCode::Esc {
            app.help = false;
        }
        return None;
    }
    if ctrl && key.code == KeyCode::Char('o') {
        app.details = !app.details;
        return None;
    }
    if let Some(d) = app.dialog.as_mut() {
        if key.code == KeyCode::Esc || (ctrl && key.code == KeyCode::Char('c')) {
            if d.choices.is_empty() {
                return app.cancel();
            }
            return app.respond(Some(-1));
        }
        if !d.choices.is_empty() {
            match key.code {
                KeyCode::Up => d.selected = d.selected.saturating_sub(1),
                KeyCode::Down => d.selected = (d.selected + 1).min(d.choices.len() - 1),
                KeyCode::Enter => return app.respond(None),
                KeyCode::Char(c) => {
                    let choice = d.shortcuts[c.to_string()]
                        .as_i64()
                        .or_else(|| c.to_digit(10).map(|i| i64::from(i) - 1));
                    if let Some(i) = choice.filter(|i| *i >= 0 && (*i as usize) < d.choices.len()) {
                        return app.respond(Some(i));
                    }
                }
                _ => (),
            }
            return None;
        }
        if key.code == KeyCode::Enter && !alt && !key.modifiers.contains(KeyModifiers::SHIFT) {
            return app.respond(None);
        }
        edit(&mut d.editor, key);
        return None;
    }
    match key.code {
        KeyCode::Esc => {
            if app.turn.is_some() {
                return app.cancel();
            }
            app.scroll = 0;
        }
        KeyCode::Char('c') if ctrl => {
            if app.turn.is_some() {
                return app.cancel();
            }
            app.editor.clear();
        }
        KeyCode::Char('d') if ctrl && app.editor.is_empty() => {
            return Some(json!({"type":"shutdown","protocol":1}))
        }
        KeyCode::Enter if !alt && !key.modifiers.contains(KeyModifiers::SHIFT) => {
            return app.submit()
        }
        KeyCode::Tab => app.complete(),
        KeyCode::PageUp => app.scroll = app.scroll.saturating_add(10),
        KeyCode::PageDown => app.scroll = app.scroll.saturating_sub(10),
        _ => edit(&mut app.editor, key),
    }
    None
}
fn edit(editor: &mut editor::Editor, key: KeyEvent) {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Enter | KeyCode::Char('j') if key.code == KeyCode::Enter || ctrl => {
            editor.insert("\n")
        }
        KeyCode::Char('u') if ctrl => editor.kill_before(),
        KeyCode::Char('k') if ctrl => editor.kill_after(),
        KeyCode::Char('w') if ctrl => editor.kill_word(),
        KeyCode::Char('a') if ctrl => editor.home(),
        KeyCode::Char('e') if ctrl => editor.end(),
        KeyCode::Char('d') if ctrl => editor.delete(),
        KeyCode::Backspace if key.modifiers.contains(KeyModifiers::ALT) => editor.kill_word(),
        KeyCode::Backspace => editor.backspace(),
        KeyCode::Delete => editor.delete(),
        KeyCode::Left => editor.left(),
        KeyCode::Right => editor.right(),
        KeyCode::Home => editor.home(),
        KeyCode::End => editor.end(),
        KeyCode::Up => {
            if !editor.up() {
                editor.history_prev();
            }
        }
        KeyCode::Down => {
            if !editor.down() {
                editor.history_next();
            }
        }
        KeyCode::Char(c) if !ctrl && !key.modifiers.contains(KeyModifiers::ALT) => {
            editor.insert(&c.to_string())
        }
        _ => (),
    }
}

fn machine(
    session: &mut Session,
    timeout: Duration,
    cancelled: &AtomicBool,
) -> Result<i32, String> {
    let (tx, requests) = mpsc::sync_channel(32);
    thread::spawn(move || {
        let mut reader = BufReader::new(io::stdin());
        loop {
            let result = crate::render::read_event(&mut reader).and_then(|v| {
                v.map(|bytes| serde_json::from_slice::<Value>(&bytes).map_err(|e| e.to_string()))
                    .transpose()
            });
            let done = !matches!(result, Ok(Some(_)));
            if tx.send(result).is_err() || done {
                break;
            }
        }
    });
    let (out_tx, out_rx) = mpsc::sync_channel::<Value>(32);
    let (written_tx, written_rx) = mpsc::channel();
    thread::spawn(move || {
        let mut out = io::stdout().lock();
        let result = (|| {
            while let Ok(value) = out_rx.recv() {
                serde_json::to_writer(&mut out, &value).map_err(|e| e.to_string())?;
                out.write_all(b"\n")
                    .and_then(|_| out.flush())
                    .map_err(|e| e.to_string())?;
            }
            Ok::<_, String>(())
        })();
        let _ = written_tx.send(result);
    });
    let mut deadline = Some(Instant::now() + timeout);
    let mut eof = false;
    let mut closed = false;
    loop {
        if cancelled.load(Ordering::SeqCst) {
            return Err("Cancelled".into());
        }
        if deadline.is_some_and(|t| Instant::now() >= t) {
            return Err("Application runtime timed out".into());
        }
        match session.events.recv_timeout(Duration::from_millis(10)) {
            Ok(Ok(Some(value))) => {
                if value["protocol"] != 1 || !value["type"].is_string() {
                    return Err("Invalid application event".into());
                }
                match value["type"].as_str().unwrap() {
                    "session.ready" | "turn.completed" if !eof => deadline = None,
                    "approval.requested" | "input.requested" if !eof => deadline = None,
                    "dialog.closed" => deadline = Some(Instant::now() + timeout),
                    "session.closed" => {
                        closed = true;
                        deadline = Some(Instant::now() + Duration::from_secs(2));
                    }
                    _ => (),
                }
                out_tx
                    .try_send(value)
                    .map_err(|_| "Application output queue is full or closed")?;
            }
            Ok(Ok(None)) => {
                if !closed {
                    return Err("Application runtime disconnected unexpectedly".into());
                }
                break;
            }
            Ok(Err(error)) => return Err(error),
            Err(mpsc::RecvTimeoutError::Timeout) => (),
            Err(_) => return Err("Application event reader disconnected".into()),
        }
        if !eof {
            match requests.try_recv() {
                Ok(Ok(Some(request))) => {
                    match request["type"].as_str() {
                        Some(
                            "turn.submit" | "dialog.respond" | "session.resume" | "session.new",
                        ) => deadline = Some(Instant::now() + timeout),
                        Some("shutdown" | "turn.cancel") => {
                            if request["type"] == "shutdown" {
                                eof = true;
                            }
                            deadline = Some(Instant::now() + Duration::from_secs(2))
                        }
                        _ => (),
                    }
                    session.send(request)?;
                }
                Ok(Ok(None)) => {
                    eof = true;
                    session.send(json!({"type":"shutdown","protocol":1}))?;
                    deadline = Some(Instant::now() + Duration::from_secs(2));
                }
                Ok(Err(error)) => return Err(error),
                Err(mpsc::TryRecvError::Empty) => (),
                Err(_) => eof = true,
            }
        }
    }
    drop(out_tx);
    written_rx
        .recv_timeout(timeout)
        .map_err(|_| "Application output could not be flushed")??;
    let end = Instant::now() + Duration::from_secs(2);
    loop {
        if let Some(status) = session.worker.child.try_wait().map_err(|e| e.to_string())? {
            return Ok(status.code().unwrap_or(1));
        }
        if Instant::now() >= end {
            return Err("Application did not exit after session.closed".into());
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn approval_shortcuts_are_explicit_and_scoped() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.editor.insert("write a file");
        app.submit();
        app.event(
            json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1","request_id":"a",
            "choices":[["Yes","once"],["No","decline"]],"shortcuts":{"y":0,"n":1}}),
        )
        .unwrap();
        let request = key_request(
            &mut app,
            KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE),
        )
        .unwrap();
        assert_eq!(request["choice"], 1);
        assert_eq!(request["turn_id"], "turn-1");
        assert_eq!(request["request_id"], "a");
        assert!(app.dialog.is_none());
    }
    #[test]
    fn draft_and_cancel_are_independent() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.editor.insert("task");
        app.submit();
        app.editor.insert("next draft");
        assert!(key_request(&mut app, KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)).is_none());
        let cancel =
            key_request(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)).unwrap();
        assert_eq!(cancel["type"], "turn.cancel");
        assert_eq!(app.editor.text(), "next draft");
    }
}
