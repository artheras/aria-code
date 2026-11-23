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
        return cmd in self.commands or cmd in self.skill_map or self._workflow_named(cmd)
    def _workflow_named(self, cmd: str) -> bool:
        """``/release`` when ``.aria/workflows/release.yaml`` exists here."""
        try:
            from aria_code.runtime.workflows import names
            root = self.terminal.config.get("_session_workspace_root") or os.getcwd()
            return cmd.lstrip("/") in names(root)
        except Exception:
            return False
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
        elif self._workflow_named(cmd_name):
            # After commands and skills: a repository file never shadows a
            # built-in. /workflow run <name> reaches it either way.
            await self.cmd_workflow(f"run {cmd_name.lstrip('/')} {args}".rstrip())
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
    def cmd_impact(self, args: str):
        """/impact <path|symbol> [...] — dependents, tests and services of a change."""
        targets = shlex.split(args)
        if not targets:
            msg = "Usage: /impact <path|symbol> [...]   e.g. /impact src/auth/session.py Session.refresh"
            self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
            return
        from aria_code.runtime.project_graph import load_project_graph

        root = self.terminal.config.get("_session_workspace_root") or os.getcwd()
        text = load_project_graph(root).impact(targets).render()
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
        """Canva Connect moved to external MCP plugin."""
        if self.context.has_rich:
            self.context.console.print("[dim]/canva 已迁移至外部 MCP 插件。[/dim]")
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

        if sub == "finance" or sub.startswith("finance "):
            import shlex
            from pathlib import Path
            from aria_code.packages.aria_mcp.config import install_financial_service_config
            try:
                parts = shlex.split(args.strip(), posix=True)
                include_trading = "--with-trading" in parts
                clean_parts = [p for p in parts if p != "--with-trading"]
                if len(clean_parts) != 3 or clean_parts[:2] != ["finance", "install"]:
                    self.context.console.print(
                        'Install financial-service in its own environment, then run: '
                        '/mcp finance install "/absolute/path/to/service-venv/bin/python" [--with-trading]')
                    return
                target = install_financial_service_config(
                    Path(MCP_CONFIG_PATH),
                    Path(clean_parts[2]),
                    include_trading=include_trading,
                )
                self.context.console.print(
                    f"Financial MCP configured at {target}. Networking is off. Run /mcp reload to connect.")
            except (OSError, ValueError) as exc:
                self.context.console.print(f"Financial MCP configuration was not changed: {exc}")
            return

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
        msg = "Native runtime has no entitlements; all features are unrestricted."
        if self.context.has_rich:
            self.context.console.print(f"  [green]{msg}[/green]")
        else:
            print(f"  {msg}")
    def cmd_ariarc(self, args: str):
        """Show or reload .ariarc project configuration."""
        from aria_cli import _HAS_ARIARC, get_ariarc, reload_ariarc
        if not _HAS_ARIARC:
            self.context.console.print("  [dim]ariarc.py not available[/dim]" if self.context.has_rich else "ariarc not available")
            return
        arc = reload_ariarc() if "reload" in args.lower() else (self.terminal.ariarc or get_ariarc())
        if "reload" in args.lower():
            self.terminal.ariarc = arc
        if not arc.found:
            msg = "No .ariarc found in current directory tree (prefer ARIA.md and .aria/settings.json)."
            self.context.console.print(f"  [dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        import json as _j
        text = _j.dumps(arc.to_dict(), indent=2, ensure_ascii=False)
        self.context.console.print(text) if self.context.has_rich else print(text)

    def cmd_recommend(self, args: str):
        """Recommend local models（已迁移至 /model）."""
        if self.context.has_rich:
            self.context.console.print("[dim]请使用 /model 查看和切换本地/云端模型。[/dim]")


_MIGRATED_ASYNC_CMDS = {
    "cmd_risk": "/risk 已迁移至 financial-service MCP（financial_risk_metrics）。",
    "cmd_chart": "/chart 已迁移至 financial-service MCP。",
    **{
        f"cmd_{name}": f"/{name.replace('_', '-')} 已迁移至 Arthera 后端 / MCP 服务。"
        for name in (
            "market", "optimize", "stress", "factors", "factor_lab", "execution",
            "stat_arb", "compliance", "signal", "predict", "cloudbt", "insights",
            "optimize_port", "ui", "dashboard", "tv", "shortterm", "longterm",
            "indices", "hot", "ta", "orcl", "edgar",
        )
    },
}


def _make_migrated_stub(message: str):
    async def _stub(self, args: str):
        if self.context.has_rich:
            self.context.console.print(f"[dim]{message}[/dim]")
    return _stub


for _cmd_name, _cmd_msg in _MIGRATED_ASYNC_CMDS.items():
    setattr(CoreCommandsMixin, _cmd_name, _make_migrated_stub(_cmd_msg))
