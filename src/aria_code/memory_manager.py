"""
memory_manager.py — Aria Code 全局用户 memory 系统

存储位置：<ARIA_HOME>/accounts/<UID 哈希>/memory/（匿名使用独立目录）
  MEMORY.md                ← 索引（每次启动加载）
  user_profile.md          ← 用户偏好、交易风格
  project_<slug>.md        ← /project load 时自动建档
  research_<topic>.md      ← 研究主题（用户触发时创建）

公开 API：
  MemoryManager.load_context(max_chars)  → 注入 system prompt
  MemoryManager.append(slug, content)    → 追加一条事实
  MemoryManager.upsert_project(name, facts) → 项目建档
  MemoryManager.list_all()               → 所有条目
  MemoryManager.clear_all()              → 清空全局 memory
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from aria_code.packages.aria_core.paths import aria_home

logger = logging.getLogger(__name__)

_MEMORY_DIR = aria_home() / "memory"
_INDEX_FILE = _MEMORY_DIR / "MEMORY.md"

_PREF_PATTERNS = [
    (r"(我?不喜欢|我?喜欢|prefer(?:ence)?s?|I always|I never|我总是|我通常)", "preference"),
    (r"(我的风险|风险偏好|risk.*(?:低|高|中|保守|激进)|conservative|aggressive)", "risk_profile"),
    (r"(?:关注|研究|在看|tracking|watching)\s*([A-Z0-9，,、和与&/\s]{2,40})", "watchlist"),
    (r"(我的策略|my strategy|我用|I use)\s+(.{4,40})", "strategy"),
]

_SENSITIVE_PATTERN = re.compile(
    r"(\d+[\.,]\d{2,}(?:%|元|USD|HKD|CNY|万|亿)?|持仓|盈亏|亏损|盈利|买入|卖出|成本价)"
)


def _slugify(name: str) -> str:
    name = re.sub(r"[^\w\-]", "_", name.lower())
    return re.sub(r"_+", "_", name).strip("_")[:40]


class MemoryManager:
    def __init__(self, root: Optional[Path] = None, config=None):
        from aria_code.account_session import read_config
        from aria_code.account_memory import AccountMemory
        self._explicit_root = root
        self._config = config if config is not None else read_config
        self.account = AccountMemory(self._config)

    @property
    def root(self):
        from aria_code.account_session import account_root
        config = self._config() if callable(self._config) else self._config
        root = self._explicit_root or account_root(config) / "memory"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @property
    def _index(self):
        return self.root / "MEMORY.md"

    def _signed_in(self):
        from aria_code.account_session import account_id
        config = self._config() if callable(self._config) else self._config
        return not self._explicit_root and account_id(config) is not None

    # ── Index management ──────────────────────────────────────────────────────

    def _read_index(self) -> list[dict]:
        if not self._index.exists():
            return []
        entries = []
        for line in self._index.read_text(encoding="utf-8").splitlines():
            m = re.match(r"-\s+\[(.+?)\]\((.+?)\)\s*—\s*(.*)", line)
            if m:
                entries.append({"title": m.group(1), "file": m.group(2), "summary": m.group(3)})
        return entries

    def _write_index(self, entries: list[dict]) -> None:
        lines = ["# Aria Memory Index\n"]
        for e in entries:
            lines.append(f"- [{e['title']}]({e['file']}) — {e['summary']}")
        self._index.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _upsert_index(self, file: str, title: str, summary: str) -> None:
        entries = self._read_index()
        for e in entries:
            if e["file"] == file:
                e["title"] = title
                e["summary"] = summary
                self._write_index(entries)
                return
        entries.append({"title": title, "file": file, "summary": summary})
        self._write_index(entries)

    # ── Public API ────────────────────────────────────────────────────────────

    def load_context(self, max_chars: int = 500) -> str:
        """Return a compact memory block for injection into the system prompt."""
        account_context = self.account.context(max_chars) if self._signed_in() else ""
        if not self._index.exists():
            return account_context
        snippets = []
        for entry in self._read_index():
            fpath = self.root / entry["file"]
            if not fpath.exists():
                continue
            text = fpath.read_text(encoding="utf-8").strip()
            lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
            snippets.extend(lines[:6])

        if not snippets:
            return account_context

        block = "\n".join(snippets)
        if len(block) > max_chars:
            block = block[:max_chars] + "…"
        return (account_context + f"## User Memory\n{block}\n")[:max_chars + 50]

    def append(self, slug: str, content: str, title: Optional[str] = None) -> None:
        """Queue a signed-in account fact, or append an anonymous local fact."""
        if _SENSITIVE_PATTERN.search(content):
            logger.debug("Memory: skipping sensitive content: %s…", content[:40])
            return False

        if self._signed_in():
            return self.account.add(content)

        slug = _slugify(slug)
        fpath = self.root / f"{slug}.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        if not fpath.exists():
            _title = title or slug.replace("_", " ").title()
            fpath.write_text(f"# {_title}\n\n", encoding="utf-8")
            self._upsert_index(fpath.name, _title, content[:80])

        with fpath.open("a", encoding="utf-8") as f:
            f.write(f"- [{ts}] {content}\n")

        entry = next((e for e in self._read_index() if e["file"] == fpath.name), None)
        if entry:
            entry["summary"] = content[:80]
            self._write_index(self._read_index())

        logger.debug("Memory: appended to %s: %s", fpath.name, content[:60])

    def upsert_project(self, name: str, facts: dict) -> None:
        """Create or refresh a project memory file."""
        slug = f"project_{_slugify(name)}"
        fpath = self.root / f"{slug}.md"
        langs = ", ".join(facts.get("languages", [])[:4]) or "unknown"
        ptype = facts.get("type", "unknown")
        root  = facts.get("root", "")
        ts    = facts.get("last_loaded", datetime.now().isoformat())[:10]
        syms  = ", ".join(facts.get("default_symbols", [])[:5])

        lines = [
            f"# Project: {name}",
            "",
            f"- **type**: {ptype}",
            f"- **languages**: {langs}",
            f"- **root**: {root}",
            f"- **last loaded**: {ts}",
        ]
        if syms:
            lines.append(f"- **default symbols**: {syms}")

        fpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary = f"{ptype} · {langs} · last {ts}"
        self._upsert_index(fpath.name, f"Project: {name}", summary)
        logger.debug("Memory: upserted project %s", name)

    def list_all(self) -> list[dict]:
        """Return all memory entries with their file content."""
        result = [{"title": "Account memory", "file": f["id"], "content": f["content"], "summary": f["content"][:80]} for f in self.account.facts()] if self._signed_in() else []
        for entry in self._read_index():
            fpath = self.root / entry["file"]
            content = fpath.read_text(encoding="utf-8").strip() if fpath.exists() else ""
            result.append({**entry, "content": content})
        return result

    def clear_all(self) -> int:
        """Delete all memory files and reset the index. Returns count deleted."""
        count = 0
        if self._signed_in():
            for fact in self.account.facts(include_disabled=True):
                self.account.forget(fact["id"])
                count += 1
        for fpath in self.root.glob("*.md"):
            if fpath.name != "MEMORY.md":
                fpath.unlink()
                count += 1
        self._index.write_text("# Aria Memory Index\n", encoding="utf-8")
        return count

    def fact_count(self) -> int:
        return len(self.list_all())


# ── Preference signal extractor (rule-based, zero LLM cost) ──────────────────

# Patterns that signal a user revealed an actionable fact mid-conversation
_MID_CONV_PATTERNS = [
    # Explicit remember requests
    (r"(记住|帮我记|remember that|please remember|note that)\s+(.{5,80})", "user_note"),
    # Risk / style preferences revealed mid-chat
    (r"(我(的)?风险|my risk|风险偏好|risk preference|risk tolerance)[^。.]{0,40}(低|高|中|保守|激进|低风险|高风险)", "risk_profile"),
    (r"(我(喜欢|偏好|倾向|通常用)|I (prefer|like|usually use|always use))\s*(.{4,60})", "preference"),
    # Symbols the user says they're tracking (single or multiple)
    (r"(我(在看|关注|持有|跟踪)|I('m)? (watching|tracking|holding))\s*(.{2,40})", "watchlist"),
    # Explicit remember / 记住 requests
    (r"(记住|帮我记|please remember|note that)\s*(.{4,80})", "user_note"),
    # Stop-loss / take-profit thresholds
    (r"(止损|止盈|stop[- ]loss|take[- ]profit)[^\d]{0,10}(\d+\.?\d*\s*%)", "trading_rule"),
    # Portfolio size hint (non-sensitive: just "大仓位" not actual amounts)
    (r"(大仓|小仓|主仓|重仓|满仓|空仓|half position|full position|light position)", "position_style"),
]

_SENSITIVE_PATTERN_STRICT = re.compile(
    r"(\d[\d,，.]+(?:万|亿|元|USD|CNY|HKD|K|M)?|\b\d{5,}\b|持仓金额|账户余额|本金)"
)


def extract_preference_signal(user_msg: str, assistant_response: str) -> Optional[str]:
    """Detect preference/fact signals worth persisting from a user message.

    Returns a single-line fact string or None. Conservative by design —
    most queries return None. Skips anything containing sensitive amounts.
    """
    # CJK characters are each 1 codepoint but convey more meaning per char,
    # so use a shorter minimum (8 chars) to avoid filtering valid short acks.
    if len(assistant_response) < 8:
        return None
    if _SENSITIVE_PATTERN.search(user_msg) or _SENSITIVE_PATTERN_STRICT.search(user_msg):
        return None

    for pattern, category in _PREF_PATTERNS + _MID_CONV_PATTERNS:
        m = re.search(pattern, user_msg, re.IGNORECASE)
        if m:
            snippet = user_msg[:120].strip().replace("\n", " ")
            return f"[{category}] {snippet}"

    return None


def auto_capture_from_turn(
    user_msg: str,
    assistant_response: str,
    memory: "MemoryManager",
) -> Optional[str]:
    """Called after each agent turn to auto-persist any detectable user preferences.

    Returns the captured fact string if something was saved, else None.
    This is intentionally lightweight: it runs synchronously after every turn
    and must not block or throw.
    """
    try:
        fact = extract_preference_signal(user_msg, assistant_response)
        if fact:
            memory.append("user_preferences", fact, title="用户偏好与设置")
            return fact
    except Exception:
        pass
    return None
