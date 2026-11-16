//! Native Rust cloud LLM SSE streaming client (OpenAI-compatible, Anthropic, Gemini).
//! Keeps cloud provider routing and streaming compiled inside the native binary.

use serde_json::{json, Value};
use std::{
    env,
    io::{BufRead, BufReader},
    time::Duration,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloudProvider {
    Arthera,
    OpenAI,
    Anthropic,
    Gemini,
    DeepSeek,
    OpenRouter,
}

impl CloudProvider {
    pub fn from_id(id: &str) -> Option<Self> {
        match id.trim().to_ascii_lowercase().as_str() {
            "arthera" | "arthera-cloud" | "aria" => Some(Self::Arthera),
            "openai" => Some(Self::OpenAI),
            "anthropic" => Some(Self::Anthropic),
            "google" | "gemini" => Some(Self::Gemini),
            "deepseek" => Some(Self::DeepSeek),
            "openrouter" => Some(Self::OpenRouter),
            _ => None,
        }
    }

    pub fn api_key_env(self) -> &'static [&'static str] {
        match self {
            Self::Arthera => &["ARIA_API_KEY", "ARTHERA_API_KEY"],
            Self::OpenAI => &["OPENAI_API_KEY"],
            Self::Anthropic => &["ANTHROPIC_API_KEY"],
            Self::Gemini => &["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            Self::DeepSeek => &["DEEPSEEK_API_KEY"],
            Self::OpenRouter => &["OPENROUTER_API_KEY"],
        }
    }

    pub fn resolve_api_key(self) -> Option<String> {
        self.api_key_env()
            .iter()
            .find_map(|name| env::var(name).ok().filter(|v| !v.trim().is_empty()))
    }

    pub fn display_name(self) -> &'static str {
        match self {
            Self::Arthera => "Arthera Backend",
            Self::OpenAI => "OpenAI",
            Self::Anthropic => "Anthropic",
            Self::Gemini => "Google Gemini",
            Self::DeepSeek => "DeepSeek",
            Self::OpenRouter => "OpenRouter",
        }
    }

    pub fn arthera_base_url() -> String {
        env::var("ARIA_API_URL")
            .ok()
            .filter(|v| !v.trim().is_empty())
            .unwrap_or_else(|| "https://api.arthera.xyz".to_owned())
    }

    pub fn endpoint(self, model: &str, api_key: &str) -> String {
        let model_id = model.split('/').next_back().unwrap_or(model);
        match self {
            Self::Arthera => format!(
                "{}/v1/chat/completions",
                Self::arthera_base_url().trim_end_matches('/')
            ),
            Self::OpenAI => "https://api.openai.com/v1/chat/completions".to_owned(),
            Self::Anthropic => "https://api.anthropic.com/v1/messages".to_owned(),
            Self::Gemini => format!(
                "https://generativelanguage.googleapis.com/v1beta/models/{model_id}:streamGenerateContent?alt=sse&key={api_key}"
            ),
            Self::DeepSeek => "https://api.deepseek.com/v1/chat/completions".to_owned(),
            Self::OpenRouter => "https://openrouter.ai/api/v1/chat/completions".to_owned(),
        }
    }
}

/// Probe the private Arthera cloud backend (`/health`) natively without Python.
pub fn probe_arthera_backend(timeout: Duration) -> (bool, u128, Option<String>) {
    let url = format!("{}/health", CloudProvider::arthera_base_url().trim_end_matches('/'));
    let start = std::time::Instant::now();
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();
    match agent.get(&url).call() {
        Ok(resp) => {
            let ms = start.elapsed().as_millis();
            let code = resp.status().as_u16();
            if (200..300).contains(&code) {
                (true, ms, None)
            } else {
                (false, ms, Some(format!("HTTP {code}")))
            }
        }
        Err(err) => (false, start.elapsed().as_millis(), Some(err.to_string())),
    }
}

/// Parse one SSE `data: ...` payload for OpenAI-compatible chat completions.
pub fn parse_openai_sse_data(data: &str) -> Option<(String, bool)> {
    let trimmed = data.trim();
    if trimmed == "[DONE]" {
        return Some((String::new(), true));
    }
    let val: Value = serde_json::from_str(trimmed).ok()?;
    let delta = val
        .get("choices")?
        .get(0)?
        .get("delta")?
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let done = val
        .get("choices")?
        .get(0)?
        .get("finish_reason")
        .is_some_and(|r| !r.is_null());
    Some((delta, done))
}

/// Parse one SSE `data: ...` payload for Anthropic Messages API.
pub fn parse_anthropic_sse_data(data: &str) -> Option<(String, bool)> {
    let val: Value = serde_json::from_str(data.trim()).ok()?;
    match val.get("type").and_then(Value::as_str)? {
        "content_block_delta" => {
            let text = val
                .get("delta")?
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            Some((text, false))
        }
        "message_stop" => Some((String::new(), true)),
        _ => None,
    }
}

/// Parse one SSE `data: ...` payload for Gemini `streamGenerateContent?alt=sse`.
pub fn parse_gemini_sse_data(data: &str) -> Option<(String, bool)> {
    let val: Value = serde_json::from_str(data.trim()).ok()?;
    let text = val
        .get("candidates")?
        .get(0)?
        .get("content")?
        .get("parts")?
        .get(0)?
        .get("text")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let done = val
        .get("candidates")?
        .get(0)?
        .get("finishReason")
        .is_some_and(|r| !r.is_null());
    Some((text, done))
}

/// Stream a cloud LLM completion natively via SSE without spawning Python.
pub fn stream_cloud_chat_cancelable(
    provider: CloudProvider,
    model: &str,
    system_prompt: &str,
    user_message: &str,
    timeout: Duration,
    should_cancel: impl Fn() -> bool,
    mut on_token: impl FnMut(&str),
) -> Result<String, String> {
    let api_key = provider.resolve_api_key().ok_or_else(|| {
        format!(
            "Missing API key ({}) for {}",
            provider.api_key_env().join(" or "),
            provider.display_name()
        )
    })?;
    let model_id = model.split('/').next_back().unwrap_or(model);
    let url = provider.endpoint(model, &api_key);
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    let mut resp = match provider {
        CloudProvider::Arthera
        | CloudProvider::OpenAI
        | CloudProvider::DeepSeek
        | CloudProvider::OpenRouter => {
            let payload = json!({
                "model": model_id,
                "stream": true,
                "messages": [
                    { "role": "system", "content": system_prompt },
                    { "role": "user", "content": user_message }
                ]
            });
            agent
                .post(&url)
                .header("Authorization", &format!("Bearer {api_key}"))
                .content_type("application/json")
                .send(payload.to_string())
        }
        CloudProvider::Anthropic => {
            let payload = json!({
                "model": model_id,
                "max_tokens": 4096,
                "stream": true,
                "system": system_prompt,
                "messages": [
                    { "role": "user", "content": user_message }
                ]
            });
            agent
                .post(&url)
                .header("x-api-key", &api_key)
                .header("anthropic-version", "2023-06-01")
                .content_type("application/json")
                .send(payload.to_string())
        }
        CloudProvider::Gemini => {
            let payload = json!({
                "system_instruction": {
                    "parts": [{ "text": system_prompt }]
                },
                "contents": [{
                    "role": "user",
                    "parts": [{ "text": user_message }]
                }]
            });
            agent
                .post(&url)
                .content_type("application/json")
                .send(payload.to_string())
        }
    }
    .map_err(|e| format!("{} request failed: {e}", provider.display_name()))?;

    let reader = BufReader::new(resp.body_mut().as_reader());
    let mut full = String::new();
    for line in reader.lines() {
        if should_cancel() {
            return Err("cancelled".into());
        }
        let line = line.map_err(|e| format!("SSE read failed: {e}"))?;
        let Some(data) = line.strip_prefix("data:") else {
            continue;
        };
        let parsed = match provider {
            CloudProvider::Arthera
            | CloudProvider::OpenAI
            | CloudProvider::DeepSeek
            | CloudProvider::OpenRouter => parse_openai_sse_data(data),
            CloudProvider::Anthropic => parse_anthropic_sse_data(data),
            CloudProvider::Gemini => parse_gemini_sse_data(data),
        };
        if let Some((delta, done)) = parsed {
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
    fn parses_openai_anthropic_and_gemini_sse_chunks() {
        let oa = r#"{"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}"#;
        assert_eq!(parse_openai_sse_data(oa), Some(("Hello".into(), false)));
        assert_eq!(parse_openai_sse_data("[DONE]"), Some(("".into(), true)));

        let ant = r#"{"type":"content_block_delta","delta":{"type":"text_delta","text":"World"}}"#;
        assert_eq!(parse_anthropic_sse_data(ant), Some(("World".into(), false)));
        assert_eq!(
            parse_anthropic_sse_data(r#"{"type":"message_stop"}"#),
            Some(("".into(), true))
        );

        let gem = r#"{"candidates":[{"content":{"parts":[{"text":"Aria"}]},"finishReason":"STOP"}]}"#;
        assert_eq!(parse_gemini_sse_data(gem), Some(("Aria".into(), true)));
    }

    #[test]
    fn maps_provider_ids_and_endpoints() {
        assert_eq!(CloudProvider::from_id("arthera"), Some(CloudProvider::Arthera));
        assert_eq!(CloudProvider::from_id("openai"), Some(CloudProvider::OpenAI));
        assert_eq!(CloudProvider::from_id("google"), Some(CloudProvider::Gemini));
        assert_eq!(
            CloudProvider::Arthera.endpoint("aria-pro", "k"),
            "https://api.arthera.xyz/v1/chat/completions"
        );
        assert_eq!(
            CloudProvider::Anthropic.endpoint("claude-3-7-sonnet", "k"),
            "https://api.anthropic.com/v1/messages"
        );
        assert!(CloudProvider::Gemini
            .endpoint("google/gemini-3.8-flash", "k")
            .contains("gemini-3.8-flash:streamGenerateContent"));
    }
}
