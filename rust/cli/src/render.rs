//! A bounded, single-turn JSONL consumer. Rendering never executes event data.
use serde_json::Value;
use std::io::{BufRead, Read, Write};

pub const MAX_EVENT: usize = 1_048_576;
const MAX_STREAM: usize = 16 * MAX_EVENT;
const MAX_EVENTS: usize = 50_000;

/// Remove terminal control characters from display only. JSONL retains data.
fn display(text: &str) -> String {
    text.chars()
        .filter(|c| {
            (!c.is_control() || *c == '\n' || *c == '\t')
                && !matches!(*c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        })
        .collect()
}

fn label(text: &str) -> String {
    display(text)
        .chars()
        .take(400)
        .map(|c| if c == '\n' || c == '\t' { ' ' } else { c })
        .collect()
}

pub struct Renderer {
    jsonl: bool,
    started: bool,
    completed: Option<bool>,
    events: usize,
    bytes: usize,
    answer_bytes: usize,
    // The gateway's final response contains tokens from all provider rounds.
    streamed: String,
    line_open: bool,
}

impl Renderer {
    pub fn new(jsonl: bool) -> Self {
        Self {
            jsonl,
            started: false,
            completed: None,
            events: 0,
            bytes: 0,
            answer_bytes: 0,
            streamed: String::new(),
            line_open: false,
        }
    }

    fn line_end(&mut self, out: &mut impl Write) -> Result<(), String> {
        if self.line_open {
            writeln!(out).map_err(|e| format!("Writing answer: {e}"))?;
            out.flush().map_err(|e| e.to_string())?;
            self.line_open = false;
        }
        Ok(())
    }

    fn text(&mut self, text: &str, out: &mut impl Write) -> Result<(), String> {
        let text = display(text);
        if !text.is_empty() {
            write!(out, "{text}").map_err(|e| format!("Writing answer: {e}"))?;
            out.flush().map_err(|e| e.to_string())?;
            self.line_open = !text.ends_with('\n');
        }
        Ok(())
    }

    pub fn consume(
        &mut self,
        bytes: &[u8],
        out: &mut impl Write,
        diagnostics: &mut impl Write,
    ) -> Result<(), String> {
        self.events += 1;
        self.bytes = self.bytes.saturating_add(bytes.len());
        if bytes.len() > MAX_EVENT || self.bytes > MAX_STREAM || self.events > MAX_EVENTS {
            return Err("Event stream exceeds its size or record limit".into());
        }
        let event: Value = serde_json::from_slice(bytes)
            .map_err(|_| "Invalid UTF-8 JSON event (worker stdout must contain JSONL only)")?;
        let kind = event["type"]
            .as_str()
            .ok_or("Event requires a string type")?;
        if self.completed.is_some() {
            return Err("Event after turn.completed".into());
        }
        if !self.started && kind != "turn.started" {
            return Err("Event stream must begin with turn.started".into());
        }
        // Validate before emitting anything from this record, including JSONL.
        match kind {
            "turn.started" => {
                if self.started || !event["model"].is_string() || !event["prompt"].is_string() {
                    return Err("Invalid or duplicate turn.started".into());
                }
                self.started = true;
            }
            "answer.delta" => {
                let text = event["text"].as_str().ok_or("answer.delta requires text")?;
                self.answer_bytes = self.answer_bytes.saturating_add(text.len());
                if self.answer_bytes > MAX_EVENT {
                    return Err("Accumulated answer exceeds 1 MiB".into());
                }
            }
            "turn.status" => {
                if !event["state"].is_string() || !event["message"].is_string() {
                    return Err("turn.status requires state and message".into());
                }
            }
            "tool.started" | "tool.completed" => {
                if event["tool"].as_str().is_none_or(str::is_empty)
                    || (kind == "tool.started" && !event["params"].is_object())
                    || (kind == "tool.completed" && !event["success"].is_boolean())
                {
                    return Err("Invalid tool event".into());
                }
            }
            "turn.completed" => {
                let success = event["success"]
                    .as_bool()
                    .ok_or("turn.completed requires boolean success")?;
                if !event["response"].is_string() {
                    return Err("turn.completed requires response text".into());
                }
                if success && event.pointer("/acceptance/verified") == Some(&Value::Bool(false)) {
                    return Err("Successful turn has failed acceptance checks".into());
                }
                self.completed = Some(success);
            }
            _ => return Err(format!("Unsupported event type: {}", label(kind))),
        }
        if self.jsonl {
            writeln!(out, "{event}").map_err(|e| format!("Writing event: {e}"))?;
            out.flush().map_err(|e| e.to_string())?;
            return Ok(());
        }
        match kind {
            "turn.started" => {
                writeln!(
                    diagnostics,
                    "[Aria] {}",
                    label(event["model"].as_str().unwrap())
                )
                .map_err(|e| e.to_string())?;
            }
            "answer.delta" => {
                let text = event["text"].as_str().unwrap();
                self.streamed.push_str(text);
                self.text(text, out)?;
            }
            "turn.status" => {
                self.line_end(out)?;
                writeln!(
                    diagnostics,
                    "[{}] {}",
                    label(event["state"].as_str().unwrap()),
                    label(event["message"].as_str().unwrap())
                )
                .map_err(|e| e.to_string())?;
            }
            "tool.started" | "tool.completed" => {
                self.line_end(out)?;
                let status = if kind == "tool.started" {
                    "running"
                } else if event["success"] == true {
                    "ok"
                } else {
                    "failed"
                };
                let error = event["error"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .map(|s| format!(": {}", label(s)))
                    .unwrap_or_default();
                writeln!(
                    diagnostics,
                    "[{status}] {}{error}",
                    label(event["tool"].as_str().unwrap())
                )
                .map_err(|e| e.to_string())?;
            }
            "turn.completed" => {
                let response = event["response"].as_str().unwrap();
                if let Some(tail) = response.strip_prefix(&self.streamed) {
                    self.text(tail, out)?;
                } else {
                    // A retry or acceptance repair can replace earlier streamed
                    // prose. Preserve it as progress and label the final answer.
                    self.line_end(out)?;
                    writeln!(diagnostics, "[final response]").map_err(|e| e.to_string())?;
                    self.text(response, out)?;
                }
                self.line_end(out)?;
                if self.completed == Some(false) {
                    let error = event["error"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("Turn failed");
                    writeln!(diagnostics, "[failed] {}", label(error))
                        .map_err(|e| e.to_string())?;
                }
            }
            _ => unreachable!(),
        }
        diagnostics.flush().map_err(|e| e.to_string())
    }

    pub fn finish(&mut self, out: &mut impl Write) -> Result<i32, String> {
        self.line_end(out)?;
        self.completed
            .map(|success| if success { 0 } else { 1 })
            .ok_or_else(|| "Incomplete event stream: missing turn.completed".into())
    }
}

/// Do not allocate an unbounded line when a worker or replay file is corrupt.
pub fn read_event(reader: &mut impl BufRead) -> Result<Option<Vec<u8>>, String> {
    let mut bytes = Vec::new();
    let count = reader
        .take((MAX_EVENT + 1) as u64)
        .read_until(b'\n', &mut bytes)
        .map_err(|e| format!("Reading event: {e}"))?;
    if count > MAX_EVENT {
        return Err("Event exceeds 1 MiB".into());
    }
    Ok((count > 0).then_some(bytes))
}

pub fn replay(reader: &mut impl BufRead, jsonl: bool) -> Result<i32, String> {
    let mut renderer = Renderer::new(jsonl);
    let mut out = std::io::stdout().lock();
    let mut diagnostics = std::io::stderr().lock();
    while let Some(bytes) = read_event(reader)? {
        renderer.consume(&bytes, &mut out, &mut diagnostics)?;
    }
    renderer.finish(&mut out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn run(events: &[Value], jsonl: bool) -> (Result<i32, String>, String, String) {
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let mut renderer = Renderer::new(jsonl);
        let result = events
            .iter()
            .try_for_each(|e| renderer.consume(e.to_string().as_bytes(), &mut out, &mut err))
            .and_then(|()| renderer.finish(&mut out));
        (
            result,
            String::from_utf8(out).unwrap(),
            String::from_utf8(err).unwrap(),
        )
    }
    fn start() -> Value {
        json!({"type":"turn.started","prompt":"hi","model":"google/gemini"})
    }
    fn end(response: &str) -> Value {
        json!({"type":"turn.completed","success":true,"response":response})
    }

    #[test]
    fn visible_text_is_streamed_once_and_control_sequences_are_inert() {
        let (result, out, err) = run(
            &[
                start(),
                json!({"type":"answer.delta","text":"你好\n```rust\n"}),
                json!({"type":"answer.delta","text":"fn main() {}\n```\u{1b}[2J"}),
                end("你好\n```rust\nfn main() {}\n```\u{1b}[2J\n"),
            ],
            false,
        );
        assert_eq!(result.unwrap(), 0);
        assert_eq!(out, "你好\n```rust\nfn main() {}\n```[2J\n");
        assert!(!err.contains("hi"));
    }
    #[test]
    fn repairs_and_failed_turns_are_visible() {
        let mut end = end("corrected");
        end["success"] = json!(false);
        end["error"] = json!("checks_failed");
        let (result, out, err) = run(
            &[start(), json!({"type":"answer.delta","text":"draft"}), end],
            false,
        );
        assert_eq!(result.unwrap(), 1);
        assert_eq!(out, "draft\ncorrected\n");
        assert!(err.contains("[final response]"));
        assert!(err.contains("checks_failed"));
    }
    #[test]
    fn accumulated_multiround_response_is_not_printed_twice() {
        let (result, out, err) = run(
            &[
                start(),
                json!({"type":"answer.delta","text":"Inspecting"}),
                json!({"type":"tool.started","tool":"read_file","params":{"path":"main.py"}}),
                json!({"type":"tool.completed","tool":"read_file","success":true}),
                json!({"type":"answer.delta","text":"Fixed"}),
                end("InspectingFixed"),
            ],
            false,
        );
        assert_eq!(result.unwrap(), 0);
        assert_eq!(out, "Inspecting\nFixed\n");
        assert!(err.contains("[running] read_file"));
        assert!(!err.contains("[final response]"));
    }
    #[test]
    fn machine_output_preserves_fields_and_has_no_terminal_chrome() {
        let events = [
            start(),
            json!({"type":"answer.delta","text":"中文\u{1b}","extra":1}),
            end("中文\u{1b}"),
        ];
        let (result, out, err) = run(&events, true);
        assert_eq!(result.unwrap(), 0);
        assert!(err.is_empty());
        assert_eq!(
            out.lines()
                .map(|l| serde_json::from_str::<Value>(l).unwrap())
                .collect::<Vec<_>>(),
            events
        );
    }
    #[test]
    fn rejects_truncated_polluted_or_out_of_order_protocol() {
        for events in [
            vec![],
            vec![start()],
            vec![end("bad")],
            vec![start(), start()],
            vec![start(), json!({"type":"answer.delta","text":1})],
            vec![start(), json!({"type":"unknown"})],
            vec![start(), end("ok"), end("extra")],
            vec![
                start(),
                json!({"type":"turn.completed","success":true,"response":"bad","acceptance":{"verified":false}}),
            ],
        ] {
            assert!(run(&events, false).0.is_err());
        }
        assert!(read_event(&mut std::io::Cursor::new(vec![b'x'; MAX_EVENT + 1])).is_err());
        assert!(Renderer::new(false)
            .consume(b"not json\n", &mut Vec::new(), &mut Vec::new())
            .is_err());
    }
}
