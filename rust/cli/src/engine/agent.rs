//! Native Rust multi-round tool-calling agent loop over Ollama `/api/chat`.

use super::{cloud::CloudProvider, compact, hooks, mcp, permissions, policy, skills, tools};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader},
    path::Path,
    time::Duration,
};

#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub name: String,
    pub arguments: Value,
}

/// Extract assistant text and native tool calls from an Ollama `/api/chat` response.
#[allow(dead_code)]
pub fn parse_ollama_turn(body: &str) -> Result<(String, Vec<ToolCall>), String> {
    let val: Value = serde_json::from_str(body).map_err(|e| format!("Invalid Ollama JSON: {e}"))?;
    let msg = val.get("message").ok_or("Missing message in response")?;
    let content = msg
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let calls = extract_ollama_tool_calls(msg);
    Ok((content, calls))
}

fn extract_ollama_tool_calls(msg: &Value) -> Vec<ToolCall> {
    msg.get("tool_calls")
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
        .unwrap_or_default()
}

/// Parse one NDJSON line from a streaming Ollama `/api/chat` response into `(delta, tool_calls, done)`.
pub fn parse_ollama_stream_line(line: &str) -> Option<(String, Vec<ToolCall>, bool)> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    let val: Value = serde_json::from_str(trimmed).ok()?;
    let done = val.get("done").and_then(Value::as_bool).unwrap_or(false);
    let msg = val.get("message");
    let content = msg
        .and_then(|m| m.get("content"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let calls = msg.map(extract_ollama_tool_calls).unwrap_or_default();
    Some((content, calls, done))
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

fn build_coding_system_prompt(workspace: &Path) -> String {
    let mut prompt = format!(
        "{}\n\nYou are Aria Code's native coding agent. You have native workspace tools (`list_files`, `read_file`, `search_code`, `find_symbol`, `repo_map`, `write_file`, `edit_file`, `multi_edit`, `restore_checkpoint`, `run_command`, `process`, `lsp_hover`, `lsp_definition`, `lsp_references`, `spawn_subagent`, `web_search`, `web_fetch`, `read_skill`, `git_status`, `git_diff`, `git_log`, `git_worktree`, `glob`, `project_scan`, `verify_workspace`) plus any configured MCP tools. \
         Inspect relevant files before answering or editing. Prefer targeted reads and minimal edits. `edit_file` fails when `old_string` is not unique; use more context or `multi_edit` for several unique replacements in one file. After code changes, run `verify_workspace` and fix failures before the final answer.",
        policy::general_system_prompt("today")
    );
    if let Ok(scan) = tools::project_scan(workspace) {
        let types = scan["project_types"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .unwrap_or_default();
        let keys = scan["key_files"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            })
            .unwrap_or_default();
        let git = scan["git_status"].as_str().unwrap_or("").trim();
        let mut ctx = String::new();
        if !types.is_empty() {
            ctx.push_str(&format!("\nWorkspace languages/stacks: {types}"));
        }
        if !keys.is_empty() {
            ctx.push_str(&format!("\nKey manifests: {keys}"));
        }
        if !git.is_empty() {
            let clipped_git: String = git.chars().take(1200).collect();
            ctx.push_str(&format!("\nGit status:\n{clipped_git}"));
        }
        if !ctx.is_empty() {
            prompt.push_str(&format!("\n\n[Workspace Snapshot]{ctx}"));
        }
    }
    if let Some((path, raw)) = skills::load_project_instructions(workspace) {
        let fname = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("ARIA.md");
        let clipped: String = raw.trim().chars().take(4000).collect();
        if !clipped.is_empty() {
            prompt.push_str(&format!("\n\n[Project Instructions: {fname}]\n{clipped}"));
        }
    }
    if let Some(mem_section) = crate::engine::memory::format_memory_prompt_section(workspace) {
        prompt.push_str("\n\n");
        prompt.push_str(&mem_section);
    }
    let discovered_skills = skills::discover_skills(workspace);
    if !discovered_skills.is_empty() {
        let lines: Vec<String> = discovered_skills
            .iter()
            .map(|s| {
                if s.description.is_empty() {
                    format!("- `{}`", s.name)
                } else {
                    format!("- `{}`: {}", s.name, s.description)
                }
            })
            .collect();
        prompt.push_str(&format!(
            "\n\n[Workspace Skills] (call `read_skill` to load full instructions)\n{}",
            lines.join("\n")
        ));
    }
    prompt
}

fn clip_tool_output(output: &Value, max_chars: usize) -> String {
    let raw = output.to_string();
    let count = raw.chars().count();
    if count <= max_chars {
        return raw;
    }
    let head_len = max_chars * 2 / 3;
    let tail_len = max_chars / 4;
    let head: String = raw.chars().take(head_len).collect();
    let tail: String = raw.chars().skip(count.saturating_sub(tail_len)).collect();
    format!(
        "{head}\n...[truncated {} chars]...\n{tail}",
        count - head_len - tail_len
    )
}

struct ToolLoopGuard {
    last_sig: Option<String>,
    repeat_count: usize,
    mcp: mcp::McpRegistry,
    hooks: hooks::HookRegistry,
    permissions: permissions::PermissionPolicy,
    mutated: bool,
    verified: bool,
    nudge_sent: bool,
}

fn tool_mutates(name: &str, args: &Value) -> bool {
    matches!(
        name,
        "write_file" | "edit_file" | "multi_edit" | "restore_checkpoint"
    ) || (name == "git_worktree"
        && args.get("action").and_then(Value::as_str).unwrap_or("list") != "list")
}

/// Return true when a built-in native tool call is strictly read-only (no filesystem or process mutation).
#[allow(dead_code)]
pub fn is_read_only_tool(name: &str, args: &Value) -> bool {
    matches!(
        name,
        "list_files"
            | "read_file"
            | "search_code"
            | "find_symbol"
            | "repo_map"
            | "git_status"
            | "git_diff"
            | "git_log"
            | "glob"
            | "project_scan"
            | "lsp_hover"
            | "lsp_definition"
            | "lsp_references"
            | "web_search"
            | "web_fetch"
            | "read_skill"
    ) || (name == "git_worktree"
        && args.get("action").and_then(Value::as_str).unwrap_or("list") == "list")
        || (name == "process"
            && matches!(
                args.get("action").and_then(Value::as_str).unwrap_or("list"),
                "list" | "read" | "status"
            ))
}

impl ToolLoopGuard {
    fn new(workspace: &Path) -> Self {
        Self {
            last_sig: None,
            repeat_count: 0,
            mcp: mcp::McpRegistry::load_from_workspace(workspace),
            hooks: hooks::HookRegistry::load(workspace),
            permissions: permissions::PermissionPolicy::load(workspace),
            mutated: false,
            verified: false,
            nudge_sent: false,
        }
    }

    /// Ask once for verification after a mutating tool, when this workspace has a runner.
    fn verify_nudge(&mut self, workspace: &Path) -> Option<&'static str> {
        if self.nudge_sent || !self.mutated || self.verified || !tools::has_verifier(workspace) {
            return None;
        }
        self.nudge_sent = true;
        Some("You changed workspace files without running verify_workspace. Call verify_workspace, repair any failure, then answer.")
    }

    /// Execute `Stop` hooks at turn completion and append any failure notes.
    fn finalize_turn(&self, workspace: &Path, mut answer: String) -> String {
        let notes = self.hooks.run_stop(workspace, &answer);
        if !notes.is_empty() {
            if !answer.is_empty() {
                answer.push_str("\n\n");
            }
            answer.push_str("[Stop hook notes]\n");
            for note in notes {
                answer.push_str("- ");
                answer.push_str(&note);
                answer.push('\n');
            }
        }
        answer
    }

    fn all_tool_schemas(&self) -> Value {
        let mut arr = tools::tool_schemas()
            .as_array()
            .cloned()
            .unwrap_or_default();
        arr.extend(self.mcp.tool_schemas().iter().cloned());
        Value::Array(arr)
    }

    fn execute(
        &mut self,
        workspace: &Path,
        call: &ToolCall,
        allow_write: bool,
        on_tool: &mut impl FnMut(&str, &Value, &Value),
    ) -> String {
        self.execute_with_start(workspace, call, allow_write, &mut |_, _| {}, on_tool)
    }

    fn execute_with_start(
        &mut self,
        workspace: &Path,
        call: &ToolCall,
        allow_write: bool,
        on_tool_start: &mut impl FnMut(&str, &Value),
        on_tool: &mut impl FnMut(&str, &Value, &Value),
    ) -> String {
        on_tool_start(&call.name, &call.arguments);
        let read_only = is_read_only_tool(&call.name, &call.arguments)
            || self.mcp.is_tool_read_only(&call.name);
        if let permissions::PermissionDecision::Deny(reason) =
            self.permissions
                .evaluate(&call.name, &call.arguments, read_only)
        {
            crate::engine::roi::record_blocked_action();
            let blocked = json!({
                "ok": false,
                "error": reason,
            });
            on_tool(&call.name, &call.arguments, &blocked);
            return clip_tool_output(&blocked, 16_000);
        }
        let pre = self
            .hooks
            .run_pre_tool(workspace, &call.name, &call.arguments);
        if !pre.allowed {
            crate::engine::roi::record_blocked_action();
            let blocked = json!({
                "ok": false,
                "error": pre.reason.unwrap_or_else(|| "blocked by PreToolUse hook".into()),
            });
            on_tool(&call.name, &call.arguments, &blocked);
            return clip_tool_output(&blocked, 16_000);
        }
        let tool_started = std::time::Instant::now();
        let mut output = self
            .mcp
            .call_tool(&call.name, call.arguments.clone())
            .unwrap_or_else(|| {
                tools::execute_tool(workspace, &call.name, &call.arguments, allow_write)
            });
        crate::engine::roi::record_tool_execution(
            &call.name,
            tool_started.elapsed().as_millis() as u64,
        );
        let post_notes = self
            .hooks
            .run_post_tool(workspace, &call.name, &call.arguments, &output);
        if !post_notes.is_empty() {
            if let Some(obj) = output.as_object_mut() {
                obj.insert("hook_notes".into(), json!(post_notes));
            }
        }
        on_tool(&call.name, &call.arguments, &output);
        if tool_mutates(&call.name, &call.arguments) {
            if self.verified {
                self.nudge_sent = false;
            }
            self.mutated = true;
            self.verified = false;
        }
        if call.name == "verify_workspace" {
            self.verified = true;
        }
        let sig = format!("{}:{}", call.name, call.arguments);
        if self.last_sig.as_deref() == Some(sig.as_str()) {
            self.repeat_count += 1;
        } else {
            self.last_sig = Some(sig);
            self.repeat_count = 1;
        }
        if self.repeat_count >= 2 {
            if let Some(obj) = output.as_object_mut() {
                obj.insert(
                    "hint".into(),
                    json!("Duplicate tool call detected; use the previous output or try a different tool/path."),
                );
            }
        }
        clip_tool_output(&output, 16_000)
    }
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

/// Stream a single Ollama `/api/chat` agent turn, forwarding text deltas live as NDJSON chunks arrive.
fn stream_ollama_agent_turn(
    agent: &ureq::Agent,
    url: &str,
    model: &str,
    messages: &[Value],
    schemas: &Value,
    mut on_delta: impl FnMut(&str),
) -> Result<(String, Vec<ToolCall>), String> {
    let payload = json!({
        "model": model,
        "stream": true,
        "messages": messages,
        "tools": schemas,
    });
    let mut resp = agent
        .post(url)
        .content_type("application/json")
        .send(payload.to_string())
        .map_err(|e| format!("Ollama agent request failed: {e}"))?;
    let reader = BufReader::new(resp.body_mut().as_reader());
    let mut content = String::new();
    let mut calls = Vec::new();
    let mut saw_chunk = false;
    for line in reader.lines() {
        let line = line.map_err(|e| format!("Read stream failed: {e}"))?;
        if let Some((delta, chunk_calls, done)) = parse_ollama_stream_line(&line) {
            saw_chunk = true;
            if !delta.is_empty() {
                on_delta(&delta);
                content.push_str(&delta);
            }
            calls.extend(chunk_calls);
            if done {
                break;
            }
        }
    }
    if !saw_chunk {
        return Err("Empty response from Ollama agent".into());
    }
    Ok((content, calls))
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
    on_tool: impl FnMut(&str, &Value, &Value),
) -> Result<String, String> {
    run_native_agent_with_history_streaming(
        base_url,
        model,
        workspace,
        history,
        prompt,
        allow_write,
        max_rounds,
        timeout,
        |_, _| {},
        on_tool,
        |_| {},
    )
}

/// Run a bounded multi-round coding/inspection loop natively in Rust, streaming token deltas
/// and emitting `on_tool_start` immediately before each tool runs.
#[allow(clippy::too_many_arguments)]
pub fn run_native_agent_with_history_streaming(
    base_url: &str,
    model: &str,
    workspace: &Path,
    history: &[(String, String)],
    prompt: &str,
    allow_write: bool,
    max_rounds: usize,
    timeout: Duration,
    mut on_tool_start: impl FnMut(&str, &Value),
    mut on_tool: impl FnMut(&str, &Value, &Value),
    mut on_delta: impl FnMut(&str),
) -> Result<String, String> {
    let url = format!("{}/api/chat", base_url.trim_end_matches('/'));
    let mut guard = ToolLoopGuard::new(workspace);
    let schemas = guard.all_tool_schemas();
    let system_prompt = build_coding_system_prompt(workspace);
    let mut messages = build_agent_messages(system_prompt, history, prompt);
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    for _ in 0..max_rounds.clamp(1, 16) {
        compact::compact_messages_if_needed(&mut messages, 24_000, 8);
        let (content, calls) =
            stream_ollama_agent_turn(&agent, &url, model, &messages, &schemas, &mut on_delta)?;
        if calls.is_empty() {
            if let Some(nudge) = guard.verify_nudge(workspace) {
                if !content.is_empty() {
                    messages.push(json!({ "role": "assistant", "content": content }));
                }
                messages.push(json!({ "role": "user", "content": nudge }));
                continue;
            }
            return Ok(guard.finalize_turn(workspace, content));
        }

        messages.push(json!({
            "role": "assistant",
            "content": content,
            "tool_calls": calls.iter().map(|c| json!({
                "function": { "name": c.name, "arguments": c.arguments }
            })).collect::<Vec<_>>()
        }));
        for call in calls {
            let content_str = guard.execute_with_start(
                workspace,
                &call,
                allow_write,
                &mut on_tool_start,
                &mut on_tool,
            );
            messages.push(json!({
                "role": "tool",
                "content": content_str,
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

fn messages_tool_schemas(all_schemas: &Value) -> Vec<Value> {
    all_schemas
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

pub(crate) fn parse_messages_turn(raw: &str) -> Result<(String, Vec<(String, ToolCall)>), String> {
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
fn run_messages_agent_with_history(
    endpoint_url: &str,
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
    let model_id = model.split('/').next_back().unwrap_or(model);
    let mut guard = ToolLoopGuard::new(workspace);
    let schemas = messages_tool_schemas(&guard.all_tool_schemas());
    let system_prompt = build_coding_system_prompt(workspace);
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
        compact::compact_messages_if_needed(&mut messages, 24_000, 8);
        let payload = json!({
            "model": model_id,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": messages,
            "tools": schemas,
        });
        let mut resp = agent
            .post(endpoint_url)
            .header("x-api-key", api_key)
            .header("anthropic-version", "2023-06-01")
            .content_type("application/json")
            .send(payload.to_string())
            .map_err(|e| format!("Native cloud agent request failed: {e}"))?;
        let raw = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("Read response failed: {e}"))?;
        let (content, calls) = parse_messages_turn(&raw)?;
        if calls.is_empty() {
            if let Some(nudge) = guard.verify_nudge(workspace) {
                if !content.is_empty() {
                    messages.push(json!({ "role": "assistant", "content": content }));
                }
                messages.push(json!({ "role": "user", "content": nudge }));
                continue;
            }
            return Ok(guard.finalize_turn(workspace, content));
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
            let content_str = guard.execute(workspace, &call, allow_write, &mut on_tool);
            tool_results.push(json!({
                "type": "tool_result",
                "tool_use_id": id,
                "content": content_str,
            }));
        }
        messages.push(json!({ "role": "user", "content": tool_results }));
    }
    Err("Native cloud agent exceeded max_rounds without final answer".into())
}

fn gemini_tool_schemas(all_schemas: &Value) -> Vec<Value> {
    let decls: Vec<Value> = all_schemas
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let func = item.get("function")?;
            Some(json!({
                "name": func.get("name")?.as_str()?,
                "description": func.get("description").and_then(Value::as_str).unwrap_or(""),
                "parameters": func.get("parameters").cloned().unwrap_or_else(|| json!({"type": "object", "properties": {}})),
            }))
        })
        .collect();
    vec![json!({ "functionDeclarations": decls })]
}

pub(crate) fn parse_gemini_turn(raw: &str) -> Result<(String, Vec<ToolCall>), String> {
    let val: Value = serde_json::from_str(raw).map_err(|e| format!("Invalid JSON: {e}"))?;
    let parts = val["candidates"]
        .get(0)
        .and_then(|c| c["content"]["parts"].as_array())
        .ok_or("Missing candidates[0].content.parts in response")?;
    let mut text_parts = Vec::new();
    let mut calls = Vec::new();
    for part in parts {
        if let Some(t) = part.get("text").and_then(Value::as_str) {
            if !t.is_empty() {
                text_parts.push(t.to_owned());
            }
        }
        if let Some(fc) = part.get("functionCall") {
            if let Some(name) = fc.get("name").and_then(Value::as_str) {
                calls.push(ToolCall {
                    name: name.to_owned(),
                    arguments: fc.get("args").cloned().unwrap_or_else(|| json!({})),
                });
            }
        }
    }
    Ok((text_parts.join("\n"), calls))
}

#[allow(clippy::too_many_arguments)]
fn run_gemini_agent_with_history(
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
    let model_id = model.split('/').next_back().unwrap_or(model);
    let url = format!(
        "https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
    );
    let mut guard = ToolLoopGuard::new(workspace);
    let tools_decl = gemini_tool_schemas(&guard.all_tool_schemas());
    let system_prompt = build_coding_system_prompt(workspace);
    let mut contents: Vec<Value> = history
        .iter()
        .filter(|(r, c)| (r == "user" || r == "assistant") && !c.trim().is_empty())
        .map(|(r, c)| {
            let role = if r == "assistant" { "model" } else { "user" };
            json!({ "role": role, "parts": [{ "text": c }] })
        })
        .collect();
    contents.push(json!({ "role": "user", "parts": [{ "text": prompt }] }));
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    for _ in 0..max_rounds.clamp(1, 16) {
        let payload = json!({
            "systemInstruction": { "parts": [{ "text": system_prompt }] },
            "contents": contents,
            "tools": tools_decl,
        });
        let mut resp = agent
            .post(&url)
            .content_type("application/json")
            .send(payload.to_string())
            .map_err(|e| format!("Native cloud agent request failed: {e}"))?;
        let raw = resp
            .body_mut()
            .read_to_string()
            .map_err(|e| format!("Read response failed: {e}"))?;
        let (content, calls) = parse_gemini_turn(&raw)?;
        if calls.is_empty() {
            if let Some(nudge) = guard.verify_nudge(workspace) {
                if !content.is_empty() {
                    contents.push(json!({ "role": "model", "parts": [{ "text": content }] }));
                }
                contents.push(json!({ "role": "user", "parts": [{ "text": nudge }] }));
                continue;
            }
            return Ok(guard.finalize_turn(workspace, content));
        }

        let mut model_parts = Vec::new();
        if !content.is_empty() {
            model_parts.push(json!({ "text": content }));
        }
        for c in &calls {
            model_parts.push(json!({
                "functionCall": { "name": c.name, "args": c.arguments }
            }));
        }
        contents.push(json!({ "role": "model", "parts": model_parts }));

        let mut response_parts = Vec::new();
        for call in calls {
            let content_str = guard.execute(workspace, &call, allow_write, &mut on_tool);
            response_parts.push(json!({
                "functionResponse": {
                    "name": call.name,
                    "response": { "result": content_str }
                }
            }));
        }
        contents.push(json!({ "role": "user", "parts": response_parts }));
    }
    Err("Native cloud agent exceeded max_rounds without final answer".into())
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
        let endpoint_url = provider.endpoint(model, api_key);
        return run_messages_agent_with_history(
            &endpoint_url,
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
        return run_gemini_agent_with_history(
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
    let url = provider.endpoint(model, api_key);
    let model_id = model.split('/').next_back().unwrap_or(model);
    let mut guard = ToolLoopGuard::new(workspace);
    let schemas = guard.all_tool_schemas();
    let system_prompt = build_coding_system_prompt(workspace);
    let mut messages = build_agent_messages(system_prompt, history, prompt);
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .new_agent();

    for _ in 0..max_rounds.clamp(1, 16) {
        compact::compact_messages_if_needed(&mut messages, 24_000, 8);
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
            if let Some(nudge) = guard.verify_nudge(workspace) {
                if !content.is_empty() {
                    messages.push(json!({ "role": "assistant", "content": content }));
                }
                messages.push(json!({ "role": "user", "content": nudge }));
                continue;
            }
            return Ok(guard.finalize_turn(workspace, content));
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
            let content_str = guard.execute(workspace, &call, allow_write, &mut on_tool);
            messages.push(json!({
                "role": "tool",
                "tool_call_id": id,
                "content": content_str,
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
    fn parses_tool_calls_from_messages_response() {
        let raw = r#"{
            "content": [
                { "type": "text", "text": "Checking symbol." },
                { "type": "tool_use", "id": "toolu_1", "name": "find_symbol", "input": { "symbol": "run_agent" } }
            ]
        }"#;
        let (content, calls) = parse_messages_turn(raw).unwrap();
        assert_eq!(content, "Checking symbol.");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, "toolu_1");
        assert_eq!(calls[0].1.name, "find_symbol");
        assert_eq!(calls[0].1.arguments["symbol"], "run_agent");
        assert!(!messages_tool_schemas(&tools::tool_schemas()).is_empty());
    }

    #[test]
    fn parses_tool_calls_from_gemini_response_and_guards_duplicates() {
        let raw = r#"{
            "candidates": [{
                "content": {
                    "parts": [
                        { "text": "Inspecting." },
                        { "functionCall": { "name": "git_status", "args": {} } }
                    ]
                }
            }]
        }"#;
        let (content, calls) = parse_gemini_turn(raw).unwrap();
        assert_eq!(content, "Inspecting.");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "git_status");
        assert!(!gemini_tool_schemas(&tools::tool_schemas()).is_empty());

        let dir = tempfile::tempdir().unwrap();
        let mut guard = ToolLoopGuard::new(dir.path());
        let call = ToolCall {
            name: "list_files".into(),
            arguments: json!({"path": "."}),
        };
        let first = guard.execute(dir.path(), &call, false, &mut |_, _, _| {});
        assert!(!first.contains("Duplicate tool call"));
        let second = guard.execute(dir.path(), &call, false, &mut |_, _, _| {});
        assert!(second.contains("Duplicate tool call"));
    }

    #[test]
    fn asks_for_verification_once_after_a_write() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("Cargo.toml"), "[package]\nname = \"t\"\n").unwrap();
        let mut guard = ToolLoopGuard::new(dir.path());
        assert!(guard.verify_nudge(dir.path()).is_none());
        let call = ToolCall {
            name: "write_file".into(),
            arguments: json!({ "path": "src/lib.rs", "content": "pub fn a() {}\n" }),
        };
        let _ = guard.execute(dir.path(), &call, true, &mut |_, _, _| {});
        let nudge = guard.verify_nudge(dir.path()).unwrap();
        assert!(nudge.contains("verify_workspace"));
        assert!(guard.verify_nudge(dir.path()).is_none());
        let verify = ToolCall {
            name: "verify_workspace".into(),
            arguments: json!({}),
        };
        let _ = guard.execute(dir.path(), &verify, true, &mut |_, _, _| {});
        assert!(guard.verify_nudge(dir.path()).is_none());
        let _ = guard.execute(dir.path(), &call, true, &mut |_, _, _| {});
        assert!(guard
            .verify_nudge(dir.path())
            .unwrap()
            .contains("verify_workspace"));

        let bare = tempfile::tempdir().unwrap();
        let mut quiet = ToolLoopGuard::new(bare.path());
        let _ = quiet.execute(bare.path(), &call, true, &mut |_, _, _| {});
        assert!(quiet.verify_nudge(bare.path()).is_none());
    }

    #[test]
    fn injects_aria_md_and_workspace_skills_into_system_prompt() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("ARIA.md"), "Always run cargo test.\n").unwrap();
        let skill_dir = dir.path().join(".aria").join("skills").join("rust-audit");
        std::fs::create_dir_all(&skill_dir).unwrap();
        std::fs::write(skill_dir.join("SKILL.md"), "# Rust audit\n").unwrap();

        let prompt = build_coding_system_prompt(dir.path());
        assert!(prompt.contains("[Project Instructions: ARIA.md]"));
        assert!(prompt.contains("Always run cargo test."));
        assert!(prompt.contains("[Workspace Skills]"));
        assert!(prompt.contains("rust-audit"));
    }

    #[test]
    fn parses_ollama_stream_lines_for_text_and_tool_calls() {
        assert!(parse_ollama_stream_line("   ").is_none());
        assert!(parse_ollama_stream_line("not json").is_none());

        let (d1, c1, done1) =
            parse_ollama_stream_line(r#"{"message":{"content":"Hello "},"done":false}"#).unwrap();
        assert_eq!(d1, "Hello ");
        assert!(c1.is_empty());
        assert!(!done1);

        let (d2, c2, done2) = parse_ollama_stream_line(
            r#"{"message":{"content":"world","tool_calls":[{"function":{"name":"read_file","arguments":{"path":"Cargo.toml"}}}]},"done":true}"#,
        )
        .unwrap();
        assert_eq!(d2, "world");
        assert_eq!(c2.len(), 1);
        assert_eq!(c2[0].name, "read_file");
        assert_eq!(c2[0].arguments["path"], "Cargo.toml");
        assert!(done2);
    }

    #[test]
    fn classifies_read_only_and_mutating_tools() {
        assert!(is_read_only_tool("read_file", &json!({"path": "a.rs"})));
        assert!(is_read_only_tool("web_search", &json!({"query": "rust"})));
        assert!(is_read_only_tool("read_skill", &json!({"name": "audit"})));
        assert!(is_read_only_tool(
            "git_worktree",
            &json!({"action": "list"})
        ));
        assert!(is_read_only_tool("process", &json!({"action": "status"})));

        assert!(!is_read_only_tool("write_file", &json!({"path": "a.rs"})));
        assert!(!is_read_only_tool("run_command", &json!({"command": "ls"})));
        assert!(!is_read_only_tool(
            "git_worktree",
            &json!({"action": "add", "name": "feat"})
        ));
        assert!(!is_read_only_tool("process", &json!({"action": "start"})));
    }

    #[test]
    fn streams_ollama_agent_turn_end_to_end_over_http() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut req = Vec::new();
            let mut buf = [0u8; 1024];
            let header_end = loop {
                let n = stream.read(&mut buf).unwrap();
                if n == 0 {
                    break req.len();
                }
                req.extend_from_slice(&buf[..n]);
                if let Some(pos) = req.windows(4).position(|w| w == b"\r\n\r\n") {
                    break pos + 4;
                }
            };
            let headers = String::from_utf8_lossy(&req[..header_end]);
            let content_len = headers
                .lines()
                .find_map(|line| {
                    let (k, v) = line.split_once(':')?;
                    k.eq_ignore_ascii_case("content-length")
                        .then(|| v.trim().parse::<usize>().ok())
                        .flatten()
                })
                .unwrap_or(0);
            while req.len() < header_end + content_len {
                let n = stream.read(&mut buf).unwrap();
                if n == 0 {
                    break;
                }
                req.extend_from_slice(&buf[..n]);
            }
            let body = concat!(
                "{\"message\":{\"content\":\"Checking \"},\"done\":false}\n",
                "{\"message\":{\"content\":\"files.\"},\"done\":false}\n",
                "{\"message\":{\"content\":\"\",\"tool_calls\":[{\"function\":{\"name\":\"list_files\",\"arguments\":{\"path\":\".\"}}}]},\"done\":true}\n"
            );
            let resp = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(resp.as_bytes()).unwrap();
            stream.flush().unwrap();
            let _ = stream.shutdown(std::net::Shutdown::Write);
        });

        let agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(5)))
            .build()
            .new_agent();
        let url = format!("http://{addr}/api/chat");
        let mut deltas = Vec::new();
        let (content, calls) = stream_ollama_agent_turn(
            &agent,
            &url,
            "qwen3:8b",
            &[json!({"role": "user", "content": "hi"})],
            &json!([]),
            |d| deltas.push(d.to_owned()),
        )
        .unwrap();
        server.join().unwrap();

        assert_eq!(deltas, vec!["Checking ".to_owned(), "files.".to_owned()]);
        assert_eq!(content, "Checking files.");
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].name, "list_files");
    }

    #[test]
    fn enforces_declarative_permission_deny_rules_in_tool_loop() {
        let dir = tempfile::tempdir().unwrap();
        let aria_dir = dir.path().join(".aria");
        std::fs::create_dir_all(&aria_dir).unwrap();
        std::fs::write(
            aria_dir.join("settings.json"),
            r#"{"permissions":{"deny":["Write(.env*)","Bash(cargo publish*)"]}}"#,
        )
        .unwrap();

        let mut guard = ToolLoopGuard::new(dir.path());
        let blocked_write = ToolCall {
            name: "write_file".into(),
            arguments: json!({"path": ".env.prod", "content": "SECRET=1"}),
        };
        let out = guard.execute(dir.path(), &blocked_write, true, &mut |_, _, _| {});
        assert!(out.contains("Denied by permission rule"));
        assert!(!dir.path().join(".env.prod").exists());

        let blocked_cmd = ToolCall {
            name: "run_command".into(),
            arguments: json!({"command": "cargo publish --dry-run"}),
        };
        let out_cmd = guard.execute(dir.path(), &blocked_cmd, true, &mut |_, _, _| {});
        assert!(out_cmd.contains("Denied by permission rule"));
    }

    #[test]
    fn read_only_mode_allows_readonly_mcp_and_blocks_destructive_mcp() {
        let py = if std::process::Command::new("python3")
            .arg("--version")
            .output()
            .is_ok()
        {
            "python3"
        } else if std::process::Command::new("python")
            .arg("--version")
            .output()
            .is_ok()
        {
            "python"
        } else {
            return;
        };

        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("mock_mcp.py");
        std::fs::write(
            &script,
            r#"import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    req_id = req.get("id")
    if req_id is None:
        continue
    m = req.get("method")
    if m == "initialize":
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
    elif m == "tools/list":
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": [
            {"name": "echo.ping", "description": "Read-only echo", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True, "destructiveHint": False}},
            {"name": "order.create", "description": "Mutating order", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False, "destructiveHint": True}}
        ]}}
    elif m == "tools/call":
        resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "ok"}]}}
    else:
        resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown"}}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"#,
        )
        .unwrap();

        let aria_dir = dir.path().join(".aria");
        std::fs::create_dir_all(&aria_dir).unwrap();
        std::fs::write(
            aria_dir.join("settings.json"),
            r#"{"permissions":{"defaultMode":"read-only"}}"#,
        )
        .unwrap();
        let cfg = mcp::McpServerConfig {
            name: "demo-srv".into(),
            command: py.into(),
            args: vec![script.to_string_lossy().into_owned()],
            env: std::collections::HashMap::new(),
        };
        mcp::write_workspace_mcp_json(dir.path(), &[cfg]).unwrap();

        let mut guard = ToolLoopGuard::new(dir.path());
        let ro_call = ToolCall {
            name: "mcp__demo_srv__echo_ping".into(),
            arguments: json!({}),
        };
        let ro_out = guard.execute(dir.path(), &ro_call, false, &mut |_, _, _| {});
        assert!(ro_out.contains("\"ok\":true"));

        let mut_call = ToolCall {
            name: "mcp__demo_srv__order_create".into(),
            arguments: json!({}),
        };
        let mut_out = guard.execute(dir.path(), &mut_call, false, &mut |_, _, _| {});
        assert!(mut_out.contains("read-only permission mode"));
    }

    #[test]
    fn emits_tool_start_strictly_before_tool_completion() {
        let dir = tempfile::tempdir().unwrap();
        let mut guard = ToolLoopGuard::new(dir.path());
        let call = ToolCall {
            name: "list_files".into(),
            arguments: json!({"path": "."}),
        };
        let events = std::cell::RefCell::new(Vec::<String>::new());
        let _ = guard.execute_with_start(
            dir.path(),
            &call,
            false,
            &mut |name, _| events.borrow_mut().push(format!("start:{name}")),
            &mut |name, _, _| events.borrow_mut().push(format!("done:{name}")),
        );
        assert_eq!(
            *events.borrow(),
            vec!["start:list_files".to_owned(), "done:list_files".to_owned()]
        );
    }
}
