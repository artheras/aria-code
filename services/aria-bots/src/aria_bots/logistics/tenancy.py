"""Shipper (货主) isolation for third-party-logistics analyses.

A 3PL holds many shippers' inventory and waybills side by side. An analysis
that sums them — an average cost per kg, a carrier ranking, a reorder list —
is computed from one client's commercial data and handed to another. That is
a data leak whether or not anyone notices, so the analyses do not decide it
case by case: every one of them goes through `scope_records` first, and it
fails closed.

  - Records that carry no owner_id at all are single-tenant data, and pass.
  - Records that all belong to one shipper pass.
  - Records that span shippers are refused unless the caller names one
    (`owner_id`, a client-facing analysis) or explicitly asks for the 3PL's
    internal cross-shipper view (`all_owners=True`), which is marked as not
    for client distribution.
  - A mix of records with and without owner_id is refused, because there is
    no safe way to attribute the unlabelled ones.

What an error message says is part of the guarantee: it may say how many
shippers the data spans, but never which, so a refusal does not itself
disclose another client's name.

Conversation scope
------------------
A chat bound to one shipper (a 3PL usually keeps one group per client) sets
ARIA_OWNER_SCOPE for every turn it runs. Then, whatever the model asks for:
  - owner_id defaults to that shipper, and naming another is refused;
  - all_owners is refused — the internal view has no place in a client chat;
  - records without owner_id are refused, because nothing shows they are this
    shipper's (a file on the host could be anyone's).
This is enforced here rather than in a prompt, so it holds for the CLI, the
MCP server and every chat channel alike, and does not depend on the model
choosing to comply.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

OWNER_FIELD = "owner_id"
OWNER_SCOPE_ENV = "ARIA_OWNER_SCOPE"

INTERNAL_MARKER = "INTERNAL — cross-shipper view; not for client distribution"


class TenancyError(ValueError):
    """The records cannot be analysed without mixing shippers."""


def active_owner_scope() -> str:
    """The shipper this process is confined to, or "" when unconfined."""
    return os.environ.get(OWNER_SCOPE_ENV, "").strip()


def _owner_of(record: dict[str, Any]) -> str:
    value = record.get(OWNER_FIELD)
    return "" if value is None else str(value).strip()


def scope_records(
    records: Iterable[dict[str, Any]],
    owner_id: str | None = None,
    *,
    all_owners: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the records an analysis may use, and a description of the scope.

    The scope dict is copied into every result, so a reader can always tell
    whose data a number came from and whether it may be shown to a client.
    """
    records = list(records)
    wanted = (owner_id or "").strip()
    if wanted and all_owners:
        raise TenancyError("Pass owner_id or all_owners, not both")

    scope = active_owner_scope()
    if scope:
        if all_owners:
            raise TenancyError(
                "This conversation is confined to one shipper; the cross-shipper view "
                "is not available here"
            )
        if wanted and wanted != scope:
            raise TenancyError("This conversation is confined to one shipper and cannot analyse another")
        wanted = scope

    labelled = [r for r in records if _owner_of(r)]
    unlabelled = len(records) - len(labelled)

    if not labelled:
        if scope:
            raise TenancyError(
                f"This conversation is confined to one shipper, so every record must carry "
                f"{OWNER_FIELD} to show it is theirs; these records have none"
            )
        if wanted:
            raise TenancyError(
                f"owner_id {wanted!r} was requested, but the records carry no {OWNER_FIELD}"
            )
        return records, {"mode": "single_tenant", "client_facing": True}

    if unlabelled:
        raise TenancyError(
            f"{unlabelled} of {len(records)} records have no {OWNER_FIELD}; they cannot "
            f"be attributed to a shipper, so they cannot be analysed alongside records "
            f"that can"
        )

    owners = {_owner_of(r) for r in labelled}

    if wanted:
        in_scope = [r for r in labelled if _owner_of(r) == wanted]
        if not in_scope:
            # Deliberately does not list the owners that *are* present.
            raise TenancyError(f"No records for owner_id {wanted!r}")
        result_scope = {"mode": "single_owner", "owner_id": wanted, "client_facing": True}
        if scope:
            result_scope["conversation_scoped"] = True
        return in_scope, result_scope

    if len(owners) == 1:
        (only,) = owners
        return labelled, {"mode": "single_owner", "owner_id": only, "client_facing": True}

    if all_owners:
        return labelled, {
            "mode": "all_owners",
            "shipper_count": len(owners),
            "client_facing": False,
            "marker": INTERNAL_MARKER,
        }

    raise TenancyError(
        f"These records belong to {len(owners)} different shippers. Pass owner_id to "
        f"analyse one shipper (safe to share with that client), or all_owners=true for "
        f"the internal cross-shipper view (not for client distribution)."
    )


def owner_params_schema() -> dict[str, Any]:
    """JSON-schema properties every tenant-aware tool accepts."""
    return {
        "owner_id": {
            "type": "string",
            "description": "Shipper (货主) to analyse. Required when records span several shippers.",
        },
        "all_owners": {
            "type": "boolean",
            "description": "Internal 3PL view across all shippers. Output is marked not for client distribution.",
        },
    }
