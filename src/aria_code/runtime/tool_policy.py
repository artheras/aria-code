"""Persistent per-tool execution policy: allowlist, denylist, ask-always.

Stores policy in ~/.arthera/tool_policy.json.
Checked in _confirm_tool_execution_decision() before any user prompt.

Usage:
    from runtime.tool_policy import check_tool_policy, add_to_policy

    verdict = check_tool_policy("write_file")  # "allow" | "deny" | "ask" | "default"
    add_to_policy("read_file", "allow")         # permanently auto-approve
    add_to_policy("run_command", "deny")        # permanently block
    add_to_policy("edit_file", "ask")           # always prompt
    remove_from_policy("read_file")             # remove from all lists
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from aria_code.packages.aria_core.paths import aria_home

PolicyVerdict = Literal["allow", "deny", "ask", "default"]

_DEFAULT_POLICY: dict = {
    "allowed": [],     # auto-approve without prompt
    "denied": [],      # always block, never execute
    "ask_always": [],  # always prompt even for non-CONFIRM_TOOLS
}


def _policy_file() -> Path:
    return aria_home() / "tool_policy.json"


def load_tool_policy() -> dict:
    """Load policy from disk; returns defaults if missing or corrupt."""
    f = _policy_file()
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            return {
                "allowed":    list(raw.get("allowed", [])),
                "denied":     list(raw.get("denied", [])),
                "ask_always": list(raw.get("ask_always", [])),
            }
        except Exception:
            pass
    return {k: list(v) for k, v in _DEFAULT_POLICY.items()}


def save_tool_policy(policy: dict) -> None:
    f = _policy_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(policy, indent=2, ensure_ascii=False), encoding="utf-8")


def _rule_target(tool_name: str, params: Optional[Mapping[str, Any]]) -> str:
    if not params:
        return ""
    if tool_name == "run_command":
        return str(params.get("command") or "").strip()
    for key in ("path", "file_path", "filename", "url", "query", "pattern"):
        if params.get(key):
            return str(params[key]).strip()
    return ""


def matches_policy_rule(rule: str, tool_name: str, params: Optional[Mapping[str, Any]] = None) -> bool:
    """Match either an exact tool name ('run_command') or a parameterized rule ('run_command(pytest *)')."""
    rule = str(rule or "").strip()
    if not rule:
        return False
    if "(" in rule and rule.endswith(")"):
        rule_tool, pattern = rule[:-1].split("(", 1)
        if rule_tool.strip() != tool_name:
            return False
        target = _rule_target(tool_name, params)
        if not target:
            return False
        return fnmatch.fnmatch(target, pattern.strip())
    return rule == tool_name


def check_tool_policy(tool_name: str, params: Optional[Mapping[str, Any]] = None) -> PolicyVerdict:
    """Return the persistent verdict for *tool_name* (and optional *params*).

    Parameter-specific rules (e.g. ``run_command(pytest *)``) are evaluated
    before generic tool-level rules so users can allow safe test commands while
    keeping ``run_command`` itself guarded.

    "allow"   → auto-approve, skip confirmation prompt
    "deny"    → block, never execute
    "ask"     → always prompt (overrides session auto-allow)
    "default" → no override, fall through to normal flow
    """
    policy = load_tool_policy()
    buckets: tuple[tuple[str, PolicyVerdict], ...] = (
        ("denied", "deny"),
        ("allowed", "allow"),
        ("ask_always", "ask"),
    )
    # Pass 1: parameterized rules take precedence over blanket tool rules.
    if params:
        for key, verdict in buckets:
            for rule in policy.get(key, []):
                if "(" in str(rule) and matches_policy_rule(str(rule), tool_name, params):
                    return verdict
    # Pass 2: exact tool-level rules.
    for key, verdict in buckets:
        for rule in policy.get(key, []):
            if "(" not in str(rule) and matches_policy_rule(str(rule), tool_name, params):
                return verdict
    return "default"


def add_to_policy(tool_name: str, verdict: Literal["allow", "deny", "ask"]) -> None:
    """Add *tool_name* to the given policy list, removing it from any other list first."""
    policy = load_tool_policy()
    for key in ("allowed", "denied", "ask_always"):
        policy.setdefault(key, [])
        if tool_name in policy[key]:
            policy[key].remove(tool_name)
    if verdict == "allow":
        policy["allowed"].append(tool_name)
    elif verdict == "deny":
        policy["denied"].append(tool_name)
    elif verdict == "ask":
        policy["ask_always"].append(tool_name)
    save_tool_policy(policy)


def remove_from_policy(tool_name: str) -> bool:
    """Remove *tool_name* from all lists. Returns True if it was present."""
    policy = load_tool_policy()
    changed = False
    for key in ("allowed", "denied", "ask_always"):
        if tool_name in policy.get(key, []):
            policy[key].remove(tool_name)
            changed = True
    if changed:
        save_tool_policy(policy)
    return changed


def policy_summary() -> dict:
    """Return current policy as a human-readable dict."""
    return load_tool_policy()
