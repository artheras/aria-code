"""Provider error classification and lightweight health state."""

from __future__ import annotations

import time
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class ProviderIssue:
    provider: str
    category: str
    message: str
    retryable: bool = True
    cooldown_seconds: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderState:
    provider: str
    status: str = "ok"
    last_error_category: str = ""
    last_error: str = ""
    failures: int = 0
    cooldown_until: float = 0.0
    last_seen_at: float = 0.0
    last_success_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["cooldown_active"] = self.cooldown_until > time.time()
        data["cooldown_remaining_seconds"] = max(0, int(self.cooldown_until - time.time()))
        return data


@dataclass(frozen=True)
class ProviderHealthSummary:
    schema: str
    total: int
    ok: int
    warn: int
    err: int
    cooldown: int
    auth_errors: int
    providers: List[str]
    status: str
    detail: str
    suggestion: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_PUBLIC_SERVICE_LABELS = {
    "market_data": "市场数据",
    "ai": "AI 助手",
}


@dataclass(frozen=True)
class PublicServiceStatus:
    """A safe, product-facing view of internal provider health."""

    schema: str
    service: str
    state: str
    label: str
    message: str
    can_retry: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def public_service_status(
    service: str, snapshot: List[Dict[str, Any]] | None = None
) -> PublicServiceStatus:
    """Project provider health into copy that is safe to render in the product.

    Provider names, endpoint details and raw failures remain internal diagnostics.
    """

    service_key = str(service or "").strip().lower() or "service"
    label = _PUBLIC_SERVICE_LABELS.get(service_key, "服务")
    rows = list(snapshot or [])

    if not rows:
        return PublicServiceStatus(
            schema="aria.public_service_status.v1",
            service=service_key,
            state="unknown",
            label=label,
            message=f"{label}状态将在首次使用后更新。",
            can_retry=False,
        )

    states = [str(row.get("status") or "unknown") for row in rows]
    categories = [str(row.get("last_error_category") or "") for row in rows]
    available_count = sum(state == "ok" for state in states)

    if available_count == len(rows):
        state, message, can_retry = "available", f"{label}可用。", False
    elif available_count:
        state, message, can_retry = (
            "degraded",
            f"{label}已自动切换到可用服务。",
            True,
        )
    elif any(category in {"auth", "model_unavailable"} for category in categories):
        state, message, can_retry = (
            "unavailable",
            f"{label}暂不可用，需要检查连接设置。",
            False,
        )
    else:
        state, message, can_retry = "unavailable", f"{label}暂不可用，请稍后重试。", True

    return PublicServiceStatus(
        schema="aria.public_service_status.v1",
        service=service_key,
        state=state,
        label=label,
        message=message,
        can_retry=can_retry,
    )


def _error_status(error: Any, text: str) -> int | None:
    """Prefer structured HTTP/SDK status over incidental words in a URL."""
    for source in (error, getattr(error, "response", None)):
        for key in ("status_code", "status", "code"):
            value = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            if isinstance(value, int) and 400 <= value <= 599:
                return value
            name = str(getattr(value, "name", value) or "").upper()
            mapped = {"UNAUTHENTICATED": 401, "PERMISSION_DENIED": 403,
                      "NOT_FOUND": 404, "RESOURCE_EXHAUSTED": 429,
                      "DEADLINE_EXCEEDED": 504}.get(name)
            if mapped:
                return mapped
    match = re.search(r"(?:^\s*|\b(?:http|status(?:_code)?|code)\s*[:=]?\s*)([45]\d{2})\b", text, re.I)
    return int(match.group(1)) if match else None


def classify_provider_error(provider: str, error: Any) -> ProviderIssue:
    text = str(error or "").strip()
    low = text.lower()
    if not text:
        return ProviderIssue(provider, "unavailable", "provider returned no usable data", True, 30)
    status = _error_status(error, text)
    if status in {401, 403} or (status is None and (
        any(token in low for token in (
            "unauthorized", "forbidden", "missing_api_key", "invalid credentials",
            "needs_credentials", "permission_denied", "unauthenticated",
        )) or re.search(r"(?:invalid|missing|expired).*api[ _]key|api[ _]key.*(?:invalid|missing|expired)", low)
    )):
        return ProviderIssue(provider, "auth", "provider authentication failed", False, 0)
    if (status == 404 and any(token in low for token in ("model", "generatecontent"))) or (status is None and re.search(
        r"\bmodel\b.*\b(?:not found|unavailable|does not exist|not supported)\b", low
    )):
        return ProviderIssue(provider, "model_unavailable", "selected model is unavailable", False, 0)
    if status == 429 or (status is None and re.search(r"\brate[\s_-]*limit(?:ed|ing)?\b|\btoo many requests\b|\bquota (?:exceeded|exhausted)\b", low)):
        return ProviderIssue(provider, "rate_limited", "provider rate limited the request", True, 60)
    if status in {408, 504} or any(token in low for token in ("timeout", "timed out", "curl: (28)", "read timed out")):
        return ProviderIssue(provider, "timeout", "provider request timed out", True, 30)
    if any(token in low for token in ("connection", "network", "refused", "remote", "dns", "name resolution")):
        return ProviderIssue(provider, "network", "provider network connection failed", True, 30)
    if any(token in low for token in ("empty", "no data", "no market data", "not found", "none", "null")):
        return ProviderIssue(provider, "no_data", "provider returned no market data", True, 15)
    # Do not retain raw provider errors here. They can contain URLs, request
    # headers, or tokens and this state is consumed by product diagnostics.
    return ProviderIssue(provider, "error", "provider request failed", True, 30)


class ProviderHealthRegistry:
    """In-process health state for data providers."""

    def __init__(self) -> None:
        self._states: Dict[str, ProviderState] = {}

    def mark_success(self, provider: str) -> None:
        if not provider:
            return
        state = self._states.setdefault(provider, ProviderState(provider=provider))
        state.status = "ok"
        state.last_error_category = ""
        state.last_error = ""
        state.failures = 0
        state.cooldown_until = 0.0
        now = time.time()
        state.last_seen_at = now
        state.last_success_at = now

    def mark_issue(self, issue: ProviderIssue) -> None:
        if not issue.provider:
            return
        state = self._states.setdefault(issue.provider, ProviderState(provider=issue.provider))
        now = time.time()
        state.status = issue.category
        state.last_error_category = issue.category
        state.last_error = issue.message
        state.failures += 1
        state.last_seen_at = now
        if issue.cooldown_seconds:
            state.cooldown_until = max(state.cooldown_until, now + issue.cooldown_seconds)

    def provider_in_cooldown(self, provider: str) -> bool:
        state = self._states.get(provider)
        return bool(state and state.cooldown_until > time.time())

    def snapshot(self) -> List[Dict[str, Any]]:
        return [self._states[name].to_dict() for name in sorted(self._states)]

    def summary(self) -> ProviderHealthSummary:
        return summarize_provider_health(self.snapshot())

    def public_status(self, service: str) -> PublicServiceStatus:
        return public_service_status(service, self.snapshot())


GLOBAL_PROVIDER_HEALTH = ProviderHealthRegistry()


def summarize_provider_health(snapshot: List[Dict[str, Any]] | None = None) -> ProviderHealthSummary:
    rows = list(snapshot or [])
    if not rows:
        return ProviderHealthSummary(
            schema="aria.provider_health_summary.v1",
            total=0,
            ok=0,
            warn=0,
            err=0,
            cooldown=0,
            auth_errors=0,
            providers=[],
            status="warn",
            detail="no provider calls recorded in this session",
            suggestion="Run /quote, /ta, /analyze, or /report to populate provider health.",
        )

    ok = warn = err = cooldown = auth_errors = 0
    providers: list[str] = []
    for row in rows:
        provider = str(row.get("provider") or "provider")
        providers.append(provider)
        status = str(row.get("status") or "unknown")
        error_category = str(row.get("last_error_category") or "")
        if status == "ok":
            ok += 1
        elif error_category == "auth":
            err += 1
            auth_errors += 1
        else:
            warn += 1
        if row.get("cooldown_active"):
            cooldown += 1

    if err:
        status = "err"
    elif warn or cooldown:
        status = "warn"
    else:
        status = "ok"

    parts = [f"{len(rows)} providers"]
    if ok:
        parts.append(f"{ok} ok")
    if warn:
        parts.append(f"{warn} warn")
    if err:
        parts.append(f"{err} err")
    if cooldown:
        parts.append(f"{cooldown} cooldown")

    suggestion = "Run /doctor --network or inspect /apikey." if status != "ok" else "All providers healthy."
    if auth_errors:
        suggestion = "Fix API keys first, then retry /doctor or /cloud health."

    return ProviderHealthSummary(
        schema="aria.provider_health_summary.v1",
        total=len(rows),
        ok=ok,
        warn=warn,
        err=err,
        cooldown=cooldown,
        auth_errors=auth_errors,
        providers=providers,
        status=status,
        detail=", ".join(parts),
        suggestion=suggestion,
    )
