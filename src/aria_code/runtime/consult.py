"""Ask another model — Claude, ChatGPT, Gemini — and bring its answer back.

Aria's own model does the work; another provider's model is useful when you want
a second opinion on a design, an unfamiliar API, or a bug the first model keeps
circling. `/consult claude <question>` sends the question (and whatever `@file`
references you put in it) to that model and files the answer in the conversation,
so the next thing you tell Aria can build on it.

Nothing goes out unless you type the command: the other model sees only the
question, never the conversation, the project or the tools.
"""

from __future__ import annotations

from typing import Mapping, Optional, Tuple

CONSULT_SYSTEM = (
    "You are being consulted by an AI coding assistant on behalf of a developer. "
    "Answer the question directly and concisely, and say what you are unsure of. "
    "You cannot see the project or the conversation; use only what is in the question."
)

#: Short names that mean a model. Only what is certain is built in; add the rest with
#: `/config set consult_aliases=...` or just type the full `provider/model`.
DEFAULT_ALIASES = {"claude": "anthropic/claude-sonnet-5-5"}


def resolve_model(name: str, config: Optional[Mapping] = None) -> Optional[str]:
    """`claude` or `provider/model` as a provider-qualified id, or None when it is neither."""
    from aria_code.apps.cli.providers.chat_routing import model_provider

    aliases = dict(DEFAULT_ALIASES)
    custom = (config or {}).get("consult_aliases")
    if isinstance(custom, Mapping):
        aliases.update({str(k).lower(): str(v) for k, v in custom.items()})
    model = aliases.get(name.strip().lower(), name.strip())
    return model if model_provider(model) else None


def split_request(args: str) -> Tuple[str, str]:
    """`claude why does this race?` -> ("claude", "why does this race?")."""
    parts = args.strip().split(maxsplit=1)
    return (parts[0], parts[1].strip()) if len(parts) == 2 else ((parts[0], "") if parts else ("", ""))


def note_for_conversation(model: str, question: str, answer: str) -> str:
    """How the answer is filed in the conversation, so Aria's model can use it."""
    return f"[second opinion from {model}]\nQuestion: {question}\n\nAnswer:\n{answer}"


async def ask(model: str, question: str, config: Mapping, *, ollama_url: str = "http://localhost:11434") -> str:
    """The other model's answer to `question`; raises on a failed call."""
    from aria_code.apps.cli.providers.runtime_bridge import make_provider_fn

    call = make_provider_fn(
        model=model, config=dict(config), api_url=None, ollama_url=ollama_url,
        tool_schemas=[], system_override=CONSULT_SYSTEM,
    )
    result = await call(question, [])
    if not result.get("success", True):
        raise RuntimeError(result.get("error") or "the model did not answer")
    answer = str(result.get("response") or "").strip()
    if not answer:
        raise RuntimeError("the model returned an empty answer")
    return answer
