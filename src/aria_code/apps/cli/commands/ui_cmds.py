"""UiCommandsMixin — vision, browser, screenshot, input, and context commands."""

from __future__ import annotations

from ._ui import Panel, print_error, rich_box

import base64
import io
import pathlib
from urllib.parse import urlsplit


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

def get_model_capability(*args, **kwargs):
    from aria_code.apps.cli._optional import get_model_capability as fn
    return fn(*args, **kwargs)
def _get__HAS_COMPUTER_USE():
    from aria_cli import _HAS_COMPUTER_USE as val
    return val
def _get__PROJECT_CONTEXT():
    from aria_cli import _PROJECT_CONTEXT as val
    return val
def _get__HAS_MODEL_CAP():
    from aria_code.apps.cli._optional import HAS_MODEL_CAP as val
    return val
def _get_rich_box():
    # Resolved locally now — see ._ui; no aria_cli round-trip.
    return rich_box
def get_model_cfg(*args, **kwargs):
    from ..model_catalog import get_model_cfg as fn
    return fn(*args, **kwargs)
def _get_Panel():
    # Resolved locally now — see ._ui; no aria_cli round-trip.
    return Panel

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class UiCommandsMixin:
    """Mixin: visual input and terminal UI commands."""

    @staticmethod
    def _short_url_label(url: str) -> str:
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit(url if url.startswith(("http://", "https://")) else f"https://{url}")
            host = parsed.netloc or parsed.path
            path = parsed.path.rstrip("/")
            if len(path) > 32:
                path = path[:29] + "..."
            return f"{host}{path}" if path and path != "/" else host
        except Exception:
            return url[:48]

    @staticmethod
    def _load_image_source(path_or_url: str) -> dict:
        """Load an image from a local path, URL, or clipboard."""
        raw = (path_or_url or "").strip().strip("\"'")
        if not raw:
            raise ValueError("Missing image source")

        mime_map = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
        }

        def _from_bytes(data: bytes, mime: str, label: str) -> dict:
            if not data:
                raise ValueError("Empty image data")
            return {
                "label": label,
                "mime": mime,
                "b64": base64.b64encode(data).decode(),
                "size_kb": max(1, len(data) // 1024),
            }

        if raw.lower() in {"clipboard", "clip", "paste"}:
            try:
                from PIL import ImageGrab
                img = ImageGrab.grabclipboard()
                if img is None:
                    raise ValueError("Clipboard does not contain an image")
                if isinstance(img, list):
                    for item in img:
                        p = pathlib.Path(str(item))
                        if p.is_file() and p.suffix.lstrip(".").lower() in mime_map:
                            return UiCommandsMixin._load_image_source(str(p))
                    raise ValueError("Clipboard does not contain a supported image file")
                if hasattr(img, "save"):
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    return _from_bytes(buf.getvalue(), "image/png", "clipboard")
                raise ValueError("Clipboard image format not supported")
            except Exception as exc:
                raise ValueError(str(exc)) from exc

        if raw.startswith(("http://", "https://", "www.")):
            try:
                import requests
                url = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
                resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                mime = content_type if content_type.startswith("image/") else "image/png"
                if mime == "application/octet-stream":
                    mime = "image/png"
                return _from_bytes(resp.content, mime, UiCommandsMixin._short_url_label(url))
            except Exception as exc:
                raise ValueError(f"Cannot download image: {exc}") from exc

        path = pathlib.Path(raw).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        suffix = path.suffix.lstrip(".").lower()
        mime = mime_map.get(suffix)
        if not mime:
            raise ValueError(f"Unsupported image type: .{suffix}")
        return _from_bytes(path.read_bytes(), mime, path.name)

    def cmd_vision(self, args: str):
        _curr_model = self.terminal.config.get("model", "")
        if _curr_model and _get__HAS_MODEL_CAP():
            _vcap = get_model_capability(_curr_model)
            if not _vcap.vision:
                _warn = (
                    f"[yellow]⚠[/yellow]  当前模型 [bold]{_curr_model}[/bold] 不支持图片输入。\n"
                    f"[dim]支持视觉的模型：llama3.2:11b · gemma3 · llava · qwen2-vl · moondream[/dim]"
                )
                if self.context.has_rich:
                    self.context.console.print(_get_Panel()(_warn, border_style="yellow", box=_get_rich_box().ROUNDED, padding=(0, 1)))
                else:
                    print(f"Warning: model {_curr_model} does not support vision input.")
                return

        path_str = args.strip().strip("\"'")
        if not path_str:
            msg = "Usage: /vision <image_path|image_url|clipboard>  (e.g. /vision ~/Pictures/chart.png)"
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return

        try:
            payload = self._load_image_source(path_str)
        except Exception as e:
            print_error(self.context, str(e), "vision")
            return

        self.terminal._pending_image = {
            "type": "image_url",
            "image_url": {"url": f"data:{payload['mime']};base64,{payload['b64']}"},
        }
        size_kb = payload["size_kb"]
        if self.context.has_rich:
            self.context.console.print(_get_Panel()(
                f"[green]✓[/green] [dim]{payload['label']}[/dim]  [dim]{size_kb} KB · {payload['mime']}[/dim]\n"
                f"[dim]Image queued — ask your question now[/dim]",
                border_style="dim",
                box=_get_rich_box().ROUNDED,
                padding=(0, 1),
            ))
        else:
            print(f"Image loaded: {payload['label']} ({size_kb} KB) — send your question now")

    async def cmd_browser(self, args: str):
        """Open a URL in a headless browser."""
        if not _get__HAS_COMPUTER_USE():
            print_error(self.context, 
                "computer_use_tools not available.",
                "Install: pip install playwright mss pyautogui pillow && playwright install chromium",
            )
            return
        from computer_use_tools import _tool_browser_navigate, _tool_browser_screenshot

        parts = args.strip().split(maxsplit=1)
        if not parts:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /browser <url>  or  /browser screenshot <url>[/dim]")
            return

        if parts[0].lower() == "screenshot" and len(parts) > 1:
            url = parts[1].strip()
            if self.context.has_rich:
                with self.context.console.status(f"[dim]Screenshotting {self._short_url_label(url)}…[/dim]", spinner="dots"):
                    result = _tool_browser_screenshot({"url": url})
            else:
                result = _tool_browser_screenshot({"url": url})
            if result.get("success"):
                d = result["data"]
                b64 = result.get("_vision_image")
                if b64:
                    self.terminal._pending_image = {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    }
                if self.context.has_rich:
                    self.context.console.print(_get_Panel()(
                        f"[green]✓[/green]  [bold]{d.get('title','')[:60]}[/bold]\n"
                        f"[dim]{self._short_url_label(url)}  ·  {d.get('size_kb', 0)} KB[/dim]\n"
                        f"[dim]Screenshot queued — ask your question now[/dim]",
                        border_style="dim", box=_get_rich_box().ROUNDED, padding=(0, 1),
                    ))
                else:
                    print(f"Screenshot ready ({d.get('size_kb', 0)} KB) — send your question")
            else:
                print_error(self.context, result.get("error", "Screenshot failed"), "browser screenshot")
        else:
            url = parts[0].strip()
            if self.context.has_rich:
                with self.context.console.status(f"[dim]Opening {self._short_url_label(url)}…[/dim]", spinner="dots"):
                    result = _tool_browser_navigate({"url": url})
            else:
                result = _tool_browser_navigate({"url": url})
            if result.get("success"):
                d = result["data"]
                title = d.get("title", "")
                text = d.get("text", "")[:2000]
                links = d.get("links", [])[:5]
                engine = d.get("engine", "")
                if self.context.has_rich:
                    link_str = "\n".join(f"  {l}" for l in links) if links else "  (none)"
                    self.context.console.print(_get_Panel()(
                        f"[bold]{title[:80]}[/bold]  [dim]({engine})[/dim]\n\n"
                        f"{text}\n\n[dim]Links:[/dim]\n{link_str}",
                        border_style="dim", box=_get_rich_box().ROUNDED, padding=(0, 1),
                        title=f"[dim]{self._short_url_label(url)}[/dim]", title_align="left",
                    ))
                else:
                    print(f"Title: {title}\n{text[:500]}")
            else:
                print_error(self.context, result.get("error", "Navigation failed"), "browser")

    async def cmd_screenshot(self, args: str):
        if not _get__HAS_COMPUTER_USE():
            print_error(self.context, 
                "computer_use_tools not available.",
                "Install: pip install mss pillow",
            )
            return
        from computer_use_tools import _tool_computer_screenshot

        monitor = int(args.strip()) if args.strip().isdigit() else 1
        if self.context.has_rich:
            with self.context.console.status("[dim]Capturing screen…[/dim]", spinner="dots"):
                result = _tool_computer_screenshot({"monitor": monitor})
        else:
            result = _tool_computer_screenshot({"monitor": monitor})

        if result.get("success"):
            d = result["data"]
            b64 = result.get("_vision_image")
            if b64:
                self.terminal._pending_image = {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                }
            if self.context.has_rich:
                self.context.console.print(_get_Panel()(
                    f"[green]✓[/green]  [dim]{d['width']}×{d['height']}  ·  {d['size_kb']} KB[/dim]\n"
                    f"[dim]Screenshot queued — ask your question now[/dim]",
                    border_style="dim", box=_get_rich_box().ROUNDED, padding=(0, 1),
                ))
            else:
                print(f"Screenshot {d['width']}×{d['height']} ({d['size_kb']} KB) — send your question")
        else:
            print_error(self.context, result.get("error", "Screenshot failed"), "screenshot")

    def cmd_input(self, args: str):
        raw = args.strip().lower()
        cfg = self.terminal.config
        valid_styles = {"panel", "box", "plain"}
        valid_themes = {"auto", "dark", "light"}

        def _save_and_show(message: str) -> None:
            self.context.save_config(cfg)
            if self.context.has_rich:
                self.context.console.print(f"[green]✓[/green] {message}")
                self.context.console.print(
                    f"  [dim]style[/dim] {cfg.get('input_style', 'panel')}  "
                    f"[dim]theme[/dim] {cfg.get('input_theme', 'auto')}"
                )
            else:
                print(message)
                print(f"  style {cfg.get('input_style', 'panel')}  theme {cfg.get('input_theme', 'auto')}")

        if not raw or raw in {"status", "show"}:
            style = cfg.get("input_style", "panel")
            theme = cfg.get("input_theme", "auto")
            if self.context.has_rich:
                self.context.console.print(_get_Panel()(
                    f"[bold]style[/bold]  {style}\n"
                    f"[bold]theme[/bold]  {theme}\n\n"
                    "[dim]Use[/dim] /input panel [dim]for the Codex-style input block[/dim]\n"
                    "[dim]Use[/dim] /input theme auto [dim]to follow the terminal/system theme[/dim]",
                    title="Input UI",
                    border_style="dim",
                    box=_get_rich_box().ROUNDED,
                    padding=(0, 1),
                ))
            else:
                print(f"input style: {style}")
                print(f"input theme: {theme}")
                print("Usage: /input panel|box|plain | /input theme auto|dark|light")
            return

        if raw == "reset":
            cfg["input_style"] = "panel"
            cfg["input_theme"] = "auto"
            _save_and_show("input UI reset to panel · auto")
            return

        parts = raw.split()
        if parts[0] == "theme":
            if len(parts) != 2 or parts[1] not in valid_themes:
                msg = "Usage: /input theme auto|dark|light"
                self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                return
            cfg["input_theme"] = parts[1]
            _save_and_show(f"input theme set to {parts[1]}")
            return

        if parts[0] in valid_themes and len(parts) == 1:
            cfg["input_theme"] = parts[0]
            _save_and_show(f"input theme set to {parts[0]}")
            return

        if parts[0] in valid_styles and len(parts) == 1:
            cfg["input_style"] = parts[0]
            _save_and_show(f"input style set to {parts[0]}")
            return

        msg = "Usage: /input panel|box|plain | /input theme auto|dark|light | /input reset"
        self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)

    def cmd_context(self, args: str):
        cfg = self.terminal.config
        conv = self.terminal.conversation
        conv_len = len(conv)
        model_id = cfg.get("model", "qwen2.5:7b")
        thinking = cfg.get("thinking_mode", "auto")
        has_auth = bool(cfg.get("auth_token"))
        local_mode = cfg.get("local_mode", False)
        auto_compact = bool(cfg.get("auto_compact_context", True))
        try:
            auto_compact_threshold = float(cfg.get("auto_compact_threshold", 0.78))
        except Exception:
            auto_compact_threshold = 0.78
        auto_compact_count = int(getattr(self.terminal, "_auto_compact_count", 0) or 0)

        total_chars = sum(len(m.get("content", "")) for m in conv)
        est_tokens = total_chars // 3
        max_ctx = get_model_cfg(model_id).get("num_ctx", 16384)
        ctx_pct = min(100, int(est_tokens / max_ctx * 100))
        ctx_color = "green" if ctx_pct < 60 else ("yellow" if ctx_pct < 85 else "red")

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]Current Context[/bold]")
            self.context.console.print()
            self.context.console.print(f"  [dim]{'Model':<20s}[/dim]{model_id}")
            self.context.console.print(f"  [dim]{'Provider':<20s}[/dim]{'[green]Local (Ollama)[/green]' if local_mode else 'AWS → Ollama fallback'}")
            self.context.console.print(f"  [dim]{'Thinking':<20s}[/dim]{thinking}")
            self.context.console.print(f"  [dim]{'Messages':<20s}[/dim]{conv_len}")
            self.context.console.print(f"  [dim]{'Est. tokens':<20s}[/dim][{ctx_color}]{est_tokens:,} / {max_ctx:,} ({ctx_pct}%)[/{ctx_color}]")
            self.context.console.print(f"  [dim]{'Authenticated':<20s}[/dim]{'yes' if has_auth else 'no'}")
            self.context.console.print(
                f"  [dim]{'Auto compact':<20s}[/dim]"
                f"{'on' if auto_compact else 'off'}"
                f" · threshold {int(auto_compact_threshold * 100)}%"
                f" · runs {auto_compact_count}"
            )
            self.context.console.print(f"  [dim]{'Session':<20s}[/dim]{self.terminal.session_id}")
            self.context.console.print(f"  [dim]{'Project context':<20s}[/dim]{'loaded' if _get__PROJECT_CONTEXT() else 'none'}")
            wl = cfg.get("watchlist", [])
            if wl:
                self.context.console.print(f"  [dim]{'Watchlist':<20s}[/dim]{', '.join(wl)}")
            if ctx_pct >= 80:
                self.context.console.print(f"\n  [yellow]⚠ Context {ctx_pct}% full — use /compact to free space[/yellow]")
            self.context.console.print()
        else:
            print(f"  Model: {model_id}  ({'local' if local_mode else 'aws'})")
            print(f"  Messages: {conv_len}  Tokens: ~{est_tokens:,}/{max_ctx:,} ({ctx_pct}%)")
            print(f"  Auto compact: {'on' if auto_compact else 'off'}  threshold={int(auto_compact_threshold * 100)}%  runs={auto_compact_count}")
            print(f"  Session: {self.terminal.session_id}")

