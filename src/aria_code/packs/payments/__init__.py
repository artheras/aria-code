"""Payments — Stripe objects.

The cleanest case the pack contract has.  Stripe object ids carry a typed
prefix (``ch_`` charge, ``pi_`` payment intent, ``cus_`` customer, ``sub_``
subscription…) followed by a long opaque suffix, so recognising one is a
resolution rather than a guess: nothing else in an English or Chinese sentence
looks like ``pi_3PqR8s2eZvKYlo2C0aBcDeFg``.

That precision is what lets this pack stay registered permanently at no cost.
It cannot fire on domain vocabulary — the words "payment", "refund" and
"订阅" never activate it — so a payments deployment adds nothing to the latency
or the failure surface of a session about anything else.
"""

from __future__ import annotations

import re
from typing import Sequence

from aria_code.packs.base import BaseDomainPack, EntityMatch, PackActivation

PACK_NAME = "payments"

PAYMENT_TOOLS: tuple[str, ...] = ()

_RESOLVED = 0.95

# prefix → what the object is. Kept explicit rather than matching a generic
# `\w+_[A-Za-z0-9]{14,}` because an unknown prefix is far more likely to be
# someone's own snake_case identifier than a Stripe object.
_OBJECT_PREFIXES: dict[str, str] = {
    "ch": "charge",
    "pi": "payment_intent",
    "cus": "customer",
    "sub": "subscription",
    "in": "invoice",
    "il": "invoice_item",
    "price": "price",
    "prod": "product",
    "acct": "account",
    "evt": "event",
    "re": "refund",
    "txn": "balance_transaction",
    "po": "payout",
    "seti": "setup_intent",
    "pm": "payment_method",
    "cs": "checkout_session",
    "dp": "dispute",
    "sk": "secret_key",
    "rk": "restricted_key",
}

# Stripe ids are prefix + '_' + at least 14 base62 characters, optionally with
# a `test_` segment in live-vs-test form (`sk_test_…`, `ch_test_…`).
_OBJECT_ID = re.compile(
    r"\b(" + "|".join(sorted(_OBJECT_PREFIXES, key=len, reverse=True)) + r")_"
    r"(?:test_|live_)?([A-Za-z0-9]{14,})\b"
)


class PaymentsPack(BaseDomainPack):
    """Recognises Stripe object ids and owns the payments workflows."""

    name = PACK_NAME

    def resolve_entities(self, message: str) -> Sequence[EntityMatch]:
        text = message or ""
        if "_" not in text:
            # Every id this pack knows contains an underscore, so this one
            # check skips the regex entirely for the overwhelming majority of
            # messages — the resolver runs on every message, for every pack.
            return ()

        seen: set[str] = set()
        entities: list[EntityMatch] = []
        for match in _OBJECT_ID.finditer(text):
            value = match.group(0)
            if value in seen:
                continue
            seen.add(value)
            entities.append(EntityMatch(
                pack=PACK_NAME,
                kind=_OBJECT_PREFIXES[match.group(1)],
                value=value,
                surface=value,
                position=match.start(),
                confidence=_RESOLVED,
            ))
        return tuple(entities)

    def tool_names(self) -> Sequence[str]:
        return PAYMENT_TOOLS

    def prompt_fragment(self, activation: PackActivation) -> str:
        kinds = ", ".join(sorted({e.kind for e in activation.entities}))
        has_key = any(e.kind.endswith("_key") for e in activation.entities)
        text = (
            f"Stripe 对象已识别（{kinds}）。\n"
            "用工具取回的账务数据回答，不要凭记忆推断金额、状态或结算周期；"
            "涉及退款、扣款、订阅变更等写操作时，先说明将要发生什么并等待确认。"
        )
        if has_key:
            # The one case where the right move is to stop rather than help.
            text += (
                "\n⚠ 消息中疑似包含 Stripe 密钥。不要复述它、不要写入文件、"
                "不要用它发起请求；提醒用户立即在 Stripe 后台轮换该密钥。"
            )
        return text

    def acceptance_commands(self, activation: PackActivation) -> Sequence[str]:
        # Looked up through the module rather than a name bound at import, so
        # what this workspace declares is read when the turn runs — a .ariarc
        # edited mid-session takes effect on the next message, not the next
        # process.
        from aria_code.packs import rules

        return rules.acceptance_commands_for(PACK_NAME)


PAYMENTS_PACK = PaymentsPack()


def register() -> PaymentsPack:
    """Register the payments pack.  Idempotent."""
    from aria_code.packs.registry import register_pack

    register_pack(PAYMENTS_PACK)
    return PAYMENTS_PACK


__all__ = [
    "PACK_NAME",
    "PAYMENTS_PACK",
    "PAYMENT_TOOLS",
    "PaymentsPack",
    "register",
]
