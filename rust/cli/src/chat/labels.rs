use super::app::App;

pub(super) const HELP: &str = "Aria interactive interface\n\nEnter  send · Alt+Enter / Ctrl+J  newline\nArrow keys  edit / browse history · /  list commands · @  pick a file · Tab  fill in\nCtrl+U / K / W  delete line / end / previous word\nPgUp / PgDn or mouse wheel  scroll transcript\nCtrl+O  toggle tool details · F1  this help\nEsc or Ctrl+C  cancel current task / dismiss dialog\nCtrl+D  exit (or delete character when input is non-empty)\n\n/new  start a new session · /resume ID  restore a session\n/sessions  list saved sessions · /help  all Aria commands\n/model  choose your model · /health  check configured services\n@file:path  reference a file · !command  run a shell command\n\nFile permissions, task isolation, checks and model routing are managed by Aria.\nApproval menus require your choice. Hidden reasoning and secret inputs\nare never written to the native transcript.";

pub(super) const HELP_ZH: &str = "Aria 交互界面\n\nEnter 发送 · Alt+Enter / Ctrl+J 换行\n方向键编辑 / 浏览输入历史 · 输入 / 列出命令 · @ 选择文件 · Tab 补全\nCtrl+U / K / W 删除行首 / 行尾 / 前一个词\nPgUp / PgDn 或鼠标滚轮查看对话历史\nCtrl+O 展开工具详情 · F1 打开帮助\nEsc / Ctrl+C 取消当前任务或关闭对话框\nCtrl+D 退出（输入非空时删除字符）\n\n/new 新会话 · /resume ID 恢复会话\n/sessions 会话列表 · /help 所有命令\n/model 选择模型 · /health 服务检查\n@file:path 引用文件 · !command 执行命令\naria -C <项目目录> 在指定项目中启动\n/config set ui_lang=zh 设置中文界面\n\n运行时管理文件权限、任务隔离与验证。\n审批需要你明确选择；秘密输入与隐藏推理不会写入界面记录。";

pub(super) fn display_provider(id: &str) -> String {
    match id {
        "google" => "Google".into(),
        "vertexai" | "vertex-ai" => "Vertex AI".into(),
        "openai" => "OpenAI".into(),
        "anthropic" => "Anthropic".into(),
        "ollama" => "Ollama".into(),
        "lmstudio" => "LM Studio".into(),
        "vllm" => "vLLM".into(),
        "llamacpp" => "llama.cpp".into(),
        "deepseek" => "DeepSeek".into(),
        "groq" => "Groq".into(),
        "zhipu" => "Zhipu".into(),
        "moonshot" => "Moonshot".into(),
        "dashscope" => "DashScope".into(),
        "siliconflow" => "SiliconFlow".into(),
        "together" => "Together".into(),
        "xai" => "xAI".into(),
        "openrouter" => "OpenRouter".into(),
        other => {
            let mut chars = other.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
                None => String::new(),
            }
        }
    }
}

/// One concrete thing to try, like Claude Code's `Try "fix typecheck errors"`,
/// picked from the session id so it stays put while the session lasts.
pub(super) fn example_prompt(app: &App) -> String {
    const EN_HOME: [&str; 4] = [
        "Ask anything, /health to check services, or aria -C <dir> for a project",
        "Run /health to inspect model and service status",
        "Switch models with /model or ask a question",
        "Type /help for all commands and shortcuts",
    ];
    const ZH_HOME: [&str; 4] = [
        "输入问题、/health 检查服务，或用 aria -C <目录> 打开项目",
        "用 /health 检查模型与服务状态",
        "用 /model 切换模型，或输入任何开发问题",
        "输入 /help 查看全部命令与快捷键",
    ];
    const EN: [&str; 4] = [
        "Add tests for utils.py",
        "Explain how this project is structured",
        "Fix the failing test",
        "Review recent changes and check for edge cases",
    ];
    const ZH: [&str; 4] = [
        "给 utils.py 补上测试",
        "讲讲这个项目的结构",
        "修好失败的测试",
        "审查最近的改动并检查边界情况",
    ];
    let pick = app.session.bytes().map(usize::from).sum::<usize>() % EN.len();
    if app.home_workspace {
        return if app.zh() {
            ZH_HOME[pick].to_string()
        } else {
            EN_HOME[pick].to_string()
        };
    }
    if app.zh() {
        ZH[pick].to_string()
    } else {
        format!("Try \"{}\"", EN[pick])
    }
}
