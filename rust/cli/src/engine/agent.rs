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

/// Build the initial OpenAI/Ollama message list with optional prior `(role, content)` turns.
fn build_agent_messages(
    system_prompt: String,
    history: &[(String, String)],
    prompt: &str,
) -> Vec<Value> {
    let mut messages = vec![json!({ "role": "system", "content": system_prompt })];
    for (role, content) in history {
        if (role == "user" || role == "assistant") && !content.trim().is_empty() {
            messages.push(json!({ "role": role, "content": content }));
        }
    }
    messages.push(json!({ "role": "user", "content": prompt }));
    messages
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
    on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    run_native_agent_with_history(
        base_url,
        model,
        workspace,
        &[],
        prompt,
        allow_write,
        max_rounds,
        timeout,
        on_tool,
    )
}

/// Run a bounded multi-round coding/inspection loop natively in Rust with prior turn history.
#[allow(clippy::too_many_arguments)]
pub fn run_native_agent_with_history(
    base_url: &str,
    model: &str,
    workspace: &Path,
    history: &[(String, String)],
    prompt: &str,
    allow_write: bool,
    max_rounds: usize,
    timeout: Duration,
    mut on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    let url = format!("{}/api/chat", base_url.trim_end_matches('/'));
    let schemas = tools::tool_schemas();
    let system_prompt = format!(
        "{}\n\nYou have native workspace tools (`list_files`, `read_file`, `search_code`, `find_symbol`, `write_file`, `edit_file`, `run_command`, `git_status`, `git_diff`, `git_log`, `glob`, `project_scan`). \
         Inspect relevant files before answering or editing.",
        policy::general_system_prompt("today")
    );
    let mut messages = build_agent_messages(system_prompt, history, prompt);
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

fn anthropic_tool_schemas() -> Vec<Value> {
    tools::tool_schemas()
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let func = item.get("function")?;
            Some(json!({
                "name": func.get("name")?.as_str()?,
                "description": func.get("description").and_then(Value::as_str).unwrap_or(""),
                "input_schema": func.get("parameters").cloned().unwrap_or_else(|| json!({"type": "object", "properties": {}})),
            }))
        })
        .collect()
}

pub(crate) fn parse_anthropic_turn(raw: &str) -> Result<(String, Vec<(String, ToolCall)>), String> {
    let val: Value = serde_json::from_str(raw).map_err(|e| format!("Invalid JSON: {e}"))?;
    let mut text_parts = Vec::new();
    let mut calls = Vec::new();
    for block in val["content"].as_array().into_iter().flatten() {
        match block.get("type").and_then(Value::as_str) {
            Some("text") => {
                if let Some(t) = block.get("text").and_then(Value::as_str) {
                    if !t.is_empty() {
                        text_parts.push(t.to_owned());
                    }
                }
            }
            Some("tool_use") => {
                if let (Some(id), Some(name)) = (
                    block.get("id").and_then(Value::as_str),
                    block.get("name").and_then(Value::as_str),
                ) {
                    calls.push((
                        id.to_owned(),
                        ToolCall {
                            name: name.to_owned(),
                            arguments: block.get("input").cloned().unwrap_or_else(|| json!({})),
                        },
                    ));
                }
            }
            _ => {}
        }
    }
    Ok((text_parts.join("\n"), calls))
}

#[allow(clippy::too_many_arguments)]
fn run_anthropic_agent_with_history(
    api_key: &str,
    model: &str,
    workspace: &Path,
    history: &[(String, String)],
    prompt: &str,
    allow_write: bool,
    max_rounds: usize,
    timeout: Duration,
    mut on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    let url = CloudProvider::Anthropic.endpoint(model, api_key);
    let model_id = model.split('/').next_back().unwrap_or(model);
    let schemas = anthropic_tool_schemas();
    let system_prompt = format!(
        "{}\n\nYou have native workspace tools (`list_files`, `read_file`, `search_code`, `find_symbol`, `write_file`, `edit_file`, `run_command`, `git_status`, `git_diff`, `git_log`, `glob`, `project_scan`). \
         Inspect relevant files before answering or editing.",
        policy::general_system_prompt("today")
    );
    let mut messages: Vec<Value> = history
        .iter()
        .filter(|(r, c)| (r == "user" || r == "assistant") && !c.trim().is_empty())
        .map(|(r, c)| json!({ "role": r, "content": c }))
        .collect();
    messages.push(json!({ "role": "user", "content": prompt }));
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    for _ in 0..max_rounds.clamp(1, 16) {
        let payload = json!({
            "model": model_id,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
            "tools": schemas,
        });
        let mut resp = agent
            .post(&url)
            .header("x-api-key", api_key)
            .header("anthropic-version", "2023-06-01")
            .content_type("application/json")
            .send(payload.to_string())
            .map_err(|e| format!("Anthropic agent request failed: {e}"))?;
        let raw = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("Read response failed: {e}"))?;
        let (content, calls) = parse_anthropic_turn(&raw)?;
        if calls.is_empty() {
            return Ok(content);
        }

        let mut assistant_blocks = Vec::new();
        if !content.is_empty() {
            assistant_blocks.push(json!({ "type": "text", "text": content }));
        }
        for (id, c) in &calls {
            assistant_blocks.push(json!({
                "type": "tool_use",
                "id": id,
                "name": c.name,
                "input": c.arguments,
            }));
        }
        messages.push(json!({ "role": "assistant", "content": assistant_blocks }));

        let mut tool_results = Vec::new();
        for (id, call) in calls {
            let output = tools::execute_tool(workspace, &call.name, &call.arguments, allow_write);
            on_tool(&call.name, &call.arguments, &output);
            tool_results.push(json!({
                "type": "tool_result",
                "tool_use_id": id,
                "content": output.to_string(),
            }));
        }
        messages.push(json!({ "role": "user", "content": tool_results }));
    }
    Err("Anthropic agent exceeded max_rounds without final answer".into())
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
    on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    run_cloud_agent_with_history(
        provider,
        api_key,
        model,
        workspace,
        &[],
        prompt,
        allow_write,
        max_rounds,
        timeout,
        on_tool,
    )
}

/// Run a bounded multi-round coding/inspection loop against an OpenAI-compatible cloud endpoint with prior turn history.
#[allow(clippy::too_many_arguments)]
pub fn run_cloud_agent_with_history(
    provider: CloudProvider,
    api_key: &str,
    model: &str,
    workspace: &Path,
    history: &[(String, String)],
    prompt: &str,
    allow_write: bool,
    max_rounds: usize,
    timeout: Duration,
    mut on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    if matches!(provider, CloudProvider::Anthropic) {
        return run_anthropic_agent_with_history(
            api_key,
            model,
            workspace,
            history,
            prompt,
            allow_write,
            max_rounds,
            timeout,
            on_tool,
        );
    }
    if matches!(provider, CloudProvider::Gemini) {
        return Err(format!(
            "Native cloud tool-calling agent currently supports OpenAI-compatible and Anthropic endpoints (got {})",
            provider.display_name()
        ));
    }
    let url = provider.endpoint(model, api_key);
    let model_id = model.split('/').next_back().unwrap_or(model);
    let schemas = tools::tool_schemas();
    let system_prompt = format!(
        "{}\n\nYou have native workspace tools (`list_files`, `read_file`, `search_code`, `find_symbol`, `write_file`, `edit_file`, `run_command`, `git_status`, `git_diff`, `git_log`, `glob`, `project_scan`). \
         Inspect relevant files before answering or editing.",
        policy::general_system_prompt("today")
    );
    let mut messages = build_agent_messages(system_prompt, history, prompt);
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

    #[test]
    fn build_agent_messages_inserts_valid_history_turns() {
        let history = vec![
            ("user".to_string(), "hi".to_string()),
            ("tool".to_string(), "ignored".to_string()),
            ("assistant".to_string(), "hello!".to_string()),
            ("user".to_string(), "   ".to_string()),
        ];
        let msgs = build_agent_messages("sys".to_string(), &history, "fix bug");
        assert_eq!(msgs.len(), 4);
        assert_eq!(msgs[0]["role"], "system");
        assert_eq!(msgs[1]["role"], "user");
        assert_eq!(msgs[1]["content"], "hi");
        assert_eq!(msgs[2]["role"], "assistant");
        assert_eq!(msgs[2]["content"], "hello!");
        assert_eq!(msgs[3]["role"], "user");
        assert_eq!(msgs[3]["content"], "fix bug");
    }

    #[test]
    fn parses_tool_calls_from_anthropic_response() {
        let raw = r#"{
            "content": [
                { "type": "text", "text": "Checking symbol." },
                { "type": "tool_use", "id": "toolu_1", "name": "find_symbol", "input": { "symbol": "run_agent" } }
            ]
        }"#;
        let (content, calls) = parse_anthropic_turn(raw).unwrap();
        assert_eq!(content, "Checking symbol.");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "toolu_1");
        assert_eq!(calls[0].1.name, "find_symbol");
        assert_eq!(calls[0].1.arguments["symbol"], "run_agent");
        assert!(!anthropic_tool_schemas().is_empty());
    }
}
