//! Native Rust Ollama HTTP client & health probe.
//! Eliminates Python cold-start overhead for local service checks and chat streaming.

use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader},
    time::{Duration, Instant},
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OllamaHealth {
    pub ok: bool,
    pub models: Vec<String>,
    pub latency_ms: u128,
    pub error: Option<String>,
}

/// Probe a local or remote Ollama server via `/api/tags`.
pub fn probe_ollama(base_url: &str, timeout: Duration) -> OllamaHealth {
    let url = format!("{}/api/tags", base_url.trim_end_matches('/'));
    let start = Instant::now();
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();
    match agent.get(&url).call() {
        Ok(mut resp) => {
            let latency_ms = start.elapsed().as_millis();
            match resp.body_mut().read_to_string() {
                Ok(body) => {
                    let models = parse_tags_models(&body);
                    OllamaHealth {
                        ok: true,
                        models,
                        latency_ms,
                        error: None,
                    }
                }
                Err(err) => OllamaHealth {
                    ok: false,
                    models: Vec::new(),
                    latency_ms,
                    error: Some(err.to_string()),
                },
            }
        }
        Err(err) => OllamaHealth {
            ok: false,
            models: Vec::new(),
            latency_ms: start.elapsed().as_millis(),
            error: Some(err.to_string()),
        },
    }
}

pub fn parse_tags_models(body: &str) -> Vec<String> {
    let Ok(val) = serde_json::from_str::<Value>(body) else {
        return Vec::new();
    };
    val.get("models")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|m| m.get("name").and_then(Value::as_str).map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

/// Parse one NDJSON line from Ollama `/api/chat` stream into `(delta_text, done)`.
pub fn parse_chat_chunk(line: &str) -> Option<(String, bool)> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    let val: Value = serde_json::from_str(trimmed).ok()?;
    let done = val.get("done").and_then(Value::as_bool).unwrap_or(false);
    let content = val
        .get("message")
        .and_then(|m| m.get("content"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    Some((content, done))
}

/// Stream a chat completion natively from Ollama `/api/chat` without spawning Python.
pub fn stream_chat(
    base_url: &str,
    model: &str,
    system_prompt: &str,
    user_message: &str,
    timeout: Duration,
    mut on_token: impl FnMut(&str),
) -> Result<String, String> {
    stream_chat_cancelable(
        base_url,
        model,
        system_prompt,
        user_message,
        timeout,
        || false,
        &mut on_token,
    )
}

/// Stream a chat completion natively with a cancellation check evaluated between chunks.
pub fn stream_chat_cancelable(
    base_url: &str,
    model: &str,
    system_prompt: &str,
    user_message: &str,
    timeout: Duration,
    should_cancel: impl Fn() -> bool,
    mut on_token: impl FnMut(&str),
) -> Result<String, String> {
    let url = format!("{}/api/chat", base_url.trim_end_matches('/'));
    let payload = json!({
        "model": model,
        "stream": true,
        "messages": [
            { "role": "system", "content": system_prompt },
            { "role": "user", "content": user_message }
        ]
    });
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();
    let mut resp = agent
        .post(&url)
        .content_type("application/json")
        .send(payload.to_string())
        .map_err(|e| format!("Ollama request failed: {e}"))?;

    let reader = BufReader::new(resp.body_mut().as_reader());
    let mut full = String::new();
    for line in reader.lines() {
        if should_cancel() {
            return Err("cancelled".into());
        }
        let line = line.map_err(|e| format!("Ollama stream read failed: {e}"))?;
        if let Some((delta, done)) = parse_chat_chunk(&line) {
            if !delta.is_empty() {
                on_token(&delta);
                full.push_str(&delta);
            }
            if done {
                break;
            }
        }
    }
    Ok(full)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_ollama_tags_response() {
        let raw = r#"{"models":[{"name":"qwen2.5:7b"},{"name":"qwen2.5-coder:1.5b"}]}"#;
        assert_eq!(
            parse_tags_models(raw),
            vec!["qwen2.5:7b".to_string(), "qwen2.5-coder:1.5b".to_string()]
        );
    }

    #[test]
    fn parses_ndjson_chat_chunks() {
        let chunk = r#"{"model":"qwen2.5:7b","message":{"role":"assistant","content":"你好"},"done":false}"#;
        assert_eq!(parse_chat_chunk(chunk), Some(("你好".to_string(), false)));

        let final_chunk =
            r#"{"model":"qwen2.5:7b","message":{"role":"assistant","content":""},"done":true}"#;
        assert_eq!(parse_chat_chunk(final_chunk), Some(("".to_string(), true)));
    }
}
