"""Which tools a turn is offered — for every provider, not just Ollama.

The gap this closes
-------------------
The pack contract has three contributions: handlers, a prompt fragment, and
*tools*.  Two of them were wired.  ``active_tool_names()`` was written, exported
and never called by anything, so the third was inert: every turn on every
provider was offered every registered tool.

Concretely, a request to fix a failing test reached Gemini carrying all 74 tool
schemas, 34 of them domain tools — market quotes, broker orders, backtests,
Stripe analysis, freight reconciliation.  That is the same shape as the system
prompt only reaching Ollama: a decision the architecture already knew how to
make, not applied on the path that matters most.

Why it matters beyond context size
----------------------------------
Wasted context is the smaller cost.  The real one is that a tool in the list is
a tool the model may call, and the incident the pack contract exists to prevent
— a question about this repository answered with a MongoDB stock quote — is
exactly a domain tool firing on a message that named nothing in its domain.
Leaving all 34 in view on every coding turn keeps that door open.

The rule
--------
A domain tool is offered only when its pack has claimed the message.  Anything
no pack claims is core and always available, which makes the default safe:
adding a tool nobody assigns to a domain keeps working exactly as before, and
the burden is on a domain to say "this one is mine".
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

__all__ = ["domain_tool_names", "select_tool_schemas"]


def _schema_name(schema: dict) -> str:
    return str((schema.get("function") or schema).get("name") or "")


def domain_tool_names() -> frozenset[str]:
    """Every tool claimed by some pack, whether or not that pack is active.

    Membership here is what makes a tool *gateable*. A tool absent from this
    set is core and always offered.
    """
    claimed: set[str] = set()
    try:
        from aria_code.packs import load_builtin_packs, registered_packs

        load_builtin_packs()
        for pack in registered_packs():
            try:
                claimed.update(str(name) for name in (pack.tool_names() or ()))
            except Exception:
                continue
    except Exception:
        return frozenset()
    return frozenset(claimed)


def select_tool_schemas(
    schemas: Sequence[dict],
    message: str,
    *,
    always: Iterable[str] = (),
    activations: Optional[Sequence] = None,
    allow_social_tools: bool = False,
) -> list[dict]:
    """The schemas to offer for *message*.

    Standalone social messages need no tools. Otherwise core tools always;
    a domain tool only while its pack has claimed the
    message. ``always`` forces specific names in regardless — for a caller that
    knows this turn needs one (a slash command that resolved to a tool, a
    subagent handed a narrowed brief).

    Degrades to the full list on any failure. Offering too many tools is a
    tax; offering too few silently removes a capability, and between the two
    the tax is the safer default.
    """
    all_schemas = list(schemas or [])
    always = tuple(always)
    if not all_schemas:
        return all_schemas
    from aria_code.apps.cli.conversation_policy import is_conversation_only_message

    if is_conversation_only_message(message) and not always and not allow_social_tools:
        return []

    # One try around the whole decision. domain_tool_names() sat outside it,
    # so a failure there propagated instead of degrading — the docstring
    # promised a safe fallback the code did not deliver on that path.
    try:
        gateable = domain_tool_names()
        if not gateable:
            return all_schemas

        from aria_code.packs import activate_packs, active_tool_names

        active = activations if activations is not None else activate_packs(message or "")
        permitted = set(active_tool_names(active))
    except Exception:
        return all_schemas

    permitted.update(str(name) for name in always if name)

    return [
        schema for schema in all_schemas
        if (name := _schema_name(schema)) not in gateable or name in permitted
    ]
