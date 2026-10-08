"""DiagnosticCommandsMixin — runtime/status/trace/health commands."""

from __future__ import annotations

import json
import os


import json
import asyncio
import datetime
import time
import shlex
from typing import Dict, Any, Optional


def _health_targets(config: dict, api_url: str) -> tuple[list[tuple[str, str, str]], str]:
    """Probe only the service used by the selected chat route."""
    from aria_code.apps.cli.providers.chat_routing import model_provider

    model = str(config.get("model") or "")
    provider = model_provider(model)
    cloud_model = bool(provider and provider not in {"ollama", "lmstudio"}) or model.lower().startswith("gemini")
    if config.get("backend_chat"):
        label = "Google Cloud · Arthera API" if cloud_model else "Arthera API"
        return [(label, api_url, "/health")], ""
    if cloud_model:
        return [], f"{model} · {provider or 'google'} configured (not probed)"
    local_provider = str(config.get("local_provider") or "ollama").lower()
    if local_provider == "ollama":
        return [("Ollama", config.get("ollama_url", "http://localhost:11434"), "/api/tags")], ""
    if local_provider in {"local", "local-server", "server"}:
        return [("Local Server", config.get("local_url", "http://localhost:8001"), "/health")], ""
    return [], f"{local_provider} configured (not probed)"

def _get_ARIA_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import ARIA_TOOLS as val
    return val
def get_model_cfg(*args, **kwargs):
    from ..model_catalog import get_model_cfg as fn
    return fn(*args, **kwargs)
def _get_MODELS():
    from aria_code.apps.cli.model_catalog import MODELS as val
    return val
def _get_LOCAL_TOOLS():
    # Owned by apps/cli/tool_registry.py; aria_cli fills it in place.
    from ..tool_registry import LOCAL_TOOLS as val
    return val
def _get_SKILLS():
    from aria_code.apps.cli.skills_catalog import SKILLS as val
    return val
def _get_Syntax():
    from ._ui import Syntax as val
    return val
def _get__SYNTAX_THEME():
    from aria_cli import _SYNTAX_THEME as val
    return val

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


class DiagnosticCommandsMixin:
    """Mixin providing runtime diagnostics commands."""

    async def cmd_status(self, args: str):
        """Runtime status panel: engine · tools · model · context · risk"""
        t = self.terminal
        cfg = t.config
        model_id = cfg.get("model", "qwen2.5:7b")
        tool_count = len(_get_ARIA_TOOLS()) + len(_get_LOCAL_TOOLS())
        skill_count = len(_get_SKILLS())

        _lp = t._last_provider or ""
        _badge = next((v.get("badge", "") for v in _get_MODELS().values() if v["id"] == model_id), "")
        from aria_code.apps.cli.providers.chat_routing import model_provider
        selected_provider = model_provider(model_id)
        if cfg.get("backend_chat"):
            runtime = "cloud (Arthera API)"
        elif selected_provider and selected_provider not in {"ollama", "lmstudio"}:
            runtime = f"cloud ({selected_provider})"
        elif model_id.lower().startswith("gemini"):
            runtime = "cloud (google)"
        elif _lp == "ollama":
            runtime = "local (Ollama)"
        elif _lp in ("deepseek", "openai", "anthropic", "groq", "dashscope", "together"):
            runtime = f"cloud ({_lp})"
        elif _badge == "Cloud" or "cloud" in model_id.lower():
            runtime = "cloud"
        else:
            runtime = "local" if getattr(t, "_ollama_alive", False) else "unknown"

        conv = t.conversation
        est_tok = sum(len(m.get("content", "")) for m in conv) // 3
        max_ctx = get_model_cfg(model_id).get("num_ctx", 16384)
        ctx_pct = min(100, int(est_tok / max_ctx * 100))
        auto_compact = bool(cfg.get("auto_compact_context", True))
        try:
            auto_compact_threshold = float(cfg.get("auto_compact_threshold", 0.78))
        except Exception:
            auto_compact_threshold = 0.78
        auto_compact_runs = int(getattr(t, "_auto_compact_count", 0) or 0)
        provider_summary = None
        try:
            from packages.aria_services.provider_health import GLOBAL_PROVIDER_HEALTH
            provider_summary = GLOBAL_PROVIDER_HEALTH.summary()
        except Exception:
            provider_summary = None

        mk = next((k for k, v in _get_MODELS().items() if v["id"] == model_id), None)
        model_display = _get_MODELS()[mk]["name"] if mk else model_id

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]Runtime Status[/bold]")
            self.context.console.print()
            rows = [
                ("runtime", runtime),
                ("model", model_display),
                ("engine", "quant engine v3.0"),
                ("tools", f"{tool_count} available  ·  {skill_count} skills"),
                ("risk", "enabled"),
                ("context", f"{est_tok:,} / {max_ctx:,} tokens  ({ctx_pct}%)"),
                ("compact", f"{'on' if auto_compact else 'off'}  ·  {int(auto_compact_threshold * 100)}%  ·  {auto_compact_runs} runs"),
            ]
            if provider_summary is not None:
                rows.append(("providers", f"{provider_summary.status}  ·  {provider_summary.detail}"))
            if getattr(t, "_project_session", None):
                rows.append(("project", f"{t._project_session.name}  ({t._project_session.stats.get('total_files',0)} files)"))
            if getattr(t, "_file_session", None) and t._file_session.get_active():
                fc = t._file_session.get_active()
                rows.append(("file", f"{fc.filename}  ({fc.size_kb:.0f} KB)"))
            rows.append(("banner", cfg.get("banner", "full")))
            rows.append(("workspace", os.getcwd().replace(os.path.expanduser("~"), "~")))
            for k, v in rows:
                self.context.console.print(f"  [dim]{k:<12}[/dim][cyan]{v}[/cyan]")
            self.context.console.print()
        else:
            print("\nRuntime Status")
            print(f"  runtime  {runtime}")
            print(f"  model    {model_display}")
            print(f"  tools    {tool_count}")
            print(f"  context  {est_tok}/{max_ctx}")
            print(f"  compact  {'on' if auto_compact else 'off'} threshold={int(auto_compact_threshold * 100)}% runs={auto_compact_runs}")
            if provider_summary is not None:
                print(f"  providers {provider_summary.status} {provider_summary.detail}")
            print()

    def cmd_trace(self, args: str):
        """Show runtime trace for recent tool calls."""
        trace = getattr(self.terminal, "runtime_trace", None)
        if trace is None:
            msg = "Runtime trace is unavailable."
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        if "--json" in args.split():
            payload = json.dumps(trace.to_dict(), ensure_ascii=False, indent=2)
            if self.context.has_rich:
                self.context.console.print(_get_Syntax()(payload, "json", theme=_get__SYNTAX_THEME()))
            else:
                print(payload)
            return
        turns = trace.turn_results[-5:]
        calls = trace.tool_calls[-20:]
        if not calls and not turns:
            msg = "No tool calls recorded yet."
            self.context.console.print(f"[dim]{msg}[/dim]" if self.context.has_rich else msg)
            return
        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print("[bold]Runtime Trace[/bold]")
            self.context.console.print()
            if turns:
                self.context.console.print("  [dim]Recent turns[/dim]")
                for turn in turns:
                    ok = bool(turn.success)
                    style = "green" if ok else "red"
                    status = turn.status or ("ok" if ok else "err")
                    summary = turn.summary or turn.final_text[:120]
                    if len(summary) > 120:
                        summary = summary[:117] + "..."
                    self.context.console.print(
                        f"    [{style}]{status:<8}[/{style}] "
                        f"[bold]{turn.provider or '?'}[/bold] "
                        f"[dim]{summary}[/dim]"
                    )
                self.context.console.print()
            for call in calls:
                ok = bool(call.result.get("success"))
                style = "green" if ok else "red"
                self.context.console.print(
                    f"  [{style}]{'ok' if ok else 'err':<3}[/{style}] "
                    f"[bold]{call.tool}[/bold] "
                    f"[dim]{call.elapsed_ms:.0f} ms[/dim]"
                )
                if not ok and call.result.get("error"):
                    self.context.console.print(f"      [red]{str(call.result.get('error'))[:180]}[/red]")
            self.context.console.print()
        else:
            print("\nRuntime Trace")
            for turn in turns:
                ok = "ok" if turn.success else "err"
                summary = turn.summary or turn.final_text[:120]
                if len(summary) > 120:
                    summary = summary[:117] + "..."
                print(f"  {ok:<3} {turn.provider or '?'} {summary}")
            for call in calls:
                ok = "ok" if call.result.get("success") else "err"
                print(f"  {ok:<3} {call.tool} {call.elapsed_ms:.0f} ms")
            print()

    async def cmd_health(self, args: str):
        try:
            parts = shlex.split(args)
        except ValueError:
            message = "Usage: /health [--model] [--tools] [--json]"
            self.context.console.print(message) if self.context.has_rich else print(message)
            return
        if parts:
            if any(part not in {"--model", "--tools", "--json"} for part in parts):
                message = "Usage: /health [--model] [--tools] [--json]"
                self.context.console.print(message) if self.context.has_rich else print(message)
                return
            if not any(part in {"--model", "--tools"} for part in parts):
                message = "Run /health --model --json to explicitly test model generation."
                self.context.console.print(message) if self.context.has_rich else print(message)
                return
            from aria_code.apps.cli.model_probe import probe_model
            report = await probe_model(self.terminal.config, self.terminal.api_url, tools="--tools" in parts)
            message = json.dumps(report.to_dict(), ensure_ascii=False) if "--json" in parts else (
                f"{report.model} · {report.route} · {report.category}: {report.message}"
                + (f"\n{report.suggestion}" if report.suggestion else "")
            )
            self.context.console.print(message, markup=False) if self.context.has_rich else print(message)
            return
        import aiohttp
        if self.context.has_rich:
            self.context.console.print()
        urls, message = _health_targets(self.terminal.config, self.terminal.api_url)
        if message:
            if self.context.has_rich:
                self.context.console.print(f"  [cyan]●[/cyan] {message}")
            else:
                print(f"  ? {message}")
        for label, url, path in urls:
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(f"{str(url).rstrip('/')}{path}", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        if label == "Ollama":
                            models = [m.get("name", "?") for m in data.get("models", [])[:3]]
                            detail = ", ".join(models)
                        else:
                            detail = f"v{data.get('version', '?')}"
                        if self.context.has_rich:
                            self.context.console.print(f"  [green]●[/green] [dim]{label}[/dim]  {detail}")
                        else:
                            print(f"  + {label}  {detail}")
            except Exception as exc:
                from aria_code.packages.aria_services.provider_health import classify_provider_error
                category = classify_provider_error(label, exc).category
                if self.context.has_rich:
                    self.context.console.print(f"  [red]●[/red] [dim]{label}[/dim]  {category}")
                else:
                    print(f"  - {label}  {category}")
        if self.context.has_rich:
            self.context.console.print()
