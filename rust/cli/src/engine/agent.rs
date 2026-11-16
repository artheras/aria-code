//! Native Rust multi-round tool-calling agent loop over Ollama `/api/chat`.

use super::{cloud::CloudProvider, policy, tools};
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub name: String,
    pub arguments: Value,
}

/// Extract assistant text and native tool calls from an Ollama `/api/chat` response.
pub fn parse_ollama_turn(body: &str) -> Result<(String, Vec<ToolCall>), String> {
    let val: Value = serde_json::from_str(body).map_err(|e| format!("Invalid Ollama JSON: {e}"))?;
    let msg = val.get("message").ok_or("Missing message in response")?;
    let content = msg
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let calls = msg
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(|item| {
                    let func = item.get("function")?;
                    let name = func.get("name")?.as_str()?.to_owned();
                    let arguments = func.get("arguments").cloned().unwrap_or_else(|| json!({}));
                    Some(ToolCall { name, arguments })
                })
                .collect()
        })
        .unwrap_or_default();
    Ok((content, calls))
}

/// Run a bounded multi-round coding/inspection loop natively in Rust.
#[allow(clippy::too_many_arguments)]
pub fn run_native_agent(
    base_url: &str,
    model: &str,
    workspace: &Path,
    prompt: &str,
    allow_write: bool,
    max_rounds: usize,
    timeout: Duration,
    mut on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    let url = format!("{}/api/chat", base_url.trim_end_matches('/'));
    let schemas = tools::tool_schemas();
    let system_prompt = format!(
        "{}\n\nYou have native workspace tools (`list_files`, `read_file`, `search_code`, `write_file`, `edit_file`, `run_command`). \
         Inspect relevant files before answering or editing.",
        policy::general_system_prompt("today")
    );
    let mut messages = vec![
        json!({ "role": "system", "content": system_prompt }),
        json!({ "role": "user", "content": prompt }),
    ];
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    for _ in 0..max_rounds.clamp(1, 16) {
        let payload = json!({
            "model": model,
            "stream": false,
            "messages": messages,
            "tools": schemas,
        });
        let mut resp = agent
            .post(&url)
            .content_type("application/json")
            .send(payload.to_string())
            .map_err(|e| format!("Ollama agent request failed: {e}"))?;
        let raw = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("Read response failed: {e}"))?;
        let (content, calls) = parse_ollama_turn(&raw)?;
        if calls.is_empty() {
            return Ok(content);
        }

        messages.push(json!({
            "role": "assistant",
            "content": content,
            "tool_calls": calls.iter().map(|c| json!({
                "function": { "name": c.name, "arguments": c.arguments }
            })).collect::<Vec<_>>()
        }));
        for call in calls {
            let output = tools::execute_tool(workspace, &call.name, &call.arguments, allow_write);
            on_tool(&call.name, &call.arguments, &output);
            messages.push(json!({
                "role": "tool",
                "content": output.to_string(),
            }));
        }
    }
    Err("Native agent exceeded max_rounds without final answer".into())
}

/// Extract assistant text and `(id, ToolCall)` pairs from an OpenAI `/v1/chat/completions` payload.
pub fn parse_openai_turn(body: &str) -> Result<(String, Vec<(String, ToolCall)>), String> {
    let val: Value = serde_json::from_str(body).map_err(|e| format!("Invalid cloud JSON: {e}"))?;
    let msg = val
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|c| c.first())
        .and_then(|c| c.get("message"))
        .ok_or("Missing choices[0].message in response")?;
    let content = msg
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let calls = msg
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .enumerate()
                .filter_map(|(idx, item)| {
                    let id = item
                        .get("id")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                        .unwrap_or_else(|| format!("call_{idx}"));
                    let func = item.get("function")?;
                    let name = func.get("name")?.as_str()?.to_owned();
                    let arguments = match func.get("arguments") {
                        Some(Value::String(raw)) => {
                            serde_json::from_str(raw).unwrap_or_else(|_| json!({}))
                        }
                        Some(other) => other.clone(),
                        None => json!({}),
                    };
                    Some((id, ToolCall { name, arguments }))
                })
                .collect()
        })
        .unwrap_or_default();
    Ok((content, calls))
}

/// Run a bounded multi-round coding/inspection loop against an OpenAI-compatible cloud endpoint.
#[allow(clippy::too_many_arguments)]
pub fn run_cloud_agent(
    provider: CloudProvider,
    api_key: &str,
    model: &str,
    workspace: &Path,
    prompt: &str,
    allow_write: bool,
    max_rounds: usize,
    timeout: Duration,
    mut on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    if matches!(provider, CloudProvider::Anthropic | CloudProvider::Gemini) {
        return Err(format!(
            "Native cloud tool-calling agent currently supports OpenAI-compatible endpoints (got {})",
            provider.display_name()
        ));
    }
    let url = provider.endpoint(model, api_key);
    let model_id = model.split('/').next_back().unwrap_or(model);
    let schemas = tools::tool_schemas();
    let system_prompt = format!(
        "{}\n\nYou have native workspace tools (`list_files`, `read_file`, `search_code`, `write_file`, `edit_file`, `run_command`, `git_status`, `git_diff`). \
         Inspect relevant files before answering or editing.",
        policy::general_system_prompt("today")
    );
    let mut messages = vec![
        json!({ "role": "system", "content": system_prompt }),
        json!({ "role": "user", "content": prompt }),
    ];
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    for _ in 0..max_rounds.clamp(1, 16) {
        let payload = json!({
            "model": model_id,
            "messages": messages,
            "tools": schemas,
        });
        let mut resp = agent
            .post(&url)
            .header("Authorization", &format!("Bearer {api_key}"))
            .content_type("application/json")
            .send(payload.to_string())
            .map_err(|e| format!("{} agent request failed: {e}", provider.display_name()))?;
        let raw = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("Read response failed: {e}"))?;
        let (content, calls) = parse_openai_turn(&raw)?;
        if calls.is_empty() {
            return Ok(content);
        }

        messages.push(json!({
            "role": "assistant",
            "content": content,
            "tool_calls": calls.iter().map(|(id, c)| json!({
                "id": id,
                "type": "function",
                "function": { "name": c.name, "arguments": c.arguments.to_string() }
            })).collect::<Vec<_>>()
        }));
        for (id, call) in calls {
            let output = tools::execute_tool(workspace, &call.name, &call.arguments, allow_write);
            on_tool(&call.name, &call.arguments, &output);
            messages.push(json!({
                "role": "tool",
                "tool_call_id": id,
                "content": output.to_string(),
            }));
        }
    }
    Err("Cloud agent exceeded max_rounds without final answer".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_tool_calls_from_ollama_response() {
        let raw = r#"{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    { "function": { "name": "read_file", "arguments": { "path": "README.md" } } }
                ]
            }
        }"#;
        let (content, calls) = parse_ollama_turn(raw).unwrap();
        assert!(content.is_empty());
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "read_file");
        assert_eq!(calls[0].arguments["path"], "README.md");
    }

    #[test]
    fn parses_tool_calls_from_openai_response() {
        let raw = r#"{
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": { "name": "git_status", "arguments": "{}" }
                    }]
                }
            }]
        }"#;
        let (content, calls) = parse_openai_turn(raw).unwrap();
        assert!(content.is_empty());
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "call_1");
        assert_eq!(calls[0].1.name, "git_status");
    }
}
