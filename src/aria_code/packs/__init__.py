"""Domain packs — subject areas that plug into a domain-neutral core.

The core knows about code, documents, data, and conversation.  Everything that
belongs to a subject area — the identifiers it uses, the tools that act on
them, the guidance a model needs to reason about them — lives in a pack and is
switched on only when the user's message names something that pack recognises.

Adding a domain means adding a pack.  It does not mean adding a branch to a
router, an intent label to the classifier, or a handler to a chain that every
message walks.

See ``aria_code.packs.base`` for the contract and ``aria_code.packs.registry``
for the activation rule.
"""

from __future__ import annotations

from aria_code.packs.base import (
    DEFAULT_ACTIVATION_THRESHOLD,
    BaseDomainPack,
    DomainPack,
    EntityMatch,
    PackActivation,
)
from aria_code.packs.registry import (
    activate_packs,
    active_acceptance_commands,
    active_handlers,
    active_prompt_fragments,
    active_tool_names,
    clear_registry,
    get_pack,
    register_pack,
    registered_packs,
    unregister_pack,
)

_BUILTIN_LOADED = False


def load_builtin_packs() -> tuple[str, ...]:
    """Register any built-in packs that ship with Aria.  Idempotent.

    Vertical domain packs (finance, healthcare, logistics, etc.) have moved to
    external MCP services; third-party packs can still register via
    :func:`register_pack`.
    """
    global _BUILTIN_LOADED
    if not _BUILTIN_LOADED:
        _BUILTIN_LOADED = True
    return tuple(getattr(p, "name", "") for p in registered_packs())


def reset_builtin_packs() -> None:
    """Clear the registry and allow built-ins to load again (tests)."""
    global _BUILTIN_LOADED
    clear_registry()
    _BUILTIN_LOADED = False


__all__ = [
    "DEFAULT_ACTIVATION_THRESHOLD",
    "BaseDomainPack",
    "DomainPack",
    "EntityMatch",
    "PackActivation",
    "activate_packs",
    "active_acceptance_commands",
    "active_handlers",
    "active_prompt_fragments",
    "active_tool_names",
    "clear_registry",
    "get_pack",
    "load_builtin_packs",
    "register_pack",
    "registered_packs",
    "reset_builtin_packs",
    "unregister_pack",
]
