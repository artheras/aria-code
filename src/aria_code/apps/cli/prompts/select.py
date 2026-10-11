"""Pick the system prompt for a turn, for every provider — not just Ollama.

The gap this closes
-------------------
``CODING_SYSTEM_PROMPT`` — the tool discipline, the orient-before-search rule,
"never say 'I will do X', just do it", the note that verification runs itself —
was built inside ``providers/llm/ollama_stream.py`` and nowhere else.  Every
other backend reached the model with an empty system prompt.

So the strongest model available ran with no instructions at all.  It showed up
exactly as the rules predict when they are missing: given a failing test,
Gemini narrated a five-step plan, read the test file, announced "now I will
read calc.py" — and stopped, having called no tool and changed nothing.  It was
not incapable; it had never been told that describing an edit is not making
one.

Why this module rather than moving the code
-------------------------------------------
``ollama_stream`` builds far more than a base prompt: prefetched market data,
project context sized to the model, injected memory, skill activation.  Moving
all of that would destabilise the path that currently works to fix the one that
does not.  This module owns only the decision every path shares — *which* base
prompt this message calls for — so the two agree on the rules without the cloud
path inheriting Ollama's plumbing.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["select_base_prompt", "build_turn_system_prompt"]


def _intent_of(message: str) -> str:
    try:
        from aria_code.apps.cli.intent_signals import classify

        return classify(message or "")
    except Exception:
        # An unclassifiable message is a coding message. The bias is the same
        # one the classifier itself uses: the cost of the coding prompt on a
        # chat turn is a longer preamble, while the cost of a chat prompt on a
        # coding turn is that nothing gets done and nobody is told why.
        return "coding"


def select_base_prompt(message: str, *, intent: Optional[str] = None) -> str:
    """The base system prompt this message calls for.

    Returns "" only for intents that are genuinely better served without one
    (a live-data lookup, where the finance prompt is assembled with fetched
    data by the caller that has it).
    """
    from aria_code.apps.cli.conversation_policy import is_social_message
    if intent is None and is_social_message(message):
        return _general_prompt()
    resolved = intent or _intent_of(message)
    if resolved == "general" and intent is None and _needs_workspace(message):
        # "Write a stock brief to report_BETA.md from inventory.csv" read as
        # general chat and got the short general prompt, with no tool
        # discipline; in the evals the agent then computed by hand. Work on
        # files in this folder is done the way code is.
        resolved = "coding"

    if resolved == "coding":
        from aria_code.apps.cli.prompts.coding import CODING_SYSTEM_PROMPT

        return CODING_SYSTEM_PROMPT

    if resolved == "analysis":
        try:
            from aria_code.apps.cli.prompts.system_prompts import build_analysis_system_prompt

            return build_analysis_system_prompt()
        except Exception:
            return ""

    if resolved in ("finance", "realtime"):
        try:
            from aria_code.apps.cli.prompts.system_prompts import build_finance_prompt

            return build_finance_prompt(message or "")
        except Exception:
            return ""

    return _general_prompt()


def _needs_workspace(message: str) -> bool:
    try:
        from aria_code.apps.cli.workspace_route import needs_workspace

        return needs_workspace(message or "")
    except Exception:
        return False


# Appended to every turn's prompt, whatever the intent. Both rules come from
# the evals: asked to write reorder.json or audit.json, the agent reported its
# results and wrote nothing; asked for a shipper's total stock, it added eight
# numbers by hand and got 500 for 560.
OUTPUT_DISCIPLINE = (
    "Output rules:\n"
    "- When the request names a file to write (a report, a JSON result), write it with a tool. "
    "Before you finish, confirm it exists by reading it back or listing the folder. Describing the "
    "result in your reply does not create the file.\n"
    "- Compute totals, counts, averages and other figures from the data with code (run a short "
    "script), not by hand. Every number you report or write must come from a tool's output.\n"
    "- Something written for one client, customer or shipper contains only their data. Data "
    "files often hold several clients: filter to theirs, and leave out every other client's "
    "name, IDs, SKUs and totals, including any that appear inside this client's own rows "
    "(notes, comments, references). Copy free-text fields into a client document only after "
    "checking them for that."
)


def _general_prompt() -> str:
    """What Aria is, for turns that are neither code nor market work.

    Returning "" here was the first version of this module and it was wrong in
    the same way the original bug was: a general turn on a cloud provider then
    reached the model with no system prompt at all, so it did not know what
    tools it had, what it was for, or what today's date is. Every intent gets
    something.
    """
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    return (
        f"You are Aria, Arthera's product and software-engineering assistant. "
        f"Today is {today}.\n"
        "You review products, understand repositories, find defects and risks, plan "
        "architecture, and write and verify code. Finance and sports analysis are "
        "optional domain capabilities, used only when the user asks for them.\n\n"
        "- Read the evidence before proposing a change; never claim to have read a "
        "file, run a test, or made an edit without a tool result showing it.\n"
        "- State the scope before a change and the verification after it.\n"
        "- Do not invent prices, exchange rates, or other live figures.\n"
        "- Answer in the user's language. Be concise and specific."
        "\n- A standalone greeting or thanks needs only a short direct reply, with no tools, "
        "directory inventory, personal-file references or internal role titles."
    )


def build_turn_system_prompt(
    message: str,
    *,
    override: Optional[str] = None,
    project_context: str = "",
    intent: Optional[str] = None,
) -> str:
    """The full system prompt for one turn.

    ``override`` wins outright and is returned unchanged: it is set by callers
    that have already decided what the model should be told (a subagent with a
    narrowed brief, a skill that owns the turn), and silently appending the
    general coding rules to those would contradict them.
    """
    if override and override.strip():
        return override

    from aria_code.apps.cli.conversation_policy import is_social_message
    if intent is None and is_social_message(message):
        return _general_prompt()

    parts = [select_base_prompt(message, intent=intent), OUTPUT_DISCIPLINE]
    if project_context and project_context.strip():
        parts.append(project_context.strip())

    # Domain guidance from whichever packs this message actually activated.
    # Same entity gate as everywhere else, so a code turn is never handed
    # market instructions.
    try:
        from aria_code.packs import (
            activate_packs,
            active_prompt_fragments,
            load_builtin_packs,
        )

        load_builtin_packs()
        parts.extend(active_prompt_fragments(activate_packs(message or "")))
    except Exception:
        pass

    return "\n\n".join(part for part in parts if part and part.strip())
