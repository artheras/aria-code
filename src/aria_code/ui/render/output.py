"""Generic tool-result and error rendering for Aria Code.

All functions accept console / has_rich as parameters so they stay
import-free from aria_cli.py and testable in isolation.

Public surface
--------------
    FINANCE_TOOL_NAMES          frozenset of tool names with dedicated renderers
    clean_tool_error_message(e) short user-facing string from any exception
    error_hint(msg, context)    actionable recovery suggestion
    print_error(msg, context, *, console, has_rich, rich_box)
    print_tool_result(...)
"""

from __future__ import annotations

import difflib
import pathlib
import re
import time


# ── Finance tool name registry ─────────────────────────────────────────────────

FINANCE_TOOL_NAMES: frozenset = frozenset({
    "get_market_data", "get_market_history", "get_crypto_data", "get_forex_data",
    "get_commodities_data", "get_futures_data", "calculate_factors",
    "backtest_strategy", "cloud_backtest", "run_portfolio_backtest", "get_risk_metrics",
    "optimize_positions", "get_sector_performance", "get_northbound_flow",
    "screen_ashare", "get_limit_up_pool", "get_market_indices",
    "analyze_news", "get_bonds_data", "get_ai_signal",
    "get_market_insights", "get_predictions",
    "broker_query", "broker_order",
})


# ── Narrow-terminal Markdown adaptation ──────────────────────────────────────

_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _markdown_table_cells(line: str) -> list[str]:
    """Split one simple GFM table row without leaking surrounding pipes."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_markdown_table_separator(line: str) -> bool:
    cells = _markdown_table_cells(line)
    return bool(cells) and all(_TABLE_SEPARATOR_CELL.fullmatch(cell) for cell in cells)


# Column headers that name no dimension — the value column of a key/value table.
_GENERIC_TABLE_HEADERS = {"value", "values", "meaning", "数值", "值", "含义", "说明"}


def _table_rows_as_lines(headers: list[str], rows: list[list[str]]) -> list[str]:
    """One line per table row: the first cell as its label, the rest after it.

    Each cell used to become its own bullet — "• 指标：最新价 / • 数值：USD 332.89"
    — so a ten-row snapshot table took about thirty lines in an 80-column
    terminal. Generic value columns ("Value", "数值") lose their header; a
    column that names a dimension keeps it ("观察：价格高于 MA20"). A value
    already contained in the one before it ("51.9，中性" then "中性") is dropped.
    """
    lines: list[str] = []
    for row in rows:
        cells = [(header, (row[pos] if pos < len(row) else "").strip())
                 for pos, header in enumerate(headers) if header]
        if not cells:
            continue
        label = cells[0][1] or "—"
        parts: list[str] = []
        previous = ""
        for header, value in cells[1:]:
            if not value or value in ("—", "-") or (previous and value in previous):
                continue
            generic = header.strip().lower() in _GENERIC_TABLE_HEADERS
            parts.append(value if generic else f"{header}{'：' if _has_cjk(header) else ': '}{value}")
            previous = value
        colon = "：" if _has_cjk(label) or any(_has_cjk(h) for h, _ in cells) else ": "
        lines.append(f"- **{label}**{colon}{' · '.join(parts) or '—'}")
    return lines


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def adapt_markdown_for_width(markup: str, width: int, *, table_breakpoint: int = 96) -> str:
    """Convert GFM tables to stacked records when the terminal is narrow.

    Rich correctly renders tables, but a three-column research table inside an
    80-column terminal has too little room for Chinese prose.  Cells are then
    truncated or wrapped until row relationships become unreadable.  The source
    Markdown remains unchanged in saved reports; only the terminal presentation
    is adapted.
    """
    if width >= table_breakpoint or "|" not in (markup or ""):
        return markup

    lines = (markup or "").splitlines()
    output: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            output.append(line)
            index += 1
            continue

        if (
            not in_fence
            and index + 1 < len(lines)
            and line.strip().startswith("|")
            and _is_markdown_table_separator(lines[index + 1])
        ):
            headers = _markdown_table_cells(line)
            cursor = index + 2
            rows: list[list[str]] = []
            while cursor < len(lines) and lines[cursor].strip().startswith("|"):
                row = _markdown_table_cells(lines[cursor])
                if row:
                    rows.append(row)
                cursor += 1

            if headers and rows:
                output.extend(_table_rows_as_lines(headers, rows))
                index = cursor
                continue

        output.append(line)
        index += 1
    return "\n".join(output)


# ── Tool display helpers ──────────────────────────────────────────────────────

def tool_display_kind(tool_name: str) -> str:
    """Return a user-facing service/tool kind without exposing local targets."""
    if tool_name.startswith("mcp__"):
        return "MCP"
    if tool_name in FINANCE_TOOL_NAMES:
        return "finance tool"
    if tool_name in {"web_search", "search_web"}:
        return "web search"
    if tool_name == "web_fetch":
        return "web fetch"
    if tool_name in {"read_file", "write_file", "edit_file", "list_files", "search_code"}:
        return "file tool"
    if tool_name == "run_command":
        return "shell tool"
    if tool_name.startswith("skill") or tool_name in {"TaskCreate", "TaskUpdate"}:
        return "skill"
    if tool_name.startswith("broker_") or tool_name in {"broker_query", "broker_order"}:
        return "broker tool"
    return "tool"


def tool_display_label(tool_name: str) -> str:
    """Short label for activity UI: tool name plus its service kind."""
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            return f"{parts[1]} · {parts[2].replace('_', ' ')} · MCP"
        return "MCP"
    return f"{tool_name} · {tool_display_kind(tool_name)}"


_RISK_STYLE = {0: "green", 1: "green", 2: "yellow", 3: "bold yellow", 4: "bold red"}


def format_risk_card(assessment, *, impact: str = "") -> list[tuple[str, str]]:
    """What an action touches, as (style, line) pairs, shown above its approval.

    The prompt used to ask about a command; this says what the command does:
    its level and score, whether it can be undone, the hosts, files and
    systems it reaches, and why it is rated that way.
    """
    level = assessment.level
    head = (f"Risk     L{level} {assessment.name} · {assessment.score}/100 · "
            f"{'reversible' if assessment.reversible else 'not reversible'}")
    lines = [(_RISK_STYLE.get(level, ""), head)]

    def _row(label: str, values) -> None:
        values = [str(v) for v in values if str(v)]
        if values:
            shown = ", ".join(values[:4]) + (f" +{len(values) - 4}" if len(values) > 4 else "")
            lines.append(("dim", f"{label:<9}{shown}"))

    _row("Network", assessment.hosts)
    _row("Files", assessment.files)
    _row("Impact", [impact] if impact else [])
    _row("Affects", assessment.systems)
    _row("Why", assessment.reasons[:2])
    return lines


def _file_impact(assessment) -> str:
    """What depends on the files an edit touches, from the cached project graph."""
    files = [str(f) for f in (getattr(assessment, "files", None) or ()) if str(f).startswith("/")]
    if not files:
        return ""
    try:
        from aria_code.runtime.project_graph import impact_line, impact_of_paths

        return impact_line(impact_of_paths(files))
    except Exception:
        return ""


def print_risk_card(console, assessment) -> None:
    from rich.markup import escape

    for style, line in format_risk_card(assessment, impact=_file_impact(assessment)):
        if console is None:
            print(f"  {line}")
        else:
            text = f"  {escape(line)}"
            console.print(f"[{style}]{text}[/{style}]" if style else text, highlight=False)


_DELIVERY_HEADINGS = {"Changed", "Verified", "Behaviour", "Review", "Risk", "Contract", "Checkpoint", "Next"}


def format_delivery_report(delivery: dict, *, run_id: str = "", root=None) -> list[tuple[str, str]]:
    """The runtime's delivery report as (style, line) pairs, for any console.

    The text comes from :meth:`DeliveryReport.render`; this only colours it:
    the status by outcome, headings dim, ✓ green, ✗ red, × and ⚠ yellow.
    """
    from aria_code.runtime.delivery import DeliveryReport

    report = DeliveryReport.from_dict(delivery)
    hint = f"/rewind code {run_id}" if run_id and report.checkpoints else ""
    styled: list[tuple[str, str]] = []
    for index, line in enumerate(report.render(rewind_hint=hint, root=root).splitlines()):
        stripped = line.strip()
        if index == 0:
            style = "bold green" if report.status == "done" else "bold yellow"
        elif stripped in _DELIVERY_HEADINGS:
            style = "dim"
        elif stripped.startswith("✓"):
            style = "green"
        elif stripped.startswith("✗"):
            style = "red"
        elif stripped.startswith(("×", "⚠")):
            style = "yellow"
        else:
            style = ""
        styled.append((style, line))
    return styled


def print_delivery_report(console, delivery: dict, *, run_id: str = "", root=None) -> None:
    """Print the report indented under the answer; plain print without Rich."""
    from rich.markup import escape

    lines = format_delivery_report(delivery, run_id=run_id, root=root)
    if console is None:
        print()
        for _, line in lines:
            print(f"  {line}" if line else "")
        return
    console.print()
    for style, line in lines:
        text = f"  {escape(line)}" if line else ""
        console.print(f"[{style}]{text}[/{style}]" if style and text else text, highlight=False)


def format_turn_footer(metadata, *, mode: str = "compact", copy_available: bool = False) -> str:
    """Return the post-response status line.

    ``full`` keeps the historical token-heavy line for debugging.  ``compact``
    is the default interactive UI: elapsed time, provider, tools, and /copy.
    """
    mode = (mode or "compact").strip().lower()
    if mode in {"off", "none", "false", "0"}:
        return ""
    parts = list(getattr(metadata, "parts", []) or [])
    if mode in {"full", "debug", "verbose"}:
        footer = " · ".join(parts)
        if copy_available:
            footer = f"{footer}  /copy" if footer else "/copy"
        return footer

    elapsed = parts[0] if parts else ""
    provider = str(getattr(metadata, "provider", "") or "").strip()
    tools = list(getattr(metadata, "tools", []) or [])
    out: list[str] = []
    if elapsed:
        out.append(elapsed)
    if provider and provider not in {"aws", "local"}:
        out.append(provider)
    if tools:
        shown = " ".join(str(t) for t in tools[:3])
        if len(tools) > 3:
            shown += f" +{len(tools) - 3}"
        out.append(shown)
    if copy_available:
        out.append("/copy")
    return " · ".join(out)


def file_uri(path: object) -> str:
    """A terminal-hyperlink target for a local file: file:///Users/…/chart.html.

    Saved files were linked as [link=/Users/…/chart.html]: OSC 8 hyperlinks
    take a URI, so terminals that support them had nothing to open. A value
    that already has a scheme (https://…) is returned unchanged.
    """
    text = str(path or "")
    if not text or "://" in text:
        return text
    try:
        return pathlib.Path(text).expanduser().resolve().as_uri()
    except (OSError, ValueError):
        return text


def display_path(path: object, *, fallback: str = "file") -> str:
    """Return a path-safe display value for user-facing UI."""
    if not path:
        return fallback
    try:
        name = pathlib.Path(str(path)).name
    except Exception:
        name = ""
    return name or fallback


# ── Error helpers ──────────────────────────────────────────────────────────────

def clean_tool_error_message(error: object) -> str:
    raw = str(error or "failed").strip()
    low = raw.lower()
    if not raw:
        return "操作失败"
    if "curl: (28)" in low or "timed out" in low or "timeout" in low:
        return "请求超时，数据源暂时不可用。请稍后重试或运行 /health 检查服务。"
    if "connection refused" in low:
        return "连接被拒绝，服务暂时不可用。请检查本地服务或网络。"
    if "connection aborted" in low or "remotedisconnected" in low:
        return "网络连接中断，数据源未完成响应。请稍后重试。"
    # Generic connection / proxy / DNS failures — collapse the verbose
    # urllib3 HTTPSConnectionPool(...) dump into a single readable line.
    if any(s in low for s in (
        "httpsconnectionpool", "httpconnectionpool", "max retries exceeded",
        "proxyerror", "failed to establish a new connection",
        "nameresolutionerror", "getaddrinfo failed", "newconnectionerror",
    )):
        import re as _re3
        _host = _re3.search(r"host=['\"]([^'\"]+)['\"]", raw)
        _hint = f"（数据源 {_host.group(1)}）" if _host else ""
        return f"数据源连接失败{_hint}，可能是网络或代理问题。请检查网络后重试。"
    if "rate" in low or "429" in low or "too many requests" in low:
        return "数据源请求频率受限，请稍后重试。"
    # Collapse verbose HTTP error strings: "web_fetch failed: 401 Client Error: Unauthorized for url: https://..."
    import re as _re
    _http = _re.match(r"web_fetch failed:\s*(\d{3})\s+\w[\w\s]+?:\s*([\w\s]+?)(?:\s+for url:.*)?$", raw, _re.I)
    if _http:
        code, phrase = _http.group(1), _http.group(2).strip()
        return f"HTTP {code} {phrase}"
    if "traceback" in low:
        return raw.splitlines()[-1][:160] if raw.splitlines() else "运行失败"
    return raw[:200]


def error_hint(error: str, context: str = "") -> str:
    err_lower = error.lower() if error else ""
    if "connection" in err_lower or "refused" in err_lower or "unreachable" in err_lower:
        return "Hint: Backend unreachable. Try /health or check your network."
    if "timeout" in err_lower or "timed out" in err_lower:
        return "Hint: Request timed out. Try again or check /health."
    # External web pages that block scraping (paywall / anti-bot) — NOT an Aria
    # login problem, so /login must not be suggested.
    _is_web = any(m in err_lower for m in (
        "http://", "https://", "www.", ".com", ".org", ".net",
        "web_fetch", "web fetch", "forbidden",
    ))
    def code(n: str) -> bool:
        # A status code as a number of its own: "1500 bars" is not a 500.
        return re.search(rf"(?<!\d){n}(?!\d)", err_lower) is not None

    if code("401") or "unauthorized" in err_lower:
        if any(h in err_lower for h in ("finnhub", "alphavantage", "polygon", "api/v1", "api/v2/finance")):
            return "Hint: API key required — /apikey set finnhub <KEY>  (free at finnhub.io)"
        if _is_web:
            return "Hint: This site blocks automated access (paywall/anti-bot). Try another source."
        return "Hint: Authentication required. Run /login to sign in."
    if code("403") or "forbidden" in err_lower:
        if _is_web:
            return "Hint: This site blocks automated access (paywall/anti-bot). Try another source."
        return "Hint: Access denied. Check your API key or subscription."
    # "rate" alone matched strategy, generate and separate: "Unknown strategy"
    # came with "Rate limited. Wait a moment and try again."
    if code("429") or re.search(r"rate[\s_-]?limit|too many requests", err_lower):
        return "Hint: Rate limited. Wait a moment and try again."
    if ("ollama" in err_lower or "ollama http" in err_lower) and (
        "not found" in err_lower or "404" in err_lower
    ):
        m = re.search(r"model ['\"]?([^'\"]+)['\"]? not found", err_lower)
        model_hint = m.group(1) if m else "the requested model"
        try:
            from local_llm_provider import list_ollama_models
            available = list_ollama_models("http://localhost:11434")
            if available:
                suggestion = available[0]
                return (
                    f"Hint: Ollama model '{model_hint}' not found.\n"
                    f"  Available: {', '.join(available[:4])}\n"
                    f"  Run: /config model {suggestion}"
                )
        except Exception:
            pass
        return (
            "Hint: Ollama model not found. Run `ollama list` to see available models.\n"
            "  Or pull one: ollama pull qwen2.5-coder:7b"
        )
    # "File not found" is a path error. Tell the model firmly NOT to keep
    # guessing filenames (it otherwise loops app.py→script.py→main.py…).
    if "file not found" in err_lower or "no such file" in err_lower:
        return ("Hint: This file does not exist. Do NOT guess other filenames — "
                "list the directory first, or this question may not need a file at all.")
    if code("404") and context == "tool":
        return "Hint: Tool not available. Check /tools for available tools."
    if "not found" in err_lower and context == "session":
        return "Hint: Session not found. Run /sessions to list available."
    if code("404") or ("not found" in err_lower and context not in ("tool", "")):
        return "Hint: Resource not found. Check the symbol or path."
    if "no data" in err_lower or "no result" in err_lower:
        return "Hint: No data returned. Verify the symbol spelling."
    if code("500") or "internal server" in err_lower:
        return "Hint: Server error. Try again in a moment or /health to check."
    if context == "login":
        return "Hint: Check email/password. Usage: /login email password"
    return ""


# ── Error panel & tree formatting ──────────────────────────────────────────────

def print_hanging(console, prefix: str, text: str, style: str = "") -> None:
    """One message after a fixed prefix, wrapped lines aligned under its first word.

    The text is plain, never markup: an error such as "/init [--force]" lost
    its brackets to Rich, and a long one wrapped back to column 0 under the
    "└" instead of beside it.
    """
    from rich.table import Table
    from rich.text import Text

    grid = Table.grid(padding=0)
    grid.add_column(no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row(Text(prefix, style=style), Text(text, style=style))
    console.print(grid)


# The contexts error_hint() understands; anything else given is a hint to show.
ERROR_CONTEXTS = frozenset({"", "tool", "login", "session", "screenshot", "vision", "browser",
                            "browser screenshot", "deep"})


def print_error(
    msg: str,
    context: str = "",
    *,
    console,
    has_rich: bool,
    rich_box=None,
    use_panel: bool = False,
) -> None:
    # The second argument is either where the error came from ("tool",
    # "login"), from which a hint is guessed, or the hint itself. Most callers
    # pass the hint: "请先用 /broker add 添加", "Usage: /compare SYMBOL …",
    # "Install: pip install mss pillow". It was taken for a context and never
    # shown, and a guess took its place.
    if str(context or "").strip().lower() in ERROR_CONTEXTS:
        hint = error_hint(msg, context)
    else:
        hint = str(context)
    if not use_panel:
        # Claude Code style: clean └ tree connector for inline error guidance
        if has_rich:
            print_hanging(console, "  └ ", msg, "red")
            if hint:
                for hline in hint.splitlines():
                    print_hanging(console, "    ", hline, "dim")
        else:
            print(f"  └ {msg}")
            if hint:
                for hline in hint.splitlines():
                    print(f"    {hline}")
        return

    if has_rich:
        from rich.panel import Panel
        box_style = getattr(rich_box, "ROUNDED", None) if rich_box else None
        body = f"[red]{msg}[/red]"
        if hint:
            body += f"\n[dim]{hint}[/dim]"
        console.print(Panel(body, border_style="red", box=box_style, padding=(0, 1)))
    else:
        print(msg)


# ── Tool result ────────────────────────────────────────────────────────────────

def print_tool_result(
    tool_name: str,
    result: dict,
    elapsed: float = 0,
    params: dict = None,
    *,
    console,
    has_rich: bool,
    rich_box,
    print_finance_fn,   # callable(tool_name, result) for finance tools
    bot_mode: bool = False,
) -> None:
    """Render a tool result summary — Codex-style ⎿ tree connector."""
    if bot_mode:
        return

    ts       = f"  [dim]{elapsed:.1f}s[/dim]" if elapsed >= 0.1 else ""
    ts_plain = f"  {elapsed:.1f}s" if elapsed >= 0.1 else ""
    params   = params or {}

    if tool_name in FINANCE_TOOL_NAMES:
        print_finance_fn(tool_name, result)
        if ts and has_rich:
            console.print(f"  [dim]⎿[/dim]{ts}")
        return

    if result.get("success"):
        data = result.get("data", {})

        if tool_name == "write_file":
            lines     = data.get("lines") or (params.get("content", "").count("\n") + 1 if params.get("content") else 0)
            size      = data.get("size_bytes") or len((params.get("content", "") or "").encode())
            size_str  = f"{size}B" if size < 1024 else f"{size // 1024}KB"
            if has_rich:
                console.print(f"  [dim]⎿[/dim]  [green]✓[/green]  [dim]file tool  {lines} lines  {size_str}[/dim]{ts}")
            else:
                print(f"  ⎿  ✓ file tool  {lines} lines  {size_str}{ts_plain}")

        elif tool_name == "edit_file":
            old = params.get("old_string", "")
            new = params.get("new_string", "")
            if old and new and has_rich:
                diff = list(difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    lineterm="",
                ))
                if diff:
                    _hdr = "  [dim]⎿[/dim]  [#C08050]file tool[/#C08050]"
                    console.print(f"{_hdr}{ts}")
                    from rich.syntax import Syntax
                    from rich.panel import Panel
                    diff_text = "".join(diff[2:])
                    console.print(Panel(
                        Syntax(diff_text, "diff", theme="monokai", padding=(0, 1)),
                        border_style="dim",
                        box=rich_box.SIMPLE,
                    ))
                else:
                    console.print(f"  [dim]⎿  no change[/dim]{ts}")
            elif has_rich:
                console.print(f"  [dim]⎿  edited[/dim]{ts}")
            else:
                print(f"  ⎿  edited{ts_plain}")

        elif tool_name == "run_command":
            stdout     = data.get("stdout", "").strip()
            returncode = data.get("returncode", data.get("exit_code", 0))
            if has_rich:
                from rich.panel import Panel
                rc_color = "green" if returncode == 0 else "red"
                rc_icon  = "✓" if returncode == 0 else "✗"
                console.print(f"  [dim]⎿[/dim]  [{rc_color}]{rc_icon} exit {returncode}[/{rc_color}]{ts}")
                if stdout:
                    out_lines = stdout.splitlines()
                    if len(out_lines) > 15:
                        head = out_lines[:5]
                        tail = out_lines[-10:]
                        truncated = "\n".join(head) + f"\n[dim]… ({len(out_lines) - 15} lines hidden) …[/dim]\n" + "\n".join(tail)
                        if data.get("full_output_path"):
                            truncated += f"\n[dim]full output saved: {data.get('full_output_path')}[/dim]"
                        console.print(Panel(
                            f"[dim]{truncated}[/dim]",
                            border_style="dim",
                            box=rich_box.SIMPLE,
                            padding=(0, 1),
                        ))
                    else:
                        truncated = "\n".join(out_lines)
                        if data.get("full_output_path"):
                            truncated += f"\n[dim]full output saved: {data.get('full_output_path')}[/dim]"
                        console.print(Panel(
                            f"[dim]{truncated}[/dim]",
                            border_style="dim",
                            box=rich_box.SIMPLE,
                            padding=(0, 1),
                        ))
            else:
                print(f"  ⎿  exit {returncode}{ts_plain}")
                for ol in stdout.splitlines()[:4]:
                    print(f"    {ol[:100]}")

        elif tool_name == "read_file":
            lines = data.get("lines", 0)
            if has_rich:
                console.print(f"  [dim]⎿  file tool  {lines} lines[/dim]{ts}")
            else:
                print(f"  ⎿  file tool  {lines} lines{ts_plain}")

        elif tool_name == "list_files":
            count = data.get("count", 0)
            if has_rich:
                color = "yellow" if count == 0 else "dim"
                msg   = "0 items — no matches" if count == 0 else f"{count} items"
                console.print(f"  [{color}]⎿  {msg}[/{color}]{ts}")
            else:
                print(f"  ⎿  {count} items{ts_plain}")

        elif tool_name == "search_code":
            matches = len(data.get("matches", []))
            if has_rich:
                console.print(f"  [dim]⎿  {matches} matches[/dim]{ts}")
            else:
                print(f"  ⎿  {matches} matches{ts_plain}")

        elif tool_name == "web_fetch":
            length = data.get("length", 0)
            trunc  = data.get("truncated", False)
            len_str = f"  {length:,} chars" if length else ""
            trunc_str = "  [dim]truncated[/dim]" if trunc else ""
            if has_rich:
                console.print(f"  [dim]⎿  web fetch{len_str}[/dim]{trunc_str}{ts}")
            else:
                print(f"  ⎿  web fetch{ts_plain}")

        elif tool_name in ("web_search", "search_web"):
            results = data.get("results", [])
            count   = len(results)
            if has_rich:
                console.print(f"  [dim]⎿  {count} results[/dim]{ts}")
            else:
                print(f"  ⎿  {count} results{ts_plain}")

        else:
            short = tool_display_kind(tool_name)
            if has_rich:
                console.print(f"  [dim]⎿  {short} done[/dim]{ts}")
            else:
                print(f"  ⎿  done{ts_plain}")

    else:
        error = clean_tool_error_message(result.get("error", "failed"))
        hint  = error_hint(str(error), context="tool")
        if has_rich:
            console.print(f"  [dim]⎿[/dim]  [red]✗ {error[:120]}[/red]")
            if hint:
                console.print(f"    [dim]{hint}[/dim]")
        else:
            print(f"  ⎿  ✗ {error[:80]}")


# ── Activity group (OpenClaw-style batch summary) ──────────────────────────────

def _one_line_tool_summary(
    tool_name: str,
    result: dict,
    elapsed: float,
    params: dict,
) -> tuple[str, str]:
    """Return (status_markup, detail_markup) for one tool in an activity table."""
    params = params or {}
    ts = f"[dim]  {elapsed:.1f}s[/dim]" if elapsed >= 0.1 else ""

    if not result.get("success"):
        error = clean_tool_error_message(result.get("error", "failed"))
        return "[red]✗[/red]", f"[red]{error[:80]}[/red]{ts}"

    data = result.get("data", {})
    kind = tool_display_kind(tool_name)

    if tool_name == "write_file":
        lines = data.get("lines") or (params.get("content", "").count("\n") + 1 if params.get("content") else 0)
        size  = data.get("size_bytes") or len((params.get("content", "") or "").encode())
        size_str = f"{size}B" if size < 1024 else f"{size // 1024}KB"
        return "[green]✓[/green]", f"[dim]{kind}  {lines} lines  {size_str}[/dim]{ts}"

    elif tool_name == "edit_file":
        return "[green]✓[/green]", f"[dim]edited  {kind}[/dim]{ts}"

    elif tool_name == "run_command":
        rc = data.get("returncode", data.get("exit_code", 0))
        icon  = "[green]✓[/green]" if rc == 0 else "[red]✗[/red]"
        color = "green" if rc == 0 else "red"
        suffix = " [dim]· full output saved[/dim]" if data.get("full_output_path") else ""
        return icon, f"[{color}]exit {rc}[/{color}]{suffix}{ts}"

    elif tool_name == "read_file":
        lines = data.get("lines", 0)
        return "[green]✓[/green]", f"[dim]{kind}  {lines} lines[/dim]{ts}"

    elif tool_name == "list_files":
        count = data.get("count", 0)
        color = "yellow" if count == 0 else "dim"
        msg   = "no matches" if count == 0 else f"{count} items"
        return "[green]✓[/green]", f"[{color}]{msg}[/{color}]{ts}"

    elif tool_name == "search_code":
        matches = len(data.get("matches", []))
        return "[green]✓[/green]", f"[dim]{matches} matches[/dim]{ts}"

    elif tool_name == "web_fetch":
        length = data.get("length", 0)
        len_s  = f"  {length:,}c" if length else ""
        return "[green]✓[/green]", f"[dim]{kind}{len_s}[/dim]{ts}"

    elif tool_name in ("web_search", "search_web"):
        count = len(data.get("results", []))
        return "[green]✓[/green]", f"[dim]{count} results[/dim]{ts}"

    else:
        return "[green]✓[/green]", f"[dim]{kind} done[/dim]{ts}"


def print_tool_activity_group(
    results: list,       # list of (tool_name, result, elapsed, params)
    *,
    console,
    has_rich: bool,
    rich_box,
    print_finance_fn,
    bot_mode: bool = False,
) -> None:
    """Render multiple tool results as a compact Activity block (OpenClaw style).

    For N >= 2 tools: prints a titled table.
    For N == 1: delegates to print_tool_result (single-line).
    """
    if bot_mode or not results:
        return

    if len(results) == 1:
        tool_name, result, elapsed, params = results[0]
        print_tool_result(tool_name, result, elapsed, params,
                          console=console, has_rich=has_rich, rich_box=rich_box,
                          print_finance_fn=print_finance_fn, bot_mode=bot_mode)
        return

    total_elapsed = sum(e for _, _, e, _ in results)
    n = len(results)

    # Finance tools: print with dedicated renderer, then add to activity table
    finance_rows = []
    for tool_name, result, elapsed, params in results:
        if tool_name in FINANCE_TOOL_NAMES:
            print_finance_fn(tool_name, result)
            finance_rows.append(tool_name)

    if has_rich:
        from rich.table import Table
        ts_total = f"  [dim]{total_elapsed:.1f}s[/dim]" if total_elapsed >= 0.1 else ""
        header = f"[dim]Activity · {n} tools[/dim]{ts_total}"
        console.print(f"\n  {header}")
        tbl = Table.grid(padding=(0, 2))
        tbl.add_column(no_wrap=True, min_width=14, style="dim")   # tool name
        tbl.add_column(no_wrap=True, min_width=2)                  # status icon
        tbl.add_column()                                            # detail

        from collections import OrderedDict
        _mcp_groups: "OrderedDict[str, list]" = OrderedDict()
        for tool_name, result, elapsed, params in results:
            if tool_name in finance_rows:
                icon = "[green]✓[/green]" if result.get("success") else "[red]✗[/red]"
                tbl.add_row(tool_name, icon, "")
            elif tool_name.startswith("mcp__"):
                # Defer MCP calls — collapse per server below
                _server = tool_name.split("__")[1] if len(tool_name.split("__")) >= 2 else "mcp"
                _mcp_groups.setdefault(_server, []).append((tool_name, result))
            else:
                icon, detail = _one_line_tool_summary(tool_name, result, elapsed, params)
                tbl.add_row(f"[dim]{tool_name}[/dim]", icon, detail)

        # Collapsed MCP rows: "server · tool" for one, "called N times" for many
        for _server, _calls in _mcp_groups.items():
            _all_ok = all(r.get("success") for _, r in _calls)
            _icon = "[green]✓[/green]" if _all_ok else "[red]✗[/red]"
            if len(_calls) == 1:
                _tn = _calls[0][0].split("__")
                _label = _tn[2].replace("_", " ") if len(_tn) >= 3 else _server
                tbl.add_row(f"[dim]{_server}[/dim]", _icon, f"[dim]{_label}  · MCP[/dim]")
            else:
                tbl.add_row(f"[dim]{_server}[/dim]", _icon,
                            f"[dim]called {len(_calls)} times · MCP[/dim]")

        from rich.padding import Padding
        console.print(Padding(tbl, (0, 0, 0, 4)))

        # For run_command with stdout, still print the output panel
        for tool_name, result, elapsed, params in results:
            if tool_name == "run_command" and result.get("success"):
                stdout = result.get("data", {}).get("stdout", "").strip()
                if stdout:
                    from rich.panel import Panel
                    out_lines = stdout.splitlines()
                    if len(out_lines) > 15:
                        head = out_lines[:5]
                        tail = out_lines[-10:]
                        truncated = "\n".join(head) + f"\n[dim]… ({len(out_lines) - 15} lines hidden) …[/dim]\n" + "\n".join(tail)
                        if result.get("data", {}).get("full_output_path"):
                            truncated += f"\n[dim]full output saved: {result.get('data', {}).get('full_output_path')}[/dim]"
                        console.print(Panel(f"[dim]{truncated}[/dim]",
                                            border_style="dim", box=rich_box.SIMPLE,
                                            padding=(0, 1)))
                    else:
                        truncated = "\n".join(out_lines)
                        if result.get("data", {}).get("full_output_path"):
                            truncated += f"\n[dim]full output saved: {result.get('data', {}).get('full_output_path')}[/dim]"
                        console.print(Panel(f"[dim]{truncated}[/dim]",
                                            border_style="dim", box=rich_box.SIMPLE,
                                            padding=(0, 1)))

            # For edit_file, still print diff
            elif tool_name == "edit_file" and result.get("success"):
                old = (params or {}).get("old_string", "")
                new = (params or {}).get("new_string", "")
                if old and new:
                    diff = list(difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        lineterm="",
                    ))
                    if diff:
                        from rich.syntax import Syntax
                        from rich.padding import Padding
                        diff_text = "".join(diff[2:])
                        console.print(Padding(Panel(
                            Syntax(diff_text, "diff", theme="monokai", padding=(0, 1)),
                            border_style="dim", box=rich_box.SIMPLE
                        ), (0, 0, 0, 4)))
    else:
        ts_total = f"  {total_elapsed:.1f}s" if total_elapsed >= 0.1 else ""
        print(f"\n  Activity · {n} tools{ts_total}")
        for tool_name, result, elapsed, params in results:
            icon, detail = _one_line_tool_summary(tool_name, result, elapsed, params)
            detail_plain = re.sub(r"\[/?[^\]]+\]", "", detail)
            icon_plain   = "✓" if result.get("success") else "✗"
            print(f"    {tool_name:<18}{icon_plain}  {detail_plain}")


# ── Fallback / model-switch toast ──────────────────────────────────────────────

def print_fallback_toast(
    from_provider: str,
    to_provider: str,
    reason: str = "",
    *,
    console,
    has_rich: bool,
) -> None:
    """Show a transient yellow notification when the active model/provider switches."""
    if not has_rich:
        print(f"\n  ⚡ 模型切换  {from_provider} → {to_provider}{('  ' + reason) if reason else ''}")
        return
    body = f"[bold #C08050]⚡[/bold #C08050]  [#C08050]{from_provider}[/#C08050] [dim]→[/dim] [#C08050]{to_provider}[/#C08050]"
    if reason:
        body += f"\n  [dim]{reason}[/dim]"
    console.print(f"\n  {body}")


# ── Context pressure warning ───────────────────────────────────────────────────

_CTX_WARNED: dict[str, float] = {}   # session_id → last warn time

def print_context_warning(
    est_tokens: int,
    max_tokens: int,
    *,
    console,
    has_rich: bool,
    session_id: str = "",
    cooldown: float = 120.0,         # only warn once every 2 min per session
) -> None:
    """Warn when context is >85% full; rate-limited to avoid spam."""
    if max_tokens <= 0:
        return
    ratio = est_tokens / max_tokens
    if ratio < 0.85:
        return
    now = time.monotonic()
    if now - _CTX_WARNED.get(session_id, 0) < cooldown:
        return
    _CTX_WARNED[session_id] = now

    def _k(n: int) -> str:
        return f"{n // 1000}K" if n >= 1000 else str(n)

    pct = int(ratio * 100)
    if has_rich:
        color  = "red" if ratio >= 0.95 else "#C08050"
        icon   = "●" if ratio >= 0.95 else "⚠"
        msg    = f"  [{color}]{icon} 上下文 {pct}% 已满  ({_k(est_tokens)}/{_k(max_tokens)} tokens)[/{color}]"
        msg   += "  [dim]→ /compact 压缩历史  /clear 重置[/dim]"
        console.print(msg)
    else:
        print(f"  ⚠ 上下文 {pct}% ({_k(est_tokens)}/{_k(max_tokens)} tokens) — /compact 或 /clear")


# ── Blocked / cancelled tool visual ───────────────────────────────────────────

def print_tool_blocked(
    tool_name: str,
    reason: str = "用户取消",
    *,
    console,
    has_rich: bool,
) -> None:
    """Show a styled 'Blocked' line when tool execution is denied or cancelled."""
    if has_rich:
        console.print(
            f"  [dim]⎿[/dim]  [#C08050]⊘  {tool_name}[/#C08050]  [dim]{reason}[/dim]"
        )
    else:
        print(f"  ⎿  ⊘ {tool_name}  {reason}")


# ── Robot thinking / response header ──────────────────────────────────────────

def print_thinking_header(*, console, has_rich: bool, agent_name: str = "Aria", color: str = "#C08050") -> None:
    """Print a subtle copper 'Aria ▸' header before each AI response stream.

    Gives the response a clear starting-point rather than appearing inline.
    Called once per turn, right before the first streaming token is printed.
    """
    if not has_rich:
        return
    console.print(f"[bold {color}]▣[/bold {color}]  [dim {color}]{agent_name}[/dim {color}]", end="  ")


def print_done_footer(elapsed: float, *, console, has_rich: bool) -> None:
    """Print a dim elapsed-time line after the response stream ends."""
    if not has_rich:
        return
    console.print(f"\n[dim]  ✓  {elapsed:.1f}s[/dim]")
