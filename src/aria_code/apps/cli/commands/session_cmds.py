"""SessionCommandsMixin — session list/load/save/export commands."""

from __future__ import annotations

from ._ui import print_error

import json

from aria_code.apps.cli.session_export import build_session_export_payload


import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional

def _arrow_select(*args, **kwargs):
    # Re-exported by aria_cli; imported from its own module here.
    from aria_code.ui.picker import arrow_select as fn
    return fn(*args, **kwargs)
def get_model_cfg(*args, **kwargs):
    from ..model_catalog import get_model_cfg as fn
    return fn(*args, **kwargs)

import json
import asyncio
import datetime
import time
import shlex
import sys
import os
from typing import Dict, Any, Optional


class SessionCommandsMixin:
    """Mixin: session list/load/save/export commands."""

    def cmd_sessions(self, args: str):
        keyword = args.strip().lower()
        sessions = self.terminal.session_mgr.list_sessions()
        if keyword:
            sessions = [s for s in sessions if keyword in s["title"].lower()]
        if not sessions:
            msg = f"No sessions matching '{keyword}'" if keyword else "No saved sessions"
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        if self.context.has_rich:
            self.context.console.print()
            header = f"  [bold]Sessions[/bold]  [dim]({len(sessions)} found)[/dim]" if keyword else "  [bold]Sessions[/bold]"
            self.context.console.print(header)
            for i, s in enumerate(sessions, 1):
                updated = s["updated"][:16] if s["updated"] else "-"
                self.context.console.print(f"    [dim]{i}.[/dim] [bold]{s['title']}[/bold]  "
                              f"[dim]{s['id'][:8]}  {s['messages']} msgs  {updated}[/dim]")
            self.context.console.print()
            self.context.console.print("  [dim]Use /load <number> to resume · /sessions <keyword> to search[/dim]")
        else:
            for i, s in enumerate(sessions, 1):
                print(f"  {i}. [{s['id'][:8]}] {s['title']} ({s['messages']} msgs)")

    def cmd_save(self, args: str):
        if not self.terminal.conversation:
            self.context.console.print("[dim]Nothing to save[/dim]" if self.context.has_rich else "Nothing to save")
            return
        sid = self.terminal.session_id
        title = args.strip().strip('"').strip("'") if args.strip() else None
        meta = {}
        if title:
            meta["title"] = title
        self.terminal.session_mgr.save_session(sid, self.terminal.conversation, metadata=meta)
        self.terminal.config["last_session_id"] = sid
        self.context.save_config(self.terminal.config)
        display = f"{title} ({sid[:8]})" if title else f"{sid[:8]}..."
        self.context.console.print(f"[green]Session saved: {display}[/green]" if self.context.has_rich
                      else f"Saved: {display}")

    def cmd_rename(self, args: str):
        """Rename current session."""
        title = args.strip().strip('"').strip("'")
        if not title:
            self.context.console.print("[dim]Usage: /rename <title>[/dim]" if self.context.has_rich else "Usage: /rename <title>")
            return
        sid = self.terminal.session_id
        data = self.terminal.session_mgr.load_session(sid)
        if data:
            meta = data.get("metadata", {})
            meta["title"] = title
            self.terminal.session_mgr.save_session(sid, self.terminal.conversation, metadata=meta)
        else:
            self.terminal.session_mgr.save_session(sid, self.terminal.conversation, metadata={"title": title})
        self.context.console.print(f"[green]Renamed: {title}[/green]" if self.context.has_rich else f"Renamed: {title}")

    def cmd_load(self, args: str):
        session_id = args.strip()
        if not session_id:
            sessions = self.terminal.session_mgr.list_sessions()
            if not sessions:
                self.context.console.print("[dim]No sessions. Usage: /load <session_id>[/dim]" if self.context.has_rich
                              else "No sessions")
                return
            options = []
            for s in sessions[:20]:
                title = s.get("metadata", {}).get("title", s["id"][:8])
                ts = s.get("updated", "")[:10]
                options.append((title, ts))
            choice = _arrow_select(options, selected=0, title="Load Session")
            if 0 <= choice < len(sessions):
                session_id = sessions[choice]["id"]
            else:
                if self.context.has_rich:
                    self.context.console.print("[dim]Cancelled[/dim]")
                else:
                    print("Cancelled")
                return

        data = self.terminal.session_mgr.load_session(session_id)
        if data:
            self.terminal.conversation = data.get("messages", [])
            self.terminal.session_id = data["id"]
            title = data.get("metadata", {}).get("title", "Untitled")
            n = len(self.terminal.conversation)
            self.context.console.print(f"[green]Loaded: {title} ({n} messages)[/green]" if self.context.has_rich
                          else f"Loaded: {title} ({n} msgs)")
        else:
            print_error(self.context, f"Session not found: {session_id}", "session")

    def cmd_recall(self, args: str):
        """Full-text search across all saved sessions: /recall <query>"""
        query = args.strip()
        if not query:
            self.context.console.print("[dim]Usage: /recall <query>[/dim]" if self.context.has_rich else "Usage: /recall <query>")
            return
        results = self.terminal.session_mgr.search_sessions(query)
        if not results:
            msg = f"No sessions found matching '{query}'"
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [bold]Recall[/bold]  [dim]{len(results)} session(s) match '{query}'[/dim]")
            self.context.console.print()
            for r in results[:10]:
                updated = r["updated"][:16] if r["updated"] else ""
                self.context.console.print(
                    f"  [bold]{r['title']}[/bold]  "
                    f"[dim]{r['id'][:8]}  {r['match_count']} hit(s)  {updated}[/dim]"
                )
                preview = r["preview"].replace("\n", " ")[:100]
                self.context.console.print(f"    [dim]…{preview}…[/dim]")
                self.context.console.print()
            self.context.console.print("  [dim]Use /load <id> to resume a session[/dim]")
        else:
            print(f"\n{len(results)} session(s) found:")
            for r in results[:10]:
                print(f"  [{r['id'][:8]}] {r['title']} ({r['match_count']} hits)")
                print(f"    ...{r['preview'][:80]}...")

    async def cmd_export(self, args: str):
        parts = args.split()
        fmt = parts[0].lower() if parts else "json"
        filename = parts[1] if len(parts) > 1 else None

        if not self.terminal.conversation:
            self.context.console.print("[dim]Nothing to export[/dim]" if self.context.has_rich else "Nothing to export")
            return

        try:
            provider_health = []
            doctor_report = None
            mcp_status = None
            context_health = None
            if fmt == "bundle":
                try:
                    from packages.aria_services.provider_health import GLOBAL_PROVIDER_HEALTH
                    provider_health = GLOBAL_PROVIDER_HEALTH.snapshot()
                except Exception:
                    provider_health = []
                # A support bundle should carry environment health and MCP/circuit
                # state — the two things a maintainer asks for first. Best-effort.
                try:
                    from aria_code.doctor import run_doctor
                    doctor_report = run_doctor(self.terminal.config)
                except Exception:
                    doctor_report = None
                try:
                    _reg = getattr(self.terminal, "_mcp_registry", None)
                    mcp_status = _reg.status() if _reg else None
                except Exception:
                    mcp_status = None
                try:
                    from packages.aria_services.context import context_health_snapshot
                    _mc = get_model_cfg(self.terminal.config.get("model", "qwen2.5:7b"))
                    context_health = context_health_snapshot(
                        self.terminal.conversation,
                        max_tokens=int(_mc.get("num_ctx", 16384)),
                        threshold=float(self.terminal.config.get("auto_compact_threshold", 0.78)),
                    )
                except Exception:
                    context_health = None
            content, ext, prefix = build_session_export_payload(
                fmt,
                self.terminal.conversation,
                session_id=self.terminal.session_id,
                config=self.terminal.config,
                trace=getattr(self.terminal, "runtime_trace", None),
                provider_health=provider_health,
                doctor_report=doctor_report,
                mcp_status=mcp_status,
                context_health=context_health,
            )
        except ValueError as exc:
            if fmt == "sft" and "No user→assistant pairs" in str(exc):
                self.context.console.print("[dim]No user→assistant pairs to export[/dim]" if self.context.has_rich else "No pairs to export")
                return
            self.context.console.print("[dim]Format: json, csv, md, sft, or bundle[/dim]" if self.context.has_rich
                          else "Format: json, csv, md, sft, bundle")
            return

        if fmt == "sft":
            pairs = json.loads(content)
            if self.context.has_rich:
                self.context.console.print(f"[dim]{len(pairs)} training pairs extracted[/dim]")
            else:
                print(f"{len(pairs)} training pairs")

        if not filename:
            from datetime import datetime
            filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        self.context.console.print(f"[green]Exported to {filename}[/green]" if self.context.has_rich
                      else f"Exported: {filename}")
