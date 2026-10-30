"""
WorkspaceCommandsMixin — Workspace commands: packages, file, project, init, setup, memory.

Extracted from aria_cli.py. Methods' __globals__ are rebound to aria_cli's namespace
by _rebind_mixin_globals() called at module load time.
"""

from __future__ import annotations

from ._ui import print_error, rich_box
from aria_code.packages.aria_core.paths import aria_home


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

def detect_ollama_models_rich(*args, **kwargs):
    from aria_code.apps.cli.helpers import detect_ollama_models_rich as fn
    return fn(*args, **kwargs)
def _load_project_context(*args, **kwargs):
    from aria_code.apps.cli.helpers import _load_project_context as fn
    return fn(*args, **kwargs)
def _get__HAS_MCP():
    from aria_code.apps.cli._optional import HAS_MCP as val
    return val
def _get_LOCAL_TOOL_SCHEMAS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOL_SCHEMAS as val
    return val
import pathlib
def _get_provider_key(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import get_provider_key as fn
    return fn(*args, **kwargs)
def _get_rich_box():
    # Resolved locally now — see ._ui; no aria_cli round-trip.
    return rich_box
def _arrow_select(*args, **kwargs):
    # Re-exported by aria_cli; taken from its own module here.
    from aria_code.ui.picker import arrow_select as fn
    return fn(*args, **kwargs)
def _get___version__():
    from aria_code.aria_cli import __version__ as val
    return val
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def get_model_capability(*args, **kwargs):
    from aria_code.apps.cli._optional import get_model_capability as fn
    return fn(*args, **kwargs)
def _get_MCP_CONFIG_PATH():
    from aria_code.apps.cli._optional import MCP_CONFIG_PATH as val
    return val
def _get_SESSIONS_DIR():
    from aria_code.aria_cli import SESSIONS_DIR as val
    return val
def _get__HAS_MODEL_CAP():
    from aria_code.apps.cli._optional import HAS_MODEL_CAP as val
    return val
def _strip_markdown_fences(*args, **kwargs):
    from aria_code.aria_cli import _strip_markdown_fences as fn
    return fn(*args, **kwargs)
def _load_data_keys(*args, **kwargs):
    from aria_code.apps.cli.provider_keys import load_data_keys as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class WorkspaceCommandsMixin:
    """Mixin: Workspace commands: packages, file, project, init, setup, memory."""

    async def cmd_packages(self, args: str):
        """Show Aria Code package facades and Arthera package bridge status."""
        try:
            from aria_code.packages.aria_agents import list_agent_manifests
            from aria_code.packages.aria_core import (
                build_package_manifest,
                list_architecture_layers,
                required_architecture_layer_names,
                write_package_manifest,
            )
            from aria_code.packages.aria_infra import (
                aria_code_identity,
                build_package_doctor_report,
                discover_arthera_packages,
            )
            from aria_code.packages.aria_mcp import (
                arthera_quant_engine_server_config,
                default_exposures,
                load_mcp_config,
                merge_server_config,
                mcp_server_status,
                mcp_tools_to_specs,
                write_mcp_config,
            )
            from aria_code.packages.aria_services import (
                list_service_specs,
                list_service_usage_specs,
                required_service_names,
            )
            from aria_code.packages.aria_services.provider_health import GLOBAL_PROVIDER_HEALTH
            from aria_code.packages.aria_skills import builtin_skill_specs
            from aria_code.packages.aria_tools import build_registry_from_legacy
        except Exception as exc:
            print_error(self.context, f"packages facade unavailable: {exc}")
            return

        sub = args.strip().lower()
        identity = aria_code_identity(_get___version__())
        arthera = discover_arthera_packages()
        tool_registry = build_registry_from_legacy(_get_LOCAL_TOOLS(), _get_LOCAL_TOOL_SCHEMAS())
        services = list_service_specs()
        agent_count = len(list_agent_manifests())
        skill_count = len(builtin_skill_specs())
        mcp_exposure_count = len(default_exposures())
        server_cfg = arthera_quant_engine_server_config()
        mcp_config_path = _get_MCP_CONFIG_PATH() if _get__HAS_MCP() else aria_home() / "mcp_servers.json"

        if sub.startswith("export-manifest") or sub.startswith("manifest"):
            raw_parts = args.strip().split(maxsplit=1)
            out_path = None
            if len(raw_parts) > 1:
                out_path = pathlib.Path(raw_parts[1]).expanduser()
            else:
                from aria_code.artifacts import artifact_dir
                out_path = artifact_dir("manifests", "packages") / "aria_package_manifest.json"

            reg = getattr(self.terminal, "_mcp_registry", None)
            mcp_tools = []
            if reg:
                try:
                    mcp_tools = [
                        tool for tool in reg.all_tools()
                        if tool.get("server") == "arthera_quant_engine"
                    ]
                except Exception:
                    mcp_tools = []
            arthera_specs = mcp_tools_to_specs(mcp_tools, "arthera_quant_engine")
            manifest = build_package_manifest(
                identity=identity,
                services=services,
                tools=tool_registry.list(),
                agents=list_agent_manifests(),
                skills=builtin_skill_specs(),
                mcp_exposures=default_exposures(),
                arthera_packages=arthera,
                arthera_mcp_tools=arthera_specs,
            )
            write_package_manifest(out_path, manifest)
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(f"  [green]Package manifest exported[/green] [dim]{out_path}[/dim]")
                self.context.console.print(
                    f"  [dim]tools={len(manifest['capabilities']['tools'])} "
                    f"services={len(manifest['capabilities']['services'])} "
                    f"agents={len(manifest['capabilities']['agents'])} "
                    f"skills={len(manifest['capabilities']['skills'])} "
                    f"arthera_mcp_tools={len(manifest['capabilities']['arthera_mcp_tools'])}[/dim]\n"
                )
            else:
                print(f"Package manifest exported: {out_path}")
            return

        if sub in ("doctor", "doctor arthera", "arthera doctor"):
            reg = getattr(self.terminal, "_mcp_registry", None)
            runtime_status = []
            mcp_tools = []
            if reg:
                try:
                    runtime_status = reg.status()
                    mcp_tools = [
                        tool for tool in reg.all_tools()
                        if tool.get("server") == "arthera_quant_engine"
                    ]
                except Exception:
                    runtime_status = []
                    mcp_tools = []
            status = mcp_server_status(mcp_config_path, "arthera_quant_engine", runtime_status)
            specs = mcp_tools_to_specs(mcp_tools, "arthera_quant_engine")
            from aria_code.artifacts import artifact_dir
            manifest_path = artifact_dir("manifests", "packages", create=False) / "aria_package_manifest.json"
            report = build_package_doctor_report(
                arthera=arthera,
                mcp_status=status,
                tool_count=len(specs),
                manifest_can_export=True,
                manifest_path=manifest_path,
                services=services,
                required_services=required_service_names(),
                architecture_layers=list_architecture_layers(),
                required_architecture_layers=required_architecture_layer_names(),
                provider_health=GLOBAL_PROVIDER_HEALTH.snapshot(),
            )
            if self.context.has_rich:
                from rich.table import Table as _Table
                color = "green" if report.status == "ok" else "yellow" if report.status == "warn" else "red"
                self.context.console.print()
                self.context.console.print(
                    f"  [bold]{identity.product}[/bold] "
                    f"[dim]· {identity.company} packages doctor ·[/dim] [{color}]{report.status}[/{color}]\n"
                )
                tbl = _Table(
                    box=_get_rich_box().ROUNDED,
                    border_style="dim",
                    show_header=True,
                    header_style="bold dim",
                )
                tbl.add_column("Check", width=22)
                tbl.add_column("Status", width=8)
                tbl.add_column("Detail")
                tbl.add_column("Next")
                for check in report.checks:
                    st_color = "green" if check.status == "ok" else "yellow" if check.status == "warn" else "red"
                    tbl.add_row(
                        check.name,
                        f"[{st_color}]{check.status}[/{st_color}]",
                        check.detail,
                        check.remediation,
                    )
                self.context.console.print(tbl)
                self.context.console.print()
            else:
                print(f"{identity.product} packages doctor: {report.status}")
                for check in report.checks:
                    print(f"{check.status:5s} {check.name}: {check.detail} {check.remediation}")
            return

        if sub in ("status", "status arthera", "arthera status"):
            runtime_status = []
            reg = getattr(self.terminal, "_mcp_registry", None)
            if reg:
                try:
                    runtime_status = reg.status()
                except Exception:
                    runtime_status = []
            status = mcp_server_status(mcp_config_path, "arthera_quant_engine", runtime_status)

            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(
                    f"  [bold]{identity.product}[/bold] "
                    f"[dim]· {identity.company} package bridge status[/dim]\n"
                )
                rows = [
                    ("config", status["config_path"]),
                    ("configured", "yes" if status["configured"] else "no"),
                    ("server", status["server_path"] or "—"),
                    ("server file", "found" if status["server_file_exists"] else "missing"),
                    ("runtime", "running" if status["running"] else "not running"),
                    ("tools", str(status["tool_count"])),
                ]
                for key, value in rows:
                    style = "green" if value in ("yes", "found", "running") else "yellow" if value in ("no", "missing", "not running") else "dim"
                    self.context.console.print(f"  [dim]{key:12s}[/dim] [{style}]{value}[/{style}]")
                if status["tools"]:
                    self.context.console.print("  [dim]tool names:[/dim] " + ", ".join(status["tools"][:12]))
                if not status["configured"]:
                    self.context.console.print("  [dim]Run /packages connect arthera to write the MCP bridge.[/dim]")
                elif not status["running"]:
                    self.context.console.print("  [dim]Run /mcp reload or /packages connect arthera --reload.[/dim]")
                self.context.console.print()
            else:
                print(f"{identity.product} · {identity.company} package bridge status")
                for key, value in status.items():
                    if key != "tools":
                        print(f"{key}: {value}")
            return

        if sub in ("tools arthera", "arthera tools", "tools"):
            reg = getattr(self.terminal, "_mcp_registry", None)
            mcp_tools = []
            if reg:
                try:
                    mcp_tools = [
                        tool for tool in reg.all_tools()
                        if tool.get("server") == "arthera_quant_engine"
                    ]
                except Exception:
                    mcp_tools = []
            specs = mcp_tools_to_specs(mcp_tools, "arthera_quant_engine")

            if self.context.has_rich:
                from rich.table import Table as _Table
                self.context.console.print()
                self.context.console.print(
                    f"  [bold]{identity.product}[/bold] "
                    f"[dim]· {identity.company} MCP tool manifests[/dim]\n"
                )
                if not specs:
                    self.context.console.print("  [yellow]No Arthera MCP tools discovered.[/yellow]")
                    self.context.console.print("  [dim]Run /packages connect arthera --reload, then retry /packages tools arthera.[/dim]\n")
                    return
                tbl = _Table(
                    title="[bold]Arthera QuantEngine Tools[/bold]",
                    box=_get_rich_box().ROUNDED,
                    border_style="dim",
                    show_header=True,
                    header_style="bold dim",
                )
                tbl.add_column("Tool", width=34)
                tbl.add_column("Permissions", width=24)
                tbl.add_column("Capabilities", width=30)
                tbl.add_column("Schema")
                for spec in specs:
                    perms = ", ".join(p.value for p in spec.permissions)
                    caps = ", ".join(spec.capabilities)
                    schema_state = "yes" if spec.schema else "no"
                    tbl.add_row(spec.name, perms, caps, schema_state)
                self.context.console.print(tbl)
                self.context.console.print()
            else:
                if not specs:
                    print("No Arthera MCP tools discovered. Run /packages connect arthera --reload.")
                    return
                for spec in specs:
                    print(f"{spec.name}: {', '.join(spec.capabilities)}")
            return

        if sub in ("services", "service", "usage", "use", "map"):
            usage_specs = list_service_usage_specs()
            if self.context.has_rich:
                from rich.table import Table as _Table
                self.context.console.print()
                self.context.console.print(
                    f"  [bold]{identity.product}[/bold] "
                    f"[dim]· service usage map[/dim]\n"
                )
                tbl = _Table(
                    title="[bold]Project Services[/bold]",
                    box=_get_rich_box().ROUNDED,
                    border_style="dim",
                    show_header=True,
                    header_style="bold dim",
                )
                tbl.add_column("Service", width=22)
                tbl.add_column("Purpose", width=34)
                tbl.add_column("CLI", width=32)
                tbl.add_column("Arthera / MCP", width=34)
                tbl.add_column("Next")
                for spec in usage_specs:
                    mcp = ", ".join(spec.mcp_tools[:4]) if spec.mcp_tools else "—"
                    packages = ", ".join(spec.package_sources[:2])
                    tbl.add_row(
                        spec.name,
                        spec.purpose,
                        ", ".join(spec.cli_entrypoints),
                        f"{packages}\n[dim]{mcp}[/dim]",
                        spec.next_step,
                    )
                self.context.console.print(tbl)
                self.context.console.print("[dim]连接 Arthera MCP: /packages connect arthera --reload；券商接入: /broker guide[/dim]\n")
            else:
                for spec in usage_specs:
                    print(f"{spec.name}: {spec.purpose} -> {', '.join(spec.cli_entrypoints)}")
            return

        if sub.startswith("connect arthera") or sub in ("connect", "connect-quant", "connect quant"):
            existing = load_mcp_config(mcp_config_path)
            updated = merge_server_config(existing, server_cfg)
            write_mcp_config(mcp_config_path, updated)
            server_path = pathlib.Path(str(server_cfg["args"][0]))
            ready = server_path.exists()
            if self.context.has_rich:
                self.context.console.print()
                self.context.console.print(
                    f"  [bold]{identity.product}[/bold] "
                    f"[dim]connected to {identity.company} package bridge[/dim]"
                )
                status = "[green]ready[/green]" if ready else "[yellow]configured, server file not found[/yellow]"
                self.context.console.print(f"  {status} [dim]{server_cfg['name']}[/dim]")
                self.context.console.print(f"  [dim]config:[/dim] {mcp_config_path}")
                self.context.console.print(f"  [dim]server:[/dim] {server_path}")
            else:
                print(f"Connected {identity.product} -> {server_cfg['name']}")
                print(f"config: {mcp_config_path}")
                print(f"server: {server_path} ({'ready' if ready else 'missing'})")

            if "--reload" in sub or " reload" in sub:
                await self.cmd_mcp("reload")
            else:
                if self.context.has_rich:
                    self.context.console.print("  [dim]Run /mcp reload to start the server.[/dim]\n")
                else:
                    print("Run /mcp reload to start the server.")
            return

        if self.context.has_rich:
            from rich.table import Table as _Table

            self.context.console.print()
            self.context.console.print(
                f"  [bold]{identity.product}[/bold] "
                f"[dim]v{identity.version} · {identity.company} product[/dim]"
            )
            self.context.console.print(f"  [dim]{identity.description}[/dim]\n")

            tbl = _Table(
                title="[bold]Package Facades[/bold]",
                box=_get_rich_box().ROUNDED,
                border_style="dim",
                show_header=True,
                header_style="bold dim",
            )
            tbl.add_column("Package")
            tbl.add_column("Status")
            tbl.add_column("Surface")
            tbl.add_row("aria_core", "ready", "CapabilityManifest, permissions")
            tbl.add_row("aria_services", "ready", f"{len(services)} service boundaries")
            tbl.add_row("aria_tools", "ready", f"{len(tool_registry.list())} tools")
            tbl.add_row("aria_agents", "ready", f"{agent_count} agents")
            tbl.add_row("aria_skills", "ready", f"{skill_count} skills")
            tbl.add_row("aria_mcp", "ready", f"{mcp_exposure_count} planned exposures")
            tbl.add_row("aria_infra", "ready", "Arthera package discovery")
            self.context.console.print(tbl)

            self.context.console.print()
            self.context.console.print("[bold]Arthera Packages[/bold]")
            from aria_code.ui.render.output import display_path as _display_path
            if arthera.available:
                self.context.console.print(f"  [green]found[/green] [dim]{_display_path(arthera.root, fallback='package root')}[/dim]")
                for name, path in sorted(arthera.packages.items()):
                    self.context.console.print(f"  [dim]·[/dim] [bold]{name:14s}[/bold] [dim]{_display_path(path, fallback='package')}[/dim]")
                if arthera.mcp_servers:
                    self.context.console.print("  [dim]MCP server candidates:[/dim]")
                    for path in arthera.mcp_servers[:5]:
                        self.context.console.print(f"    [dim]{_display_path(path, fallback='server')}[/dim]")
            else:
                self.context.console.print(f"  [yellow]not found[/yellow] [dim]{_display_path(arthera.root, fallback='package root')}[/dim]")

            self.context.console.print()
            self.context.console.print("[bold]Recommended MCP bridge[/bold]")
            self.context.console.print(f"  [dim]name:[/dim] {server_cfg['name']}")
            self.context.console.print(f"  [dim]command:[/dim] {server_cfg['command']} {' '.join(server_cfg['args'])}")
            self.context.console.print(f"  [dim]env PYTHONPATH:[/dim] {server_cfg['env']['PYTHONPATH']}")
            self.context.console.print(f"  [dim]config:[/dim] {mcp_config_path}")
            self.context.console.print("  [dim]Run /packages connect arthera to write this MCP bridge.[/dim]\n")
        else:
            print(f"{identity.product} v{identity.version} · {identity.company} product")
            print(f"services={len(services)} tools={len(tool_registry.list())} agents={agent_count} skills={skill_count} mcp={mcp_exposure_count}")
            print(f"Arthera packages: {'found' if arthera.available else 'not found'} {arthera.root}")
            print(f"Recommended MCP: {server_cfg}")

    async def cmd_file(self, args: str):
        """
        /file load <路径>            — 加载文件到会话
        /file analyze [1|2|3|4]      — 分层分析（1=摘要 2=深度 3=领域 4=建议）
        /file ask <问题>             — 就已加载文件提问
        /file list                   — 列出会话中的所有文件
        /file switch <文件名>        — 切换活跃文件
        /file clear [文件名]         — 清除文件
        /file check                  — 检查可用解析器
        """
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        parts = args.strip().split(None, 1) if args.strip() else []
        sub   = parts[0].lower() if parts else "help"
        rest  = parts[1].strip() if len(parts) > 1 else ""

        # ── 确保 file_session 已初始化 ────────────────────────────────────────
        if self.terminal._file_session is None:
            try:
                from aria_code.file_analysis_tools import FileSession
                self.terminal._file_session = FileSession()
            except ImportError as e:
                if self.context.has_rich:
                    self.context.console.print(f"[red]file_analysis_tools 未加载: {e}[/red]")
                return

        fs = self.terminal._file_session

        # ────────────────── /file load ────────────────────────────────────────
        if sub == "load":
            if not rest:
                if self.context.has_rich:
                    self.context.console.print("[dim]用法: /file load <文件路径>[/dim]")
                    self.context.console.print("[dim]支持: PDF DOCX XLSX CSV JSON TXT MD 图片 代码文件[/dim]")
                return

            if self.context.has_rich:
                self.context.console.print(f"[dim]正在解析 {rest}...[/dim]")

            # Include images only for vision-capable models
            _curr_model = self.terminal.config.get("model", "")
            include_img = False
            if _get__HAS_MODEL_CAP():
                try:
                    _mc = get_model_capability(_curr_model)
                    include_img = bool(_mc.vision)
                except Exception:
                    pass

            from aria_code.file_analysis_tools import parse_file, check_parsers
            fc = await loop.run_in_executor(
                None, lambda: parse_file(rest, include_images=include_img))

            if not fc.success:
                if self.context.has_rich:
                    self.context.console.print(f"[red]解析失败: {fc.error}[/red]")
                    # Show which parsers are available
                    parsers = check_parsers()
                    missing = [k for k, v in parsers.items() if not v]
                    if missing:
                        self.context.console.print(f"[yellow]⚠ 未安装解析器: {', '.join(missing)}[/yellow]")
                        self.context.console.print(f"[dim]安装命令: pip install {' '.join(missing)}[/dim]")
                return

            fs.load(rest, include_images=include_img)
            self.terminal._file_ctx_injected = False  # Reset so next msg injects file

            if self.context.has_rich:
                from rich.panel import Panel as _P
                from rich import box as _box
                info_lines = [
                    f"[green]✓[/green] [bold]{fc.filename}[/bold]",
                    f"[dim]类型: {fc.file_type.upper()}  大小: {fc.size_kb:.1f} KB  "
                    f"提取: {fc.char_count:,} 字符[/dim]",
                ]
                for k, v in fc.metadata.items():
                    if k in ("pages","rows","columns","lines","language",
                             "sheets","title","author","symbols"):
                        val = v[:5] if isinstance(v, list) else v
                        info_lines.append(f"[dim]{k}: {val}[/dim]")
                if fc.truncated:
                    info_lines.append("[yellow]⚠ 内容已截断（文件较大）[/yellow]")
                if fc.tables:
                    info_lines.append(f"[dim]包含 {len(fc.tables)} 个表格[/dim]")
                info_lines.append("\n[dim]发送任何消息即可开始分析，或使用 /file analyze 1-4[/dim]")
                self.context.console.print(_P("\n".join(info_lines),
                                 title="[bold]📄 文件已加载[/bold]",
                                 border_style="green", box=_box.ROUNDED))

        # ────────────────── /file analyze ─────────────────────────────────────
        elif sub == "analyze":
            fc = fs.get_active()
            if not fc:
                if self.context.has_rich: self.context.console.print("[dim]请先使用 /file load <路径> 加载文件[/dim]")
                return

            # Determine layer(s) to run
            layer_arg = rest.strip()
            if layer_arg == "all":
                layers_to_run = [1, 2, 3, 4]
            else:
                try:
                    layers_to_run = [int(layer_arg)] if layer_arg else [1, 2]
                except ValueError:
                    layers_to_run = [1, 2]

            layer_names = {1: "📌 快速摘要", 2: "🔍 深度分析", 3: "💡 领域洞察", 4: "✅ 行动建议"}

            from aria_code.file_analysis_tools import build_analysis_prompt

            for layer in layers_to_run:
                if self.context.has_rich:
                    self.context.console.print(f"\n[bold]{layer_names.get(layer, f'层{layer}')}[/bold]")
                    self.context.console.print(f"[dim]{'─'*50}[/dim]")

                prompt = build_analysis_prompt(fc, layer=layer)
                # Send to LLM via the normal message pipeline
                await self.terminal.send_message(
                    prompt,
                    system_override=(
                        "你是专业文档分析助手，具备金融、法律、技术、不动产等多领域知识。"
                        "分析要精确、结构化，优先使用数字和具体事实。"
                    ),
                )

                if len(layers_to_run) > 1 and layer < layers_to_run[-1]:
                    if self.context.has_rich:
                        self.context.console.print(f"\n[dim]{'═'*60}[/dim]")
                        self.context.console.print("[dim]进入下一层分析...[/dim]\n")

        # ────────────────── /file ask ──────────────────────────────────────────
        elif sub == "ask":
            fc = fs.get_active()
            if not fc:
                if self.context.has_rich: self.context.console.print("[dim]请先使用 /file load <路径> 加载文件[/dim]")
                return
            question = rest.strip()
            if not question:
                if self.context.has_rich:
                    from rich.prompt import Prompt as _Prompt
                    question = _Prompt.ask("  请输入问题")
                else:
                    question = input("请输入问题: ")
            if not question:
                return

            from aria_code.file_analysis_tools import build_analysis_prompt
            prompt = build_analysis_prompt(fc, layer=0, question=question)
            await self.terminal.send_message(
                prompt,
                system_override=(
                    "你是专业文档分析助手。请基于用户提供的文件内容准确回答问题，"
                    "若文件中无法找到答案，请如实说明。"
                ),
            )

        # ────────────────── /file list ─────────────────────────────────────────
        elif sub == "list":
            files = fs.list_files()
            if not files:
                if self.context.has_rich: self.context.console.print("[dim]会话中暂无已加载文件。使用 /file load <路径>[/dim]")
                return
            if self.context.has_rich:
                from rich.table import Table as _T
                from rich import box as _box
                tb = _T(title="[bold]📂 已加载文件[/bold]", box=_box.ROUNDED)
                tb.add_column("状态", width=4); tb.add_column("文件名")
                tb.add_column("类型", style="dim"); tb.add_column("大小KB", justify="right", style="dim")
                tb.add_column("字符数", justify="right", style="dim"); tb.add_column("截断", style="dim")
                for f in files:
                    status = "[green]●[/green]" if f["active"] else "[dim]○[/dim]"
                    tb.add_row(status, f["filename"], f["type"].upper(),
                               str(f["size_kb"]), f"{f['chars']:,}",
                               "[yellow]是[/yellow]" if f["truncated"] else "否")
                self.context.console.print(tb)
                self.context.console.print("[dim]/file ask <问题> 向活跃文件提问[/dim]")

        # ────────────────── /file switch ──────────────────────────────────────
        elif sub == "switch":
            if not rest:
                if self.context.has_rich: self.context.console.print("[dim]用法: /file switch <文件名>[/dim]")
                return
            if fs.set_active(rest):
                fc = fs.get_active()
                self.terminal._file_ctx_injected = False
                if self.context.has_rich:
                    self.context.console.print(f"[green]✓ 已切换到: {fc.filename}[/green]")
            else:
                if self.context.has_rich: self.context.console.print(f"[red]未找到文件: {rest}[/red]")

        # ────────────────── /file clear ──────────────────────────────────────
        elif sub == "clear":
            fs.clear(rest if rest else None)
            self.terminal._file_ctx_injected = False
            msg = f"已清除文件: {rest}" if rest else "已清除所有已加载文件"
            if self.context.has_rich: self.context.console.print(f"[dim]{msg}[/dim]")

        # ────────────────── /file check ───────────────────────────────────────
        elif sub == "check":
            from aria_code.file_analysis_tools import check_parsers
            parsers = check_parsers()
            if self.context.has_rich:
                from rich.table import Table as _T
                from rich import box as _box
                tb = _T(title="[bold]📦 文件解析器状态[/bold]", box=_box.ROUNDED)
                tb.add_column("库"); tb.add_column("状态"); tb.add_column("安装命令", style="dim")
                _CMDS = {
                    "pdfplumber": "pip install pdfplumber",
                    "pypdf":      "pip install pypdf",
                    "python-docx":"pip install python-docx",
                    "pandas":     "pip install pandas",
                    "openpyxl":   "pip install openpyxl",
                    "beautifulsoup4": "pip install beautifulsoup4",
                    "Pillow":     "pip install Pillow",
                }
                for lib, ok in parsers.items():
                    status = "[green]✓ 已安装[/green]" if ok else "[red]✗ 未安装[/red]"
                    tb.add_row(lib, status, "" if ok else _CMDS.get(lib,""))
                self.context.console.print(tb)
                formats_ok = []
                if parsers.get("pdfplumber") or parsers.get("pypdf"):
                    formats_ok.append("PDF")
                if parsers.get("python-docx"):
                    formats_ok.append("Word/DOCX")
                if parsers.get("pandas") and parsers.get("openpyxl"):
                    formats_ok.append("Excel/CSV")
                formats_ok.extend(["JSON", "TXT/MD", "代码文件"])
                self.context.console.print(f"[dim]可解析格式: {', '.join(formats_ok)}[/dim]")

        # ────────────────── /file help ─────────────────────────────────────────
        else:
            if self.context.has_rich:
                self.context.console.print("[bold]📄 /file 文件分析命令[/bold]")
                rows = [
                    ("/file load <路径>",          "加载文件 (PDF/DOCX/XLSX/CSV/JSON/TXT/代码/图片)"),
                    ("/file analyze 1",            "快速摘要 (300字)"),
                    ("/file analyze 2",            "深度内容分析 (结构/要点/异常)"),
                    ("/file analyze 3",            "领域专项分析 (财务/法律/技术/不动产)"),
                    ("/file analyze 4",            "行动建议与风险清单"),
                    ("/file analyze all",          "依次运行 4 层分析"),
                    ("/file ask <问题>",            "就文件内容多轮提问"),
                    ("/file list",                 "查看已加载文件"),
                    ("/file switch <文件名>",       "切换分析目标文件"),
                    ("/file clear",                "清除所有已加载文件"),
                    ("/file check",                "检查已安装的解析器"),
                ]
                from rich.table import Table as _T
                from rich import box as _box
                tb = _T(box=_box.MINIMAL)
                tb.add_column("命令", style="cyan"); tb.add_column("说明", style="dim")
                for cmd, desc in rows:
                    tb.add_row(cmd, desc)
                self.context.console.print(tb)

    async def cmd_project(self, args: str):
        """项目分析 (Claude Code / Codex 风格): /project load|tree|grep|ask|task|status|info <参数>"""
        # 本方法体内三处用到 _T / _box，但导入只写在别的方法里。普通方法的
        # __globals__ 被 _rebind_mixin_globals 换成了 aria_cli 的，本文件模块级
        # 的名字一律看不见——所以必须在方法内自己导入，跟本文件其它方法一致。
        from rich.table import Table as _T
        from rich import box as _box
        try:
            from aria_code.project_tools import ProjectSession, scan_project, format_grep_results
            _HAS_PT = True
        except ImportError:
            _HAS_PT = False

        if not _HAS_PT:
            self.context.console.print("[red]❌ project_tools.py 未找到，请确保文件存在。[/red]")
            return

        parts  = args.strip().split(maxsplit=1)
        sub    = parts[0].lower() if parts else "info"
        rest   = parts[1].strip() if len(parts) > 1 else ""
        ps     = self.terminal._project_session  # type: ignore[attr-defined]

        # ── load ──────────────────────────────────────────────────────────────
        if sub == "load":
            if not rest:
                self.context.console.print("[yellow]用法: /project load <目录路径>[/yellow]")
                return
            from pathlib import Path as _Path
            target = _Path(rest).expanduser().resolve()
            if not target.exists():
                self.context.console.print(f"[red]路径不存在: {target}[/red]")
                return

            self.context.console.print(f"[dim]正在扫描项目: {target} …[/dim]")
            try:
                new_ps = scan_project(str(target), max_files=2000)
            except Exception as e:
                self.context.console.print(f"[red]扫描失败: {e}[/red]")
                return

            self.terminal._project_session = new_ps       # type: ignore[attr-defined]
            self.terminal._project_ctx_injected = False   # type: ignore[attr-defined]

            # Auto-archive project into global memory
            if getattr(self.terminal, "memory_mgr", None):
                try:
                    _ps_s = new_ps.summary()
                    self.terminal.memory_mgr.upsert_project(_ps_s["name"], {
                        "root":     _ps_s["root"],
                        "type":     _ps_s["type"],
                        "languages": _ps_s.get("languages", []),
                    })
                except Exception:
                    pass

            s = new_ps.summary()
            tb = _T(box=_box.ROUNDED, show_header=False, padding=(0, 1))
            tb.add_column("k", style="dim", width=14)
            tb.add_column("v", style="cyan")
            tb.add_row("项目名",   s["name"])
            tb.add_row("路径",     s["root"])
            tb.add_row("类型",     s["type"])
            tb.add_row("语言",     ", ".join(s["languages"][:4]))
            tb.add_row("文件数",   str(s["total_files"]))
            tb.add_row("代码行",   f"{s['total_lines']:,}")
            tb.add_row("总大小",   f"{s['total_size_kb']} KB")
            if s["git"].get("branch"):
                tb.add_row("Git 分支", s["git"]["branch"])
            if s["git"].get("changed_count"):
                tb.add_row("变更文件", str(s["git"]["changed_count"]))
            self.context.console.print("\n[bold]项目已加载 ✓[/bold]")
            self.context.console.print(tb)
            self.context.console.print(f"\n[dim]关键文件: {', '.join(s['key_files'][:6])}[/dim]")
            self.context.console.print("[dim]现在可以直接对话，Aria 将根据项目上下文回答。[/dim]\n")
            return

        # ── 未加载时提示 ──────────────────────────────────────────────────────
        if ps is None:
            self.context.console.print("[yellow]请先加载项目: /project load <目录路径>[/yellow]")
            return

        # ── tree ──────────────────────────────────────────────────────────────
        if sub == "tree":
            depth_arg = rest.strip()
            max_lines = 120
            if depth_arg.isdigit():
                max_lines = int(depth_arg) * 30  # rough approximation
            tree_str = ps.get_tree(max_lines=max_lines)
            self.context.console.print(f"\n[bold]{ps.name}/[/bold]")
            self.context.console.print(f"[dim]{tree_str}[/dim]")
            self.context.console.print(f"\n[dim]共 {ps.stats.get('total_files', 0)} 个文件[/dim]\n")

        # ── grep / search ─────────────────────────────────────────────────────
        elif sub in ("grep", "search"):
            if not rest:
                self.context.console.print("[yellow]用法: /project grep <正则表达式> [glob模式][/yellow]")
                return
            parts2 = rest.split(maxsplit=1)
            pattern = parts2[0]
            glob    = parts2[1] if len(parts2) > 1 else "**/*"
            self.context.console.print(f"[dim]搜索 \"{pattern}\" …[/dim]")
            results = ps.grep(pattern, glob=glob, max_results=60)
            self.context.console.print(format_grep_results(results, pattern))

        # ── status ────────────────────────────────────────────────────────────
        elif sub == "status":
            gi = ps.git_info
            if not gi:
                self.context.console.print("[dim]当前项目不是 Git 仓库[/dim]")
                return
            self.context.console.print(f"\n[bold]Git 状态[/bold] — {ps.name}")
            self.context.console.print(f"  分支: [cyan]{gi.get('branch','?')}[/cyan]  "
                          f"变更: [yellow]{gi.get('changed_count', 0)}[/yellow] 个文件")
            if gi.get("changed_files"):
                for f in gi["changed_files"][:15]:
                    self.context.console.print(f"  [dim]{f}[/dim]")
            if gi.get("recent_commits"):
                self.context.console.print("\n[bold]最近提交:[/bold]")
                for c in gi["recent_commits"][:5]:
                    self.context.console.print(f"  [dim]{c}[/dim]")
            self.context.console.print()

        # ── info ──────────────────────────────────────────────────────────────
        elif sub in ("info", "summary", ""):
            s = ps.summary()
            tb = _T(box=_box.ROUNDED, show_header=False, padding=(0, 1))
            tb.add_column("k", style="dim", width=14)
            tb.add_column("v", style="cyan")
            tb.add_row("项目名",   s["name"])
            tb.add_row("路径",     s["root"])
            tb.add_row("类型",     s["type"])
            tb.add_row("主要语言", ", ".join(s["languages"][:4]))
            tb.add_row("文件数",   str(s["total_files"]))
            tb.add_row("代码行",   f"{s['total_lines']:,}")
            tb.add_row("大小",     f"{s['total_size_kb']} KB")
            if s["git"].get("branch"):
                tb.add_row("Git 分支", s["git"]["branch"])
            self.context.console.print(tb)
            self.context.console.print(f"\n[dim]关键文件: {', '.join(s['key_files'][:8])}[/dim]\n")

        # ── read ──────────────────────────────────────────────────────────────
        elif sub == "read":
            if not rest:
                self.context.console.print("[yellow]用法: /project read <文件路径>[/yellow]")
                return
            ok, content = ps.read_file(rest)
            if not ok:
                self.context.console.print(f"[red]{content}[/red]")
                return
            lang = rest.rsplit(".", 1)[-1] if "." in rest else "text"
            self.context.console.print(f"\n[bold]{rest}[/bold]")
            if self.context.has_rich:
                from rich.syntax import Syntax
                self.context.console.print(Syntax(content, lang, theme="monokai", line_numbers=True,
                                     word_wrap=False))
            else:
                print(content)

        # ── clear ─────────────────────────────────────────────────────────────
        elif sub == "clear":
            self.terminal._project_session = None            # type: ignore[attr-defined]
            self.terminal._project_ctx_injected = False      # type: ignore[attr-defined]
            self.context.console.print("[dim]项目上下文已清除[/dim]")

        # ── ask / task → forward to AI with project context ───────────────────
        elif sub in ("ask", "task"):
            if not rest:
                self.context.console.print(f"[yellow]用法: /project {sub} <问题或任务描述>[/yellow]")
                return
            # Delegate to the AI; project context is injected automatically via send_message
            prefix = "请基于当前项目完成以下任务：\n" if sub == "task" else ""
            await self.terminal.send_message(prefix + rest)  # type: ignore[attr-defined]

        # ── help ─────────────────────────────────────────────────────────────
        else:
            rows = [
                ("/project load <path>",        "加载项目目录，构建文件索引"),
                ("/project tree [depth]",        "显示文件树结构"),
                ("/project grep <pattern>",      "跨文件正则搜索"),
                ("/project read <file>",         "查看项目中的文件内容"),
                ("/project status",              "Git 状态 + 最近提交"),
                ("/project info",                "项目摘要（类型/语言/规模）"),
                ("/project ask <question>",      "向 AI 提问（使用项目上下文）"),
                ("/project task <description>",  "让 AI 执行任务（工具调用模式）"),
                ("/project clear",               "卸载当前项目上下文"),
            ]
            tb = _T(box=_box.MINIMAL)
            tb.add_column("命令", style="cyan")
            tb.add_column("说明", style="dim")
            for cmd, desc in rows:
                tb.add_row(cmd, desc)
            self.context.console.print("\n[bold]/project — 项目分析命令[/bold]\n")
            self.context.console.print(tb)
            self.context.console.print()

    async def cmd_init(self, args: str):
        """Bootstrap an ARIA.md memory file, or scaffold a new project.

        Without arguments: scans the current directory and generates ARIA.md.
        With a template name: creates a fully-runnable project scaffold.

        Usage:
            /init                    — generate ARIA.md for current project
            /init --force            — regenerate even if ARIA.md already exists
            /init list               — list available scaffold templates
            /init quant [dir]        — quantitative strategy project
            /init analysis [dir]     — data analysis project
            /init fastapi [dir]      — FastAPI financial data service
            /init dashboard [dir]    — Plotly Dash interactive dashboard
        """
        global _PROJECT_CONTEXT
        cwd = pathlib.Path.cwd()

        # ── /init <template> — project scaffold ──────────────────────────────
        _tmpl_key = args.strip().lower().split()[0] if args.strip() else ""
        if _tmpl_key == "list":
            rows = [(k, v["desc"]) for k, v in self._SCAFFOLD_TEMPLATES.items()]
            if self.context.has_rich:
                from rich.table import Table as _Table
                t = _Table(box=None, show_header=True, header_style="bold cyan", padding=(0,2))
                t.add_column("模板", style="green")
                t.add_column("说明")
                for k, d in rows:
                    t.add_row(k, d)
                self.context.console.print("\n  [bold]可用脚手架模板[/bold]")
                self.context.console.print(t)
                self.context.console.print("\n  用法: [cyan]/init <模板名> [目录名][/cyan]\n")
            else:
                print("可用模板:"); [print(f"  {k}: {d}") for k, d in rows]
            return

        if _tmpl_key in self._SCAFFOLD_TEMPLATES:
            tmpl = self._SCAFFOLD_TEMPLATES[_tmpl_key]
            # Optional second arg: target directory name
            _args_parts = args.strip().split()
            _target_name = _args_parts[1] if len(_args_parts) > 1 else f"{_tmpl_key}_project"
            _target_path = pathlib.Path(_target_name).expanduser()
            if _target_path.is_absolute():
                target_dir = _target_path
            else:
                from aria_code.artifacts import user_projects_dir as _user_projects_dir
                target_dir = _user_projects_dir() / _target_name
            if target_dir.exists():
                self.context.console.print(f"[yellow]目录已存在: {target_dir}[/yellow]") if self.context.has_rich else print(f"目录已存在: {target_dir}")
            else:
                target_dir.mkdir(parents=True)
            created = self._create_scaffold(target_dir, tmpl)
            if self.context.has_rich:
                from rich.panel import Panel as _SPanel
                from rich import box as _sbox
                lines = "\n".join(f"  [dim]{pathlib.Path(p)}[/dim]" for p in created)
                self.context.console.print(_SPanel(
                    f"[green]✅ 项目脚手架已创建[/green]  [bold]{_target_name}[/bold]\n\n{lines}\n\n"
                    f"[dim]cd \"{target_dir}\" && pip install -r requirements.txt[/dim]",
                    title=f"[bold cyan]/init {_tmpl_key}[/bold cyan]",
                    border_style="cyan",
                    box=_sbox.ROUNDED,
                    padding=(1, 2),
                ))
            else:
                print(f"✅ 创建: {target_dir}"); [print(f"  {p}") for p in created]
            # Optionally generate ARIA.md inside the new project
            try:
                from aria_code.apps.cli.project_aria import build_project_aria_md
                _aria_tgt = target_dir / "ARIA.md"
                if not _aria_tgt.exists():
                    _aria_tgt.write_text(
                        build_project_aria_md(
                            project_name=_target_name,
                            stack=tmpl["desc"],
                            entry="main.py",
                            purpose=f"由 /init {_tmpl_key} 生成的脚手架项目",
                            notes=[
                                "脚手架目录已经就绪，后续功能与服务应继续按模块拆分。",
                            ],
                        ),
                        encoding="utf-8",
                    )
            except Exception:
                pass
            return
        # ── /init [--force] — generate ARIA.md for current project ──────────

        aria_md = cwd / "ARIA.md"
        force = "--force" in args

        if aria_md.exists() and not force:
            msg = "ARIA.md already exists. Use /init --force to regenerate."
            self.context.console.print(f"[yellow]{msg}[/yellow]") if self.context.has_rich else print(msg)
            return

        # Scan for common project signal files
        _SCAN_FILES = [
            "README.md", "README.rst", "README.txt",
            "package.json", "pyproject.toml", "setup.py", "setup.cfg",
            "requirements.txt", "Pipfile", "poetry.lock",
            "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
            "Makefile", "Dockerfile", ".env.example",
            "CLAUDE.md", ".ariarc",
        ]
        snippets, found_files = [], []
        for fname in _SCAN_FILES:
            fp = cwd / fname
            if fp.exists():
                found_files.append(fname)
                try:
                    snippets.append(f"### {fname}\n{fp.read_text(errors='replace')[:1200]}")
                except Exception:
                    pass

        code_exts = {".py", ".ts", ".js", ".go", ".rs", ".java", ".cpp", ".c"}
        code_files = sorted(
            f.name for f in cwd.iterdir()
            if f.is_file() and f.suffix in code_exts
        )[:10]

        scan_summary = "\n\n".join(snippets[:5])

        prompt = (
            f"分析以下项目信息，生成一个 ARIA.md 记忆文件。\n\n"
            f"目录: {cwd}\n"
            f"发现的配置文件: {', '.join(found_files) or '无'}\n"
            f"代码文件: {', '.join(code_files) or '无'}\n\n"
            f"文件内容:\n{scan_summary}\n\n"
            f"请生成符合以下格式的 ARIA.md（只输出文件内容本身，不加任何解释）:\n\n"
            f"# Memory\n\n"
            f"- **Project**: <项目名称>\n"
            f"- **Stack**: <语言/框架>\n"
            f"- **Entry**: <主入口文件>\n"
            f"- **Conventions**: <代码规范或约定>\n"
            f"- **Notes**: <其他重要信息>\n"
        )

        self.context.console.print("[dim]分析项目结构中...[/dim]") if self.context.has_rich else print("Analyzing project...")
        await self.terminal.send_message(prompt)

        # Extract the last assistant response and write to ARIA.md
        if self.terminal.conversation:
            last_ai = next(
                (m["content"] for m in reversed(self.terminal.conversation)
                 if m.get("role") == "assistant"),
                None,
            )
            if last_ai:
                content = _strip_markdown_fences(last_ai).strip()
                # Strip injected market-data blocks (lines starting with ##  📊 or *⚠️*)
                # that the market-data prefetch may have appended to the AI response.
                import re as _re_init
                content = _re_init.sub(
                    r'\n*## 📊.*?(?=\n#|\Z)', '', content, flags=_re_init.DOTALL
                ).strip()
                content = _re_init.sub(r'\n*\*⚠️.*?\*\n*', '\n', content).strip()
                if not content.startswith("# Memory"):
                    content = "# Memory\n\n" + content
                aria_md.write_text(content + "\n", encoding="utf-8")
                _PROJECT_CONTEXT = _load_project_context()
                msg = f"ARIA.md created at {aria_md}"
                self.context.console.print(f"\n[green]{msg}[/green]") if self.context.has_rich else print(f"\n{msg}")

    async def cmd_setup(self, args: str):
        """Guided first-run setup wizard (Open Interpreter style).

        Usage: /setup
        """
        import getpass as _gp

        _is_interactive = sys.stdin.isatty()

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold cyan]━━ Aria Setup Wizard ━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]")
            self.context.console.print()

        # ── Step 1: Detect LOCAL backends only (not cloud LLM providers) ───────
        _LOCAL_BACKENDS_ONLY = {"ollama", "lmstudio", "vllm", "llamacpp", "jan"}
        try:
            from aria_code.local_llm_provider import probe_all_backends, BACKEND_DEFAULTS
            _all_backends = probe_all_backends()
            # Filter to only true local backends — cloud providers appear in Step 3
            backends = {k: v for k, v in _all_backends.items() if k in _LOCAL_BACKENDS_ONLY}
        except ImportError:
            backends = {}

        self.context.console.print("  [bold]Step 1/4 · 本地 Backend[/bold]") if self.context.has_rich else print("Step 1: Local Backends")
        ollama_online = backends.get("ollama", False)
        for name, ok in backends.items():
            icon  = "✅" if ok else "○"
            color = "green" if ok else "dim"
            url   = BACKEND_DEFAULTS.get(name, {}).get("default_url", "") if "BACKEND_DEFAULTS" in dir() else ""
            if self.context.has_rich:
                self.context.console.print(f"  {icon} [{color}]{name:12s}[/{color}] [dim]{url}[/dim]")
            else:
                print(f"  {'✓' if ok else '✗'} {name:12s} {url}")
        self.context.console.print() if self.context.has_rich else print()

        # ── Step 2: Pick default Ollama model (if Ollama online) ────────────
        if ollama_online and _is_interactive:
            self.context.console.print("  [bold]Step 2/4 · 选择默认本地模型[/bold]") if self.context.has_rich else print("Step 2: Default model")
            rich_models, _ = detect_ollama_models_rich(
                self.terminal.config.get("ollama_url", "http://localhost:11434")
            )
            if rich_models:
                model_names = [m["name"] for m in rich_models]
                current_id  = self.terminal.config.get("model", "")
                sel_idx     = next((i for i, n in enumerate(model_names) if n == current_id), 0)
                options     = [(f"  {n}", "") for n in model_names]
                picked      = _arrow_select(options, sel_idx, "选择默认模型")
                if picked is not None:
                    chosen = model_names[picked]
                    self.terminal.config["model"] = chosen
                    self.context.save_config(self.terminal.config)
                    msg = f"✓ 默认模型设为 {chosen}"
                    self.context.console.print(f"  [green]{msg}[/green]") if self.context.has_rich else print(f"  {msg}")
            self.context.console.print() if self.context.has_rich else print()
        else:
            self.context.console.print("  [dim]Step 2/4 · (Ollama 未运行，跳过模型选择)[/dim]") if self.context.has_rich else print("  Skipping model select (Ollama offline)")
            self.context.console.print() if self.context.has_rich else print()

        # ── Step 3: Cloud API keys ───────────────────────────────────────────
        self.context.console.print("  [bold]Step 3/4 · Cloud API Key 配置[/bold]") if self.context.has_rich else print("Step 3: Cloud API Keys")
        _SETUP_PROVIDERS = [
            ("deepseek",  "DeepSeek",  "推荐：deepseek-chat，性价比最高"),
            ("openai",    "OpenAI",    "GPT-4o，o1等"),
            ("groq",      "Groq",      "免费 llama/mixtral 推理，极速"),
            ("anthropic", "Anthropic", "Claude 3.5/3.7"),
        ]
        for prov, label, desc in _SETUP_PROVIDERS:
            existing_key = _get_provider_key(prov)
            if existing_key:
                masked = existing_key[:6] + "****" + existing_key[-4:]
                self.context.console.print(f"  🔑 {label:12s} [dim]已配置 ({masked})[/dim]") if self.context.has_rich else print(f"  {label}: 已配置")
                continue
            if _is_interactive:
                self.context.console.print(f"  [cyan]{label}[/cyan] [dim]({desc})[/dim]") if self.context.has_rich else print(f"  {label}: {desc}")
                try:
                    key = _gp.getpass(f"  Enter {label} API key (留空跳过): ").strip()
                except Exception:
                    key = ""
                if key:
                    self.cmd_apikey(f"set {prov} {key}")
            else:
                self.context.console.print(f"  ○ {label:12s} [dim]未配置  → /apikey set {prov} <key>[/dim]") if self.context.has_rich else print(f"  {label}: not configured")
        self.context.console.print() if self.context.has_rich else print()

        # ── Step 3.5: Data Service API keys ──────────────────────────────────
        self.context.console.print("  [bold]Step 3.5/4 · 市场数据服务 Key（后端离线时使用）[/bold]") if self.context.has_rich else print("Step 3.5: Data Service Keys")
        _SETUP_DATA = [
            ("finnhub",      "Finnhub",      "股票实时行情+新闻",     "https://finnhub.io/register"),
            ("newsapi",      "NewsAPI",       "财经新闻聚合",          "https://newsapi.org/register"),
            ("brave",        "Brave Search",  "网页搜索",             "https://api.search.brave.com/app/keys"),
            ("alphavantage", "Alpha Vantage", "股票历史数据",          "https://www.alphavantage.co/support/#api-key"),
        ]
        _existing_data = _load_data_keys()
        for svc, label, desc, signup_url in _SETUP_DATA:
            existing_key = _existing_data.get(svc, "")
            if existing_key:
                masked = existing_key[:6] + "****" + existing_key[-4:]
                self.context.console.print(f"  🔑 {label:16s} [dim]已配置 ({masked})[/dim]") if self.context.has_rich else print(f"  {label}: configured")
                continue
            if _is_interactive:
                self.context.console.print(f"  [cyan]{label}[/cyan] [dim]({desc})[/dim]") if self.context.has_rich else print(f"  {label}: {desc}")
                self.context.console.print(f"  [dim]注册：{signup_url}[/dim]") if self.context.has_rich else print(f"  Register: {signup_url}")
                try:
                    key = _gp.getpass(f"  Enter {label} API key (留空跳过): ").strip()
                except Exception:
                    key = ""
                if key:
                    self.cmd_apikey(f"set {svc} {key}")
            else:
                if self.context.has_rich:
                    self.context.console.print(f"  ○ {label:16s} [dim]未配置  → /apikey set {svc} <key>[/dim]")
                    self.context.console.print(f"    [dim]注册：{signup_url}[/dim]")
                else:
                    print(f"  {label}: not configured  → /apikey set {svc} <key>")
        self.context.console.print() if self.context.has_rich else print()

        # ── Step 3.8: MCP servers ────────────────────────────────────────────
        sub = args.strip().lower()
        if sub in ("mcp", "all"):
            self.context.console.print("  [bold]Step 3.8/4 · MCP 服务器[/bold]") if self.context.has_rich else print("Step 3.8: MCP Servers")
            _mcp_cfg_path = aria_home() / "mcp_servers.json"
            if _mcp_cfg_path.exists():
                try:
                    import json as _j2
                    _mcp_data = _j2.loads(_mcp_cfg_path.read_text())
                    _servers = _mcp_data.get("servers", [])
                    enabled_srv = [s for s in _servers if s.get("enabled", False)]
                    disabled_srv = [s for s in _servers if not s.get("enabled", True)]
                    for s in enabled_srv:
                        self.context.console.print(f"  ✅ {s['name']:16s} [dim]{s.get('description','')[:50]}[/dim]") if self.context.has_rich else print(f"  ✓ {s['name']}")
                    for s in disabled_srv:
                        note = s.get("_setup", "")
                        self.context.console.print(f"  ○  {s['name']:16s} [dim]{s.get('description','')[:50]}[/dim]") if self.context.has_rich else print(f"  ✗ {s['name']}")
                        if note:
                            self.context.console.print(f"     [dim]安装: {note}[/dim]") if self.context.has_rich else print(f"     Setup: {note}")
                    if disabled_srv:
                        self.context.console.print() if self.context.has_rich else print()
                        self.context.console.print("  [dim]安装后编辑 ~/.arthera/mcp_servers.json 将对应项 enabled 改为 true[/dim]") if self.context.has_rich else print("  Edit mcp_servers.json: set enabled=true after installing")
                except Exception:
                    pass
            self.context.console.print() if self.context.has_rich else print()

        # ── Step 4: Messaging channels (Feishu / Telegram) ──────────────────
        if sub in ("feishu", "telegram", "notify", "all", ""):
            self.context.console.print("  [bold]Step 4/5 · 消息通知连接[/bold]") if self.context.has_rich else print("Step 4: Messaging")
            _env_path = pathlib.Path.home() / ".aria" / ".env"
            _env_vars: dict = {}
            if _env_path.exists():
                for _line in _env_path.read_text().splitlines():
                    if "=" in _line and not _line.startswith("#"):
                        k, _, v = _line.partition("=")
                        _env_vars[k.strip()] = v.strip()

            # Feishu status
            _fs_mode  = _env_vars.get("ARIA_RELAY_MODE", "")
            _fs_id    = _env_vars.get("ARIA_RELAY_CLIENT_ID", "")
            _fs_app   = _env_vars.get("FEISHU_APP_ID", "")
            if _fs_mode == "relay" and _fs_id:
                _fs_status = f"[green]✓ 中继模式[/green]  ID: {_fs_id[:12]}…"
            elif _fs_mode == "own_app" and _fs_app:
                _fs_status = f"[green]✓ 自建应用[/green]  {_fs_app}"
            else:
                _fs_status = "[dim]未配置[/dim]  → /setup feishu"

            # Telegram status
            _tg_token = _env_vars.get("TELEGRAM_BOT_TOKEN", "")
            _tg_ids   = _env_vars.get("TELEGRAM_ALLOWED_IDS", "")
            if _tg_token and _tg_token != "your_bot_token_here":
                _tg_status = f"[green]✓ 已配置[/green]  Chat IDs: {_tg_ids or '(未设置)'}"
            else:
                _tg_status = "[dim]未配置[/dim]  → /setup telegram"

            if self.context.has_rich:
                self.context.console.print(f"  飞书  {_fs_status}")
                self.context.console.print(f"  Telegram  {_tg_status}")
            else:
                print(f"  Feishu: {_fs_mode or 'not configured'}")
                print(f"  Telegram: {'configured' if _tg_token else 'not configured'}")

            # Sub-command: launch wizard for just this channel
            if sub in ("feishu", "telegram"):
                self.context.console.print() if self.context.has_rich else print()
                try:
                    # setup_wizard 是随包发布的顶层模块（见 pyproject py-modules），
                    # 直接 import 即可。原先用 __file__ 往上数四层拼路径：这个方法
                    # 的 __globals__ 被 _rebind_mixin_globals 换成了 aria_cli 的，
                    # __file__ 因此指向仓库根的 aria_cli.py 而非本文件，四层 parent
                    # 算出 /Users/setup_wizard.py，永远不存在——异常被下面的
                    # except 吞掉，这条分支一直静默退化成"请手动运行"提示。
                    from aria_code import setup_wizard as _wiz
                    _e = _wiz._load_env()
                    if sub == "feishu":
                        _wiz.setup_feishu(_e)
                    else:
                        _wiz.setup_telegram(_e)
                    _wiz._save_env(_e)
                except Exception as _we:
                    _fallback_flag = "--feishu" if sub == "feishu" else "--telegram"
                    if self.context.has_rich:
                        self.context.console.print(f"  [yellow]请运行: python3 -m aria_code.setup_wizard {_fallback_flag}[/yellow]")
                    else:
                        print(f"  Run: python3 -m aria_code.setup_wizard {_fallback_flag}")
                return

            self.context.console.print() if self.context.has_rich else print()

        # ── Step 5: Summary ─────────────────────────────────────────────────
        self.context.console.print("  [bold]Step 5/5 · 配置完成[/bold]") if self.context.has_rich else print("Step 5: Done")
        model = self.terminal.config.get("model", "?")
        provider = self.terminal.config.get("local_provider", "ollama")
        self.context.console.print(f"  模型: [cyan]{model}[/cyan]  Provider: [cyan]{provider}[/cyan]") if self.context.has_rich else print(f"  Model: {model}  Provider: {provider}")
        self.context.console.print()  if self.context.has_rich else print()
        self.context.console.print(
            "  [dim]提示: /model — 切换模型   /providers — 查看所有 provider\n"
            "        /setup feishu — 配置飞书   /setup telegram — 配置 Telegram[/dim]"
        ) if self.context.has_rich else print("  Tip: /model  /providers  /setup feishu  /setup telegram")
        self.context.console.print("[bold cyan]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold cyan]") if self.context.has_rich else print("─" * 50)
        self.context.console.print() if self.context.has_rich else print()

    def cmd_memory(self, args: str):
        """Manage persistent memory: project ARIA.md and global user profile.

        Usage:
            /memory show             — display current project ARIA.md
            /memory add <fact>       — append fact to project ARIA.md
            /memory clear            — wipe project ARIA.md memory section
            /memory search <query>   — search across ARIA.md + sessions
            /memory profile          — show global 当前账户的 ARIA.md (injected every session)
            /memory profile add <text>  — append to global profile
            /memory profile clear    — reset global profile
            /memory global           — legacy global Memory entries
        """
        global _PROJECT_CONTEXT
        aria_md = pathlib.Path.cwd() / "ARIA.md"
        parts = args.strip().split(maxsplit=1)
        sub  = parts[0].lower() if parts else "show"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in {"sync", "status", "account", "forget"}:
            from aria_code.account_memory import AccountMemory
            memory = AccountMemory(self.terminal.config, save=self.context.save_config)
            if sub == "sync":
                result = memory.sync(force=True)
                message = "账户记忆已同步。" if result.get("ok") else "账户记忆未同步：请检查登录状态或网络，待同步内容仍保留。"
                if result.get("ok") and result.get("pending"):
                    message = f"本次同步已完成，仍有 {result['pending']} 条待同步。"
                if result.get("enabled") is False:
                    message = "账户记忆已关闭，未上传或使用记忆。"
            elif sub == "forget":
                message = "已标记删除，下次同步会更新云端。" if memory.forget(rest) else "请先运行 /login。"
            elif sub == "account":
                if rest.startswith("add "):
                    from aria_code.memory_manager import _SENSITIVE_PATTERN
                    fact = rest[4:].strip()
                    saved = bool(fact) and not _SENSITIVE_PATTERN.search(fact) and memory.add(fact)
                    message = "已保存到当前账户的待同步记忆。" if saved else "未保存：请检查登录、记忆开关或内容。"
                else:
                    facts = memory.facts()
                    message = "\n".join(f"{f['id']}  {f['content']}" for f in facts) or "当前账户暂无记忆。"
            else:
                status = memory.status()
                sync_label = {"sign-in-required":"需要重新登录", "service-unavailable":"服务暂不可用，请稍后重试", "pending-retry":"部分记忆待重试"}.get(status['error'], '部分记忆待同步' if status['pending'] else ('已同步' if status['last_synced'] else '尚未同步'))
                message = (f"账户：{status['email'] or ('已登录账户' if status['account_id'] else '未登录')}\n"
                           f"记忆：{'开启' if status['enabled'] else '关闭'} · 待同步：{status['pending']}\n"
                           f"同步状态：{sync_label}")
            # Memory is user-authored content, never interpret Rich markup in it.
            print(message)
            return

        if sub == "show":
            if not aria_md.exists():
                msg = f"No ARIA.md in {pathlib.Path.cwd()}"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)
                return
            content = aria_md.read_text(encoding="utf-8")
            if self.context.has_rich:
                try:
                    from rich.markdown import Markdown as _RMd
                    self.context.console.print(_RMd(content))
                except Exception:
                    self.context.console.print(content)
            else:
                print(content)

        elif sub == "add":
            if not rest:
                self.context.console.print("[dim]Usage: /memory add <fact>[/dim]") if self.context.has_rich else print("Usage: /memory add <fact>")
                return
            self.cmd_note(rest)

        elif sub == "clear":
            if aria_md.exists():
                aria_md.write_text("# Memory\n\n", encoding="utf-8")
                _PROJECT_CONTEXT = _load_project_context()
                self.context.console.print("[dim]Memory cleared.[/dim]") if self.context.has_rich else print("Memory cleared.")
            else:
                self.context.console.print("[dim]Nothing to clear.[/dim]") if self.context.has_rich else print("Nothing to clear.")

        elif sub == "search":
            # Semantic search in ARIA.md and strategy vault using simple grep
            # (ChromaDB RAG upgrade planned for Phase 2)
            if not rest:
                self.context.console.print("[dim]Usage: /memory search <query>[/dim]") if self.context.has_rich else print("Usage: /memory search <query>")
                return
            query_low = rest.lower()
            results = []
            # 1. Search ARIA.md
            if aria_md.exists():
                for line in aria_md.read_text(encoding="utf-8").splitlines():
                    if query_low in line.lower() and line.strip():
                        results.append(("ARIA.md", line.strip()))
            # 2. Search session history titles
            for sess_file in sorted(_get_SESSIONS_DIR().glob("*.json"), key=lambda p: -p.stat().st_mtime)[:20]:
                try:
                    sess = json.loads(sess_file.read_text(encoding="utf-8"))
                    title = sess.get("metadata", {}).get("title", "")
                    if query_low in title.lower():
                        results.append(("Session", title[:80]))
                except Exception:
                    pass
            # 3. Search strategy vault
            try:
                from aria_code.strategy_vault import get_vault as _gv
                vault = _gv()
                for s in (vault.list() or []):
                    name = str(s.get("name", ""))
                    msg  = str(s.get("message", ""))
                    if query_low in name.lower() or query_low in msg.lower():
                        results.append(("Strategy", f"{name}: {msg[:60]}"))
            except Exception:
                pass

            if results:
                if self.context.has_rich:
                    self.context.console.print()
                    self.context.console.print(f"  [bold]记忆搜索: '{rest}'[/bold]  [dim]{len(results)} 条结果[/dim]")
                    self.context.console.print()
                    for src, text in results[:15]:
                        self.context.console.print(f"  [dim]{src:<12s}[/dim]  {text}")
                    self.context.console.print()
                else:
                    print(f"  Search '{rest}': {len(results)} results")
                    for src, text in results[:15]:
                        print(f"  [{src}] {text}")
            else:
                msg = f"未找到与 '{rest}' 相关的记忆"
                self.context.console.print(f"[dim]{msg}[/dim]") if self.context.has_rich else print(msg)

        elif sub == "profile":
            # Per-user ARIA.md at 当前账户的 ARIA.md — injected into every session
            from aria_code.account_session import profile_path
            _profile_path = profile_path(self.terminal.config)
            gparts = rest.strip().split(maxsplit=1)
            gsub   = gparts[0].lower() if gparts else "show"
            grest  = gparts[1].strip() if len(gparts) > 1 else ""

            if gsub == "show":
                if not _profile_path.exists():
                    if self.context.has_rich:
                        self.context.console.print("[dim]当前账户的 ARIA.md 还不存在。用 /memory profile add <内容> 创建。[/dim]")
                    else:
                        print("当前账户的 ARIA.md not found. Use /memory profile add <text> to create.")
                    return
                content = _profile_path.read_text(encoding="utf-8")
                if self.context.has_rich:
                    try:
                        from rich.markdown import Markdown as _RMd3
                        self.context.console.print()
                        self.context.console.print("  [dim]当前账户的 ARIA.md[/dim]")
                        self.context.console.print(_RMd3(content))
                    except Exception:
                        self.context.console.print(content)
                else:
                    print(content)

            elif gsub == "add":
                if not grest:
                    self.context.console.print("[dim]Usage: /memory profile add <内容>[/dim]") if self.context.has_rich else print("Usage: /memory profile add <text>")
                    return
                _profile_path.parent.mkdir(parents=True, exist_ok=True)
                now_str = datetime.now().strftime("%Y-%m-%d")
                if _profile_path.exists():
                    existing = _profile_path.read_text(encoding="utf-8")
                    if "## 偏好与背景" not in existing and "## Preferences" not in existing:
                        existing += "\n\n## 偏好与背景\n"
                    existing += f"\n- [{now_str}] {grest}"
                    _profile_path.write_text(existing, encoding="utf-8")
                else:
                    _profile_path.write_text(
                        f"# 用户背景\n\n## 偏好与背景\n\n- [{now_str}] {grest}\n",
                        encoding="utf-8",
                    )
                from aria_code.account_memory import AccountMemory
                from aria_code.memory_manager import _SENSITIVE_PATTERN
                if not _SENSITIVE_PATTERN.search(grest):
                    AccountMemory(self.terminal.config).add(grest)
                # Refresh project context so change takes effect immediately
                _PROJECT_CONTEXT = _load_project_context()
                if self.context.has_rich:
                    self.context.console.print("  [dim]✓ 已写入 当前账户的 ARIA.md — 下次对话自动注入[/dim]")
                else:
                    print("Saved to 当前账户的 ARIA.md")

            elif gsub == "clear":
                if _profile_path.exists():
                    _profile_path.write_text("# 用户背景\n\n", encoding="utf-8")
                    _PROJECT_CONTEXT = _load_project_context()
                    self.context.console.print("[dim]当前账户的 ARIA.md 已清空。[/dim]") if self.context.has_rich else print("Profile cleared.")
                else:
                    self.context.console.print("[dim]文件不存在，无需清空。[/dim]") if self.context.has_rich else print("Nothing to clear.")

            else:
                if self.context.has_rich:
                    self.context.console.print("[dim]Usage: /memory profile [show|add <内容>|clear][/dim]")
                else:
                    print("Usage: /memory profile [show|add <text>|clear]")

        elif sub == "global":
            # Global user memory (cross-project, cross-session)
            if not self.memory_mgr:
                self.context.console.print("[dim]Memory manager not available.[/dim]") if self.context.has_rich else print("Memory manager not available.")
                return
            gparts = rest.strip().split(maxsplit=1)
            gsub   = gparts[0].lower() if gparts else "show"
            grest  = gparts[1].strip() if len(gparts) > 1 else ""

            if gsub == "show":
                entries = self.memory_mgr.list_all()
                if not entries:
                    self.context.console.print("[dim]全局 Memory 为空。用 /memory global add <内容> 添加。[/dim]") if self.context.has_rich else print("Global memory is empty.")
                    return
                if self.context.has_rich:
                    from rich.markdown import Markdown as _RMd2
                    for e in entries:
                        self.context.console.print(f"\n[bold cyan]{e['title']}[/bold cyan]  [dim]{e['file']}[/dim]")
                        self.context.console.print(_RMd2(e["content"]) if e["content"] else "[dim](empty)[/dim]")
                else:
                    for e in entries:
                        print(f"\n## {e['title']}\n{e['content']}")

            elif gsub == "add":
                if not grest:
                    self.context.console.print("[dim]Usage: /memory global add <内容>[/dim]") if self.context.has_rich else print("Usage: /memory global add <content>")
                    return
                saved = self.memory_mgr.append("user_profile", grest, title="User Profile")
                print("已保存记忆；账户记忆将在下次对话或 /memory sync 时同步。" if saved is not False else "未保存：请检查记忆开关和内容。")

            elif gsub == "clear":
                n = self.memory_mgr.clear_all()
                print(f"已清除当前记忆（{n} 项）；云端删除将在下次同步时完成。")

            else:
                self.context.console.print("[dim]Usage: /memory global [show|add <内容>|clear][/dim]") if self.context.has_rich else print("Usage: /memory global [show|add|clear]")

        else:
            if self.context.has_rich:
                self.context.console.print("[dim]Usage: /memory [show|add <fact>|clear|search <query>|profile|global][/dim]")
                self.context.console.print("[dim]       /memory profile add <内容>  — 写入全局用户背景（每次会话自动注入）[/dim]")
            else:
                print("Usage: /memory [show|add <fact>|clear|search <query>|profile|global]")
