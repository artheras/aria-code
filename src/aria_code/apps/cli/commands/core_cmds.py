"""Core slash commands mixin.

本文件绝大多数名字（self.context.console / self.context.has_rich / MODELS …）是有意留作裸名的，由
aria_cli 的 _rebind_mixin_globals() 把方法 __globals__ 指向它的命名空间来解析。

但 pathlib 不能这么处理：它出现在 _create_scaffold() 的**参数注解**里，而注解
在 class 语句执行时就要求值——那时候 rebind 还没发生（rebind 作用于已定义好的
类）。所以这一个必须真的 import。

2026-08-19 发现：此前没有这行，导致本模块在 Python 3.10–3.13 上 import 即
NameError，而这正是 requires-python = "<3.14,>=3.10" 声明支持的全部范围。
开发机 .venv 是 3.14（PEP 649 注解惰性求值）所以一直没暴露，1352 个测试也
全绿。已发布的 4.3.0 同样中招：pip install 后运行 aria-code 直接崩溃。
"""

from aria_code.ui.render.output import file_uri as _file_uri
from aria_code.apps.cli.i18n import ui_text
from ._ui import print_error

import pathlib


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class CoreCommandsMixin:
    async def cmd_update(self, args: str):
        """Use the same updater from the REPL and the shell."""
        from aria_code.apps.cli.updater import main

        await asyncio.to_thread(main, shlex.split(args))

    def _cmd_rewind_unavailable(self, args: str):
        # cmd_rewind lands with the checkpoint store (runtime/checkpoints.py,
        # still a separate, uncommitted change) — degrade instead of crashing
        # SlashCommands construction when it isn't present yet.
        msg = "/rewind is not available in this build yet."
        self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
    def is_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        # Only match registered commands and skills, not file paths like /Users/...
        cmd = text.split(maxsplit=1)[0].lower()
        return cmd in self.commands or cmd in self.skill_map
    async def execute(self, text: str):
        from aria_code.apps.cli.helpers import _fuzzy_match
        reference_service = getattr(self.terminal, "_reference_service", None)
        if reference_service is not None and "@" in text:
            prepared = reference_service.prepare(text)
            if prepared.errors:
                self.terminal._print_reference_errors(prepared)
                return
            if prepared.references:
                self.terminal._print_reference_summary(prepared)
                text = prepared.expanded_text
        parts = text.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd_name in self.commands:
            handler, _ = self.commands[cmd_name]
            try:
                result = handler(args)
                if asyncio.iscoroutine(result):
                    await result
            except KeyboardInterrupt:
                if self.context.has_rich:
                    self.context.console.print("\n[dim]已取消[/dim]")
                else:
                    print("\n已取消")
            except Exception as _cmd_err:
                import traceback as _tb
                _tb_str = _tb.format_exc()
                if self.context.has_rich:
                    from rich.panel import Panel as _P
                    from rich import box as _rbox
                    self.context.console.print(_P(
                        f"[red]{type(_cmd_err).__name__}: {_cmd_err}[/red]\n"
                        f"[dim]{_tb_str.strip()[-800:]}[/dim]",
                        title=f"[red]{cmd_name} 崩溃[/red]",
                        border_style="red",
                        box=_rbox.ROUNDED,
                    ))
                else:
                    print(f"\n  ✗ {cmd_name} error: {_cmd_err}\n{_tb_str}")
        elif cmd_name in self.skill_map:
            await self._execute_skill(self.skill_map[cmd_name], args)
        else:
            # Fuzzy match: suggest closest command
            all_cmds = list(self.commands.keys()) + list(self.skill_map.keys())
            suggestions = _fuzzy_match(cmd_name, all_cmds, max_results=3)
            if self.context.has_rich:
                self.context.console.print(f"[red]Unknown command: {cmd_name}[/red]")
                if suggestions:
                    self.context.console.print(f"  [dim]Did you mean: {', '.join(suggestions)}?[/dim]")
            else:
                print(f"Unknown command: {cmd_name}")
                if suggestions:
                    print(f"  Did you mean: {', '.join(suggestions)}?")
    def cmd_help(self, args: str):
        # Contextual help: /help <command>
        from aria_code.apps.cli.skills_catalog import SKILLS
        target = args.strip().lower()
        if target and target != "all":
            from aria_code.apps.cli.commands.catalog import HELP_TOPIC_MAP
            if target in HELP_TOPIC_MAP:
                self._print_help_topic(HELP_TOPIC_MAP[target])
                return
            cmd_key = target if target.startswith("/") else f"/{target}"
            if cmd_key in self.commands:
                _, desc = self.commands[cmd_key]
                if self.context.has_rich:
                    self.context.console.print()
                    self.context.console.print(f"  [bold #C08050]{cmd_key}[/bold #C08050]  [dim]{desc}[/dim]")
                    h = self._COMMAND_HELP.get(cmd_key)
                    if h:
                        self.context.console.print(f"  {h[0]}")
                        self.context.console.print()
                        self.context.console.print("  [dim]Examples:[/dim]")
                        for ex in h[1]:
                            self.context.console.print(f"    [bold]{ex}[/bold]")
                    self.context.console.print()
                else:
                    print(f"\n  {cmd_key}  {desc}")
                return
            # Check skills
            for s in SKILLS:
                if s["command"] == cmd_key:
                    if self.context.has_rich:
                        self.context.console.print()
                        self.context.console.print(f"  [bold #C08050]{s['command']}[/bold #C08050]  [dim]{s['description']}[/dim]")
                        self.context.console.print(f"  [dim]Category:[/dim] {s['category']}")
                        self.context.console.print()
                    else:
                        print(f"\n  {s['command']}  {s['description']}")
                    return
            self.context.console.print(f"[dim]No help for: {target}. Try /help[/dim]" if self.context.has_rich else f"No help for: {target}")
            return

        if not target:
            self._print_help_overview()
            return

        # /help all: every command, grouped
        if self.context.has_rich:
            self.context.console.print()

            # ── Natural language first ──────────────────────────────────────
            self.context.console.print("[bold]Just type what you want[/bold]  [dim]— no command needed[/dim]")
            self.context.console.print()
            nl_examples = [
                ("宁德时代今天怎么样?",        "How's CATL today?"),
                ("帮我画AAPL的K线图",          "Draw a candlestick chart for AAPL"),
                ("分析我的组合风险",            "Analyze my portfolio risk"),
                ("生成今日A股晨报",             "Generate a morning brief for A-shares"),
                ("NVDA和AMD哪个更值得买?",      "NVDA vs AMD — which is a better buy?"),
                ("给我写一个动量回测策略",       "Write a momentum backtest strategy"),
            ]
            for zh, en in nl_examples:
                self.context.console.print(f"  [#C08050]{zh}[/#C08050]  [dim]{en}[/dim]")
            self.context.console.print()

            # ── Context references ─────────────────────────────────────────
            _ref_is_zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
            _ref_title = "上下文引用" if _ref_is_zh else "Context references"
            _ref_subtitle = (
                "@ 只附加只读上下文，不执行操作"
                if _ref_is_zh else
                "@ attaches read-only context; it never runs an action"
            )
            self.context.console.print(f"[bold]{_ref_title}[/bold]  [dim]({_ref_subtitle})[/dim]")
            self.context.console.print()
            for example, description_en, description_zh in (
                ("@file:src/model.py", "file pointer → read_file", "文件引用 → read_file"),
                ("@folder:apps/cli", "folder pointer → list_files", "目录引用 → list_files"),
                ("@asset:AAPL", "market asset", "市场资产"),
                ("@portfolio:core", "saved portfolio", "已保存投资组合"),
                ("@strategy:momentum-v2", "saved strategy", "已保存策略"),
                ("@dataset:prices", "local dataset", "本地数据集"),
                ("@run:walk-forward-42", "research run", "研究运行"),
                ("@report:daily", "generated report", "生成报告"),
            ):
                description = description_zh if _ref_is_zh else description_en
                self.context.console.print(f"  [#C08050]{example:27s}[/#C08050] [dim]{description}[/dim]")
            _combine = "组合使用" if _ref_is_zh else "Combine them"
            self.context.console.print(f"  [dim]{_combine}: /risk @portfolio:core · /review @folder:apps/cli[/dim]")
            self.context.console.print()

            # ── Slash commands — grouped ────────────────────────────────────
            self.context.console.print("[bold]Slash commands[/bold]  [dim](for direct actions and mode switches)[/dim]")
            self.context.console.print()
            groups = [
                ("Session", ["/help","/clear","/compact","/cost","/status","/health",
                             "/regen","/undo","/rewind","/copy","/recap","/btw",
                             "/save","/load","/sessions","/export","/export-pdf"]),
                ("Config",  ["/model","/thinking","/config","/privacy","/local","/update",
                             "/setup","/apikey","/doctor","/mcp"]),
                ("Data",    ["/alert","/journal","/watch","/note","/todo","/memory",
                             "/artifacts","/strategy","/accuracy"]),
                ("Broker",  ["/broker","/paper","/trade","/account","/positions","/orders"]),
                ("Code",    ["/project","/init","/review","/code","/plan","/run",
                             "/read","/write","/edit","/ls","/search","/verify",
                             "/scaffold","/apply","/changes"]),
                ("Quant",   ["/backtest","/wf","/compare","/auto-strategy","/execution"]),
                ("UI",      ["/ui","/vision","/file"]),
                ("Info",    ["/skills","/services","/tools","/providers","/ariarc",
                             "/packages","/datasource"]),
            ]
            for group_name, cmd_names in groups:
                visible = [n for n in cmd_names if n in self.commands]
                if not visible:
                    continue
                self.context.console.print(f"  [dim]{group_name}[/dim]")
                for name in visible:
                    _, desc = self.commands[name]
                    self.context.console.print(f"    [bold #C08050]{name:18s}[/bold #C08050][dim]{desc}[/dim]")
                self.context.console.print()

            # ── Skills ─────────────────────────────────────────────────────
            self.context.console.print("[bold]Skills[/bold]  [dim](type the command or just describe what you want)[/dim]")
            self.context.console.print()
            categories: dict = {}
            for s in SKILLS:
                categories.setdefault(s["category"], []).append(s)
            for cat, skills in categories.items():
                self.context.console.print(f"  [dim]{cat}[/dim]")
                for s in skills:
                    self.context.console.print(f"    [bold #C08050]{s['command']:20s}[/bold #C08050][dim]{s['description']}[/dim]")
            self.context.console.print()

            # ── Keys ───────────────────────────────────────────────────────
            self.context.console.print("[dim]ESC cancel  ·  Ctrl+D exit  ·  ↑↓ history  ·  Tab autocomplete  ·  \"\"\" multiline[/dim]")
            self.context.console.print()

        else:
            print("\nJust type what you want — or use a slash command:\n")
            for name, (_, desc) in self.commands.items():
                if name in self._visible_cmds:
                    print(f"  {name:18s} {desc}")
            print("\nSkills:")
            for s in SKILLS:
                print(f"  {s['command']:20s} {s['description']}")
    def _print_help_overview(self) -> None:
        """One screen: what to type, the core commands, a way into each capability.

        /help printed about 90 lines — every group, every skill — so the
        commands that matter scrolled out of view. The rest is a topic away.
        """
        from aria_code.apps.cli.commands.catalog import (
            CORE_SLASH_COMMANDS, ENTRY_SLASH_COMMANDS, HELP_TOPICS, describe,
        )
        zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
        T = (lambda a, b: a) if zh else (lambda a, b: b)
        lang = "zh" if zh else "en"
        core = [(n, describe(n, self.commands[n][1], lang)) for n in CORE_SLASH_COMMANDS if n in self.commands]
        entry = [(n, describe(n, self.commands[n][1], lang)) for n in ENTRY_SLASH_COMMANDS if n in self.commands]
        examples = (
            T("给 utils.py 补上测试并运行", "Add tests for utils.py and run them"),
            T("NVDA 这周走势怎么样？", "How's NVDA this week?"),
            T("看看 skus.csv 哪些 SKU 该补货", "Which SKUs in skus.csv need reordering?"),
        )
        topics = " · ".join(t.key for t in HELP_TOPICS)
        if not self.context.has_rich:
            print("\n" + T("直接描述要做的事，Aria 会选择工具：", "Describe what you want; Aria picks the tools:"))
            for example in examples:
                print(f"  {example}")
            print("\n" + T("常用命令", "Commands"))
            for name, desc in core + entry:
                print(f"  {name:13s} {desc}")
            print("\n" + T(f"更多：/help {topics} · all · <命令>", f"More: /help {topics} · all · <command>"))
            return

        from rich.cells import cell_len, set_cell_size
        from rich.text import Text

        console = self.context.console
        console.print()
        console.print(f"[bold]{T('直接描述要做的事', 'Describe what you want')}[/bold]  "
                      f"[dim]{T('Aria 会选择工具', 'Aria picks the tools')}[/dim]")
        for example in examples:
            console.print(f"  [#C08050]{example}[/#C08050]")
        console.print()

        name_width = max(len(name) for name, _ in core + entry)
        # indent 2, name, gap 2, description, gap 3, name, gap 2, description
        desc_width = max(12, (console.width - 2 - 2 * name_width - 2 - 3 - 2) // 2)

        # Widths in terminal cells, not characters: a Chinese character takes
        # two. Counted as one, the Chinese /help padded each left description
        # to twice its room and the right column wrapped back to column 0.
        def cell(text: str) -> str:
            return text if cell_len(text) <= desc_width else set_cell_size(text, desc_width - 1) + "…"

        def pad(text: str, width: int) -> str:
            return text + " " * max(0, width - cell_len(text))

        two_columns = console.width >= 96
        if not two_columns:
            desc_width = max(12, console.width - 2 - name_width - 2)

        def grid(rows):
            if two_columns:
                half = (len(rows) + 1) // 2
                pairs = zip(rows[:half], rows[half:] + [("", "")])
            else:
                pairs = ((row, ("", "")) for row in rows)
            lines = Text()
            for left, right in pairs:
                lines.append("  " + left[0].ljust(name_width + 2), style="bold #C08050")
                lines.append(pad(cell(left[1]), desc_width + 3) if right[0] else cell(left[1]), style="dim")
                if right[0]:
                    lines.append(right[0].ljust(name_width + 2), style="bold #C08050")
                    lines.append(cell(right[1]), style="dim")
                lines.append("\n")
            lines.rstrip()
            return lines

        console.print(f"[bold]{T('命令', 'Commands')}[/bold]")
        console.print(grid(core))
        console.print()
        console.print(f"[bold]{T('开始一项任务', 'Start a task')}[/bold]  "
                      f"[dim]{T('代码 · 金融分析 · 物流', 'code · financial analysis · logistics')}[/dim]")
        console.print(grid(entry))
        console.print()
        console.print(f"[dim]{T('更多', 'More')}: /help {topics} · all · <{T('命令', 'command')}>[/dim]")
        console.print(f"[dim]@ {T('附加上下文', 'attaches context')} · ! {T('运行 shell 命令', 'runs a shell command')}"
                      f" · Esc {T('中断', 'interrupts')}[/dim]")
        console.print()

    def _print_help_topic(self, topic) -> None:
        from aria_code.apps.cli.commands.catalog import short_description

        zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
        rows = [(n, self.commands[n][1]) for n in topic.commands if n in self.commands]
        title = topic.title_zh if zh else topic.title_en
        if not self.context.has_rich:
            print(f"\n{title}")
            for name, desc in rows:
                print(f"  {name:16s} {desc}")
            return
        from rich.markup import escape

        console = self.context.console
        console.print()
        console.print(f"[bold]{title}[/bold]")
        for name, desc in rows:
            console.print(f"  [bold #C08050]{name:16s}[/bold #C08050][dim]{escape(desc)}[/dim]", highlight=False)
        console.print()

    async def cmd_artifacts(self, args: str):
        from aria_code.ui.render.output import display_path as _display_path
        from aria_code.apps.cli.helpers import _copy_text_to_clipboard, _open_path_or_url, _reveal_path_in_finder
        tokens = [part for part in args.split() if part]
        mode = "list"
        limit = 20
        keep = 20
        dry_run = False
        selector = "latest"
        if tokens:
            head = tokens[0].lower()
            if head in {"prune", "cleanup", "gc", "purge"}:
                mode = "prune"
                for token in tokens[1:]:
                    if token == "--dry-run":
                        dry_run = True
                        continue
                    try:
                        keep = int(token)
                    except Exception:
                        continue
            elif head in {"stats", "summary"}:
                mode = "stats"
            elif head in {"open", "reveal", "show", "path", "copy-path", "copy"}:
                mode = "copy-path" if head == "copy" else head
                selector = tokens[1] if len(tokens) > 1 else "latest"
            else:
                try:
                    limit = int(head)
                except Exception:
                    limit = 20
        from artifacts import artifact_summary_all, prune_artifacts_all, recent_artifacts_all

        if mode in {"open", "reveal", "show", "path", "copy-path"}:
            items = recent_artifacts_all(limit=100)
            selected = None
            if items:
                if selector.lower() in {"latest", "last", "newest", "最近", "最新"}:
                    selected = items[0]
                elif selector.isdigit() and 1 <= int(selector) <= len(items):
                    selected = items[int(selector) - 1]
                else:
                    needle = selector.lower()
                    selected = next(
                        (
                            item for item in items
                            if needle in pathlib.Path(str(item.get("path") or "")).name.lower()
                        ),
                        None,
                    )
            target = str((selected or {}).get("path") or "").strip()
            if not target:
                msg = f"未找到产物 {selector!r}。先运行 /artifacts 查看可用编号。"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return

            ok = True
            error = ""
            if mode == "open":
                ok, error = _open_path_or_url(target)
                action_text = "已打开"
            elif mode in {"reveal", "show"}:
                ok, error = _reveal_path_in_finder(target)
                action_text = "已在访达中定位"
            elif mode == "copy-path":
                ok, error = _copy_text_to_clipboard(target)
                action_text = "路径已复制"
            else:
                action_text = "文件路径"
            if ok:
                msg = f"{action_text}：{target}"
                self.terminal._pending_market_artifact = {
                    "kind": str((selected or {}).get("kind") or "artifact"),
                    "path": target,
                }
                if self.context.has_rich:
                    self.context.console.print(f"[green]✓[/green] {msg}")
                else:
                    print(msg)
            else:
                msg = f"操作失败：{error or target}"
                if self.context.has_rich:
                    self.context.console.print(f"[red]✗[/red] {msg}")
                else:
                    print(msg)
            return

        if mode == "stats":
            summary = artifact_summary_all()
            roots = [pathlib.Path(str(r)) for r in summary.get("roots") or []]
            total = int(summary.get("total") or 0)
            total_size = int(summary.get("total_size_bytes") or 0)
            by_kind = summary.get("by_kind") or {}
            if self.context.has_rich:
                self.context.console.print("[bold]Artifact inventory[/bold]")
                for r in roots:
                    self.context.console.print(f"  [dim]root[/dim]: {_display_path(r, fallback=r.name)}")
                self.context.console.print(f"  total: [bold]{total}[/bold]  size: [bold]{total_size:,} bytes[/bold]")
                if by_kind:
                    for kind, count in by_kind.items():
                        self.context.console.print(f"  [dim]{kind}[/dim]: {count}")
            else:
                print("Artifact inventory")
                print(f"  total: {total}")
                print(f"  size: {total_size} bytes")
                for kind, count in by_kind.items():
                    print(f"  {kind}: {count}")
            return

        if mode == "prune":
            result = prune_artifacts_all(keep=keep, dry_run=dry_run)
            removed = int(result.get("removed") or 0)
            scanned = int(result.get("scanned") or 0)
            action = "Would remove" if dry_run else "Removed"
            if self.context.has_rich:
                self.context.console.print("[bold]Artifact prune[/bold]")
                for r in result.get("roots") or []:
                    self.context.console.print(f"  [dim]root[/dim]: {_display_path(r, fallback=pathlib.Path(str(r)).name)}")
                self.context.console.print(f"  keep: [bold]{result.get('keep', keep)}[/bold]  scanned: [bold]{scanned}[/bold]  removed: [bold]{removed}[/bold]")
                if removed:
                    for entry in result.get("deleted") or []:
                        name = pathlib.Path(str(entry.get("path") or entry.get("metadata_path") or "")).name
                        root_name = pathlib.Path(str(entry.get("root") or "")).name
                        self.context.console.print(f"  [dim]{action}[/dim] {name} [dim]({root_name})[/dim]")
            else:
                print("Artifact prune")
                print(f"  keep: {result.get('keep', keep)}")
                print(f"  scanned: {scanned}")
                print(f"  removed: {removed}")
                for entry in result.get("deleted") or []:
                    name = pathlib.Path(str(entry.get("path") or entry.get("metadata_path") or "")).name
                    root_name = pathlib.Path(str(entry.get("root") or "")).name
                    print(f"  {action}: {name} ({root_name})")
            return

        items = recent_artifacts_all(limit=limit)
        if not items:
            msg = "No artifacts found"
            self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
            return

        title = "生成的产物" if str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh") else "Generated artifacts"
        if self.context.has_rich:
            self.context.console.print(f"[bold]{title}[/bold]")
        else:
            print(title)
        for index, item in enumerate(items, start=1):
            _path = item.get("path") or item.get("metadata_path") or ""
            _name = pathlib.Path(str(_path)).name if _path else ""
            _root_name = pathlib.Path(str(item.get("root") or "")).name if item.get("root") else ""
            _status = str(item.get("status") or "unknown")
            _icon = "✓" if _status == "complete" else "!" if _status in {"partial", "failed"} else "·"
            _kind = str(item.get("kind") or "artifact")
            _topic = str(item.get("topic") or "").strip()
            _headline = f"#{index}  {_icon} {_kind}" + (f" · {_topic}" if _topic else "")
            _detail = f"    {_name}" + (f"  ·  {_root_name}" if _root_name else "")
            if self.context.has_rich:
                self.context.console.print(f"[bold]{_headline}[/bold]")
                self.context.console.print(f"[dim]{_detail}[/dim]")
            else:
                print(_headline)
                print(_detail)
        hint = "操作：/artifacts open 1 · reveal 1 · copy-path 1"
        self.context.console.print(f"[dim]{hint}[/dim]") if self.context.has_rich else print(hint)
    async def _run_in_executor(self, fn, *args):
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, fn, *args)
        return result
    def cmd_plan(self, args: str):
        from aria_cli import OpsCommandsMixin, _PLAN_MODE
        sub = args.strip().lower().split()[0] if args.strip() else ""

        if sub in ("mode", "enter", "on", "start"):
            _PLAN_MODE.enter()
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(
                    "  [bold cyan]◆ 计划模式已激活[/bold cyan]  "
                    "[dim]每个工具调用前都会显示确认提示[/dim]"
                )
                self.context.console.print("  [dim]/plan exit  — 退出计划模式[/dim]")
                self.context.console.print()
            else:
                print("  Plan mode ON — every tool call will ask for approval.")
            return

        if sub in ("exit", "off", "stop", "quit"):
            summary = _PLAN_MODE.summary()
            _PLAN_MODE.exit()
            if self.context.has_rich:
                self.context.console.print(
                    f"  [dim]◆ 计划模式已退出  "
                    f"(共 {summary['total']} 步 · "
                    f"[green]{summary['approved']} 执行[/green] · "
                    f"[red]{summary['rejected']} 跳过[/red])[/dim]"
                )
            else:
                print(f"  Plan mode OFF. ({summary['total']} steps, {summary['approved']} approved)")
            return

        if sub == "status":
            if _PLAN_MODE.active:
                summary = _PLAN_MODE.summary()
                msg = (
                    f"计划模式: [bold cyan]激活[/bold cyan]  "
                    f"(已执行 {summary['approved']} 步，跳过 {summary['rejected']} 步)"
                )
            else:
                msg = "计划模式: [dim]未激活[/dim]  (/plan mode 开启)"
            self.context.console.print(f"  {msg}") if self.context.has_rich else print(f"  {msg.replace('[bold cyan]', '').replace('[/bold cyan]', '').replace('[dim]', '').replace('[/dim]', '')}")
            return

        return OpsCommandsMixin.cmd_plan(self, args)
    def cmd_tasks(self, args: str):
        """Show and manage background subagent tasks."""
        try:
            from runtime.subagent import _TASKS, clear_finished_tasks, tool_task_cancel
        except ImportError:
            msg = "Subagent module not available."
            self.context.console.print(f"[red]{msg}[/red]") if self.context.has_rich else print(msg)
            return

        parts = args.strip().split()
        sub = parts[0].lower() if parts else "list"

        if sub == "cancel" and len(parts) > 1:
            result = tool_task_cancel({"task_id": parts[1]})
            if result.get("success"):
                msg = f"✓ Task {parts[1]} cancelled"
                self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            else:
                self.context.console.print(f"[red]{result.get('error', 'Error')}[/red]") if self.context.has_rich else print(result.get("error"))
            return

        if sub == "clear":
            msg = f"✓ Cleared {clear_finished_tasks()} finished tasks"
            self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            return

        # Default: list all tasks
        tasks = list(_TASKS.values())
        if not tasks:
            msg = "没有活跃的后台任务。使用 spawn_task 工具创建任务。"
            self.context.console.print(f"  [dim]{msg}[/dim]") if self.context.has_rich else print(msg)
            return

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]后台任务[/bold]")
            self.context.console.print()
            status_colors = {"pending": "yellow", "running": "cyan", "done": "green",
                             "failed": "red", "cancelled": "dim", "interrupted": "dim"}
            for t in tasks:
                col = status_colors.get(t.status, "white")
                preview = t.prompt[:60] + ("…" if len(t.prompt) > 60 else "")
                self.context.console.print(
                    f"  [{col}]●[/{col}] [bold]{t.task_id}[/bold]  "
                    f"[{col}]{t.status:10s}[/{col}]  "
                    f"[dim]{t.age_str():>5s}[/dim]  {preview}"
                )
            self.context.console.print()
            self.context.console.print("  [dim]/tasks cancel <id>  — 取消任务[/dim]")
            self.context.console.print("  [dim]/tasks clear        — 清除已结束的任务[/dim]")
            self.context.console.print()
        else:
            print(f"\n  Background Tasks ({len(tasks)}):")
            for t in tasks:
                preview = t.prompt[:50]
                print(f"  {t.task_id}  {t.status:10s}  {t.age_str():>5s}  {preview}")
    def cmd_task(self, args: str):
        """Show, diff, apply or discard the coding task's worktree."""
        from apps.cli import task_isolation

        text = task_isolation.command(args, self.terminal.config, session_id=self.terminal.session_id or "")
        if self.context.has_rich:
            from rich.markup import escape
            self.context.console.print(escape(text))
        else:
            print(text)
    def cmd_delegate(self, args: str):
        """Delegate a task to the Claude Code or Codex CLI as a background subagent."""
        try:
            from runtime.subagent import tool_spawn_task
        except ImportError:
            msg = "Subagent module not available."
            self.context.console.print(f"[red]{msg}[/red]") if self.context.has_rich else print(msg)
            return

        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2 or parts[0].lower() not in {"claude", "codex"}:
            msg = 'Usage: /delegate claude|codex "<prompt>"'
            self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
            return

        backend, prompt = parts[0].lower(), parts[1].strip().strip('"')
        result = tool_spawn_task({
            "prompt": prompt,
            "backend": backend,
            "mode": "workspace-write",
            "isolation": "auto",
        })
        if not result.get("success"):
            self.context.console.print(f"[red]{result.get('error', 'Error')}[/red]") if self.context.has_rich else print(result.get("error"))
            return
        task_id = result["task_id"]
        msg = f"✓ Delegated to {backend}: task {task_id}. Check with /tasks or task_status('{task_id}')."
        self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
    def cmd_canva(self, args: str):
        """Manage the Canva Connect integration used for report design drafts."""
        parts = args.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts else "status"

        if sub == "connect":
            if len(parts) < 3:
                msg = "Usage: /canva connect <client_id> <client_secret>  (register an app first at https://www.canva.com/developers/)"
                self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
                return
            client_id, client_secret = parts[1], parts[2]
            from canva_client import connect as _canva_connect
            msg = "打开浏览器完成 Canva 授权…"
            self.context.console.print(f"[cyan]{msg}[/cyan]") if self.context.has_rich else print(msg)
            result = _canva_connect(client_id, client_secret)
            if result.get("success"):
                msg = "✓ Canva 已连接"
                self.context.console.print(f"[green]{msg}[/green]") if self.context.has_rich else print(msg)
            else:
                self.context.console.print(f"[red]{result.get('error')}[/red]") if self.context.has_rich else print(result.get("error"))
            return

        if sub == "status":
            from canva_client import _load_canva_config
            entry = _load_canva_config()
            msg = "✓ Canva 已连接" if entry.get("access_token") else "未连接 Canva。运行 /canva connect <client_id> <client_secret>"
            self.context.console.print(msg) if self.context.has_rich else print(msg)
            return

        msg = "Usage: /canva connect <client_id> <client_secret> | /canva status"
        self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
    def _confirm_high_risk_command(self, command: str, risk: str, policy: str) -> bool:
        """Double-confirm high-risk commands even if policy allows them."""
        msg = f"High-risk command under policy '{policy}' (risk={risk}): {command}\nRun it? [y/N]: "
        try:
            answer = self.context.console.input(msg) if self.context.has_rich else input(msg)
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}
    def _open_file(self, path: str):
        """Open a local file using platform default app."""
        path_q = shlex.quote(path)
        if sys.platform == "darwin":
            os.system(f"open {path_q}")
        elif os.name == "nt":
            os.system(f'start "" {path_q}')
        else:
            os.system(f"xdg-open {path_q} >/dev/null 2>&1")
    async def _smart_compact_async(self, silent: bool = False):
        """AI-powered conversation compaction (inspired by Claude Code).

        Sends all messages to the current model and asks for a dense summary,
        then replaces conversation with [system summary] + last 2 message pairs.
        Falls back to hard trim if the summary call fails.
        """
        from aria_cli import OllamaProvider, stream_provider_result
        from aria_code.apps.cli.model_catalog import get_model_cfg
        conv = self.terminal.conversation
        if len(conv) <= 4:
            if not silent:
                self.context.console.print("[dim]Context small enough — no compaction needed[/dim]" if self.context.has_rich
                              else "Context small enough")
            return

        if not silent and self.context.has_rich:
            self.context.console.print("[dim]Summarising conversation...[/dim]")

        from packages.aria_services.context import build_context_service

        model_key = self.terminal.config.get("model", "qwen2.5:7b")
        max_ctx = int(get_model_cfg(model_key).get("num_ctx", 16384) or 16384)
        context_service = build_context_service(max_tokens=max_ctx)
        summary_prompt = context_service.build_summary_prompt(conv)

        summary = ""
        try:
            result = await stream_provider_result(
                OllamaProvider(
                    self.terminal.config.get("ollama_url", "http://localhost:11434"),
                    self.terminal.config.get("model", "qwen2.5:7b"),
                    show_market_prefetch_status=False,
                ),
                summary_prompt,
                [],   # no history — pure summarisation task
                tools=[],
            )
            if result.get("success") and result.get("response"):
                summary = result["response"].strip()
        except Exception:
            pass

        if not summary:
            # Fallback: local structural compaction before hard trimming.
            try:
                compacted = context_service.compact_messages(conv)
            except Exception:
                compacted = []
            self.terminal.conversation = compacted if compacted and len(compacted) < len(conv) else conv[-8:]
            if not silent:
                self.context.console.print("[dim]Compacted (summary failed, used local fallback)[/dim]" if self.context.has_rich
                              else "Compacted (summary fallback)")
            return

        envelope = context_service.build_summary_envelope(conv, summary)
        self.terminal.conversation = envelope.messages
        new_count = len(self.terminal.conversation)
        old_count = len(conv)
        if not silent:
            if self.context.has_rich:
                self.context.console.print(
                    f"  [dim]✓ Compacted {old_count} → {new_count} messages "
                    f"(summary preserved context)[/dim]"
                )
            else:
                print(f"Compacted {old_count} → {new_count} messages")
    def cmd_doctor(self, args: str):
        from aria_cli import DiagnosticOpsCommandsMixin
        _r = DiagnosticOpsCommandsMixin.cmd_doctor(self, args)
        # Architecture coverage summary (observability layer) — /architecture for the
        # full layered view. Best-effort; never let it break /doctor.
        try:
            from packages.aria_core import architecture_gaps, architecture_status_counts
            _c = architecture_status_counts()
            _done, _total = _c.get("done", 0), sum(_c.values())
            _line = (f"架构契约: {_done}/{_total} 层完成 · {len(architecture_gaps())} 层待办"
                     f"  (/architecture 看详情, --gaps 只看待办)")
            self.context.console.print(f"\n  [dim]{_line}[/dim]") if self.context.has_rich else print(f"\n  {_line}")
        except Exception:
            pass
        return _r
    @staticmethod
    def _create_scaffold(target_dir: pathlib.Path, template: dict) -> list:
        """Create dirs + write files from a scaffold template. Returns list of created paths."""
        created = []
        for d in template.get("dirs", []):
            dp = target_dir / d
            dp.mkdir(parents=True, exist_ok=True)
            created.append(str(dp))
        for rel, content in template.get("files", {}).items():
            fp = target_dir / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            if not fp.exists():
                fp.write_text(content, encoding="utf-8")
                created.append(str(fp))
        return created
    def cmd_verify(self, args: str):
        """Infer and run focused verification checks."""
        from aria_cli import VerificationPlanner, _SYNTAX_THEME, _tool_run_command
        from ._ui import Syntax
        parts = args.split()
        dry_run = "--dry-run" in parts
        paths = [p for p in parts if p != "--dry-run"]
        plan = VerificationPlanner(pathlib.Path.cwd()).infer(paths)
        if not plan.commands:
            msg = "No verification command inferred."
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        if self.context.has_rich:
            self.context.console.print(f"[dim]Verification plan: {plan.reason}[/dim]")
            for idx, command in enumerate(plan.commands, 1):
                self.context.console.print(f"  [bold]{idx}.[/bold] {command}")
        else:
            print(f"Verification plan: {plan.reason}")
            for idx, command in enumerate(plan.commands, 1):
                print(f"  {idx}. {command}")
        if dry_run:
            return
        for command in plan.commands:
            result = _tool_run_command({
                "command": command,
                "policy": "balanced",
                "permission_mode": self.terminal.config.get("permission_mode", "workspace-write"),
                "network_enabled": bool(self.terminal.config.get("network_enabled", True)),
                "user_approved": True,
                "timeout": 300,
            })
            if not result.get("success"):
                self.context.console.print(f"[red]Verification failed: {command}[/red]" if self.context.has_rich else f"Verification failed: {command}")
                self.context.console.print(f"[red]{result.get('error', '')}[/red]" if self.context.has_rich else result.get("error", ""))
                return
            data = result.get("data", {})
            if data.get("stdout"):
                self.context.console.print(Syntax(data["stdout"], "text", theme=_SYNTAX_THEME) if self.context.has_rich else data["stdout"])
            if data.get("stderr"):
                self.context.console.print(f"[yellow]{data['stderr']}[/yellow]" if self.context.has_rich else data["stderr"])
        msg = "Verification passed."
        self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)
    def cmd_run(self, args: str):
        """Run a command: /run <command>"""
        from aria_cli import _SYNTAX_THEME, _tool_run_command
        from aria_code.safety import SafetyService
        from ._ui import Syntax
        if not args.strip():
            self.context.console.print("[dim]Usage: /run [--dry-run] <command>[/dim]" if self.context.has_rich
                          else "Usage: /run [--dry-run] <command>")
            return
        text = args.strip()
        dry_run = False
        if text.startswith("--dry-run "):
            dry_run = True
            text = text[len("--dry-run "):].strip()
        if not text:
            self.context.console.print("[dim]Usage: /run [--dry-run] <command>[/dim]" if self.context.has_rich
                          else "Usage: /run [--dry-run] <command>")
            return

        policy = self.terminal.config.get("command_policy", "safe")
        # Built per call, not held: self.terminal.config is mutated in place by
        # /config set, so a cached service would answer with a stale mode.
        decision = SafetyService(self.terminal.config).evaluate_command(text, policy)
        if not dry_run and decision.allowed and decision.risk == "high":
            if not self._confirm_high_risk_command(decision.normalized_command, decision.risk, policy):
                msg = "Cancelled by user."
                self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
                return
        result = _tool_run_command({
            "command": text,
            "policy": policy,
            "permission_mode": self.terminal.config.get("permission_mode", "workspace-write"),
            "network_enabled": bool(self.terminal.config.get("network_enabled", True)),
            "dry_run": dry_run,
        })
        if result["success"]:
            data = result["data"]
            if dry_run:
                msg = (
                    f"Dry run: risk={data.get('risk', '?')} "
                    f"policy={data.get('policy', '?')} "
                    f"approval={data.get('requires_approval', False)} "
                    f"network={data.get('network', False)} "
                    f"command={data.get('command', '')}"
                )
                self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
                return
            if data["stdout"]:
                if self.context.has_rich:
                    self.context.console.print(Syntax(data["stdout"], "text", theme=_SYNTAX_THEME))
                else:
                    print(data["stdout"])
            if data["stderr"]:
                if self.context.has_rich:
                    self.context.console.print(f"[red]{data['stderr']}[/red]")
                else:
                    print(data["stderr"], file=sys.stderr)
        else:
            self.context.console.print(f"[red]{result['error']}[/red]" if self.context.has_rich else result["error"])
    def cmd_apply(self, args: str):
        """Extract code from last AI response and save to file."""
        from aria_code.apps.cli.helpers import _extract_code_block
        from aria_cli import _SYNTAX_THEME, _tool_write_file
        from ._ui import Syntax
        filename = args.strip()
        last_response = ""
        for msg in reversed(self.terminal.conversation):
            if msg["role"] == "assistant":
                last_response = msg["content"]
                break
        if not last_response:
            self.context.console.print("[dim]No AI response to extract from[/dim]" if self.context.has_rich
                          else "No response")
            return

        code = _extract_code_block(last_response)
        if not code:
            self.context.console.print("[dim]No code block found in last response[/dim]" if self.context.has_rich
                          else "No code block found")
            return

        if not filename:
            # Show code preview and ask for filename
            preview = code[:500] + ("..." if len(code) > 500 else "")
            if self.context.has_rich:
                self.context.console.print(f"\n[dim]Found code block ({len(code.splitlines())} lines):[/dim]")
                self.context.console.print(Syntax(preview, "python", theme=_SYNTAX_THEME))
            else:
                print(f"\nFound code ({len(code.splitlines())} lines):")
                print(preview)
            try:
                filename = (self.context.console.input("\n[bold]>[/bold] Save to: ") if self.context.has_rich
                            else input("\nSave to: ")).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not filename:
                return

        result = _tool_write_file({"path": filename, "content": code})
        if not result["success"]:
            self.context.console.print(f"[red]{result['error']}[/red]" if self.context.has_rich else result["error"])
    async def cmd_code(self, args: str):
        """Generate code and optionally save to file. Usage: /code <description> [--save file.py]"""
        from aria_code.apps.cli.helpers import _extract_code_block
        from aria_code.ui.render.output import display_path as _display_path
        if not args.strip():
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /code <description> [--save file.py][/dim]")
                self.context.console.print("[dim]Examples:[/dim]")
                self.context.console.print("[dim]  /code momentum strategy for AAPL[/dim]")
                self.context.console.print("[dim]  /code portfolio optimizer --save optimizer.py[/dim]")
                self.context.console.print("[dim]  /code backtest report generator --save report.py[/dim]")
            else:
                print("Usage: /code <description> [--save file.py]")
            return

        # Parse --save flag
        save_path = None
        description = args
        if "--save" in args:
            parts = args.split("--save")
            description = parts[0].strip()
            save_path = parts[1].strip() if len(parts) > 1 else None

        from artifacts import user_generated_dir
        from apps.cli.codegen_paths import resolve_user_code_path
        default_save = resolve_user_code_path(
            description,
            None,
            user_generated_dir=user_generated_dir(),
        )

        # Build code generation prompt
        prompt = (
            f"Generate complete, production-ready Python code for: {description}\n\n"
            "Requirements:\n"
            "- Include all necessary imports\n"
            "- Add clear inline comments\n"
            "- Include error handling\n"
            "- Use type hints where appropriate\n"
            "- Make it runnable as a standalone script\n\n"
            "Return the code wrapped in ```python``` fences."
        )

        if self.context.has_rich:
            self.context.console.print(f"[bold]Generating code:[/bold] [bold]{description}[/bold]")
        else:
            print(f"Generating: {description}")

        # The configured model writes the code. This used to switch to
        # qwen2.5:7b for the call, whatever the user had chosen — a Gemini or
        # Claude user got a small local model, or an error without Ollama.
        await self.terminal.send_message(prompt)

        # Extract code from last AI response and save if requested
        if save_path or description:
            last_response = ""
            for msg in reversed(self.terminal.conversation):
                if msg["role"] == "assistant":
                    last_response = msg["content"]
                    break
            code = _extract_code_block(last_response)
            if code:
                if save_path:
                    _save_path = resolve_user_code_path(
                        description,
                        save_path,
                        user_generated_dir=user_generated_dir(),
                    )
                else:
                    _save_path = default_save
                _save_path.parent.mkdir(parents=True, exist_ok=True)
                _save_path.write_text(code, encoding="utf-8")
                _save_label = _display_path(str(_save_path))
                if self.context.has_rich:
                    self.context.console.print(f"\n[green]Code saved to {_save_label}[/green] "
                                  f"[dim]({len(code.splitlines())} lines)[/dim]")
                else:
                    print(f"\nSaved: {_save_label} ({len(code.splitlines())} lines)")
            else:
                if self.context.has_rich:
                    self.context.console.print("[dim]No code block found in response to save[/dim]")
                else:
                    print("No code block found to save")
    async def cmd_feedback(self, args: str):
        """Rate the last AI response and store feedback locally by default.

        Usage: /feedback good|bad [comment]
               /feedback note <comment>
        """
        from aria_cli import CONFIG_DIR
        from aria_code.privacy import FeedbackRecord
        from aria_code.privacy import FeedbackStore
        from aria_code.ui.render.output import display_path as _display_path
        parts = args.strip().split(maxsplit=1)
        vote = parts[0].lower() if parts else ""
        comment = parts[1].strip() if len(parts) > 1 else ""

        aliases = {
            "good": "positive", "up": "positive", "1": "positive", "+": "positive",
            "bad": "negative", "down": "negative", "0": "negative", "-": "negative",
            "note": "note",
        }
        rating = aliases.get(vote)
        if rating is None or (rating == "note" and not comment):
            self.context.console.print("[dim]Usage: /feedback good|bad [comment] | /feedback note <comment>[/dim]" if self.context.has_rich
                          else "Usage: /feedback good|bad [comment] | /feedback note <comment>")
            return

        # Find last assistant message and its position
        last_msg = None
        msg_idx = None
        for i in range(len(self.terminal.conversation) - 1, -1, -1):
            if self.terminal.conversation[i]["role"] == "assistant":
                last_msg = self.terminal.conversation[i]["content"][:500]
                msg_idx = i
                break
        if not last_msg:
            self.context.console.print("[dim]No AI response to rate[/dim]" if self.context.has_rich else "No response to rate")
            return

        from aria_code.safety import SafetyService
        settings = SafetyService(self.terminal.config).privacy()
        record = FeedbackRecord.create(
            rating=rating,
            message=last_msg,
            comment=comment,
            model=self.terminal.config.get("model", ""),
            session_id=self.terminal.session_id,
            message_index=msg_idx,
            shared=settings.data_sharing and settings.feedback_upload,
        )
        store = FeedbackStore(CONFIG_DIR)

        # Persist locally first. This is the default and works offline.
        try:
            feedback_path = store.append(record)
        except Exception as exc:
            msg = f"Could not save feedback locally: {exc}"
            self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
            return

        # Optional remote upload only after explicit opt-in. Posts to the
        # backend /feedback endpoint in the shape it expects (FeedbackRequest)
        # so the rating reaches production_data_collector → DPO training.
        # No trail_id needed: the backend matches the latest sample for this
        # session_id (good→thumbs_up, bad→thumbs_down; bad+comment→DPO pair).
        api_success = False
        upload_attempted = settings.data_sharing and settings.feedback_upload
        if upload_attempted and rating in ("positive", "negative"):
            try:
                import aiohttp
                headers = {}
                if self.terminal.config.get("auth_token"):
                    headers["Authorization"] = f"Bearer {self.terminal.config['auth_token']}"
                payload = {
                    "user_id":       self.terminal.config.get("user_id") or "cli",
                    "session_id":    self.terminal.session_id,
                    "feedback_type": "thumbs_up" if rating == "positive" else "thumbs_down",
                    "response_id":   self.terminal.session_id,  # latest-in-session match
                    "value":         comment or None,
                }
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.post(
                        f"{self.terminal.api_url}/feedback",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        api_success = resp.status in (200, 201, 204)
            except Exception:
                api_success = False

        icon = "↑" if rating == "positive" else ("↓" if rating == "negative" else "note")
        if upload_attempted:
            sync_note = "" if api_success else " [dim](saved locally; upload failed)[/dim]"
        else:
            sync_note = " [dim](saved locally; sharing off)[/dim]"
        if self.context.has_rich:
            comment_note = f" — {comment}" if comment else ""
            self.context.console.print(f"[green]Feedback {icon}[/green]{comment_note}{sync_note}")
            self.context.console.print(f"[dim]Saved: {_display_path(feedback_path)}[/dim]")
        else:
            print(f"Feedback {icon}" + (f" — {comment}" if comment else "") +
                  (" (uploaded)" if api_success else " (saved locally)"))
    def cmd_privacy(self, args: str):
        """Manage local privacy and feedback-sharing settings."""
        from aria_cli import CONFIG_DIR
        from aria_code.privacy import FeedbackStore
        from aria_code.privacy import PrivacySettings
        from aria_code.ui.render.output import display_path as _display_path
        parts = args.strip().split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        store = FeedbackStore(CONFIG_DIR)
        from aria_code.safety import SafetyService
        settings = SafetyService(self.terminal.config).privacy()

        def _save_settings(new_settings: PrivacySettings):
            new_settings.apply_to_config(self.terminal.config)
            self.context.save_config(self.terminal.config)

        if sub in {"status", "show"}:
            shared_state = "ON — feedback may be shared with Arthera" if (
                settings.data_sharing and settings.feedback_upload
            ) else "OFF — local-only, nothing leaves this machine"
            lines = [
                "Privacy status",
                f"  sharing: {shared_state}",
                f"  data_sharing: {settings.data_sharing}",
                f"  feedback_upload: {settings.feedback_upload}",
                f"  feedback_records: {store.count()}",
                f"  local_feedback: {store.feedback_file}",
                "  default: local-only; no upload unless you run /privacy opt-in",
                "  full policy: /privacy policy  (or see PRIVACY.md)",
            ]
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("[bold]Privacy[/bold]")
                for line in lines[1:]:
                    self.context.console.print(f"[dim]{line}[/dim]")
            else:
                print("\n".join(lines))
            return

        if sub == "policy":
            url = "https://github.com/artheras/aria-code/blob/main/PRIVACY.md"
            local = pathlib.Path(__file__).resolve().parent / "PRIVACY.md"
            lines = [
                "Aria Code is local-first: by default nothing is collected or uploaded.",
                "Only `/privacy opt-in` shares feedback (rating + related message) with Arthera.",
                "Credentials, positions, and financial data always stay on your machine.",
                f"  full policy: {local if local.exists() else url}",
                "  manage: /privacy opt-in | opt-out | export [path] | delete",
            ]
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print("[bold]Privacy policy[/bold]")
                for line in lines:
                    self.context.console.print(f"[dim]{line}[/dim]")
            else:
                print("\n".join(lines))
            return

        if sub in {"opt-in", "on", "enable"}:
            _save_settings(PrivacySettings(data_sharing=True, feedback_upload=True))
            if self.context.has_rich:
                self.context.console.print("[green]Data sharing enabled.[/green]")
                self.context.console.print("[dim]  You consent to share /feedback records (rating, the related[/dim]")
                self.context.console.print("[dim]  model message, optional comment, model id, session id, time)[/dim]")
                self.context.console.print("[dim]  with Arthera to improve the product. Local copies are kept.[/dim]")
                self.context.console.print("[dim]  Credentials & financial data are never shared. Details: /privacy policy[/dim]")
                self.context.console.print("[dim]  Withdraw any time: /privacy opt-out[/dim]")
            else:
                print("Data sharing enabled. You consent to share /feedback records "
                      "(rating + related message) with Arthera. Credentials & financial "
                      "data are never shared. Withdraw: /privacy opt-out. Details: /privacy policy")
            return

        if sub in {"opt-out", "off", "disable"}:
            _save_settings(PrivacySettings(data_sharing=False, feedback_upload=False))
            msg = "Data sharing disabled. Feedback stays local only."
            self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)
            return

        if sub == "export":
            dest = rest or None
            try:
                path = store.export_jsonl(dest)
            except Exception as exc:
                msg = f"Export failed: {exc}"
                self.context.console.print(f"[red]{msg}[/red]" if self.context.has_rich else msg)
                return
            msg = f"Exported feedback to {_display_path(path)}"
            self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)
            return

        if sub in {"delete", "clear"}:
            count = store.delete_all()
            msg = f"Deleted {count} local feedback record(s)."
            self.context.console.print(f"[green]{msg}[/green]" if self.context.has_rich else msg)
            return

        msg = "Usage: /privacy [status|policy|opt-in|opt-out|export [path]|delete]"
        self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
    async def _run_tool_cmd(self, tool_name: str, params: dict, label: str = ""):
        """Generic helper: run tool with spinner and formatted output.

        Routing priority:
          1. LOCAL_TOOLS (via executor — never blocks event loop)
          2. Remote Aria backend (AWS) — if local not available
          3. Graceful error if both fail
        """
        from aria_cli import Text
        from aria_code.apps.cli.tool_executor import execute_aria_tool
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS
        display = label or tool_name

        # ── 1. Try LOCAL_TOOLS first (run in executor to avoid blocking) ──
        if tool_name in LOCAL_TOOLS:
            handler, _ = LOCAL_TOOLS[tool_name]
            if self.context.has_rich:
                with self.context.console.status(f"[dim]{display}...[/dim]", spinner="dots"):
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, handler, params
                    )
            else:
                print(f"Running {display}...")
                result = await asyncio.get_event_loop().run_in_executor(
                    None, handler, params
                )
        else:
            # ── 2. Fall through to remote Aria backend ────────────────────
            local_mode = self.terminal.config.get("local_mode", False)
            if local_mode:
                result = {
                    "success": False,
                    "error":   f"Tool '{tool_name}' has no local implementation. "
                               "Run '/local off' to use the Aria backend, or "
                               "add a handler in aria_tools.py.",
                }
            else:
                if self.context.has_rich:
                    with self.context.console.status(f"[dim]Running {display}...[/dim]", spinner="dots"):
                        result = await execute_aria_tool(self.terminal.api_url, tool_name, params)
                else:
                    print(f"Running {display}...")
                    result = await execute_aria_tool(self.terminal.api_url, tool_name, params)

        if result.get("success"):
            data = result.get("data", {})
            if isinstance(data, dict) and self.context.has_rich:
                out = Text()
                for k, v in data.items():
                    if k in ("chart_prices", "raw", "metadata"):
                        continue
                    label_str = k.replace("_", " ").title()
                    val_str = f"{v:.4f}" if isinstance(v, float) else str(v)
                    if isinstance(v, (int, float)):
                        color = "green" if v >= 0 else "red" if v < 0 else ""
                        out.append(f"  {label_str:<20s}", style="dim")
                        out.append(f"{val_str}\n", style=color if color else "")
                    else:
                        out.append(f"  {label_str:<20s}", style="dim")
                        out.append(f"{val_str}\n")
                self.context.console.print(out)
            else:
                self.context.console.print(f"  [dim]{json.dumps(data, ensure_ascii=False)[:500]}[/dim]" if self.context.has_rich
                              else json.dumps(data, ensure_ascii=False)[:500])
        else:
            print_error(self.context, f"Failed: {result.get('error', 'No data')}")
    async def _run_parallel(self, tool_name: str,
                             param_list: list,
                             label_fn=None):
        """Run a tool in parallel for multiple param dicts, display each result."""
        from aria_cli import _print_finance_result
        from aria_code.apps.cli.tool_executor import execute_aria_tool
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS
        tasks = [
            asyncio.create_task(
                asyncio.get_event_loop().run_in_executor(
                    None, LOCAL_TOOLS[tool_name][0], p
                ) if tool_name in LOCAL_TOOLS
                else execute_aria_tool(self.terminal.api_url, tool_name, p)
            )
            for p in param_list
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for p, r in zip(param_list, results):
            lbl = label_fn(p) if label_fn else tool_name
            if isinstance(r, Exception):
                print_error(self.context, f"{lbl}: {r}")
            else:
                _print_finance_result(tool_name, r)
    async def _fetch_and_display_finance(self, tool_name: str, params: dict, label: str,
                                          mdc_fallback_symbol: str = "") -> bool:
        """Try tool → local finance tool → market_data_client fallback. Returns True if data shown."""
        from aria_cli import _print_finance_result
        from aria_code.apps.cli.tool_executor import execute_aria_tool
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS
        from aria_code.apps.cli._optional import HAS_MDC as _HAS_MDC, get_mdc as _get_mdc
        result = None
        # 1. LOCAL_TOOLS (ccxt / local finance)
        if tool_name in LOCAL_TOOLS:
            fn = LOCAL_TOOLS[tool_name][0]
            result = await asyncio.get_event_loop().run_in_executor(None, fn, params)
        # 2. Remote backend
        if not (result and result.get("success")):
            result = await execute_aria_tool(self.terminal.api_url, tool_name, params)
        # 3. MarketDataClient yfinance fallback
        if not (result and result.get("success")) and _HAS_MDC and mdc_fallback_symbol:
            try:
                mdc = _get_mdc()
                result = mdc.quote(mdc_fallback_symbol)
                if result.get("success"):
                    result["provider"] = "yfinance"
            except Exception:
                pass

        if result and result.get("success"):
            _print_finance_result(tool_name, result)
            # Also show basic price line if _print_finance_result didn't handle this tool
            if tool_name not in ("get_market_data", "get_crypto_data", "get_forex_data"):
                px   = result.get("price", result.get("rate", 0))
                chg  = result.get("change_pct", 0)
                sign = "+" if chg >= 0 else ""
                color = "green" if chg >= 0 else "red"
                prov  = result.get("provider", "")
                if self.context.has_rich and px:
                    self.context.console.print(f"  [bold]{label:<12}[/bold]  {px}  [{color}]{sign}{chg:.2f}%[/{color}]  [dim]{prov}[/dim]")
            return True
        else:
            err = (result or {}).get("error") or "数据暂不可用"
            if self.context.has_rich:
                self.context.console.print(f"  [yellow]⚠ {label}: {err}[/yellow]")
            else:
                print(f"  ⚠ {label}: {err}")
            return False
    async def cmd_risk(self, args: str):
        """Risk metrics: /risk AAPL or /risk portfolio"""
        from aria_code.apps.cli.tool_executor import execute_aria_tool
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS
        target = args.strip().upper() or "AAPL"
        if target == "PORTFOLIO":
            await self._run_tool_cmd("assess_portfolio_risk", {
                "holdings": self.terminal.config.get("watchlist", ["AAPL", "MSFT"]),
            }, "portfolio risk")
            return

        # Backend first, the local calculation if it is unavailable. The local
        # tool returns its numbers at the top level, not under "data", so this
        # printed the heading and nothing else; the backend's were printed as
        # raw keys and fractions.
        result = await execute_aria_tool(self.terminal.api_url, "get_risk_metrics", {"symbol": target})
        source = "backend"
        if not result.get("success") and "get_risk_metrics" in LOCAL_TOOLS:
            local_fn = LOCAL_TOOLS["get_risk_metrics"][0]
            result = await asyncio.get_event_loop().run_in_executor(None, local_fn, {"symbol": target})
            source = "local"
        if not result.get("success"):
            print_error(self.context,
                        ui_text(self, f"{target} 风险指标暂不可用", f"Risk metrics unavailable for {target}"),
                        str(result.get("error") or "")[:200])
            return
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        self._print_risk_metrics(target, data, source)

    def _print_risk_metrics(self, target: str, d: dict, source: str) -> None:
        """One labelled line per metric, percentages as percentages."""
        zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
        conf = d.get("confidence_level") or 0.95
        signed = lambda v: f"{float(v) * 100:+.2f}%" if isinstance(v, (int, float)) else "—"  # noqa: E731
        pct = lambda v: f"{abs(float(v)) * 100:.2f}%" if isinstance(v, (int, float)) else "—"  # noqa: E731
        plain = lambda v: f"{float(v):.2f}" if isinstance(v, (int, float)) else "—"  # noqa: E731
        rows = [
            (f"VaR {conf:.0%} · 1 天" if zh else f"VaR {conf:.0%} · 1 day", d.get("var_daily"), pct,
             f"最差的 {1 - conf:.0%} 交易日里的跌幅" if zh else f"loss on the worst {1 - conf:.0%} of days"),
            (f"VaR {conf:.0%} · 1 个月" if zh else f"VaR {conf:.0%} · 1 month", d.get("var_monthly"), pct, ""),
            ("CVaR · 1 天" if zh else "CVaR · 1 day", d.get("cvar_daily"), pct,
             "超出 VaR 那些天的平均跌幅" if zh else "average loss on days beyond the VaR"),
            ("最大回撤" if zh else "Max drawdown", d.get("max_drawdown"), signed, ""),
            ("年化波动" if zh else "Annual volatility", d.get("annual_volatility"), pct, ""),
            ("年化收益" if zh else "Annual return", d.get("annual_return"), signed, ""),
            ("下行偏差" if zh else "Downside deviation", d.get("downside_deviation"), pct, ""),
            ("Sharpe", d.get("sharpe_ratio"), plain, ""),
            ("Calmar", d.get("calmar_ratio"), plain, ""),
            ("偏度" if zh else "Skewness", d.get("skewness"), plain, ""),
            ("峰度" if zh else "Kurtosis", d.get("kurtosis"), plain, ""),
        ]
        rows = [(label, fmt(value), note) for label, value, fmt, note in rows if value is not None]
        title = f"{target} {'风险指标' if zh else 'risk metrics'}"
        sub = ("本地计算 · 近一年日线" if zh else "local calculation · past year, daily") if source == "local" \
            else ("后端计算" if zh else "from the backend")
        if not self.context.has_rich:
            print(f"  {title} ({sub})")
            for label, value, note in rows:
                print(f"  {label:<22} {value:>9}  {note}")
            return
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column(justify="right")
        table.add_column(style="dim")
        for label, value, note in rows:
            style = "red" if value.startswith("-") and value.endswith("%") else ""
            table.add_row(label, f"[{style}]{value}[/{style}]" if style else value, note)
        self.context.console.print()
        self.context.console.print(f"  [bold]{title}[/bold]  [dim]{sub}[/dim]")
        from rich.padding import Padding
        self.context.console.print(Padding(table, (0, 0, 0, 2)))
        self.context.console.print()

    async def cmd_market(self, args: str):
        """Market overview: /market [indices|sectors]"""
        from aria_code.apps.cli.tool_executor import execute_aria_tool
        from aria_code.apps.cli._optional import HAS_MDC as _HAS_MDC, get_mdc as _get_mdc
        sub = args.strip().lower()
        if sub == "sectors":
            await self._run_tool_cmd("get_sector_performance", {}, "sector performance")
        else:
            # Try remote tool first; fall back to local MarketDataClient if backend unavailable
            result = await execute_aria_tool(self.terminal.api_url, "get_market_indices", {})
            if result and result.get("success"):
                await self._run_tool_cmd("get_market_indices", {}, "market indices")
            elif _HAS_MDC:
                # Local fallback via MarketDataClient.indices()
                try:
                    mdc = _get_mdc()
                    idx_result = mdc.indices()
                    if idx_result.get("success") and idx_result.get("indices"):
                        if self.context.has_rich:
                            self.context.console.print()
                            self.context.console.print("  [bold]Global Indices[/bold]  [dim](local data)[/dim]")
                            self.context.console.print()
                        for name, d in idx_result["indices"].items():
                            price = d.get("price", "N/A")
                            chg   = d.get("change_pct", 0)
                            sign  = "+" if chg >= 0 else ""
                            color = "green" if chg >= 0 else "red"
                            if self.context.has_rich:
                                self.context.console.print(f"  [dim]{name:<20s}[/dim]  {price:>10}  [{color}]{sign}{chg:.2f}%[/{color}]")
                            else:
                                print(f"  {name:<20s}  {price:>10}  {sign}{chg:.2f}%")
                    else:
                        self.context.console.print("[dim]市场数据暂不可用。请检查网络连接。[/dim]") if self.context.has_rich else print("Market data unavailable.")
                except Exception as _e:
                    self.context.console.print(f"[dim]本地数据获取失败: {_e}[/dim]") if self.context.has_rich else print(f"Local data error: {_e}")
            else:
                self.context.console.print("[dim]后端不可用，本地数据模块未加载。使用 /indices 命令查看实时行情。[/dim]") if self.context.has_rich else print("Backend unavailable. Try /indices.")
    async def cmd_optimize(self, args: str):
        """Optimize portfolio: /optimize [symbols...]"""
        symbols = args.upper().split() if args else self.terminal.config.get("watchlist", ["AAPL", "MSFT", "GOOGL"])
        await self._run_tool_cmd("optimize_positions", {
            "symbols": symbols, "objective": "max_sharpe",
        }, f"optimizing {len(symbols)} positions")
    async def cmd_stress(self, args: str):
        """Stress test: /stress <strategy> [symbol]"""
        parts = args.split() if args else ["momentum", "SPY"]
        strategy = parts[0] if parts else "momentum"
        symbol = parts[1].upper() if len(parts) > 1 else "SPY"
        await self._run_tool_cmd("stress_test_strategy", {
            "strategy": strategy, "symbol": symbol,
        }, f"stress test {strategy}/{symbol}")
    async def cmd_factors(self, args: str):
        """Factor analysis: /factors AAPL"""
        symbol = args.strip().upper() or "AAPL"
        await self._run_tool_cmd("calculate_factors", {"symbol": symbol}, f"factors {symbol}")
    async def cmd_factor_lab(self, args: str):
        """/factor-lab <SYMBOL> [days=252] — 量化因子工作台（动量/波动率/Sharpe/Amihud）"""
        parts  = args.strip().split()
        symbol = parts[0].upper() if parts else "AAPL"
        market = "CN" if any(symbol.startswith(p) for p in ("SH", "SZ", "6", "0", "3")) else "US"

        await self._run_tool_cmd(
            "equity_factor_scores",
            {"symbol": symbol, "period": "1y", "market": market},
            f"factor-lab {symbol}",
        )
    async def cmd_execution(self, args: str):
        """/execution <SYMBOL> <buy|sell> <qty> [algo=compare] [price=0] — 执行算法对比"""
        parts = args.strip().split()
        if len(parts) < 3:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /execution AAPL buy 100000 [algo=compare] [price=180][/dim]")
            return

        symbol    = parts[0].upper()
        side      = parts[1].lower()
        try:
            total_qty = float(parts[2].replace(",", ""))
        except ValueError:
            if self.context.has_rich: self.context.console.print("[red]qty 必须是数字[/red]")
            return

        algo  = "compare"
        price = 0.0
        for p in parts[3:]:
            if p.startswith("algo="):
                algo = p.split("=", 1)[1]
            elif p.startswith("price="):
                try:
                    price = float(p.split("=", 1)[1])
                except ValueError:
                    pass

        if price <= 0:
            # 尝试从市场数据获取现价
            try:
                import yfinance as yf
                t = yf.Ticker(symbol)
                info = t.fast_info
                price = float(getattr(info, "last_price", 0) or 0)
            except Exception:
                price = 100.0

        await self._run_tool_cmd(
            "execution_schedule",
            {
                "symbol":          symbol,
                "side":            side,
                "total_qty":       total_qty,
                "benchmark_price": price,
                "algo":            algo,
            },
            f"执行计划 {symbol} {side} {total_qty:,.0f}股",
        )
    async def cmd_stat_arb(self, args: str):
        """/stat-arb <SYMBOL_A> <SYMBOL_B> [period=2y] — 配对协整检验 + 当前 z-score"""
        from aria_cli import _generate_stat_arb_chart
        parts = args.strip().split()
        if len(parts) < 2:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /stat-arb GLD SLV [period=2y][/dim]")
            return

        sym_a  = parts[0].upper()
        sym_b  = parts[1].upper()
        period = "2y"
        for p in parts[2:]:
            if p.startswith("period="):
                period = p.split("=", 1)[1]

        await self._run_tool_cmd(
            "pair_stats",
            {"symbol_a": sym_a, "symbol_b": sym_b, "period": period},
            f"配对检验 {sym_a}/{sym_b}",
        )
        # Generate interactive z-score chart
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: _generate_stat_arb_chart(sym_a, sym_b, period)
        )
    async def cmd_compliance(self, args: str):
        """Compliance check: /compliance <strategy>"""
        strategy = args.strip() or "momentum"
        await self._run_tool_cmd("check_strategy_compliance", {
            "strategy": strategy,
        }, f"compliance {strategy}")
    async def cmd_search_web(self, args: str):
        """Web search: /web <query>"""
        query = args.strip()
        if not query:
            self.context.console.print("[dim]Usage: /web <search query>[/dim]" if self.context.has_rich else "Usage: /web <query>")
            return
        await self._run_tool_cmd("web_search", {"query": query}, f"searching: {query[:30]}")
    def cmd_local(self, args: str):
        """Toggle local-only mode (skip AWS, always use Ollama)."""
        cfg = self.terminal.config
        arg = args.strip().lower()
        if arg in ("on", "1", "true", "yes"):
            cfg["local_mode"] = True
        elif arg in ("off", "0", "false", "no"):
            cfg["local_mode"] = False
        else:
            cfg["local_mode"] = not cfg.get("local_mode", False)
        self.context.save_config(cfg)
        state = "ON" if cfg["local_mode"] else "OFF"
        model = cfg.get("model", "qwen2.5:7b")
        if self.context.has_rich:
            color = "green" if cfg["local_mode"] else "yellow"
            self.context.console.print(f"  [{color}]Local mode {state}[/{color}]  model=[bold]{model}[/bold]  ollama={cfg.get('ollama_url','http://localhost:11434')}")
        else:
            print(f"  Local mode {state}  model={model}")
    async def cmd_mcp(self, args: str):
        """Manage MCP servers: /mcp status | /mcp tools | /mcp reload [server]"""
        from aria_cli import CONFIG_DIR
        from aria_code.apps.cli._optional import MCP_CONFIG_PATH
        from aria_code.apps.cli._optional import HAS_MCP as _HAS_MCP
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS
        if not _HAS_MCP:
            self.context.console.print("  [dim]mcp_client.py not available[/dim]" if self.context.has_rich else "MCP not available")
            return
        sub = args.strip().lower()
        reg = self.terminal._mcp_registry

        _parts = sub.split()
        if _parts and _parts[0] in ("reload", "restart") and len(_parts) > 1:
            # Per-server reload: restart one subprocess + reset its circuit,
            # without tearing down every other server.
            _name = args.strip().split()[1]
            if not reg:
                self.context.console.print("  [dim]No MCP servers running[/dim]" if self.context.has_rich else "No MCP servers")
                return
            ok = await reg.reload_server(_name)
            msg = (f"MCP server {_name!r} reloaded" if ok
                   else f"MCP server {_name!r} not found or failed to restart")
            if self.context.has_rich:
                self.context.console.print(f"  [{'green' if ok else 'red'}]{msg}[/{'green' if ok else 'red'}]")
            else:
                print(f"  {msg}")
            return

        if sub in ("reload", "restart"):
            if reg:
                await reg.stop_all()
            self.terminal._mcp_started = False
            self.terminal._mcp_registry = None
            if self.context.has_rich:
                self.context.console.print("  [dim]Restarting MCP servers…[/dim]")
            from mcp_client import MCPToolRegistry
            self.terminal._mcp_registry = MCPToolRegistry()
            results = await self.terminal._mcp_registry.start_all()
            n = self.terminal._mcp_registry.register_into(LOCAL_TOOLS, LOCAL_TOOL_SCHEMAS, overwrite=True)
            if self.context.has_rich:
                self.context.console.print(f"  [green]MCP reloaded: {n} tools from {len(results)} servers[/green]")
            return

        if sub == "tools":
            if not reg:
                self.context.console.print("  [dim]No MCP servers running[/dim]" if self.context.has_rich else "No MCP servers")
                return
            tools = reg.all_tools()
            if self.context.has_rich:
                self.context.console.print(f"\n  [bold]MCP Tools[/bold] ({len(tools)} total)\n")
                for t in tools:
                    self.context.console.print(f"    [bold]{t['qualified_name']:40s}[/bold][dim]{t.get('description','')[:60]}[/dim]")
                self.context.console.print()
            else:
                for t in tools:
                    print(f"  {t['qualified_name']:40s} {t.get('description','')[:60]}")
            return

        # Default: status
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]MCP Servers[/bold]")
            if not _HAS_MCP:
                self.context.console.print("  [dim]Not available (mcp_client.py missing)[/dim]")
            elif not reg:
                config_path = str(MCP_CONFIG_PATH)
                self.context.console.print(f"  [dim]No servers started. Configure: {config_path}[/dim]")
                self.context.console.print("  [dim]Example: add quant_engine MCP server pointing to your mcp_server.py[/dim]")
            else:
                for s in reg.status():
                    color = "green" if s["running"] else "red"
                    icon  = "●" if s["running"] else "○"
                    self.context.console.print(
                        f"  [{color}]{icon}[/{color}] [bold]{s['name']:20s}[/bold]"
                        f" [dim]{s['tool_count']} tools  {s['description'][:50]}[/dim]"
                    )
            self.context.console.print()
        else:
            if not reg:
                print(f"  No MCP servers. Configure {CONFIG_DIR}/mcp_servers.json")
            else:
                for s in reg.status():
                    print(f"  {'●' if s['running'] else '○'} {s['name']:20s} {s['tool_count']} tools")
    def cmd_license(self, args: str):
        """Show feature license / entitlement status."""
        try:
            from licensing import current_license, license_status
            current_license(refresh=True)   # re-read in case a key was just installed
            st = license_status()
        except Exception as e:
            self.context.console.print(f"  [yellow]license 模块不可用: {e}[/yellow]" if self.context.has_rich else f"license unavailable: {e}")
            return
        tier = st.get("tier", "free")
        valid = st.get("valid", True)
        if self.context.has_rich:
            color = "green" if (tier != "free" and valid) else "dim"
            self.context.console.print(f"  授权等级: [{color}]{tier}[/{color}]  有效: {valid}"
                          + (f"  到期: {st['exp']}" if st.get("exp") else ""))
            if st.get("reason"):
                self.context.console.print(f"  [yellow]{st['reason']}[/yellow]")
            feats = st.get("features") or []
            self.context.console.print(f"  已解锁: {', '.join(feats) if feats else '仅免费功能'}")
            self.context.console.print(f"  签名校验模式: {'开启' if st.get('signed_mode') else '关闭(开发/自托管)'}")
            self.context.console.print("  [dim]免费版含全部核心功能;专业功能配置 ARIA_LICENSE_KEY 或 ~/.arthera/license.json 解锁。[/dim]")
        else:
            print(f"license tier={tier} valid={valid} features={st.get('features')}")
    def cmd_ariarc(self, args: str):
        """Show or reload .ariarc project configuration."""
        from aria_cli import _HAS_ARIARC, get_ariarc, reload_ariarc
        if not _HAS_ARIARC:
            self.context.console.print("  [dim]ariarc.py not available[/dim]" if self.context.has_rich else "ariarc not available")
            return
        if "reload" in args.lower():
            arc = reload_ariarc()
            self.terminal.ariarc = arc
            if self.context.has_rich:
                if arc.found:
                    self.context.console.print(f"  [green]ariarc reloaded: {arc.source_path}[/green]")
                else:
                    self.context.console.print("  [yellow]No .ariarc found in current directory tree[/yellow]")
            return

        arc = self.terminal.ariarc or get_ariarc()
        if self.context.has_rich:
            self.context.console.print()
            if not arc.found:
                self.context.console.print("  [dim]No .ariarc found (create .ariarc in your project root)[/dim]")
                self.context.console.print()
                _example = """{
  "project": "My Quant Strategy",
  "description": "A-share momentum + mean-reversion strategy",
  "market": "cn",
  "default_symbols": ["sh600519", "sh601318", "sz000858"],
  "system_prompt": "Focus on A-share market mechanics and T+1 constraints.",
  "context_files": ["README.md"],
  "auto_context": ["strategy/main.py"],
  "commands": {
    "/morning-cn": "生成A股早盘简报，重点关注 {default_symbols}"
  }
}"""
                self.context.console.print(f"  [dim]Example .ariarc:[/dim]\n{_example}")
            else:
                d = arc.to_dict()
                self.context.console.print(f"  [bold]Project:[/bold] {arc.project or '(unnamed)'}")
                self.context.console.print(f"  [bold]Source:[/bold]  [dim]{d['source_path']}[/dim]")
                self.context.console.print(f"  [bold]Market:[/bold]  {arc.market}")
                if arc.default_symbols:
                    self.context.console.print(f"  [bold]Symbols:[/bold] {', '.join(arc.default_symbols)}")
                if arc.commands:
                    self.context.console.print(f"  [bold]Commands:[/bold] {', '.join(arc.commands.keys())}")
                if arc.tools_blacklist:
                    self.context.console.print(f"  [bold]Blocked tools:[/bold] {', '.join(arc.tools_blacklist)}")
                if arc.auto_context:
                    self.context.console.print(f"  [bold]Auto context:[/bold] {', '.join(arc.auto_context)}")
            self.context.console.print()
        else:
            if arc.found:
                import json as _j
                print(_j.dumps(arc.to_dict(), indent=2, ensure_ascii=False))
    async def cmd_signal(self, args: str):
        """
        AI trading signal (BUY/SELL/HOLD) from Alibaba Cloud.
        Usage: /signal sh600519   /signal AAPL US
        """
        from aria_code.apps.cli.helpers import _is_ashare_symbol
        parts  = args.strip().split()
        symbol = parts[0].upper() if parts else "sh600519"
        market = parts[1].upper() if len(parts) > 1 else ("CN" if _is_ashare_symbol(symbol) else "US")
        await self._run_tool_cmd("get_ai_signal", {"symbol": symbol, "market": market},
                                 f"AI signal {symbol}")
    async def cmd_predict(self, args: str):
        """
        ML return predictions for a list of symbols.
        Usage: /predict sh600519 sh601318 sz000858
        """
        parts   = args.strip().split() if args.strip() else ["sh600519"]
        symbols = [s for s in parts if not s.isdigit() or len(s) == 6]
        days    = 5
        for p in parts:
            if p.startswith("d="):
                try:
                    days = int(p[2:])
                except ValueError:
                    pass
        await self._run_tool_cmd("get_predictions",
                                 {"symbols": symbols, "prediction_days": days},
                                 f"ML predict {len(symbols)} stocks")
    async def cmd_cloudbt(self, args: str):
        """
        Full ML-powered backtest on Alibaba Cloud.
        Usage: /cloudbt sh600519 sh601318 [model=lightgbm] [months=12] [freq=weekly] [top=3]
        """
        parts   = args.strip().split() if args.strip() else []
        symbols = []
        kwargs: Dict[str, Any] = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                kwargs[k] = v
            else:
                symbols.append(p)
        if not symbols:
            symbols = ["sh600519"]
        params = {
            "symbols":        symbols,
            "model_type":     kwargs.get("model", "lightgbm"),
            "months":         int(kwargs.get("months", 12)),
            "rebalance_freq": kwargs.get("freq", "weekly"),
            "top_k":          int(kwargs.get("top", 3)),
        }
        await self._run_tool_cmd("cloud_backtest", params,
                                 f"cloud backtest {len(symbols)} stocks")
    async def cmd_insights(self, args: str):
        """
        AI market insights for a basket of stocks.
        Usage: /insights sh600519 sh601318 sz000858
        """
        parts   = args.strip().split() if args.strip() else ["sh600519"]
        symbols = parts
        await self._run_tool_cmd("get_market_insights",
                                 {"symbols": symbols},
                                 f"market insights {len(symbols)} stocks")
    def cmd_recommend(self, args: str):
        """Recommend best local models for financial analysis."""
        from aria_cli import RECOMMENDED_FINANCE_MODELS
        from aria_code.apps.cli.helpers import detect_ollama_models
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]Recommended Local Models for Finance[/bold]")
            self.context.console.print()
            try:
                available = detect_ollama_models(
                    self.terminal.config.get("ollama_url", "http://localhost:11434")
                )
                for rec in RECOMMENDED_FINANCE_MODELS:
                    model_id = rec["model"]
                    installed = any(a.startswith(model_id.split(":")[0]) for a in available)
                    icon  = "[green]●[/green]" if installed else "[dim]○[/dim]"
                    vram  = rec.get("vram_gb", "?")
                    self.context.console.print(
                        f"  {icon} [bold]{model_id:30s}[/bold] "
                        f"[dim]VRAM≈{vram}GB  {rec['reason'][:60]}[/dim]"
                    )
                    if not installed:
                        self.context.console.print(f"    [dim]Install: {rec['install']}[/dim]")
                self.context.console.print()
            except Exception:
                self.context.console.print("  [dim]Could not check installed models[/dim]")
        else:
            for rec in RECOMMENDED_FINANCE_MODELS:
                print(f"  {rec['model']:30s} {rec['reason']}")
                print(f"    Install: {rec['install']}")
    async def cmd_optimize_port(self, args: str):
        """Portfolio weight optimisation."""
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS
        symbols = [s.strip().upper() for s in args.split() if s.strip()]
        if not symbols:
            self.context.console.print("  [dim]Usage: /optimize-port AAPL MSFT GOOGL [method=max_sharpe][/dim]" if self.context.has_rich
                          else "Usage: /optimize-port AAPL MSFT [method=max_sharpe]")
            return
        # Check if last token is method=X
        method = "max_sharpe"
        if symbols and "=" in symbols[-1]:
            k, v = symbols.pop().split("=", 1)
            if k == "method":
                method = v
        params = {"symbols": symbols, "method": method}
        tool_name = "optimize_positions"
        if tool_name in LOCAL_TOOLS:
            await self._run_local_tool(tool_name, params, f"优化持仓 {method}")
        else:
            await self.terminal.send_message(
                f"帮我用 {method} 方法优化以下资产组合的仓位权重：{', '.join(symbols)}"
            )
    async def _run_local_tool(self, tool_name: str, params: dict, label: str = ""):
        """Run a LOCAL_TOOLS entry, display result with Rich formatting."""
        from aria_cli import _print_tool_result
        from aria_code.apps.cli.tool_registry import LOCAL_TOOLS
        from aria_code.apps.cli.helpers import _clean_tool_error_message
        if tool_name not in LOCAL_TOOLS:
            if self.context.has_rich:
                self.context.console.print(f"  [dim]Tool {tool_name!r} not available[/dim]")
            return
        handler, _ = LOCAL_TOOLS[tool_name]
        label_text = label or tool_name
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{label_text}…[/dim]", spinner="dots"):
                result = handler(params)
        else:
            print(f"  {label_text}…")
            result = handler(params)

        if not result.get("success", True):
            err = _clean_tool_error_message(result.get("error", "unknown error"))
            if self.context.has_rich:
                self.context.console.print(f"  [red]✗[/red] {err}")
            else:
                print(f"  ✗ {err}")
            return

        # Pretty-print result
        _print_tool_result(tool_name, result, elapsed=0)
    async def cmd_ui(self, args: str):
        """
        Generate a Bloomberg Terminal-style HTML file for any requested view.
        Usage: /ui <description>
               /ui 今日A股板块热力图
               /ui 持仓组合风险报告
               /ui 宏观利率与大宗商品看板
        """
        desc = args.strip()
        if not desc:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /ui <描述>[/dim]")
                self.context.console.print("[dim]  /ui 今日A股热力图[/dim]")
                self.context.console.print("[dim]  /ui 持仓组合报告[/dim]")
                self.context.console.print("[dim]  /ui 市场晨报看板[/dim]")
            else:
                print("Usage: /ui <description>")
            return

        try:
            from apps.cli.prompts.ui import UI_SYSTEM_PROMPT
        except ImportError:
            UI_SYSTEM_PROMPT = ""
        try:
            from artifacts import user_generated_dir
            generated_dir = user_generated_dir()
        except Exception:
            generated_dir = pathlib.Path.home() / "Documents" / "Aria Code" / "generated"

        prompt = (
            f"{UI_SYSTEM_PROMPT}\n\n"
            "---\n\n"
            f"USER REQUEST: {desc}\n\n"
            "Generate a Bloomberg Terminal-style HTML dashboard for this request.\n"
            "Follow the workflow:\n"
            "1. Identify what data you need (symbols, metrics, time range).\n"
            f"2. Write a Python generator script under {generated_dir} using write_file:\n"
            "   - Fetch all data with yfinance / akshare\n"
            "   - Embed data as JS/HTML constants\n"
            "   - Apply the full Bloomberg CSS design system (dark/light via prefers-color-scheme)\n"
            f"   - Save timestamped HTML under {generated_dir}\n"
            "3. Run the script with run_command.\n"
            "4. Open the output HTML in the browser.\n\n"
            "Design requirements (non-negotiable):\n"
            "- No emojis anywhere — use [+] [-] [!] [ACTIVE] text indicators\n"
            "- border-radius: 0 everywhere\n"
            "- IBM Plex Mono for all prices and numbers\n"
            "- prefers-color-scheme: dark default, light override\n"
            "- ALL CAPS section headers, 10px, letter-spacing 0.12em\n"
            "- Accent color: dark=#F5A623, light=#B8520A\n"
        )

        if self.context.has_rich:
            self.context.console.print(f"[bold]UI Generation:[/bold] {desc}")
        else:
            print(f"Generating UI: {desc}")

        await self.terminal.send_message(prompt)
    async def cmd_dashboard(self, args: str):
        """
        生成个人化 Dashboard HTML，自动在浏览器打开。
        Usage: /dashboard [brief|market|portfolio|full]
        数据来源: 本地持仓DB + 价格预警DB + yfinance 实时行情 + 最近生成文件
        """
        try:
            from dashboard_generator import generate_and_open
        except ImportError:
            if self.context.has_rich:
                self.context.console.print("[red]dashboard_generator.py 未找到，请检查安装[/red]")
            else:
                print("dashboard_generator.py 未找到")
            return

        parts = args.strip().split()
        mode = parts[0].lower() if parts and parts[0].lower() in {"brief", "market", "portfolio", "full"} else "brief"
        watchlist = self.terminal.config.get("watchlist", [])
        if self.context.has_rich:
            self.context.console.print(f"[dim]正在抓取数据并生成 Dashboard（{mode}）…[/dim]")
        else:
            print(f"正在生成 Dashboard（{mode}）…")

        try:
            out = generate_and_open(watchlist=watchlist, config=self.terminal.config, mode=mode)
            if self.context.has_rich:
                self.context.console.print(
                    f"  [green]✓[/green] Dashboard 已生成并在浏览器打开\n"
                    f"  [dim]路径: [bold]{out}[/bold][/dim]"
                )
            else:
                print(f"Dashboard saved: {out}")
        except Exception as exc:
            if self.context.has_rich:
                self.context.console.print(f"[red]生成失败: {exc}[/red]")
            else:
                print(f"生成失败: {exc}")
    async def cmd_tv(self, args: str):
        """Print a TradingView chart URL or export a Pine Script strategy."""
        from aria_cli import _build_tradingview_indicator_readout, _resolve_market_arg_symbol, _src_market_snapshot_analysis, _strip_latex, _write_text_companion, make_markdown
        from aria_code.apps.cli.helpers import _chart_display_label, _copy_text_to_clipboard, _open_path_or_url, _reveal_path_in_finder
        parts = [p.strip() for p in args.strip().split() if p.strip()]
        open_browser = any(p in {"--open", "-o", "open"} for p in parts)
        export_pine = any(p in {"--pine", "pine", "--strategy", "strategy", "策略"} for p in parts)
        copy_pine = any(p in {"--copy", "copy", "复制"} for p in parts)
        reveal_file = any(p in {"--reveal", "reveal", "finder", "访达", "目录"} for p in parts)
        txt_copy = any(p in {"--txt", "txt", "文本"} for p in parts)
        bullish_analysis = any(p in {"--bullish", "bullish", "看涨", "偏多"} for p in parts)
        bearish_analysis = any(p in {"--bearish", "bearish", "看跌", "偏空"} for p in parts)
        general_analysis = any(p in {"--analyze", "--analysis", "analyze", "analysis", "分析"} for p in parts)
        tv_analysis_mode = "bullish" if bullish_analysis else ("bearish" if bearish_analysis else ("analyze" if general_analysis else ""))
        interval = None
        symbol_parts = []
        skip_next = False
        for idx, part in enumerate(parts):
            if skip_next:
                skip_next = False
                continue
            low = part.lower()
            if low in {
                "--open", "-o", "open", "--pine", "pine", "--strategy", "strategy", "策略",
                "--copy", "copy", "复制", "--reveal", "reveal", "finder", "访达", "目录",
                "--txt", "txt", "文本", "--bullish", "bullish", "看涨", "偏多",
                "--bearish", "bearish", "看跌", "偏空", "--analyze", "--analysis",
                "analyze", "analysis", "分析",
            }:
                continue
            if low.startswith("--interval="):
                interval = part.split("=", 1)[1]
                continue
            if low == "--interval" and idx + 1 < len(parts):
                interval = parts[idx + 1]
                skip_next = True
                continue
            symbol_parts.append(part.strip(",，"))

        raw_symbol = symbol_parts[0] if symbol_parts else "AAPL"
        symbol = _resolve_market_arg_symbol(raw_symbol)
        try:
            from apps.cli.tradingview_bridge import (
                export_pine_strategy,
                tradingview_symbol,
                tradingview_url,
            )
            tv_symbol = tradingview_symbol(symbol)
            url = tradingview_url(symbol, interval=interval)
        except Exception as exc:
            print_error(self.context, f"TradingView URL 生成失败: {exc}")
            return

        if self.context.has_rich:
            self.context.console.print(f"\n  [bold]TradingView[/bold]  {_chart_display_label(raw_symbol, symbol)}")
            self.context.console.print(f"  [dim]symbol:[/dim] {symbol}  [dim]tv:[/dim] {tv_symbol}")
            self.context.console.print(f"  [link={url}]{url}[/link]")
        else:
            print(f"TradingView {symbol} -> {tv_symbol}")
            print(url)
        self.terminal._pending_market_artifact = {
            "kind": "tradingview_chart",
            "symbol": symbol,
            "display": _chart_display_label(raw_symbol, symbol),
            "tv_symbol": tv_symbol,
            "url": url,
            "command": f"/tv {symbol}",
        }

        if export_pine:
            try:
                pine_path = export_pine_strategy(symbol)
                self.terminal._pending_market_artifact = {
                    "kind": "tradingview_pine_strategy",
                    "symbol": symbol,
                    "display": _chart_display_label(raw_symbol, symbol),
                    "tv_symbol": tv_symbol,
                    "url": url,
                    "pine_path": str(pine_path),
                    "path": str(pine_path),
                    "command": f"/tv {symbol} --pine",
                }
                if self.context.has_rich:
                    self.context.console.print(f"  [green]✓[/green] Pine strategy saved: [link={_file_uri(pine_path)}]{pine_path}[/link]")
                    self.context.console.print("  [dim]Use: TradingView → Pine Editor → paste script → Save → Add to chart[/dim]")
                else:
                    print(f"Pine strategy saved: {pine_path}")
                    print("Use: TradingView -> Pine Editor -> paste script -> Save -> Add to chart")
                if txt_copy:
                    companion = _write_text_companion(str(pine_path))
                    if companion:
                        if self.context.has_rich:
                            self.context.console.print(f"  [green]✓[/green] Text copy saved: [link={_file_uri(companion)}]{companion}[/link]")
                        else:
                            print(f"Text copy saved: {companion}")
                if copy_pine:
                    try:
                        copied, copy_err = _copy_text_to_clipboard(pine_path.read_text(encoding="utf-8"))
                    except Exception as exc:
                        copied, copy_err = False, str(exc)
                    if self.context.has_rich:
                        self.context.console.print("  [green]✓[/green] Pine script copied to clipboard" if copied else f"  [yellow]copy failed:[/yellow] {copy_err}")
                    else:
                        print("Pine script copied to clipboard" if copied else f"copy failed: {copy_err}")
                if reveal_file:
                    revealed, reveal_err = _reveal_path_in_finder(str(pine_path))
                    if self.context.has_rich:
                        self.context.console.print("  [green]✓[/green] Revealed in Finder" if revealed else f"  [yellow]reveal failed:[/yellow] {reveal_err}")
                    else:
                        print("Revealed in Finder" if revealed else f"reveal failed: {reveal_err}")
            except Exception as exc:
                print_error(self.context, f"Pine Script 导出失败: {exc}")
                return

        if open_browser and url:
            opened, open_err = _open_path_or_url(url)
            if self.context.has_rich:
                self.context.console.print("  [green]✓[/green] opened in browser" if opened else f"  [yellow]open failed:[/yellow] {open_err}")
            else:
                print("opened in browser" if opened else f"open failed: {open_err}")

        if tv_analysis_mode:
            snapshot = _src_market_snapshot_analysis(f"分析 {symbol} 股票")
            if snapshot.get("success"):
                readout = _build_tradingview_indicator_readout(
                    snapshot,
                    tv_symbol=tv_symbol,
                    mode=tv_analysis_mode,
                )
                if self.context.has_rich:
                    self.context.console.print(make_markdown(_strip_latex(readout)))
                else:
                    print(readout)
                self.terminal._last_response = readout
            else:
                msg = (
                    f"TradingView 已打开，但当前无法生成 {symbol} 的指标分析："
                    f"{snapshot.get('error') or '行情数据不可用'}"
                )
                if self.context.has_rich:
                    self.context.console.print(f"  [yellow]{msg}[/yellow]")
                else:
                    print(msg)
    async def cmd_chart(self, args: str):
        """
        生成股票分析图表（HTML，含K线/均线/RSI/MACD）。
        Usage: /chart AAPL [period]
               /chart 600519 3m   (A股，3个月)
               /chart BTC-USD 2y
        支持 period: 1m 3m 6m 1y 2y 3y 5y ytd max
        """
        from aria_cli import _generate_chart_sync, _resolve_market_arg_symbol, sanitize_chart_symbol_args
        from aria_code.ui.render.output import display_path as _display_path
        from aria_code.apps.cli.helpers import _chart_display_label
        _VALID_PERIODS = {"1m","3m","6m","1y","2y","3y","5y","ytd","max",
                          "1mo","3mo","6mo"}
        parts  = args.strip().split()
        period = "1y"
        symbol_parts = []
        for p in parts:
            if p.lower() in _VALID_PERIODS:
                period = p.lower()
            else:
                symbol_parts.append(p.strip(",，"))
        raw_symbols = sanitize_chart_symbol_args(symbol_parts) or ["AAPL"]
        symbols = []
        seen_symbols = set()
        for raw_symbol in raw_symbols:
            resolved = _resolve_market_arg_symbol(raw_symbol)
            if resolved and resolved not in seen_symbols:
                seen_symbols.add(resolved)
                symbols.append((raw_symbol, resolved))
        if not symbols:
            symbols = [("AAPL", "AAPL")]

        if len(symbols) > 1:
            await self._cmd_chart_multi(symbols, period)
            return

        raw_symbol, symbol = symbols[0]

        msg = f"chart {_chart_display_label(raw_symbol, symbol)} · {period}"
        chart_start = time.perf_counter()
        if self.context.has_rich:
            self.context.console.print(f"\n  [bold]⏺[/bold] {msg}")
            with self.context.console.status("[dim]fetching OHLCV and calculating indicators…[/dim]", spinner="dots"):
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _generate_chart_sync(symbol, period=period)
                )
        else:
            print(f"  > {msg}")
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _generate_chart_sync(symbol, period=period)
            )
        elapsed_ms = int((time.perf_counter() - chart_start) * 1000)

        if result.get("success"):
            path    = result.get("chart_path", "")
            path_label = _display_path(path, fallback="chart")
            issues  = result.get("review_issues") or []
            sup3    = result.get("support") or []
            res3    = result.get("resistance") or []
            rsi_val = result.get("rsi")
            provider = result.get("provider") or "market data"
            display_label = _chart_display_label(raw_symbol, symbol, result)
            self.terminal._pending_market_artifact = {
                "kind": "stock_chart",
                "symbol": symbol,
                "display": display_label,
                "period": period,
                "html_path": path,
                "png_path": result.get("png_path") or "",
                "command": f"/chart {symbol} {period}",
                "provider": provider,
                "metrics": {
                    "trend": result.get("trend"),
                    "rsi": rsi_val,
                    "support": sup3,
                    "resistance": res3,
                },
            }
            # The summary was Chinese whatever the UI language ("趋势: 偏多 …
            # 自审通过"); the trend value itself comes from the chart in Chinese.
            _zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
            _T = (lambda a, b: a) if _zh else (lambda a, b: b)
            _trend = str(result.get("trend") or "—")
            if not _zh:
                _trend = {"偏多": "bullish", "偏空": "bearish", "震荡": "range-bound",
                          "震荡/中性": "range-bound"}.get(_trend, _trend)
            _summary = (
                f"{display_label}  {_T('趋势', 'trend')}: {_trend}  "
                f"RSI: {f'{rsi_val:.1f}' if rsi_val else '—'}  "
                f"{_T('支撑', 'support')}: {'/'.join(str(v) for v in sup3) or '—'}  "
                f"{_T('阻力', 'resistance')}: {'/'.join(str(v) for v in res3) or '—'}  "
                f"{_T('数据', 'source')}: {provider}"
            )
            _issues_head = _T(f"自审发现 {len(issues)} 个问题:", f"self-check found {len(issues)} issue(s):")
            _clean = _T("自审通过（数据质量正常）", "self-check passed (data quality normal)")
            if self.context.has_rich:
                from rich.markup import escape as _esc

                self.context.console.print(f"  [green]✓[/green] {_T('图表已生成', 'chart generated')}  [dim]({elapsed_ms}ms)[/dim]")
                self.context.console.print(f"    {_T('已保存', 'saved')}: [link={_file_uri(path)}]{path_label}[/link]")
                self.context.console.print(f"    [dim]{_esc(_summary)}[/dim]")
                if issues:
                    self.context.console.print(f"  [yellow]⚠ {_issues_head}[/yellow]")
                    for iss in issues:
                        self.context.console.print(f"    [yellow]· {_esc(str(iss))}[/yellow]")
                else:
                    self.context.console.print(f"  [green dim]✓ {_clean}[/green dim]")
            else:
                print(f"  OK {_T('图表已生成', 'chart generated')} ({elapsed_ms}ms): {path_label}")
                print(f"  {_summary}")
                if issues:
                    print(f"  ⚠ {_issues_head}")
                    for iss in issues:
                        print(f"    · {iss}")
            import subprocess as _sp
            try:
                _sp.Popen(["open", path])
            except Exception:
                pass
        else:
            err = result.get("error") or result.get("response", "未知错误")
            print_error(self.context, f"图表生成失败: {err[:120]}")
    async def _cmd_chart_multi(self, symbols: list[tuple[str, str]], period: str):
        """Generate a normalized comparison chart plus individual K-line charts."""
        from aria_code.ui.render.output import display_path as _display_path
        from aria_code.apps.cli.helpers import _chart_display_label
        labels = [_chart_display_label(raw, resolved) for raw, resolved in symbols]
        if self.context.has_rich:
            self.context.console.print(f"\n  [bold]⏺[/bold] compare {' · '.join(labels)} · {period}")
        else:
            print(f"  > compare {' '.join(labels)} · {period}")

        compare_start = time.perf_counter()
        try:
            from apps.cli.handlers.chart_handlers import handle_multi_stock_comparison_direct as _multi_chart
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _multi_chart([resolved for _, resolved in symbols], period=period)
            )
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
        elapsed_ms = int((time.perf_counter() - compare_start) * 1000)

        if result.get("success"):
            path = result.get("chart_path", "")
            path_label = _display_path(path, fallback="comparison chart")
            self.terminal._pending_market_artifact = {
                "kind": "stock_comparison_chart",
                "symbol": " ".join(resolved for _, resolved in symbols),
                "display": " vs ".join(labels),
                "period": period,
                "html_path": path,
                "command": f"/chart {' '.join(resolved for _, resolved in symbols)} {period}",
                "metrics": result.get("metrics") or [],
                "children": [],
            }
            if self.context.has_rich:
                self.context.console.print(f"  [green]✓[/green] comparison chart generated  [dim]({elapsed_ms}ms)[/dim]")
                self.context.console.print(f"    saved: [link={_file_uri(path)}]{path_label}[/link]")
                rows = result.get("metrics") or []
                for row in rows[:6]:
                    self.context.console.print(
                        f"    [dim]{row.get('symbol')}: "
                        f"收益 {row.get('total_return_pct', 0):+.2f}%  "
                        f"波动 {row.get('volatility_pct', 0):.2f}%  "
                        f"最大回撤 {row.get('max_drawdown_pct', 0):.2f}%[/dim]"
                    )
            else:
                print(f"  OK comparison chart generated ({elapsed_ms}ms): {path_label}")
        else:
            print_error(self.context, f"对比图生成失败: {(result.get('error') or 'unknown')[:120]}")

        child_artifacts = []
        for raw_symbol, resolved in symbols:
            await self.cmd_chart(f"{resolved} {period}")
            child = dict(getattr(self.terminal, "_pending_market_artifact", {}) or {})
            if child.get("html_path"):
                child_artifacts.append(child)
        if result.get("success"):
            self.terminal._pending_market_artifact = {
                "kind": "stock_comparison_chart",
                "symbol": " ".join(resolved for _, resolved in symbols),
                "display": " vs ".join(labels),
                "period": period,
                "html_path": result.get("chart_path", ""),
                "command": f"/chart {' '.join(resolved for _, resolved in symbols)} {period}",
                "metrics": result.get("metrics") or [],
                "children": child_artifacts,
            }
    async def cmd_shortterm(self, args: str):
        """
        运行 A股短线分析（日线级别，3-15交易日）并输出报告。
        Usage: /shortterm
               /shortterm 000333 601138 300750
        """

        import subprocess
        import sys as _sys
        _base = pathlib.Path(__file__).parent.parent.parent / "research" / "shortterm"
        script = _base / "run_shortterm.py"
        if not script.exists():
            print_error(self.context, f"短线分析脚本未找到: {script}")
            return
        codes = args.strip().split()
        cmd   = [_sys.executable, str(script)]
        if codes:
            cmd += ["--code"] + codes
        if self.context.has_rich:
            self.context.console.print("\n  📊 运行短线分析...\n")
        else:
            print("\n  📊 运行短线分析...\n")
        result = subprocess.run(cmd, text=True, capture_output=False)
        if result.returncode != 0:
            print_error(self.context, "短线分析执行失败，请检查 research/shortterm/")
    async def cmd_longterm(self, args: str):
        """
        运行 A股长线分析（月线级别，3-18个月目标）并输出报告。
        Usage: /longterm
               /longterm --quick   (只分析 core 级标的)
               /longterm 600519 000858
        """

        import subprocess
        import sys as _sys
        _base = pathlib.Path(__file__).parent.parent.parent / "research" / "longterm"
        script = _base / "run_longterm.py"
        if not script.exists():
            print_error(self.context, f"长线分析脚本未找到: {script}")
            return
        parts = args.strip().split()
        cmd   = [_sys.executable, str(script)]
        if "--quick" in parts:
            cmd.append("--quick")
            parts.remove("--quick")
        if parts:
            cmd += ["--code"] + parts
        if self.context.has_rich:
            self.context.console.print("\n  📈 运行长线分析...\n")
        else:
            print("\n  📈 运行长线分析...\n")
        result = subprocess.run(cmd, text=True, capture_output=False)
        if result.returncode != 0:
            print_error(self.context, "长线分析执行失败，请检查 research/longterm/")
    async def cmd_indices(self, args: str):
        """全球主要指数实时行情."""
        from aria_code.apps.cli._optional import HAS_MDC as _HAS_MDC, get_mdc as _get_mdc
        from aria_code.apps.cli.helpers import _clean_tool_error_message
        if not _HAS_MDC:
            self.context.console.print("  [dim]market_data_client 未加载[/dim]" if self.context.has_rich else "market_data_client not loaded")
            return
        mdc = _get_mdc()
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{ui_text(self, '获取全球指数', 'Fetching global indices')}...[/dim]", spinner="dots"):
                r = mdc.indices()
        else:
            print("  获取全球指数...")
            r = mdc.indices()

        if not r.get("success"):
            err = _clean_tool_error_message(r.get("error", "failed"))
            self.context.console.print(f"  [red]{err}[/red]" if self.context.has_rich else err)
            return

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("  [bold]全球指数行情[/bold]  "
                          f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim]")
            self.context.console.print()
            for name, d in r["indices"].items():
                chg = d.get("change_pct", 0)
                color = "green" if chg >= 0 else "red"
                sign  = "+" if chg >= 0 else ""
                self.context.console.print(
                    f"  [bold]{name:<14s}[/bold]"
                    f"  {str(d.get('price','')):<12}"
                    f"  [{color}]{sign}{chg:.2f}%[/{color}]"
                )
            self.context.console.print()
        else:
            for name, d in r["indices"].items():
                chg = d.get("change_pct", 0)
                sign = "+" if chg >= 0 else ""
                print(f"  {name:<16} {str(d.get('price','')):<12} {sign}{chg:.2f}%")
    async def cmd_hot(self, args: str):
        """热门/活跃股票榜单.  Usage: /hot [cn|us] [top=20]"""
        from aria_code.apps.cli._optional import HAS_MDC as _HAS_MDC, get_mdc as _get_mdc
        if not _HAS_MDC:
            self.context.console.print("  [dim]market_data_client 未加载[/dim]" if self.context.has_rich else "market_data_client not loaded")
            return
        parts  = args.strip().lower().split()
        market = "us" if "us" in parts else "cn"
        top_n  = 20
        for p in parts:
            if p.startswith("top="):
                try: top_n = int(p.split("=")[1])
                except ValueError: pass

        mdc = _get_mdc()
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{ui_text(self, f'获取{market.upper()}热门股', f'Fetching top {market.upper()} movers')}...[/dim]", spinner="dots"):
                r = mdc.hot_stocks(market=market, top_n=top_n)
        else:
            r = mdc.hot_stocks(market=market, top_n=top_n)

        if not r.get("success"):
            self.context.console.print(f"  [red]{r.get('error','failed')}[/red]" if self.context.has_rich else r.get('error'))
            return

        stocks = r.get("stocks", [])
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [bold]{market.upper()} 热门股 Top {len(stocks)}[/bold]  "
                          f"[dim]provider: {r.get('provider','')}[/dim]")
            self.context.console.print()
            for i, s in enumerate(stocks, 1):
                sym  = s.get("code") or s.get("symbol","")
                name = s.get("name", sym)
                p    = s.get("price", "-")
                chg  = s.get("change_pct", 0)
                color = "green" if chg >= 0 else "red"
                sign  = "+" if chg >= 0 else ""
                self.context.console.print(
                    f"  [dim]{i:2d}.[/dim] [bold]{name:<8s}[/bold] "
                    f"[dim]{sym:<8s}[/dim] {str(p):<8} "
                    f"[{color}]{sign}{chg:.2f}%[/{color}]"
                )
            self.context.console.print()
        else:
            for s in stocks:
                sym = s.get("code") or s.get("symbol","")
                print(f"  {s.get('name',sym):<10} {sym:<8} {s.get('price','-'):<8} {s.get('change_pct',0):+.2f}%")
    async def cmd_ta(self, args: str):
        """技术指标分析.  Usage: /ta NVDA [days=120]"""
        from aria_cli import _generate_chart_sync, parse_technical_args, print_ta_result
        from aria_code.ui.render.output import display_path as _display_path
        from aria_code.apps.cli.helpers import _chart_period_from_ta_days, _display_value
        zh = str(self.terminal.config.get("ui_lang", "en")).lower().startswith("zh")
        T = (lambda zh_text, en_text: zh_text) if zh else (lambda zh_text, en_text: en_text)
        parsed = parse_technical_args(args)
        symbol = parsed.symbol
        days = parsed.days

        service_result = None
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{T(f'计算 {symbol} 技术指标', f'Calculating {symbol} indicators')}...[/dim]", spinner="dots"):
                from packages.aria_services.data import DataService
                service_result = DataService().technical_indicators(symbol, days=days)
        else:
            from packages.aria_services.data import DataService
            service_result = DataService().technical_indicators(symbol, days=days)
        if not service_result or not service_result.success:
            _ta_warns = (service_result.warnings or []) if service_result else []
            _ta_errs  = (service_result.errors   or []) if service_result else []
            _ta_data  = (service_result.data or {})    if service_result else {}
            _missing  = ", ".join(service_result.missing_fields) if service_result else ""
            _all_msgs = " ".join(_ta_warns + _ta_errs).lower()
            # Show current price when we have partial data (e.g. new IPO with 1 bar)
            _price_line = ""
            if _ta_data.get("price"):
                _price_line = f"  {T('当前价格', 'Price')}  [bold]{_display_value(_ta_data['price'])}[/bold]"
                if _ta_data.get("history_bars"):
                    _price_line += f"  [dim]({_ta_data['history_bars']} {T('个交易日数据', 'trading days of data')})[/dim]"
                _price_line += "\n"
            if "数据不足" in _all_msgs or "新上市" in _all_msgs:
                _reason = T(
                    f"[yellow]历史数据不足[/yellow] — {symbol} 上市时间较短（< 14 个交易日），TA 指标无法计算\n  [dim]可待更多交易日积累后重试，或运行 `/analyze {symbol}` 查看基本面[/dim]",
                    f"[yellow]Not enough history[/yellow] — {symbol} has fewer than 14 trading days, so indicators cannot be computed\n  [dim]Try again later, or run `/analyze {symbol}` for fundamentals[/dim]")
            elif "rate" in _all_msgs or "429" in _all_msgs or "too many" in _all_msgs:
                _reason = T("[yellow]数据源频率限制[/yellow] — 稍后重试，或用 `/apikey set finnhub <KEY>` 切换数据源",
                            "[yellow]Rate-limited by the data source[/yellow] — retry later, or switch sources with `/apikey set finnhub <KEY>`")
            else:
                _err = "; ".join(_ta_errs or _ta_warns) or T("数据源暂时不可用", "data source temporarily unavailable")
                _reason = f"[red]{_err[:120]}[/red]"
                if _missing:
                    _reason += f"  [dim]missing: {_missing}[/dim]"
            if self.context.has_rich:
                if _price_line:
                    self.context.console.print(f"\n{_price_line}")
                self.context.console.print(f"  {_reason}\n")
            else:
                import re as _re
                print(f"\n  {_re.sub(r'[[/].*?]', '', _price_line + _reason)}\n")
            return

        print_ta_result(
            console=self.context.console,
            has_rich=self.context.has_rich,
            symbol=symbol,
            days=days,
            service_result=service_result,
            formatter=_display_value,
            lang="zh" if zh else "en",
        )

        period = _chart_period_from_ta_days(days)
        self.terminal._pending_market_artifact = {
            "kind": "ta_chart",
            "symbol": symbol,
            "period": period,
            "command": f"/chart {symbol} {period}",
        }
        chart_result = None
        if self.context.has_rich:
            with self.context.console.status(f"[dim]{T(f'生成 {symbol} 技术图表', f'Drawing the {symbol} chart')} HTML/PNG...[/dim]", spinner="dots"):
                chart_result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _generate_chart_sync(symbol, period=period)
                )
        else:
            print(f"  {T(f'生成 {symbol} 技术图表', f'Drawing the {symbol} chart')} HTML/PNG...")
            chart_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _generate_chart_sync(symbol, period=period)
            )

        if chart_result and chart_result.get("success"):
            html_path = chart_result.get("chart_path") or ""
            png_path = chart_result.get("png_path") or ""
            png_error = chart_result.get("png_error") or ""
            self.terminal._pending_market_artifact = {
                "kind": "ta_chart",
                "symbol": symbol,
                "period": period,
                "html_path": html_path,
                "png_path": png_path,
                "command": f"/chart {symbol} {period}",
            }
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(f"  [green]✓[/green] {T('技术图表已生成', 'Chart saved')}")
                if html_path:
                    self.context.console.print(f"  [dim]HTML:[/dim] [link={_file_uri(html_path)}]{_display_path(html_path)}[/link]")
                if png_path:
                    self.context.console.print(f"  [dim]PNG :[/dim] [link={_file_uri(png_path)}]{_display_path(png_path)}[/link]")
                elif png_error:
                    self.context.console.print(f"  [yellow]{T('PNG 跳过', 'PNG skipped')}:[/yellow] {png_error[:90]}")
            else:
                print(f"\n  {T('技术图表已生成', 'Chart saved')}")
                if html_path:
                    print(f"  HTML: {_display_path(html_path)}")
                if png_path:
                    print(f"  PNG : {_display_path(png_path)}")
                elif png_error:
                    print(f"  {T('PNG 跳过', 'PNG skipped')}: {png_error[:90]}")
        elif chart_result:
            err = chart_result.get("error") or T("图表生成失败", "chart failed")
            if self.context.has_rich:
                self.context.console.print(f"  [yellow]{T('图表生成跳过', 'Chart skipped')}:[/yellow] {err[:120]}")
            else:
                print(f"  {T('图表生成跳过', 'Chart skipped')}: {err[:120]}")
    def _extract_last_code(self) -> str:
        """从对话历史中提取最后一段 Python 代码块."""
        import re
        for msg in reversed(self.terminal.conversation):
            content = msg.get("content", "")
            # Match ```python ... ``` blocks
            matches = re.findall(r"```(?:python)?\n(.*?)```", content, re.DOTALL)
            if matches:
                # Return the longest code block
                return max(matches, key=len)
        return ""
    async def cmd_orcl(self, args: str):
        """Oracle Corporation (ORCL) analysis."""
        deep = "deep" in args.lower()
        if deep:
            prompt = (
                "Perform a comprehensive multi-factor analysis of Oracle Corporation (ORCL):\n"
                "1. Technical: trend, RSI, MACD, key support/resistance levels\n"
                "2. Fundamental: revenue growth, cloud transition progress, margins, PE vs peers (MSFT, SAP, NOW)\n"
                "3. Competitive: OCI vs AWS/Azure/GCP market share, Autonomous DB moat\n"
                "4. AI angle: Oracle's AI infrastructure deals (NVIDIA partnership, xAI, OpenAI cloud)\n"
                "5. Risks: debt load from cloud capex, Cerner integration, FX exposure\n"
                "6. Verdict: Bull/Bear/Neutral with price target and conviction level"
            )
        else:
            prompt = (
                "Give me a quick snapshot of Oracle (ORCL):\n"
                "1. Current price, YTD performance vs S&P500\n"
                "2. Key metrics: PE, forward PE, revenue growth, cloud ARR\n"
                "3. Recent news and catalysts\n"
                "4. Technical signal: Buy/Hold/Sell\n"
                "5. One-line thesis"
            )
        await self.terminal._handle_ai_message(prompt)
    async def cmd_edgar(self, args: str):
        """
        SEC EDGAR 美国上市公司财报与披露查询（完全免费）。
        Usage: /edgar AAPL              — 最近财报列表
               /edgar MSFT filings      — 10-K/10-Q 提交记录
               /edgar TSLA facts        — 财务事实（收入/利润历史）
               /edgar NVDA insider      — 内幕交易披露 (Form 4)
        """
        from aria_cli import _fetch_edgar_data
        parts = args.strip().split()
        if not parts:
            self.context.console.print("  [dim]Usage: /edgar SYMBOL [filings|facts|insider][/dim]" if self.context.has_rich
                         else "Usage: /edgar SYMBOL [filings|facts|insider]")
            return

        symbol = parts[0].upper()
        sub    = parts[1].lower() if len(parts) > 1 else "filings"

        if self.context.has_rich:
            with self.context.console.status(f"[dim]{ui_text(self, f'查询 EDGAR {symbol}', f'Querying EDGAR for {symbol}')}...[/dim]", spinner="dots"):
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _fetch_edgar_data(symbol, sub)
                )
        else:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _fetch_edgar_data(symbol, sub)
            )

        if not result:
            print_error(self.context, f"未找到 {symbol} 的 EDGAR 数据")
            return

        if self.context.has_rich:
            from rich.table import Table
            from rich import box as rich_box
            if sub == "filings":
                table = Table(title=f"[bold]{symbol}[/bold] SEC 财报提交",
                              box=rich_box.SIMPLE, header_style="bold dim")
                table.add_column("类型", width=6)
                table.add_column("日期", width=12)
                table.add_column("链接", style="dim")
                for f in result[:10]:
                    table.add_row(f.get("form",""), f.get("date",""), f.get("url","")[:60])
                self.context.console.print(table)
            elif sub == "facts":
                self.context.console.print(f"  [bold]{symbol}[/bold] 财务摘要:")
                for metric, entries in result.get("metrics", {}).items():
                    if entries:
                        latest = entries[0]
                        self.context.console.print(f"  [dim]{metric}[/dim]  {latest.get('val',0):,.0f}  ({latest.get('end','')})")
            elif sub == "insider":
                self.context.console.print(f"  [bold]{symbol}[/bold] 近期内幕交易 ({len(result)} 条):")
                for f in result[:10]:
                    self.context.console.print(f"  [dim]{f.get('date','')}[/dim]  Form 4")
        else:
            print(f"  {symbol} EDGAR 数据: {len(result) if isinstance(result, list) else 'OK'}")
