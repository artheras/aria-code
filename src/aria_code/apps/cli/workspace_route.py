"""A task about this folder, sent to a model that cannot touch it.

With backend_chat on, turns go to the Arthera backend, which receives the
prompt and history but none of Aria's local tools. Asked to run
`python3 -c 'import calc; print(calc.add(2,3))'` in a folder, it said it had
no filesystem access and gave the output as 5; the function subtracts, so it
is -1. The turn reported success with no tools used, and `-p` exited 0. A
small local model without tool calling is in the same position.

``needs_workspace`` asks only whether a message needs this machine: it names
a file or imports a module that exists here, uses an @file reference, asks
for a command to be run, or says "this project", "the failing tests" and the
like. A question about code in general ("explain Python decorators") does not
need the folder and is left alone.
"""

from __future__ import annotations

import re
from pathlib import Path

_PATH_TOKEN = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,6}\b|[\w-]+/[\w./-]+")
_HERE_EN = re.compile(
    r"\b(?:this|my|our|current)\s+(?:project|repo|repository|folder|directory|codebase|workspace|"
    r"working\s+tree)\b|\bin\s+here\b|\b(?:the\s+)?failing\s+tests?\b|\bmy\s+(?:code|tests)\b",
    re.I,
)
# Asking for a command to be run needs a tool to run it, wherever it points.
_RUN_EN = re.compile(r"\b(?:run|execute)\s+(?:`|python3?\b|pytest\b|npm\b|npx\b|node\b|make\b|cargo\b|"
                     r"go\s+(?:test|run|build)\b|bash\b|sh\b|pip3?\b|uv\b|git\b|ls\b|the\s+tests?\b)", re.I)
_RUN_ZH = re.compile(r"(?:运行|执行|跑一下|跑)\s*(?:`|python|pytest|npm|node|git|make|命令|脚本|测试)", re.I)
_IMPORT = re.compile(r"\b(?:import|from)\s+([A-Za-z_]\w*)")
_HERE_ZH = ("这个项目", "该项目", "当前项目", "这个仓库", "当前仓库", "这个目录", "当前目录", "这个文件夹",
            "当前文件夹", "这个代码库", "本项目", "本仓库", "失败的测试", "我的代码",
            "我的项目", "本地项目", "本地文件", "电脑文件", "桌面文件", "创建项目", "新建项目")


def needs_workspace(text: str, cwd: Path | str | None = None) -> bool:
    """True when the message is about files in ``cwd`` (default: the current directory)."""
    message = str(text or "")
    if "@file:" in message or "@folder:" in message:
        return True
    if _HERE_EN.search(message) or any(word in message for word in _HERE_ZH):
        return True
    if re.search(r"\b(?:create|build|scaffold|initialize)\s+(?:a\s+|an\s+|new\s+)?(?:react\s+|next\.js\s+)?(?:app|project|website)\b", message, re.I):
        return True
    if _RUN_EN.search(message) or _RUN_ZH.search(message):
        return True
    base = Path(cwd or Path.cwd())
    for module in _IMPORT.findall(message):
        if (base / f"{module}.py").is_file() or (base / module / "__init__.py").is_file():
            return True
    tokens = _PATH_TOKEN.findall(message)
    # Chinese prose can touch an ASCII filename without whitespace (修改calc.py).
    tokens += re.findall(r"[A-Za-z0-9_./~-]+\.[A-Za-z0-9]{1,6}\b", message)
    for token in tokens:
        candidate = token.strip("./") if token.startswith("./") else token
        try:
            if candidate and (base / candidate).exists():
                return True
        except (OSError, ValueError):
            continue
    return False


def route_has_tools(model: str, config: dict, api_url: str | None) -> bool:
    """Whether this turn's model can call Aria's local tools."""
    try:
        from aria_code.model_capability import get_model_capability

        capability = get_model_capability(model)
        if not (capability.tool_calls and capability.context_window >= 8192):
            return False
    except Exception:
        pass
    try:
        from aria_code.apps.cli.providers.chat_routing import model_receives_local_tools

        return model_receives_local_tools(model, config, api_url)
    except Exception:
        return True


def no_tools_message(config: dict, *, lang: str = "en") -> str:
    """Why the model cannot do this here, and what to change."""
    backend = bool(config.get("backend_chat"))
    if lang == "zh":
        why = ("当前通过 Arthera 云端对话（backend_chat），那里的模型读不到这个文件夹，也不能运行命令"
               if backend else "当前模型不支持工具调用，读不到这个文件夹，也不能运行命令")
        return (f"{why}，关于这里文件的回答只能是推测。要在这个文件夹里写代码、跑测试，请用 /model "
                "选一个能调用工具的模型（例如 Vertex 上的 Gemini、OpenAI 或 Ollama 的 qwen2.5-coder），"
                "或用 aria-code --local。")
    why = ("Turns go to Arthera cloud chat (backend_chat), where the model cannot read this folder or run "
           "commands" if backend else "The current model cannot call tools, so it cannot read this folder "
           "or run commands")
    return (f"{why}; anything it says about files here is a guess. To work on code in this folder, pick a "
            "model that runs tools with /model (Gemini on Vertex, OpenAI, or an Ollama model such as "
            "qwen2.5-coder), or start with aria-code --local.")


def workspace_config(text: str, model: str, config: dict, api_url: str | None) -> dict:
    """Choose authenticated Google inference with local tools for workspace tasks.

    Change only this turn's inference transport, never the saved config or
    the user's model. A backend chat setting must not turn edits into guesses.
    """
    from aria_code.apps.cli.providers.chat_routing import force_backend, model_provider, normalize_provider_name
    if not needs_workspace(text) or not force_backend(config, api_url):
        return config
    if config.get("backend_local_tools"):
        return config
    provider = model_provider(model) or normalize_provider_name(config.get("local_provider", ""))
    if provider not in {"google", "vertexai", "vertex-ai", "google-genai"}:
        return config
    from aria_code.apps.cli.providers.base import google_readiness
    if google_readiness(config):
        return config
    return {**config, "backend_chat": False, "_workspace_transport": "google"}


__all__ = ["needs_workspace", "route_has_tools", "no_tools_message", "workspace_config"]
