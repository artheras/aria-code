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
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .startup_dashboard import StartupDashboardViewModel


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


def _robot_text():
    from rich.text import Text

    from .robot import ROBOT_ROW_COUNT, get_robot_row

    face = Text()
    for idx in range(ROBOT_ROW_COUNT):
        for style, value in get_robot_row(2, idx):
            face.append(value, style=style)
        if idx < ROBOT_ROW_COUNT - 1:
            face.append("\n")
    return face


@lru_cache(maxsize=1)
def _artwork_pixels():
    """Rich half-blocks sampled from the reference, without importing Pillow."""
    import base64
    import zlib
    from rich.text import Text
    from .robot_pixels import HEIGHT, PIXELS, WIDTH

    rgb = zlib.decompress(base64.b85decode(PIXELS))
    face = Text()
    for y in range(0, HEIGHT, 2):
        for x in range(WIDTH):
            top = rgb[(y * WIDTH + x) * 3:(y * WIDTH + x) * 3 + 3].hex()
            bottom = rgb[((y + 1) * WIDTH + x) * 3:((y + 1) * WIDTH + x) * 3 + 3].hex()
            face.append("▀", style=f"#{top} on #{bottom}")
        if y + 2 < HEIGHT:
            face.append("\n")
    return face


def _mascot(console, width: int):
    """Return (renderable, columns, rows, optional native image sequence).

    Inline graphics go only to a real TTY. Rich's text parser strips OSC/APC
    escapes, so the PNG is written separately after reserving its cells.
    """
    from rich.text import Text
    from .robot_pixels import BOUNDS, HEIGHT, WIDTH
    mode = os.getenv("ARIA_ROBOT_RENDER", "auto").strip().lower()
    tty = bool(console.is_terminal and getattr(console.file, "isatty", lambda: False)())
    if mode == "off":
        return Text(), 0, 4, None
    if console.no_color or console.color_system is None:
        # Uncoloured half-blocks are a solid rectangle, not a robot.
        return _robot_text(), 9, 4, None
    if mode == "compact" or width < 60 or (not tty and mode not in ("pixels", "image")):
        return _robot_text(), 9, 4, None
    if mode != "pixels" and tty:
        from .image_render import best_method, render_image
        method = best_method()
        if method in ("iterm", "kitty"):
            rows = (HEIGHT + 1) // 2 + 1  # aspect-preserving image may occupy a fraction more
            asset = Path(__file__).parent / "assets" / "aria-robot.png"
            sequence = render_image(str(asset), WIDTH, method, crop=BOUNDS, cells_high=rows)
            if sequence and sequence.startswith(("\x1b]1337;", "\x1b_G")):
                return Text("\n".join([" " * WIDTH] * rows)), WIDTH, rows, sequence
    return _artwork_pixels().copy(), WIDTH, HEIGHT // 2, None


def _summary_lines(view: StartupDashboardViewModel) -> list[str]:
    """The four lines beside the robot — one per robot row, as Claude Code does."""
    from rich.markup import escape

    model = _normalize_dim_markup(view.runtime_label)
    if view.compact_health:
        model += f" {_mark('dim', '·')} {_mark('muted', escape(view.compact_health))}"
    place = _mark("muted", escape(view.cwd))
    if view.workspace_state:
        place += f"  {_mark('dim', escape(view.workspace_state))}"
    control = " · ".join(view.control_status.split(" · ")[:2])
    return [
        f"{_mark('primary', 'Aria Code')} {_mark('subtle', f'v{view.version}')}",
        model,
        place,
        _mark("muted", escape(view.capabilities)) + f" {_mark('dim', '·')} " + _normalize_dim_markup(control),
    ]


def _notes(view: StartupDashboardViewModel) -> list[str]:
    """What goes under the robot, only when there is something to say."""
    from rich.markup import escape

    notes = []
    if view.first_run:
        notes.append(_mark("muted", " · ".join(view.getting_started_lines)))
    if view.update_notice:
        notes.append(view.update_notice)
    if view.auto_healed_from:
        notes.append(
            f"{_mark('muted', '⚙ ' + _t('auto_matched', view.lang))}  "
            f"[yellow]{escape(view.auto_healed_from)}[/yellow]"
            f" {_mark('dim', '→')} [bold]{escape(view.current_id)}[/bold]"
        )
    if view.badge == "Fast" and view.best_lite_id and not view.best_lite_installed:
        notes.append(
            f"[yellow]{_t('tip', view.lang)}[/yellow]  "
            f"{_mark('muted', _t('lite', view.lang) + ' model · ')}"
            f"[bold]ollama pull {escape(view.best_lite_id)}[/bold]"
        )
    return notes


def render_startup_dashboard(
    view: StartupDashboardViewModel,
    *,
    console,
    has_rich: bool,
    rich_box=None,
    terminal_width: Optional[int] = None,
    terminal_height: Optional[int] = None,
) -> None:
    """The original mascot beside the model/workspace summary, then notes."""
    del rich_box, terminal_height
    from .robot import ROBOT_ROW_COUNT, get_robot_row

    if not has_rich:
        import re

        plain = [re.sub(r"\[/?[^\]]*\]", "", line) for line in _summary_lines(view)]
        for row in range(ROBOT_ROW_COUNT):
            glyphs = "".join(fragment for _, fragment in get_robot_row(0, row))
            print(f" {glyphs}  {plain[row] if row < len(plain) else ''}".rstrip())
        for note in _notes(view):
            print("  " + re.sub(r"\[/?[^\]]*\]", "", note))
        return

    from rich.table import Table
    from rich.text import Text

    width = terminal_width or _console_width(console)
    face, columns, rows, sequence = _mascot(console, width)
    block = Table.grid(padding=(0, 2))
    block.add_column(no_wrap=True, width=columns or None)
    # One line per row, cut rather than wrapped, so the block keeps its height.
    block.add_column(no_wrap=True, overflow="ellipsis", max_width=max(10, width - columns - 3))
    block.add_row(face, Text.from_markup("\n".join(_summary_lines(view)), overflow="ellipsis"))
    console.print(block)
    if sequence:
        # Reserve the rows first (also handles scrolling), draw at their top,
        # then restore the cursor before the notes and prompt are printed.
        console.file.write(f"\x1b7\x1b[{rows}A\r{sequence}\x1b8")
        console.file.flush()
    for note in _notes(view):
        console.print(Text.from_markup("  " + note, overflow="ellipsis"), no_wrap=True)


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
    # One example per thing Aria does — code, finance, logistics — so the first
    # screen says what the tool is for. Widths are measured, not hand-counted.
    from wcwidth import wcswidth

    examples = (["给 utils.py 补上测试", "宁德时代今天怎么样?", "/inventory skus.csv"] if lang == "zh"
                else ["Add tests for utils.py", "How's NVDA this week?", "/inventory skus.csv"])
    hints = [(_mark("accent", text), max(wcswidth(text), len(text))) for text in examples]
    hints.append((_mark("subtle", "/help"), 5))
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
