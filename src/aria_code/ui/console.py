"""Shared Rich console, availability flags, and ESC-key watcher.

Import this instead of repeating the try/except blocks everywhere:

    from ui.console import console, HAS_RICH, HAS_PT, _SYNTAX_THEME
    from ui.console import _EscWatcher, _esc_watcher
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from typing import Optional

# ── Rich ───────────────────────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.live import Live
    from rich.text import Text
    from rich.status import Status
    from rich.syntax import Syntax
    from rich.panel import Panel
    from rich.rule import Rule
    from rich import box as rich_box
    from rich.theme import Theme
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    Console = Markdown = Live = Text = Status = Syntax = Panel = Rule = rich_box = Theme = None

# ── prompt_toolkit ─────────────────────────────────────────────────────────────

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style as PTStyle
    HAS_PT = True
except ImportError:
    HAS_PT = False

# ── Console singleton ──────────────────────────────────────────────────────────

_SYNTAX_THEME: str = "monokai"


def _detect_terminal_theme() -> str:
    """Return a best-effort terminal theme: ``dark`` or ``light``."""
    explicit = os.getenv("ARIA_RICH_THEME", os.getenv("ARIA_INPUT_THEME", "")).strip().lower()
    if explicit in {"dark", "light"}:
        return explicit
    colorfgbg = os.getenv("COLORFGBG", "")
    if colorfgbg:
        try:
            return "dark" if int(colorfgbg.split(";")[-1]) < 8 else "light"
        except ValueError:
            pass
    # macOS appearance is not the Terminal profile appearance. Apple Terminal's
    # default Basic profile is light even when the OS itself is in dark mode;
    # using AppleInterfaceStyle here produced pale dark-theme headings on white.
    if os.getenv("TERM_PROGRAM", "") == "Apple_Terminal":
        return "light"
    # os.uname() is Unix-only — doesn't exist on Windows at all. sys.platform
    # is the portable equivalent and already covers the "Darwin" case here.
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=0.2, check=False,
            )
            return "dark" if (r.returncode == 0 and "dark" in r.stdout.lower()) else "light"
        except Exception:
            pass
    return "dark"


def _build_rich_theme(theme: str) -> "Theme":
    """Build a Markdown palette with enough contrast for the terminal theme."""
    if theme == "light":
        return Theme({
            "markdown.h1": "bold #24292f",
            "markdown.h2": "bold #24292f",
            "markdown.h3": "bold #8a5a00",
            "markdown.h4": "bold #8a5a00",
            "markdown.h5": "bold #8a5a00",
            "markdown.h6": "bold #8a5a00",
            "markdown.heading": "bold #24292f",
            "markdown.code": "bold #8a5a00",
            "markdown.code_inline": "bold #8a5a00",
            "markdown.link": "underline #0969da",
            "markdown.link_url": "underline #57606a",
            "markdown.item.bullet": "bold #8a5a00",
            "markdown.item.number": "bold #8a5a00",
            "markdown.table.header": "bold #8a5a00",
            "markdown.table.border": "#8c959f",
            "markdown.hr": "#8c959f",
            "markdown.strong": "bold #1f2328",
            "markdown.em": "italic #57606a",
            "markdown.block_quote": "#6e7781",
        })
    return Theme({
        # Keep Markdown close to the terminal palette: neutral text, copper
        # accents, no blue/purple headings, and no black inline-code blocks.
        "markdown.h1": "bold #e8e0d4",
        "markdown.h2": "bold #e8e0d4",
        "markdown.h3": "bold #d6ba8e",
        "markdown.h4": "bold #d6ba8e",
        "markdown.h5": "bold #d6ba8e",
        "markdown.h6": "bold #d6ba8e",
        "markdown.heading": "bold #e8e0d4",
        "markdown.code": "bold #c08050",
        "markdown.code_inline": "bold #c08050",
        "markdown.link": "underline #c08050",
        "markdown.link_url": "underline #8f867a",
        "markdown.item.bullet": "bold #d6ba8e",
        "markdown.item.number": "bold #d6ba8e",
        "markdown.table.header": "bold #d6ba8e",
        "markdown.table.border": "#6f675d",
        "markdown.hr": "#6f675d",
        "markdown.strong": "bold #e8e0d4",
        "markdown.em": "italic #c7beb2",
        "markdown.block_quote": "#a8a096",
    })


if HAS_RICH:
    ARIA_RICH_THEME_NAME = _detect_terminal_theme()
    ARIA_RICH_THEME = _build_rich_theme(ARIA_RICH_THEME_NAME)
    console = Console(highlight=False, theme=ARIA_RICH_THEME)

    def make_markdown(markup: str) -> Markdown:
        """Create Markdown with Aria's low-saturation terminal theme."""
        from ui.render.output import adapt_markdown_for_width

        markup = adapt_markdown_for_width(markup, getattr(console, "width", 80))
        # Rich's "bw" theme paints its own white background, so every code
        # block was a white slab in a dark terminal. The ANSI themes keep the
        # terminal's background and colour with its palette.
        code_theme = "ansi_light" if ARIA_RICH_THEME_NAME == "light" else "ansi_dark"
        return Markdown(
            markup,
            code_theme=code_theme,
            inline_code_theme=code_theme,
        )
else:
    class _FallbackConsole:
        def print(self, *a, **kw):
            print(*[str(x) for x in a])

        def input(self, prompt: str = "") -> str:
            return input(prompt)

        def status(self, msg: str):
            class _Ctx:
                def __enter__(self):
                    print(msg)
                    return self
                def __exit__(self, *a):
                    pass
                def update(self, msg: str):
                    print(msg)
            return _Ctx()

    console = _FallbackConsole()

    def make_markdown(markup: str) -> str:
        return markup

# ── termios / raw-mode availability ───────────────────────────────────────────

try:
    import termios
    import tty
    import select as _select
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False


# ── ESC-key watcher ───────────────────────────────────────────────────────────

class _EscWatcher:
    """Background thread that watches for ESC key press to cancel streaming."""

    def __init__(self):
        self._active = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._old_settings = None
        self._cancel_event: Optional[asyncio.Event] = None
        self._fd: Optional[int] = None

    def start(self, cancel_event: asyncio.Event):
        if not _HAS_TERMIOS or not sys.stdin.isatty():
            return
        self._cancel_event = cancel_event
        self._fd = sys.stdin.fileno()
        try:
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._old_settings = None
            return
        self._active = True
        self._paused = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        self._paused = True
        if self._old_settings and self._fd is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def resume(self):
        if not self._active or not _HAS_TERMIOS or self._fd is None:
            return
        if self._cancel_event and self._cancel_event.is_set():
            return
        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            return
        self._paused = False

    def stop(self):
        self._active = False
        self._paused = False
        if self._old_settings and self._fd is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass
            self._old_settings = None
        if self._thread:
            self._thread.join(timeout=0.3)
            self._thread = None

    def _run(self):
        fd = self._fd
        try:
            while self._active:
                if self._paused:
                    time.sleep(0.1)
                    continue
                try:
                    ready, _, _ = _select.select([fd], [], [], 0.15)
                except (ValueError, OSError):
                    break
                if not self._active or self._paused:
                    continue
                if ready:
                    try:
                        ch = os.read(fd, 1)
                    except OSError:
                        break
                    if ch == b'\x1b':
                        try:
                            r2, _, _ = _select.select([fd], [], [], 0.05)
                        except (ValueError, OSError):
                            break
                        if r2:
                            try:
                                os.read(fd, 16)
                            except OSError:
                                pass
                        else:
                            if self._cancel_event:
                                self._cancel_event.set()
                            self._active = False
        except Exception:
            pass


_esc_watcher = _EscWatcher()
