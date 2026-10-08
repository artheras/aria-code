"""Explicit model diagnostics using the selected transport, without file tools."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import time

from aria_code.apps.cli.providers.chat_routing import first_round_route
from aria_code.packages.aria_services.provider_health import classify_provider_error


@dataclass(frozen=True)
class ModelProbe:
    model: str
    route: str
    success: bool
    category: str
    message: str
    duration_ms: int
    text_verified: bool = False
    tools_verified: bool = False
    provider: str = ""
    first_token_ms: int | None = None
    suggestion: str = ""
    schema: str = "aria.model_probe.v1"

    def to_dict(self) -> dict:
        return asdict(self)


_PROBE_TOOL = {"type": "function", "function": {
    "name": "aria_probe_ping",
    "description": "A diagnostic echo with no file access or side effects.",
    "parameters": {"type": "object", "properties": {"echo": {"type": "string"}}, "required": ["echo"]},
}}


async def probe_model(config: dict, api_url: str = "", *, tools: bool = False,
                      timeout: float = 30) -> ModelProbe:
    """One text request, optionally one inert tool call and its round trip.

    Do not use the agent gateway: its provider fallback and project tools can
    hide a broken primary route or perform work. Never change saved settings.
    """
    from aria_code.apps.cli.providers.base import AriaSSEProvider, ConfiguredProvider, OllamaProvider
    from aria_code.packages.aria_sdk.streaming import stream_provider_result

    cfg = dict(config)
    model = str(cfg.get("model") or "")
    route = first_round_route(model, cfg, api_url)
    started = time.monotonic()
    cancel = asyncio.Event()
    text_ok = tool_ok = False
    provider_name = ""
    first_token = None

    def token(_text):
        nonlocal first_token
        if _text.strip() and first_token is None:
            first_token = round((time.monotonic() - started) * 1000)

    def result(ok, category, message, suggestion=""):
        return ModelProbe(model, route, ok, category, message,
                          round((time.monotonic() - started) * 1000),
                          text_ok, tool_ok, provider_name, first_token, suggestion)

    def failed(error):
        raw = str(error or "empty_response")
        if "backend_local_tools_unsupported" in raw or "tool_protocol" in raw:
            return result(False, "protocol_unsupported", "Backend does not support the local tool protocol.",
                          "Update the backend protocol or use a direct provider for local project tasks.")
        issue = classify_provider_error(provider_name or route, error)
        suggestions = {
            "auth": "Check credentials and project permissions; for Google Cloud, verify gcloud/ADC authorization.",
            "configuration": "Set your Google Cloud project: /config set gcp_project=<project-id> or GOOGLE_CLOUD_PROJECT.",
            "model_unavailable": "Check the selected model ID, project and serving region with /model.",
            "rate_limited": "Wait for the quota cooldown or check the provider quota.",
            "timeout": "Check network and provider latency; increase --timeout if needed.",
            "no_data": "The selected route returned no visible text; check its streaming/model configuration.",
        }
        return result(False, issue.category, issue.message,
                      suggestions.get(issue.category, "Check the selected provider and connection settings."))

    def provider(local_tools=False):
        if route == "cloud":
            return AriaSSEProvider(api_url, model, auth_token=cfg.get("auth_token"),
                                   use_react_gateway=bool(cfg.get("arthera_react_gateway")),
                                   local_tools=local_tools)
        if route == "ollama":
            return OllamaProvider(str(cfg.get("ollama_url") or "http://localhost:11434"), model,
                                  system_override="Only perform the requested diagnostic.",
                                  show_market_prefetch_status=False)
        return ConfiguredProvider(cfg, model)

    async def run():
        nonlocal text_ok, tool_ok, provider_name
        reply = await stream_provider_result(provider(), "Reply with the single word READY.", [],
                                             tools=[], cancel_event=cancel, on_token=token)
        provider_name = str(reply.get("provider") or route)
        if not reply.get("success") or not str(reply.get("response") or "").strip():
            return failed(reply.get("error") or "empty_response")
        text_ok = True
        if not tools:
            return result(True, "ok", "Selected model returned visible text.")
        diagnostic = provider(local_tools=True)
        request = "Call aria_probe_ping exactly once with echo='READY'. Do not answer in text before calling it."
        call = await stream_provider_result(diagnostic, request, [], tools=[_PROBE_TOOL], cancel_event=cancel)
        if not call.get("success"):
            return failed(call.get("error"))
        calls = list(call.get("tool_calls_pending") or [])
        if len(calls) != 1 or calls[0].get("tool") != "aria_probe_ping" or calls[0].get("params") != {"echo": "READY"}:
            return result(False, "tool_protocol", "Model did not return the requested diagnostic tool call.",
                          "Check that this model and transport support function calling.")
        history = [{"role": "user", "content": request},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "aria-probe-1", "type": "function",
                    "function": {"name": "aria_probe_ping", "arguments": '{"echo":"READY"}'}}]},
                   {"role": "tool", "name": "aria_probe_ping", "tool_call_id": "aria-probe-1", "content": '{"echo":"READY"}'}]
        reply = await stream_provider_result(diagnostic, "The diagnostic tool returned READY. Reply READY.", history,
                                             tools=[], cancel_event=cancel)
        if not reply.get("success") or not str(reply.get("response") or "").strip():
            return failed(reply.get("error") or "empty_response")
        tool_ok = True
        return result(True, "ok", "Selected model returned text and completed the diagnostic tool round trip.")

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError:
        return failed("model probe timed out")
    except Exception as exc:
        return failed(exc)
    finally:
        cancel.set()


def main(argv=None) -> int:
    """Fast standalone equivalent of /health --model; no REPL or agent import."""
    parser = argparse.ArgumentParser(prog="aria health", description="Probe the selected model without accessing project files")
    parser.add_argument("--model", action="store_true", help="Send a short text generation request")
    parser.add_argument("--tools", action="store_true", help="Also verify an inert tool call and its result")
    parser.add_argument("--model-id", help="Probe this model without changing your saved selection")
    parser.add_argument("--timeout", type=float, default=30, help="Total probe deadline in seconds (1–60)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout must be between 1 and 60 seconds")
    from aria_code.apps.cli.bootstrap import initialize_cli_environment, default_config, runtime_paths
    from aria_code.apps.cli.config_store import load_cli_config

    initialize_cli_environment()
    cfg = load_cli_config(runtime_paths(), default_config())
    if args.model_id:
        cfg["model"] = args.model_id
    if not (args.model or args.tools):
        report = ModelProbe(str(cfg.get("model") or ""), first_round_route(str(cfg.get("model") or ""), cfg, cfg.get("api_url")),
                            False, "not_probed", "Run aria health --model to test generation; add --tools to test function calling.", 0)
    else:
        report = asyncio.run(probe_model(cfg, str(cfg.get("api_url") or ""), tools=args.tools, timeout=args.timeout))
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        print(f"{report.model} · {report.route} · {report.category}: {report.message}")
        if report.suggestion:
            print(report.suggestion)
    return 0 if report.success else (2 if report.category == "not_probed" else 1)
