"""Startup banner and status-label rendering for Aria Code.

All functions accept only primitive values (strings, dicts, ints, bools)
so aria_cli.py can do data gathering while this module owns all display.

Public surface
--------------
    render_compact_banner(...)   — one-line banner (banner=compact)
    render_full_banner(...)      — compatibility wrapper for the dashboard
    render_startup_dashboard(...) — responsive wide/stacked/minimal dashboard
    render_try_hints(console)    — "try analyze AAPL · /help" line below panel
    privacy_status_label(...)
    control_status_label(...)
    ollama_status_label(...)
    bottom_toolbar_parts(...)
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from .startup_dashboard import StartupDashboardViewModel, select_dashboard_layout


def _t(key: str, lang: str) -> str:
    """Thin wrapper around i18n.t() — tolerates import failure."""
    try:
        from apps.cli.i18n import t as _translate
        return _translate(key, lang=lang)
    except Exception:
        _fallback = {
            "sharing_on": "sharing on", "local_only": "local-only",
            "network_on": "network on", "network_off": "network off",
            "privacy": "privacy", "ollama_online": "Ollama online",
            "ollama_offline": "Ollama offline", "cloud": "cloud",
            "local_first_agent": "local-first agent",
            "model": "model", "workspace": "workspace",
            "mode": "mode", "status": "status",
            "tools": "tools", "skills": "skills", "quant": "quant",
            "try": "try", "local": "local", "lite": "lite",
            "local_retention": "local retention",
        }
        return _fallback.get(key, key)


# ── Status label helpers ───────────────────────────────────────────────────────

def privacy_status_label(config: dict, rich: bool = False, lang: str = "") -> str:
    sharing = bool(config.get("data_sharing", False))
    upload  = bool(config.get("feedback_upload", False))
    _lang   = lang or config.get("ui_lang", "en")
    if sharing and upload:
        label = _t("sharing_on", _lang)
        return _mark("accent", label) if rich else label
    return _t("local_only", _lang)


def control_status_label(config: dict, rich: bool = False, lang: str = "") -> str:
    _lang      = lang or config.get("ui_lang", "en")
    permission = config.get("permission_mode", "workspace-write")
    if _lang.lower().startswith("zh"):
        permission = {
            "read-only": "只读",
            "workspace-write": "工作区可写",
            "full-access": "完全访问",
        }.get(permission, permission)
    net_key    = "network_on" if bool(config.get("network_enabled", True)) else "network_off"
    network    = _t(net_key, _lang)
    sharing = bool(config.get("data_sharing", False) and config.get("feedback_upload", False))
    retention = _t("sharing_on", _lang) if sharing else _t("local_retention", _lang)
    if rich:
        retention = _mark("muted", retention)
    return f"{permission} · {network} · {retention}"


def ollama_status_label(
    ollama_alive: bool,
    installed_models: set,
    config: dict,
    rich: bool = False,
    lang: str = "",
) -> str:
    _lang = lang or config.get("ui_lang", "en")
    count = len(installed_models)
    has_cloud = bool(
        config.get("auth_token")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
    )
    cloud_word = _t("cloud", _lang)
    cloud_rich = _mark("muted", f"· {cloud_word} ✓")
    cloud_tag = (
        f"  {cloud_rich}" if rich
        else f"  · {cloud_word} ✓"
    ) if has_cloud else ""
    model_word = _t("model_singular" if count == 1 else "model_plural", _lang)
    if ollama_alive:
        label = f"{_t('ollama_online', _lang)} · {count} {model_word}"
        return f"{label}{cloud_tag}"
    base = _t("ollama_offline", _lang)
    if has_cloud:
        return (f"{base}  {cloud_rich}" if rich else f"{base}  · {cloud_word} ✓")
    return base


def bottom_toolbar_parts(
    conversation: list,
    config: dict,
    actual_model: Optional[str],
    get_model_cfg_fn,
    known_context_tokens: int = 0,
) -> tuple:
    """Return (model_label, cwd, privacy, est_tokens, max_ctx).

    est_tokens 优先取 provider 上一轮真实上报的 prompt+completion tokens
    (known_context_tokens,含系统提示/skills 等字符估算看不见的开销),
    对话字符估算仅作首轮兜底——否则加载了 skills 的会话会误显 "ctx 0%"。
    """
    char_est = sum(len(m.get("content", "")) for m in conversation) // 3
    est_tokens = max(char_est, int(known_context_tokens or 0))
    mkey       = config.get("model", "qwen2.5:7b")
    max_ctx    = get_model_cfg_fn(mkey).get("num_ctx", 16384)
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    model_label = actual_model or mkey
    if len(model_label) > 28:
        model_label = "…" + model_label[-27:]
    if len(cwd) > 34:
        cwd = "…" + cwd[-33:]
    privacy = "sharing" if bool(config.get("data_sharing", False)) else "local-only"
    return model_label, cwd, privacy, est_tokens, max_ctx


# ── Banner renderers ───────────────────────────────────────────────────────────

_MASCOT = "[bold #C08050]◉[/bold #C08050]"


def _is_light_theme() -> bool:
    try:
        from .robot import detect_theme
        return detect_theme() == "light"
    except Exception:
        return False


def _style_tag(style: str, text: str) -> str:
    if style == "dim":
        return f"[dim]{text}[/dim]"
    return f"[{style}]{text}[/{style}]"


def _banner_style(role: str) -> str:
    if _is_light_theme():
        return {
            "primary": "bold #1F2328",
            "muted": "#57606A",
            "subtle": "#6E7781",
            "dim": "#8C959F",
            "accent": "#9A6700",
        }.get(role, "#57606A")
    return {
        "primary": "bold",
        "muted": "dim",
        "subtle": "dim",
        "dim": "dim",
        "accent": "#C08050",
    }.get(role, "dim")


def _mark(role: str, text: str) -> str:
    return _style_tag(_banner_style(role), text)


def _normalize_dim_markup(markup: str) -> str:
    """Make caller-supplied [dim] markup readable in light terminals."""
    if not _is_light_theme():
        return markup
    return markup.replace("[dim]", "[#57606A]").replace("[/dim]", "[/#57606A]")


def _console_width(console) -> int:
    try:
        return max(20, int(console.width))
    except Exception:
        return max(20, shutil.get_terminal_size((80, 24)).columns)


def _robot_text(columns: int = 28):
    from rich.text import Text

    from .robot import _art_rows, get_robot_row

    face = Text()
    row_count = len(_art_rows(columns))
    for idx in range(row_count):
        for style, value in get_robot_row(2, idx, columns):
            face.append(value, style=style)
        if idx < row_count - 1:
            face.append("\n")
    return face


def _identity_markup(view: StartupDashboardViewModel) -> str:
    from rich.markup import escape

    welcome_text = "欢迎使用 Aria!" if view.is_zh else "Welcome to Aria!"
    lines = [
        f"{_mark('primary', welcome_text)}",
        _normalize_dim_markup(view.runtime_label),
        _mark("muted", escape(view.cwd)),
    ]
    if view.workspace_state:
        lines.append(_mark("muted", escape(view.workspace_state)))
    if view.auto_healed_from:
        lines.append(
            f"{_mark('muted', '⚙ ' + _t('auto_matched', view.lang))}  "
            f"[yellow]{escape(view.auto_healed_from)}[/yellow]"
            f" {_mark('dim', '→')} [bold]{escape(view.current_id)}[/bold]"
        )
    if view.badge == "Fast" and view.best_lite_id and not view.best_lite_installed:
        lines.append(
            f"[yellow]{_t('tip', view.lang)}[/yellow]  "
            f"{_mark('muted', _t('lite', view.lang) + ' model · ')}"
            f"[bold]ollama pull {escape(view.best_lite_id)}[/bold]"
        )
    return "\n".join(lines)


def _runtime_markup(view: StartupDashboardViewModel, *, include_heading: bool = True) -> str:
    from rich.markup import escape

    lines = []
    if include_heading:
        lines.append(_mark("primary", view.runtime_title))
    lines.extend([
        _normalize_dim_markup(view.control_status),
        _normalize_dim_markup(view.health_status),
        _mark("muted", escape(view.capabilities)),
    ])
    return "\n".join(line for line in lines if line)


def _compact_runtime_markup(view: StartupDashboardViewModel) -> str:
    control_parts = view.control_status.split(" · ")
    control_line = " · ".join(control_parts[:2])
    retention = " · ".join(control_parts[2:])
    health_line = " · ".join(part for part in (retention, view.compact_health) if part)
    lines = [
        _mark("primary", view.runtime_title),
        _normalize_dim_markup(control_line),
        _normalize_dim_markup(health_line),
        _mark("muted", view.capabilities),
    ]
    return "\n".join(line for line in lines if line)


def _compact_guidance_markup(view: StartupDashboardViewModel) -> str:
    if not view.first_run and not view.update_notice:
        return "\n".join(f" {line}" for line in _compact_runtime_markup(view).splitlines())
    compact_health = view.compact_health.replace("Local: ", "").replace("本地: ", "")
    quick_counts = []
    if view.mcp_server_count:
        quick_counts.append(f"MCP {view.mcp_server_count}")
    quick_counts.append(f"{view.tool_count} {'个工具' if view.is_zh else 'tools'}")

    lines = [
        _mark("primary", view.getting_started_title),
        *(_mark("muted", line) for line in view.getting_started_lines),
        f"{_mark('primary', view.runtime_title)} {_mark('dim', '·')} "
        f"{_normalize_dim_markup(' · '.join(view.control_status.split(' · ')[:2]))}",
        _mark("muted", " · ".join([compact_health, *quick_counts])),
    ]
    if view.update_notice:
        lines.append(f"{_mark('primary', view.whats_new_title)} {_mark('dim', '·')} {view.update_notice}")
    return "\n".join(f" {line}" for line in lines)


def _guidance_markup(view: StartupDashboardViewModel) -> str:
    from rich.markup import escape

    sections = []
    if view.first_run:
        start_lines = "\n".join(_mark("muted", escape(line)) for line in view.getting_started_lines)
        sections.append(f"{_mark('primary', view.getting_started_title)}\n{start_lines}")
    else:
        sections.append(_runtime_markup(view))

    if view.update_notice:
        sections.append(f"{_mark('primary', view.whats_new_title)}\n{view.update_notice}")
    elif view.first_run:
        sections.append(_runtime_markup(view))
    return "\n\n".join(sections)


def render_startup_dashboard(
    view: StartupDashboardViewModel,
    *,
    console,
    has_rich: bool,
    rich_box,
    terminal_width: Optional[int] = None,
    terminal_height: Optional[int] = None,
) -> None:
    """Render startup state using a layout selected from terminal width."""
    if not has_rich:
        from .robot import ROBOT_ROW_COUNT, get_robot_row

        for row in range(ROBOT_ROW_COUNT):
            print("  " + "".join(fragment for _, fragment in get_robot_row(0, row)))
        print(f"\n  Aria Code v{view.version}")
        print(f"  {view.runtime_label}")
        print(f"  {view.cwd}")
        print(f"  {view.control_status}")
        print(f"  {view.health_status}")
        print(f"  {view.capabilities}")
        print("─" * 60)
        return

    from rich.console import Group
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    terminal_size = shutil.get_terminal_size((80, 24))
    width = terminal_width or _console_width(console)
    height = terminal_height or terminal_size.lines
    layout = select_dashboard_layout(width, height)
    mascot_columns = 28 if width >= 110 else 20

    if layout == "minimal":
        identity = Table.grid(padding=(0, 1))
        identity.add_column(no_wrap=True, vertical="top")
        identity.add_column(vertical="top")
        identity.add_row(
            _robot_text(mascot_columns),
            Text.from_markup(
                f"{_mark('primary', 'Aria Code')} {_mark('subtle', f'v{view.version}')}\n"
                f"{_normalize_dim_markup(view.runtime_label)}\n"
                f"{_mark('muted', escape(view.cwd))}\n"
                f"{_mark('muted', escape(view.capabilities))}\n"
                f"{_normalize_dim_markup(view.control_status)}\n"
                f"{_normalize_dim_markup(view.health_status)}"
            ),
        )
        console.print(identity)
        if view.first_run:
            console.print(_mark("muted", " · ".join(view.getting_started_lines)))
        if view.update_notice:
            console.print(view.update_notice)
        return

    identity = Table.grid(padding=(0, 2))
    identity.add_column(no_wrap=True, vertical="top")
    identity.add_column(vertical="top")
    identity.add_row(_robot_text(mascot_columns), Text.from_markup(_identity_markup(view)))

    border_style = _banner_style("dim")
    panel_box = getattr(rich_box, "ROUNDED", None)
    panel_title = f"[bold #C08050]Aria Code[/bold #C08050] [dim]v{view.version}[/dim]"

    if layout == "stacked":
        details = _compact_guidance_markup(view)
        if view.update_notice and not view.first_run:
            details = f"{details}\n{_mark('primary', view.whats_new_title)} {_mark('dim', '·')} {view.update_notice}"
        content = Group(identity, Text.from_markup("\n" + details))
        console.print(Panel(content, title=panel_title, title_align="left", box=panel_box, border_style=border_style, padding=(0, 1)))
        return

    body = Table.grid(expand=True, padding=0)
    body.add_column(ratio=5, vertical="top")
    body.add_column(width=1, vertical="top")
    body.add_column(ratio=6, vertical="top")
    from .robot import _art_rows
    divider = Text("\n".join("│" for _ in _art_rows(mascot_columns)), style=_banner_style("dim"))
    body.add_row(identity, divider, Text.from_markup(_compact_guidance_markup(view)))
    console.print(Panel(body, title=panel_title, title_align="left", box=panel_box, border_style=border_style, padding=(0, 1)))


def render_compact_banner(
    *,
    version: str,
    model_label: str,
    runtime: str,       # "cloud" | "local"
    cwd: str,
    control_status_rich: str,
    tool_count: int,
    update_notice: Optional[str] = None,
    console,
    has_rich: bool,
    lang: str = "en",
) -> None:
    if not has_rich:
        print(f"  Aria Code v{version}  {model_label}  {cwd}")
        return
    _rt_word = _t("cloud", lang) if runtime == "cloud" else _t("local", lang)
    _rt = _mark("muted", _rt_word)
    _tools_word = _t("tools", lang)
    _mascot = _style_tag(f"bold {_banner_style('accent')}", "◉")
    console.print(
        f"  {_mascot} {_mark('primary', 'Aria Code')} {_mark('subtle', f'v{version}')}"
        f"  {_mark('dim', '·')} {model_label} {_rt}"
        f"  {_mark('dim', '·')} {_mark('muted', cwd)}"
    )
    console.print(_mark("muted", f"  {_normalize_dim_markup(control_status_rich)} · {tool_count} {_tools_word} · /help"))
    if update_notice:
        console.print(f"  {update_notice}")


def render_full_banner(
    *,
    version: str,
    rt_label: str,              # Rich markup: "GPT-OSS 120B  [dim]cloud[/dim]"
    cwd: str,
    control_status_rich: str,
    ollama_status_rich: str,
    tool_count: int,
    skill_count: int,
    auto_healed_from: str = "",
    current_id: str = "",
    badge: str = "",
    installed_models: frozenset = frozenset(),
    best_lite_id: str = "",     # model ID to suggest when lite badge + not installed
    update_notice: Optional[str] = None,
    first_run: bool = False,
    terminal_width: Optional[int] = None,
    console,
    has_rich: bool,
    rich_box,
    lang: str = "en",
) -> None:
    view = StartupDashboardViewModel(
        version=version,
        runtime_label=rt_label,
        cwd=cwd,
        control_status=control_status_rich,
        health_status=ollama_status_rich,
        tool_count=tool_count,
        skill_count=skill_count,
        lang=lang,
        first_run=first_run,
        update_notice=update_notice,
        auto_healed_from=auto_healed_from,
        current_id=current_id,
        badge=badge,
        best_lite_id=best_lite_id,
        best_lite_installed=(not best_lite_id or best_lite_id in installed_models),
    )
    render_startup_dashboard(
        view,
        console=console,
        has_rich=has_rich,
        rich_box=rich_box,
        terminal_width=terminal_width,
    )


def render_try_hints(console, has_rich: bool, lang: str = "en") -> None:
    """Show natural-language examples that demonstrate LLM-native usage."""
    if not has_rich:
        return
    tcols = shutil.get_terminal_size((80, 24)).columns
    # Hints are natural language sentences — NOT slash commands.
    # The point: users should feel free to just type what they want.
    if lang == "zh":
        hints = [
            (_mark("accent", "宁德时代今天怎么样?"),          19),  # 9 CJK×2 + 1
            (_mark("accent", "帮我分析一下持仓风险"),          20),  # 10 CJK×2
            (_mark("accent", "生成今日A股晨报看板"),           19),  # 9 CJK×2 + "A"×1
            (_mark("subtle", "/help"),                         5),
        ]
    else:
        hints = [
            (_mark("accent", "How's NVDA this week?"),          18),
            (_mark("accent", "Analyze my portfolio risk"),       24),
            (_mark("accent", "Generate a morning brief HTML"),   30),
            (_mark("subtle", "/help"),                            5),
        ]
    sep   = f"  {_mark('dim', '·')}  "
    parts = []
    used  = 8
    for hint_rich, hint_len in hints:
        cost = hint_len + (5 if parts else 0)
        if used + cost <= tcols - 4:
            parts.append(hint_rich)
            used += cost
    _try_word = _t("try", lang)
    console.print(f"  {_mark('subtle', _try_word)}  " + sep.join(parts))
