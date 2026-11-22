//! Native Rust conversation policy and system-prompt selection.
//! Keeps core routing and Aria Code identity compiled inside the binary.

const SOCIAL_MESSAGES: &[&str] = &[
    "hi",
    "hello",
    "hey",
    "hiya",
    "hi there",
    "hello there",
    "how are you",
    "how are you doing",
    "good morning",
    "good afternoon",
    "good evening",
    "hi aria",
    "hello aria",
    "hey aria",
    "thanks",
    "thank you",
    "thanks aria",
    "bye",
    "goodbye",
    "你好",
    "你好啊",
    "你好呀",
    "您好",
    "嗨",
    "哈喽",
    "在吗",
    "你在吗",
    "早安",
    "晚安",
    "早上好",
    "下午好",
    "晚上好",
    "👋",
    "你好aria",
    "aria你好",
    "你好 aria",
    "aria 你好",
    "hiaria",
    "helloaria",
    "heyaria",
    "谢谢",
    "谢谢你",
    "再见",
];

fn normalize_text(raw: &str) -> String {
    let trimmed = raw.trim().to_lowercase();
    let stripped: String = trimmed
        .chars()
        .map(|c| {
            if matches!(
                c,
                '!' | '！' | '?' | '？' | '。' | '，' | ',' | '.' | '~' | '～'
            ) {
                ' '
            } else {
                c
            }
        })
        .collect();
    stripped.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Standalone greetings and short social turns that require no tools or repo context.
pub fn is_social_message(message: &str) -> bool {
    let norm = normalize_text(message);
    SOCIAL_MESSAGES.contains(&norm.as_str())
}

/// Standalone questions about Aria Code's capabilities or `/health` service diagnostics.
pub fn is_capability_question(message: &str) -> bool {
    let norm = normalize_text(message);
    let compact: String = norm.chars().filter(|c| !c.is_whitespace()).collect();
    if compact.is_empty() {
        return false;
    }

    // Reject mixed action requests that happen to mention capabilities or files.
    if [
        "read ", "fix ", "edit ", "write ", "run ", "readme", ".py", ".rs", ".ts", "读取", "修改",
        "修复", "运行",
    ]
    .iter()
    .any(|kw| norm.contains(kw))
    {
        return false;
    }

    let zh_capability = [
        "你能帮我做什么",
        "你可以帮我做什么",
        "你能做什么",
        "你可以做什么",
        "你有什么功能",
        "你有哪些功能",
        "你有哪些能力",
        "你有什么能力",
        "你有哪些服务",
        "你有什么服务",
        "介绍一下你的能力",
        "介绍一下你的功能",
        "介绍一下你的服务",
    ];
    if zh_capability.iter().any(|p| compact == *p) {
        return true;
    }

    let is_health_service = (compact.contains("healthservice")
        || compact.contains("健康检查")
        || compact.contains("服务检查")
        || compact.contains("健康服务"))
        && (compact.contains("有哪些")
            || compact.contains("有什么")
            || compact.contains("做什么")
            || compact.contains("功能")
            || compact.contains("服务")
            || compact.contains("介绍"));
    if is_health_service {
        return true;
    }

    matches!(
        norm.as_str(),
        "what can you do"
            | "what can you help me with"
            | "what are your capabilities"
            | "what are your features"
            | "what are your services"
            | "what are your health services"
            | "what does your health service do"
            | "what does your health service check"
    )
}

pub fn is_conversation_only_message(message: &str) -> bool {
    is_social_message(message) || is_capability_question(message)
}

/// Core Aria Code identity prompt compiled directly into the native binary.
pub fn general_system_prompt(today: &str) -> String {
    format!(
        "You are Aria (Aria Code), Arthera's terminal-first software-engineering and general task assistant. Today is {today}.\n\
You help with software engineering (reading codebases, debugging, editing, testing, code review), research, datasets, and documents. \
You are NOT limited to quantitative finance; specialist domain rules apply only when the user asks about that domain. Never list or offer capabilities unprompted.\n\n\
Built-in CLI services & commands:\n\
- `/help` / `/clear` / `/new`: show native command reference or start a fresh conversation.\n\
- `/health` / `/doctor`: check configured services and workspace diagnostics (local Ollama, cloud LLM providers, MCP servers, runtime, git/workspace, and sandbox status).\n\
- `/config` / `/permissions [init]`: inspect active config and `.aria/settings.json` declarative allow/deny rules.\n\
- `/mcp` / `/mcp connect` / `/mcp connect --uvx` / `/mcp probe`: list, connect (local or zero-clone `uvx`), and live-probe external ecosystem MCP servers (`arthera-finance`, `aria-healthcare`, `erp-service`) when domain tasks need them.\n\
- `/hooks [init]`, `/sandbox`, `/skills`, `/init [--force]`, `/compact`: manage lifecycle hooks, OS sandbox confinement, workspace skills, `ARIA.md`, and context compaction.\n\
- `/model`: switch models; `/sessions`: inspect saved sessions; `@file:path`: reference files; `aria -C <dir>`: open a project directory.\n\
- When the user asks about `health service` or service diagnostics, explain Aria Code's `/health` service checks rather than medical health or finance.\n\n\
- Read the evidence before proposing a change; never claim to have read a file, run a test, or made an edit without a tool result showing it.\n\
- Answer in the user's language. Be concise and specific."
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recognizes_social_greetings() {
        assert!(is_social_message("你好！"));
        assert!(is_social_message("Hello, Aria!"));
        assert!(!is_social_message("你好，读取 README.md"));
    }

    #[test]
    fn recognizes_health_service_capability_queries() {
        assert!(is_capability_question(
            "你的health service有哪些服务以及能帮助我做什么"
        ));
        assert!(is_capability_question("What does your health service do?"));
        assert!(is_capability_question("what are your services"));
        assert!(!is_capability_question("fix health_service.py"));
    }

    #[test]
    fn general_prompt_establishes_aria_code_identity() {
        let prompt = general_system_prompt("2026-11-16");
        assert!(prompt.contains("Aria Code"));
        assert!(prompt.contains("/health"));
        assert!(prompt.contains("/doctor"));
        assert!(prompt.contains("/permissions"));
        assert!(prompt.contains("/mcp connect --uvx"));
        assert!(prompt.contains("NOT limited to quantitative finance"));
    }
}
