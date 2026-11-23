"""CanvasCommandsMixin — `/canvas` stub (migrated to native Rust `rust/cli/src/engine/canvas.rs`)."""

from __future__ import annotations

from pathlib import Path
import json


class CanvasCommandsMixin:
    """Mixin providing the `/canvas` command notice after Rust migration."""

    async def cmd_canvas(self, args: str):
        """实时预览面板: `/canvas` 已迁移至原生 Rust CLI (`aria`) 内置预览服务。"""
        discovery = Path.home() / ".arthera" / "canvas.json"
        live_url = None
        if discovery.is_file():
            try:
                live_url = json.loads(discovery.read_text(encoding="utf-8")).get("url")
            except Exception:
                live_url = None
        if live_url:
            msg = f"原生 Rust Canvas 预览服务器正在运行: {live_url}（在原生 `aria` CLI 中执行 `/canvas stop` 可关闭）"
        else:
            msg = "本地 `/canvas` 预览服务已迁移至原生 Rust CLI（`rust/cli/src/engine/canvas.rs`），请在 `aria` 中直接运行 `/canvas [start|status|stop]`。"
        self.context.console.print(f"[cyan]{msg}[/cyan]") if self.context.has_rich else print(msg)
