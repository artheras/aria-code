//! Native context auto-compaction for long multi-turn coding sessions.
//!
//! Keeps the system prompt and the most recent turns intact while collapsing older
//! middle turns into a bounded structured summary when estimated token usage exceeds
//! the configured context budget.

use serde_json::{json, Value};

/// Conservative token estimate (~3.5 chars per token, handles mixed ASCII/CJK safely).
pub fn estimate_tokens(text: &str) -> usize {
    let chars = text.chars().count();
    if chars == 0 {
        0
    } else {
        (chars * 10).div_ceil(35)
    }
}

pub fn estimate_messages_tokens(messages: &[Value]) -> usize {
    messages
        .iter()
        .map(|m| {
            let content = m.get("content").and_then(Value::as_str).unwrap_or("");
            let tool_calls = m
                .get("tool_calls")
                .map(|v| v.to_string())
                .unwrap_or_default();
            estimate_tokens(content) + estimate_tokens(&tool_calls) + 6
        })
        .sum()
}

/// Compact `messages` in place when estimated tokens exceed `max_tokens`.
/// Preserves the leading system message (if any) and the last `keep_recent` messages.
/// Returns `true` if compaction occurred.
pub fn compact_messages_if_needed(
    messages: &mut Vec<Value>,
    max_tokens: usize,
    keep_recent: usize,
) -> bool {
    if estimate_messages_tokens(messages) <= max_tokens {
        return false;
    }
    let has_system = messages
        .first()
        .and_then(|m| m.get("role"))
        .and_then(Value::as_str)
        == Some("system");
    let start_idx = usize::from(has_system);
    let keep = keep_recent.max(2);
    if messages.len() <= start_idx + keep {
        return false;
    }
    let split_at = messages.len().saturating_sub(keep);
    let dropped = &messages[start_idx..split_at];
    let mut bullets = Vec::new();
    for msg in dropped {
        let role = msg.get("role").and_then(Value::as_str).unwrap_or("unknown");
        let content = msg.get("content").and_then(Value::as_str).unwrap_or("").trim();
        if content.is_empty() {
            continue;
        }
        let snippet: String = content.chars().take(180).collect();
        let suffix = if content.chars().count() > 180 { "…" } else { "" };
        bullets.push(format!("- [{role}] {snippet}{suffix}"));
        if bullets.len() >= 18 {
            break;
        }
    }
    let summary = format!(
        "[Auto-compacted earlier context: {} message(s) collapsed to stay within token budget]\n{}",
        dropped.len(),
        bullets.join("\n")
    );
    let mut next = Vec::with_capacity(keep + 2);
    if has_system {
        next.push(messages[0].clone());
    }
    next.push(json!({ "role": "system", "content": summary }));
    next.extend_from_slice(&messages[split_at..]);
    *messages = next;
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compacts_middle_messages_when_over_budget() {
        let mut msgs = vec![json!({"role": "system", "content": "sys"})];
        for i in 0..10 {
            msgs.push(json!({
                "role": if i % 2 == 0 { "user" } else { "assistant" },
                "content": format!("turn {i}: {}", "x".repeat(400))
            }));
        }
        let changed = compact_messages_if_needed(&mut msgs, 300, 4);
        assert!(changed);
        assert_eq!(msgs.len(), 6); // 1 system + 1 compacted summary + 4 recent
        assert_eq!(msgs[0]["content"], "sys");
        assert!(msgs[1]["content"]
            .as_str()
            .unwrap()
            .contains("Auto-compacted earlier context: 6 message(s)"));
        assert!(msgs[5]["content"].as_str().unwrap().starts_with("turn 9:"));
    }

    #[test]
    fn leaves_short_conversations_untouched() {
        let mut msgs = vec![
            json!({"role": "system", "content": "sys"}),
            json!({"role": "user", "content": "hello"}),
        ];
        assert!(!compact_messages_if_needed(&mut msgs, 4000, 4));
        assert_eq!(msgs.len(), 2);
    }
}
