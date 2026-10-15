"""aria-code MCP server — exposes read-only aria-code capabilities over stdio JSON-RPC.

Lets any MCP-compatible client (Claude Code, Codex, Cursor, ...) call into
aria-code's market data, multi-agent research team, and generated-artifact
list. Pattern mirrors Arthera's packages/quant_engine/mcp_server.py (hand-rolled
stdio JSON-RPC, no `mcp` SDK dependency).

Usage (from repo root)::

    python3 aria_mcp_server.py

Register with Claude Code::

    claude mcp add aria-code -- python3 /path/to/aria-code/aria_mcp_server.py

Scope: ``skill:``-backed exposures (report/backtest skills) are still never
registered as *executable* tools — a skill is a prompt-time instruction
document, not a clean JSON-in/JSON-out callable. They are instead readable
via ``aria.skill.list``/``aria.skill.get``, which hand the SKILL.md text to
the calling model so it can follow the workflow itself. Beyond
that, everything is read-only EXCEPT the small, explicit ``_WRITE_SAFE``
allowlist below (order previews, chart/PDF generation, cloud video
generation behind its own confirm gate) — each entry there is individually
justified as unable to move money without a further, explicit gate of its
own.

Order *execution* is the one capability with real financial stakes reachable
from this server at all, and it has two independent gates, both of which
must pass — this is intentional defense in depth, not redundancy to trim:
  1. The broker itself must have chat-confirm explicitly opted in via
     ``/trade allow-chat-confirm <broker_id>`` — typed at the aria-code
     terminal keyboard, by a human, with the broker id retyped as
     confirmation. This can NEVER be turned on from MCP/chat — see
     ``brokers/config.py``'s ``set_chat_confirm_enabled`` and its callers.
  2. Even with that broker opted in, each individual ``aria.broker.confirm_order``
     call still requires an explicit ``confirmed: true`` argument (same
     pattern as ``aria.video.generate_submit``'s cost gate).
Without opt-in #1, ``aria.broker.confirm_order`` refuses unconditionally —
default behavior for every broker is unchanged from before: only
``/trade confirm`` at the terminal can execute a trade.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("aria_mcp_server")

MCP_PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Input schemas for the exposed tools (default_exposures() carries names +
# descriptions but not JSON Schema — kept here, next to the handlers that
# actually consume the arguments).
# ---------------------------------------------------------------------------

_INPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "aria.market.quote": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker/symbol, e.g. AAPL, 600519, BTC-USD"},
        },
        "required": ["symbol"],
    },
    "aria.agent.team": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker/symbol to research"},
            "agents": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of agent names to run (default: all)",
            },
        },
        "required": ["symbol"],
    },
    "aria.artifacts.list": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max artifacts to return (default 20)"},
        },
        "required": [],
    },
    "aria.skill.list": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Task description to rank skills against, e.g. \"design a landing page for a healthcare startup\". Omit to list all installed skills."},
        },
        "required": [],
    },
    "aria.skill.get": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name from aria.skill.list — either the bare name (\"ui-design-system\") or the qualified form (\"app-engineering-skills:ui-design-system\")"},
            "reference": {"type": "string", "description": "Optional. Fetch one of the skill's bundled reference docs instead of its main instructions — pass a filename from the `references` list returned by a plain aria.skill.get call, e.g. \"density_and_semantics.md\"."},
        },
        "required": ["name"],
    },
    "aria.broker.positions": {
        "type": "object",
        "properties": {
            "broker_id": {"type": "string", "description": "Configured broker id (~/.aria-code/brokers.json). Omit to use the default broker."},
        },
        "required": [],
    },
    "aria.broker.list_previews": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max previews to return (default 10)"},
        },
        "required": [],
    },
    "aria.broker.preview_order": {
        "type": "object",
        "properties": {
            "broker_id": {"type": "string", "description": "Configured broker id. Omit to use the default broker."},
            "symbol": {"type": "string", "description": "Ticker/symbol to trade"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "description": "Share/unit quantity"},
            "order_type": {"type": "string", "enum": ["limit", "market"], "description": "Default: limit"},
            "price": {"type": "number", "description": "Limit price (required for limit orders)"},
        },
        "required": ["symbol", "side"],
    },
    "aria.broker.confirm_order": {
        "type": "object",
        "properties": {
            "broker_id": {"type": "string", "description": "Configured broker id. Omit to use the default broker."},
            "preview_id": {"type": "string", "description": "preview_id returned by aria.broker.preview_order"},
            "confirmed": {"type": "boolean", "description": "Must be true. Also requires the broker to have chat-confirm opted in via /trade allow-chat-confirm <broker_id> at the aria-code terminal — see this tool's description."},
        },
        "required": ["preview_id", "confirmed"],
    },
    "aria.report.chart": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker/symbol to chart"},
        },
        "required": ["symbol"],
    },
    "aria.report.indicator_chart": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker/symbol to chart"},
        },
        "required": ["symbol"],
    },
    "aria.report.comparison_chart": {
        "type": "object",
        "properties": {
            "symbols": {"type": "array", "items": {"type": "string"}, "description": "2+ tickers to compare, normalized to base=100"},
            "title": {"type": "string", "description": "Optional chart title"},
        },
        "required": ["symbols"],
    },
    "aria.report.allocation_chart": {
        "type": "object",
        "properties": {
            "broker_id": {"type": "string", "description": "Configured broker id. Omit to use the default broker."},
            "title": {"type": "string", "description": "Optional chart title"},
        },
        "required": [],
    },
    "aria.report.pdf": {
        "type": "object",
        "properties": {
            "markdown": {"type": "string", "description": "Markdown text to render"},
            "title": {"type": "string", "description": "Report title, used for the filename"},
        },
        "required": ["markdown"],
    },
    "aria.report.estimate_image_cost": {
        "type": "object",
        "properties": {
            "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"], "description": "Default: 1024x1536"},
            "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"], "description": "Default: high"},
        },
        "required": [],
    },
    "aria.report.generate_image": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text prompt (e.g. minimal-editorial-poster's compiled prompt)"},
            "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"], "description": "Default: 1024x1536 (portrait poster)"},
            "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"], "description": "Default: high"},
            "confirmed": {"type": "boolean", "description": "Must be true — this spends real money the instant it succeeds"},
        },
        "required": ["prompt", "confirmed"],
    },
    "aria.report.edit_image": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Local path to the existing photo to transform"},
            "prompt": {"type": "string", "description": "How to transform it, e.g. \"convert to duotone with warm ochre accent, simplify the busy rock background into flat negative space, add subtle scan-noise texture\""},
            "mask_path": {"type": "string", "description": "Optional local path to a mask PNG for inpainting — transparent (alpha=0) areas mark where the edit applies; everything opaque is left untouched. Omit to edit the whole image."},
            "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536", "auto"], "description": "Default: 1024x1536"},
            "quality": {"type": "string", "enum": ["low", "medium", "high", "auto"], "description": "Default: high"},
            "confirmed": {"type": "boolean", "description": "Must be true — this spends real money the instant it succeeds"},
        },
        "required": ["image_path", "prompt", "confirmed"],
    },
    "aria.report.generate_image_local": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text prompt (e.g. minimal-editorial-poster's compiled prompt)"},
            "width": {"type": "integer", "description": "Default: 1024"},
            "height": {"type": "integer", "description": "Default: 1024"},
            "steps": {"type": "integer", "description": "Inference steps; default 4 (tuned for SDXL-Turbo)"},
        },
        "required": ["prompt"],
    },
    "aria.report.edit_image_local": {
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Local path to the existing photo to transform"},
            "prompt": {"type": "string", "description": "How to transform it"},
            "strength": {"type": "number", "description": "0-1, how much to diverge from the original (default 0.6)"},
        },
        "required": ["image_path", "prompt"],
    },
    "aria.report.canva_design": {
        "type": "object",
        "properties": {
            "template_id": {"type": "string", "description": "Canva brand template id"},
            "data": {"type": "object", "description": "Field name -> typed DatasetValue, e.g. {\"headline\": {\"type\": \"text\", \"text\": \"Q3 Report\"}, \"chart_img\": {\"type\": \"image\", \"asset_id\": \"<from aria.report.canva_upload_asset>\"}}. Value types: text, image, video, chart, sheet — see Canva's Autofill API docs."},
        },
        "required": ["template_id", "data"],
    },
    "aria.report.canva_upload_asset": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Local path to the image file to upload (e.g. the path returned by aria.report.chart)"},
        },
        "required": ["file_path"],
    },
    "aria.report.docx": {
        "type": "object",
        "properties": {
            "markdown": {"type": "string", "description": "Markdown text to render"},
            "title": {"type": "string", "description": "Document title, used for the filename and cover heading"},
        },
        "required": ["markdown"],
    },
    "aria.report.pptx": {
        "type": "object",
        "properties": {
            "markdown": {"type": "string", "description": "Markdown text to render (each heading becomes a slide)"},
            "title": {"type": "string", "description": "Deck title, used for the filename and title slide"},
        },
        "required": ["markdown"],
    },
    "aria.figma.read_file": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key (from the file's URL: figma.com/file/<file_key>/...)"},
            "depth": {"type": "integer", "description": "How many tree levels to include per page (default 2)"},
        },
        "required": ["file_key"],
    },
    "aria.figma.nodes": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key or URL (e.g. figma.com/design/<key>/<name>?node-id=1-23)"},
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of node IDs to inspect (e.g. ['1:23', '2:4']). Optional if node-id is in URL.",
            },
            "depth": {"type": "integer", "description": "Tree depth limit for children nodes (optional)"},
        },
        "required": ["file_key"],
    },
    "aria.figma.export_images": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key or URL"},
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of node IDs to export as images/vectors",
            },
            "format": {"type": "string", "enum": ["svg", "png", "jpg", "pdf"], "description": "Export format (default: svg)"},
            "scale": {"type": "number", "description": "Export resolution scale (1-4, default: 1.0)"},
        },
        "required": ["file_key"],
    },
    "aria.figma.variables": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key or URL"},
        },
        "required": ["file_key"],
    },
    "aria.figma.comments": {
        "type": "object",
        "properties": {
            "file_key": {"type": "string", "description": "Figma file key or URL"},
        },
        "required": ["file_key"],
    },
    "aria.report.generate": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker/symbol to research"},
        },
        "required": ["symbol"],
    },
    "aria.backtest.run": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker/symbol to backtest"},
            "strategy": {"type": "string", "enum": ["buy_hold", "sma_cross"], "description": "Default: sma_cross"},
            "days": {"type": "integer", "description": "History window in days (default 365)"},
        },
        "required": ["symbol"],
    },
    "aria.video.probe": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "description": "Local path to the video"},
        },
        "required": ["input_path"],
    },
    "aria.video.trim": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "start": {"type": "number", "description": "Start time in seconds"},
            "end": {"type": "number", "description": "End time in seconds"},
        },
        "required": ["input_path", "start", "end"],
    },
    "aria.video.concat": {
        "type": "object",
        "properties": {
            "input_paths": {"type": "array", "items": {"type": "string"}, "description": "Video paths, in order"},
        },
        "required": ["input_paths"],
    },
    "aria.video.overlay_text": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "text": {"type": "string"},
            "position": {"type": "string", "enum": ["top", "bottom", "center"], "description": "Default: bottom"},
            "font_size": {"type": "integer", "description": "Default: 36"},
            "font_color": {"type": "string", "description": "Default: white"},
        },
        "required": ["input_path", "text"],
    },
    "aria.video.overlay_audio": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "audio_path": {"type": "string"},
            "replace": {"type": "boolean", "description": "true = drop original audio; false (default) = mix under it"},
        },
        "required": ["input_path", "audio_path"],
    },
    "aria.video.convert": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "output_format": {"type": "string", "description": "e.g. mp4, mov, webm"},
            "aspect": {"type": "string", "description": "e.g. \"9:16\" — center-crop to this aspect ratio"},
        },
        "required": ["input_path"],
    },
    "aria.video.change_speed": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "factor": {"type": "number", "description": ">1 speeds up, <1 slows down"},
        },
        "required": ["input_path", "factor"],
    },
    "aria.video.transcribe": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "model_size": {"type": "string", "description": "faster-whisper model size, default 'base'"},
            "language": {"type": "string", "description": "ISO language code; omit to auto-detect"},
        },
        "required": ["input_path"],
    },
    "aria.video.detect_scenes": {
        "type": "object",
        "properties": {
            "input_path": {"type": "string"},
            "threshold": {"type": "number", "description": "Higher = less sensitive (default 30.0)"},
        },
        "required": ["input_path"],
    },
    "aria.video.generate_estimate": {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["kling", "runway"]},
            "duration": {"type": "integer", "description": "Video length in seconds"},
        },
        "required": ["provider", "duration"],
    },
    "aria.video.generate_submit": {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["kling", "runway"]},
            "prompt": {"type": "string"},
            "duration": {"type": "integer", "description": "Default: 5"},
            "aspect_ratio": {"type": "string", "description": "e.g. \"16:9\" (kling) or \"1280:720\" (runway) — format is provider-specific"},
            "confirmed": {"type": "boolean", "description": "Must be true — this spends real money the instant it succeeds"},
        },
        "required": ["provider", "prompt", "confirmed"],
    },
    "aria.video.workflow_plan": {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "What the video says, in the user's words"},
            "asset_paths": {"type": "array", "items": {"type": "string"}, "description": "Local image or video files, in any order"},
            "project_dir": {"type": "string", "description": "Folder for the plan, narration, clips and final video"},
            "platform": {"type": "string", "description": "douyin, xiaohongshu, tiktok, shorts, bilibili, youtube, x, linkedin. Default douyin"},
            "aspect_ratio": {"type": "string", "enum": ["9:16", "16:9", "1:1"], "description": "Overrides the platform default"},
            "target_seconds": {"type": "integer", "description": "Approximate length. Default 30"},
            "allow_generation": {"type": "boolean", "description": "Let the plan use Veo-generated shots where assets are missing (paid at render). Default false"},
            "language": {"type": "string", "description": "Narration language, default zh"},
        },
        "required": ["script", "asset_paths", "project_dir"],
    },
    "aria.video.workflow_render": {
        "type": "object",
        "properties": {
            "project_dir": {"type": "string"},
            "confirm_generation": {"type": "boolean", "description": "Must be true to render beats that Veo generates (billed per second)"},
        },
        "required": ["project_dir"],
    },
    "aria.video.generate_status": {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["kling", "runway"]},
            "task_id": {"type": "string"},
        },
        "required": ["provider", "task_id"],
    },
    "aria.video.engine_config": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "set_active", "configure_custom"], "description": "Default: list"},
            "engine": {"type": "string", "enum": ["google", "custom", "local"]},
            "endpoint_url": {"type": "string"},
            "auth_token": {"type": "string"},
            "model_name": {"type": "string"},
        },
        "required": [],
    },
    "aria.video.produce_from_script": {
        "type": "object",
        "properties": {
            "script": {"type": "string", "description": "Script narration / prompt"},
            "images": {"type": "array", "items": {"type": "string"}, "description": "Paths to source images"},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"], "description": "Default: 16:9"},
            "target_duration": {"type": "number", "description": "Default: 15"},
            "provider": {"type": "string", "enum": ["auto", "google", "custom", "local"], "description": "Default: auto"},
            "audio_path": {"type": "string"},
            "bgm_path": {"type": "string"},
        },
        "required": ["script"],
    },
    "aria.video.project_create": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "script": {"type": "string"},
            "images": {"type": "array", "items": {"type": "string"}},
            "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
            "target_duration": {"type": "number"},
            "provider": {"type": "string", "enum": ["auto", "google", "custom", "local"]},
        },
        "required": ["script"],
    },
    "aria.video.shot_regenerate": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "shot_index": {"type": "integer"},
            "new_prompt": {"type": "string"},
            "engine": {"type": "string", "enum": ["google", "custom", "local"]},
        },
        "required": ["project_id", "shot_index"],
    },
}

# Exposures that are NOT read_only (they write a local file, a preview
# record, a Canva design draft, or a paid cloud generation job) but are
# individually verified safe to reach from MCP without a human in this
# module blocking it first. Almost all of them can't move money or place a
# live trade — the exceptions are aria.report.generate_image / edit_image
# (real per-call OpenAI cost, billed the instant it succeeds — safe here
# only because their handlers hard-refuse without confirmed=true, see
# _call_generate_image / _call_edit_image), aria.video.generate_submit (same
# pattern for cloud video generation, see _call_video_generate_submit), and
# aria.broker.confirm_order (real trade execution — safe here only because
# its handler hard-refuses unless the broker was separately, explicitly
# opted into chat-confirm at the aria-code terminal keyboard via
# /trade allow-chat-confirm, AND confirmed=true is passed; see
# _call_broker_confirm_order and the module docstring). Every other
# non-read_only exposure stays blocked by default — see _build_tools().
_WRITE_SAFE = {
    "aria.broker.preview_order", "aria.broker.confirm_order",
    "aria.report.chart", "aria.report.indicator_chart", "aria.report.comparison_chart",
    "aria.report.allocation_chart", "aria.report.pdf",
    "aria.report.canva_design", "aria.report.canva_upload_asset",
    "aria.report.docx", "aria.report.pptx",
    "aria.report.generate", "aria.backtest.run",
    "aria.report.generate_image", "aria.report.edit_image",
    "aria.report.generate_image_local", "aria.report.edit_image_local",
    "aria.video.trim", "aria.video.concat", "aria.video.overlay_text",
    "aria.video.overlay_audio", "aria.video.convert", "aria.video.change_speed",
    "aria.video.generate_submit",
    # Writes into project_dir only. Paid Veo generation inside render is refused
    # unless confirm_generation=true (see pipeline.render_video).
    "aria.video.workflow_plan", "aria.video.workflow_render",
    "aria.video.engine_config", "aria.video.produce_from_script",
    "aria.video.project_create", "aria.video.shot_regenerate",
}


# ---------------------------------------------------------------------------
# Handlers — each is `async def handler(arguments: dict) -> dict`
# ---------------------------------------------------------------------------

async def _call_market_quote(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.runtime.tool_executor import ToolExecutor
    from aria_code.apps.cli.tools.market_tools import tool_get_market_data

    executor = ToolExecutor({"get_market_data": (tool_get_market_data, "quote + technicals")})
    return await executor.execute("get_market_data", {"symbol": args.get("symbol", "")})


async def _call_agent_team(args: Dict[str, Any]) -> Dict[str, Any]:
    from dataclasses import asdict

    from aria_code.agents.team import run_team
    from aria_code.datasources.router import get_router

    symbol = str(args.get("symbol", "")).strip()
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        data_router = get_router()
    except Exception as exc:
        logger.debug("get_router() unavailable, running without live data bundle: %s", exc)
        data_router = None
    # llm_provider intentionally left None: agents fall back to their
    # deterministic template mode (see agents/base.py) rather than this
    # server needing to bootstrap the CLI's full provider/config stack.
    # Output is real (real data, real rule-based signals) but not
    # LLM-narrated — documented in the tool description surfaced to clients.
    result = await run_team(symbol, agents=args.get("agents"), llm_provider=None, data_router=data_router)
    return {"success": True, **asdict(result)}


async def _call_artifacts_list(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.artifacts import recent_artifacts_all

    limit = int(args.get("limit", 20) or 20)
    return {"success": True, "artifacts": recent_artifacts_all(limit=limit)}


def _skill_summary(skill: Any) -> Dict[str, Any]:
    """Listing shape — deliberately excludes `instructions` so a list call
    stays small; aria.skill.get fetches the full text for one skill."""
    policy = getattr(skill, "policy", None)
    return {
        "name": skill.name,
        "qualified_name": skill.qualified_name,
        "description": skill.description,
        "plugin": skill.plugin_name,
        "plugin_version": getattr(skill, "plugin_version", ""),
        "repository": getattr(skill, "repository", ""),
        "content_sha256": getattr(skill, "content_sha256", ""),
        # Surfaced so the caller can tell a signed catalog skill from an
        # unverified local drop-in — these are instruction documents the
        # model will follow, so provenance is worth showing, not hiding.
        "integrity": skill.integrity,
        "policy": {
            "allowed_tools": list(getattr(policy, "allowed_tools", ()) or ()),
            "permissions": list(getattr(policy, "permissions", ()) or ()),
            "agents": list(getattr(policy, "agents", ()) or ()),
            "script_execution": str(getattr(policy, "script_execution", "approval")),
            "script_network": bool(getattr(policy, "script_network", False)),
            "script_workspace_write": bool(getattr(policy, "script_workspace_write", False)),
        },
    }


async def _call_skill_list(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.packages.aria_skills.loader import discover_external_skills, select_external_skills

    query = str(args.get("query", "")).strip()
    try:
        skills = discover_external_skills()
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    if query:
        ranked = select_external_skills(query, skills)
        # select_* returns only what clears its relevance bar, which can be
        # empty for an off-topic query — fall back to the full list rather
        # than answering "no skills exist", which would be misleading.
        chosen = ranked or skills
        matched = bool(ranked)
    else:
        chosen = skills
        matched = False

    return {
        "success": True,
        "query": query,
        "matched_by_relevance": matched,
        "skills": [_skill_summary(s) for s in chosen],
    }


def _skill_reference_names(skill: Any) -> List[str]:
    """Reference docs bundled beside a SKILL.md. Several skills' instructions
    say things like "read references/density_and_semantics.md first" — over
    MCP the caller has no filesystem access to this machine, so those files
    have to be fetchable through this tool or that instruction is a dead end."""
    try:
        ref_dir = Path(skill.path).parent / "references"
        if not ref_dir.is_dir():
            return []
        return sorted(p.name for p in ref_dir.glob("*.md"))
    except Exception:
        return []


async def _call_skill_get(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.packages.aria_skills.loader import discover_external_skills

    name = str(args.get("name", "")).strip()
    if not name:
        return {"success": False, "error": "name is required"}

    try:
        skills = discover_external_skills()
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    match = next(
        (s for s in skills if name in (s.name, s.qualified_name)),
        None,
    )
    if match is None:
        return {
            "success": False,
            "error": f"No skill named {name!r}. Call aria.skill.list to see installed skills.",
            "available": [s.qualified_name for s in skills],
        }

    references = _skill_reference_names(match)
    requested = str(args.get("reference", "")).strip()
    if requested:
        # `reference` is caller-controlled and lands in a filesystem path, so
        # resolve it and require the result to stay inside this skill's own
        # references/ dir — a bare name check would still let "../../.ssh/id_rsa"
        # or a symlink out of the sandbox through.
        ref_dir = (Path(match.path).parent / "references").resolve()
        target = (ref_dir / requested).resolve()
        if not str(target).startswith(str(ref_dir) + os.sep) or not target.is_file():
            return {
                "success": False,
                "error": f"No reference {requested!r} in skill {match.qualified_name!r}.",
                "references": references,
            }
        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            **_skill_summary(match),
            "reference": requested,
            "content": content,
        }

    return {
        "success": True,
        **_skill_summary(match),
        "instructions": match.instructions,
        "references": references,
    }


def _get_broker(broker_id: str = ""):
    from aria_code.brokers.registry import get_registry

    reg = get_registry()
    if broker_id:
        return reg.connect(broker_id)
    broker = reg.connect_default()
    if broker is None:
        raise ValueError("No broker_id given and no default broker configured (~/.aria-code/brokers.json)")
    return broker


async def _call_broker_positions(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from dataclasses import asdict

    loop = asyncio.get_event_loop()

    def _run():
        broker = _get_broker(str(args.get("broker_id", "")))
        return asdict(broker.account_info()), [asdict(p) for p in broker.positions()]

    try:
        account, positions = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "account": account, "positions": positions}


async def _call_broker_list_previews(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.brokers.trading import list_order_previews

    limit = int(args.get("limit", 10) or 10)
    return {"success": True, "previews": list_order_previews(limit=limit)}


async def _call_broker_preview_order(args: Dict[str, Any]) -> Dict[str, Any]:
    # Building a preview here can never by itself result in a live order —
    # execute_order_preview is only imported by _call_broker_confirm_order,
    # which has its own two-gate check. See module docstring.
    import asyncio

    from aria_code.brokers.trading import OrderIntent, build_order_preview

    symbol = str(args.get("symbol", "")).strip()
    side = str(args.get("side", "")).strip().lower()
    if not symbol or side not in ("buy", "sell"):
        return {"success": False, "error": "symbol and side (buy/sell) are required"}

    intent = OrderIntent(
        symbol=symbol,
        side=side,
        quantity=args.get("quantity"),
        order_type=str(args.get("order_type") or "limit"),
        price=args.get("price"),
        source="mcp",
    )
    loop = asyncio.get_event_loop()

    def _run():
        broker = _get_broker(str(args.get("broker_id", "")))
        return build_order_preview(broker, intent)

    try:
        preview = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, **preview}


async def _call_broker_confirm_order(args: Dict[str, Any]) -> Dict[str, Any]:
    # The one place in this module that imports execute_order_preview. Two
    # gates, both required, checked here (not just trusted from the caller):
    #   1. is_chat_confirm_enabled(broker_id) — set only by a human running
    #      /trade allow-chat-confirm <broker_id> at the aria-code terminal
    #      keyboard and retyping the exact broker id. Off by default for
    #      every broker. This gate cannot be flipped on from MCP/chat.
    #   2. confirmed is True on this specific call.
    # Both must pass before execute_order_preview(confirmed=True) is ever
    # reached. See the module docstring for the full rationale.
    import asyncio

    from aria_code.brokers.config import is_chat_confirm_enabled
    from aria_code.brokers.trading import execute_order_preview

    preview_id = str(args.get("preview_id", "")).strip()
    if not preview_id:
        return {"success": False, "error": "preview_id is required"}
    if args.get("confirmed") is not True:
        return {
            "success": False,
            "error": "confirmed must be true to execute a real order — this places a live trade.",
        }

    broker_id = str(args.get("broker_id", "")).strip()
    loop = asyncio.get_event_loop()

    def _run():
        broker = _get_broker(broker_id)
        resolved_broker_id = broker_id or getattr(broker, "broker_id", "")
        if not is_chat_confirm_enabled(resolved_broker_id):
            return {
                "success": False,
                "error": (
                    f"Chat-confirmed execution is not enabled for broker '{resolved_broker_id}'. "
                    f"Run /trade allow-chat-confirm {resolved_broker_id} in the aria-code terminal "
                    "yourself first — this cannot be turned on from chat/MCP."
                ),
            }
        return execute_order_preview(broker, preview_id, confirmed=True, source="chat_mcp")

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_report_chart(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    import base64

    from aria_code.artifacts import create_user_artifact
    from aria_code.report_generator import _fetch_report_data_sync, generate_price_chart

    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return {"success": False, "error": "symbol is required"}

    loop = asyncio.get_event_loop()

    def _run():
        df, _clean_result, fundamentals = _fetch_report_data_sync(symbol)
        if df is None or df.empty:
            return None
        return generate_price_chart(df, symbol, fundamentals)

    try:
        b64_png = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not b64_png:
        return {"success": False, "error": f"No price data available for {symbol}"}

    artifact = create_user_artifact("chart", symbol, f"{symbol}_chart", ".png")
    artifact.path.write_bytes(base64.b64decode(b64_png))
    return {"success": True, "path": str(artifact.path)}


async def _call_indicator_chart(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    import base64

    from aria_code.artifacts import create_user_artifact
    from aria_code.report_generator import _fetch_report_data_sync, generate_indicator_chart

    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return {"success": False, "error": "symbol is required"}

    loop = asyncio.get_event_loop()

    def _run():
        df, _clean_result, _fundamentals = _fetch_report_data_sync(symbol)
        if df is None or df.empty:
            return None
        return generate_indicator_chart(df, symbol)

    try:
        b64_png = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not b64_png:
        return {"success": False, "error": f"No price data available for {symbol}"}

    artifact = create_user_artifact("chart", symbol, f"{symbol}_indicators", ".png")
    artifact.path.write_bytes(base64.b64decode(b64_png))
    return {"success": True, "path": str(artifact.path)}


async def _call_comparison_chart(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    import base64

    from aria_code.artifacts import create_user_artifact
    from aria_code.report_generator import _fetch_report_data_sync, generate_comparison_chart

    symbols = args.get("symbols") or []
    if not isinstance(symbols, list) or len(symbols) < 2:
        return {"success": False, "error": "symbols must be a list of at least 2 tickers"}
    symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
    title = str(args.get("title") or "").strip()

    loop = asyncio.get_event_loop()

    def _run():
        price_data = {}
        for symbol in symbols:
            try:
                df, _clean_result, _fundamentals = _fetch_report_data_sync(symbol)
                if df is not None and not df.empty:
                    price_data[symbol] = df
            except Exception as exc:
                logger.debug("[mcp] comparison_chart: skipping %s (%s)", symbol, exc)
        return generate_comparison_chart(price_data, title)

    try:
        b64_png = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not b64_png:
        return {"success": False, "error": f"No usable price data for any of {symbols}"}

    artifact = create_user_artifact("chart", "_".join(symbols[:4]), "comparison", ".png")
    artifact.path.write_bytes(base64.b64decode(b64_png))
    return {"success": True, "path": str(artifact.path)}


async def _call_allocation_chart(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    import base64

    from aria_code.artifacts import create_user_artifact
    from aria_code.report_generator import generate_allocation_chart

    title = str(args.get("title") or "").strip()
    loop = asyncio.get_event_loop()

    def _asdict_position(p):
        from dataclasses import asdict, is_dataclass
        return asdict(p) if is_dataclass(p) else dict(p)

    def _run():
        broker = _get_broker(str(args.get("broker_id", "")))
        positions = [_asdict_position(p) for p in broker.positions()]
        return generate_allocation_chart(positions, title)

    try:
        b64_png = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not b64_png:
        return {"success": False, "error": "No positions with usable market value found"}

    artifact = create_user_artifact("chart", "portfolio", "allocation", ".png")
    artifact.path.write_bytes(base64.b64decode(b64_png))
    return {"success": True, "path": str(artifact.path)}


async def _call_report_pdf(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.artifacts import create_user_artifact
    from aria_code.markdown_pdf import markdown_to_pdf

    md = str(args.get("markdown", ""))
    if not md.strip():
        return {"success": False, "error": "markdown is required"}
    title = str(args.get("title") or "report").strip() or "report"

    artifact = create_user_artifact("report", title, title, ".pdf")
    loop = asyncio.get_event_loop()
    try:
        result_path = await loop.run_in_executor(None, markdown_to_pdf, md, artifact.path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not result_path:
        return {"success": False, "error": "PDF rendering failed (no HTML→PDF engine available)"}
    return {"success": True, "path": str(result_path)}


async def _call_report_generate(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.report_generator import generate_report

    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        path = await generate_report(symbol)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not path:
        return {"success": False, "error": f"Report generation failed for {symbol} (no data?)"}
    return {"success": True, "path": str(path)}


async def _call_backtest_run(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from dataclasses import asdict

    from aria_code.backtest_engine import BacktestEngine, get_strategy, load_bars

    symbol = str(args.get("symbol", "")).strip().upper()
    if not symbol:
        return {"success": False, "error": "symbol is required"}
    strategy_name = str(args.get("strategy") or "sma_cross")
    days = int(args.get("days", 365) or 365)

    loop = asyncio.get_event_loop()

    def _run():
        bars = load_bars(symbol, days=days)
        if not bars:
            raise ValueError(f"No price history available for {symbol}")
        strategy = get_strategy(strategy_name)
        engine = BacktestEngine()
        return engine.run({symbol: bars}, strategy)

    try:
        result = await loop.run_in_executor(None, _run)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, **asdict(result)}


async def _call_figma_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.figma_client import get_file_summary, parse_figma_url

    raw_key = str(args.get("file_key", "")).strip()
    parsed = parse_figma_url(raw_key)
    file_key = parsed.get("file_key") or raw_key
    if not file_key:
        return {"success": False, "error": "file_key is required"}
    depth = int(args.get("depth", 2) or 2)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: get_file_summary(file_key, depth=depth))
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_figma_nodes(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.figma_client import get_file_nodes, parse_figma_url

    raw_key = str(args.get("file_key", "")).strip()
    parsed = parse_figma_url(raw_key)
    file_key = parsed.get("file_key") or raw_key
    if not file_key:
        return {"success": False, "error": "file_key is required"}

    node_ids = args.get("node_ids") or []
    if isinstance(node_ids, str):
        node_ids = [n.strip() for n in node_ids.split(",") if n.strip()]
    if not node_ids and parsed.get("node_id"):
        node_ids = [parsed["node_id"]]
    if not node_ids:
        return {"success": False, "error": "node_ids is required (or supply a Figma URL with ?node-id=...)"}

    depth = args.get("depth")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: get_file_nodes(file_key, node_ids, depth=depth))
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_figma_export_images(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.figma_client import export_images, parse_figma_url

    raw_key = str(args.get("file_key", "")).strip()
    parsed = parse_figma_url(raw_key)
    file_key = parsed.get("file_key") or raw_key
    if not file_key:
        return {"success": False, "error": "file_key is required"}

    node_ids = args.get("node_ids") or []
    if isinstance(node_ids, str):
        node_ids = [n.strip() for n in node_ids.split(",") if n.strip()]
    if not node_ids and parsed.get("node_id"):
        node_ids = [parsed["node_id"]]
    if not node_ids:
        return {"success": False, "error": "node_ids is required (or supply a Figma URL with ?node-id=...)"}

    fmt = str(args.get("format", "svg")).strip().lower()
    scale = float(args.get("scale", 1.0) or 1.0)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: export_images(file_key, node_ids, format=fmt, scale=scale))
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_figma_variables(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.figma_client import get_local_variables, parse_figma_url

    raw_key = str(args.get("file_key", "")).strip()
    parsed = parse_figma_url(raw_key)
    file_key = parsed.get("file_key") or raw_key
    if not file_key:
        return {"success": False, "error": "file_key is required"}

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, lambda: get_local_variables(file_key))
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_figma_comments(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.figma_client import list_comments, parse_figma_url

    raw_key = str(args.get("file_key", "")).strip()
    parsed = parse_figma_url(raw_key)
    file_key = parsed.get("file_key") or raw_key
    if not file_key:
        return {"success": False, "error": "file_key is required"}
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, list_comments, file_key)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_probe(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.video_editor import probe_video

    input_path = str(args.get("input_path", "")).strip()
    if not input_path:
        return {"success": False, "error": "input_path is required"}
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, probe_video, input_path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_trim(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_editor import trim_video

    input_path = str(args.get("input_path", "")).strip()
    if not input_path or "start" not in args or "end" not in args:
        return {"success": False, "error": "input_path, start, and end are required"}
    loop = asyncio.get_event_loop()
    fn = partial(trim_video, input_path, float(args["start"]), float(args["end"]))
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_concat(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.video_editor import concat_videos

    input_paths = args.get("input_paths") or []
    if not isinstance(input_paths, list) or len(input_paths) < 2:
        return {"success": False, "error": "input_paths must be a list of at least 2 paths"}
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, concat_videos, [str(p) for p in input_paths])
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_overlay_text(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_editor import overlay_text

    input_path = str(args.get("input_path", "")).strip()
    text = str(args.get("text", ""))
    if not input_path or not text:
        return {"success": False, "error": "input_path and text are required"}
    loop = asyncio.get_event_loop()
    fn = partial(
        overlay_text, input_path, text,
        position=args.get("position") or "bottom",
        font_size=int(args.get("font_size", 36) or 36),
        font_color=args.get("font_color") or "white",
    )
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_overlay_audio(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_editor import overlay_audio

    input_path = str(args.get("input_path", "")).strip()
    audio_path = str(args.get("audio_path", "")).strip()
    if not input_path or not audio_path:
        return {"success": False, "error": "input_path and audio_path are required"}
    loop = asyncio.get_event_loop()
    fn = partial(overlay_audio, input_path, audio_path, replace=bool(args.get("replace", False)))
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_convert(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_editor import convert_video

    input_path = str(args.get("input_path", "")).strip()
    if not input_path:
        return {"success": False, "error": "input_path is required"}
    loop = asyncio.get_event_loop()
    fn = partial(convert_video, input_path, output_format=args.get("output_format"), aspect=args.get("aspect"))
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_change_speed(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_editor import change_speed

    input_path = str(args.get("input_path", "")).strip()
    if not input_path or "factor" not in args:
        return {"success": False, "error": "input_path and factor are required"}
    loop = asyncio.get_event_loop()
    fn = partial(change_speed, input_path, float(args["factor"]))
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_transcribe(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_analysis import transcribe_video

    input_path = str(args.get("input_path", "")).strip()
    if not input_path:
        return {"success": False, "error": "input_path is required"}
    loop = asyncio.get_event_loop()
    fn = partial(
        transcribe_video, input_path,
        model_size=args.get("model_size") or "base",
        language=args.get("language"),
    )
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_detect_scenes(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_analysis import detect_scenes

    input_path = str(args.get("input_path", "")).strip()
    if not input_path:
        return {"success": False, "error": "input_path is required"}
    loop = asyncio.get_event_loop()
    fn = partial(detect_scenes, input_path, threshold=float(args.get("threshold", 30.0) or 30.0))
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


_VIDEO_GEN_PROVIDERS = {"kling": "kling_video_client", "runway": "runway_video_client"}


async def _call_video_generate_estimate(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(args.get("provider", "")).strip().lower()
    module_name = _VIDEO_GEN_PROVIDERS.get(provider)
    if not module_name:
        return {"success": False, "error": f"provider must be one of {sorted(_VIDEO_GEN_PROVIDERS)}"}
    duration = int(args.get("duration", 0) or 0)
    if duration <= 0:
        return {"success": False, "error": "duration must be a positive number of seconds"}

    import importlib
    client = importlib.import_module(module_name)
    return {"success": True, **client.estimate_cost(duration)}


async def _call_video_generate_submit(args: Dict[str, Any]) -> Dict[str, Any]:
    # Real money is spent the instant client.submit_video() succeeds — this
    # check is the actual safety boundary for aria.video.generate_submit
    # being in _WRITE_SAFE, not just the allowlist membership itself.
    if args.get("confirmed") is not True:
        return {
            "success": False,
            "error": (
                "confirmed must be true to submit a real (paid) video generation job. "
                "Call aria.video.generate_estimate first to see the cost, then resubmit "
                "with confirmed=true."
            ),
        }

    provider = str(args.get("provider", "")).strip().lower()
    module_name = _VIDEO_GEN_PROVIDERS.get(provider)
    if not module_name:
        return {"success": False, "error": f"provider must be one of {sorted(_VIDEO_GEN_PROVIDERS)}"}
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}

    import asyncio
    import importlib
    from functools import partial

    client = importlib.import_module(module_name)
    kwargs: Dict[str, Any] = {"duration": int(args.get("duration", 5) or 5)}
    aspect_ratio = args.get("aspect_ratio")
    if aspect_ratio:
        kwargs["aspect_ratio" if provider == "kling" else "ratio"] = aspect_ratio

    loop = asyncio.get_event_loop()
    fn = partial(client.submit_video, prompt, **kwargs)
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_workflow_plan(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_workflow import VideoWorkflowError, plan_video
    from aria_code.video_workflow.google_models import GoogleModelError

    script = str(args.get("script", "")).strip()
    paths = args.get("asset_paths") or []
    project_dir = str(args.get("project_dir", "")).strip()
    if not script or not isinstance(paths, list) or not paths or not project_dir:
        return {"success": False, "error": "script, asset_paths (non-empty list) and project_dir are required"}
    fn = partial(
        plan_video, script, [str(p) for p in paths], project_dir,
        platform=str(args.get("platform") or "douyin"),
        aspect_ratio=args.get("aspect_ratio"),
        language=str(args.get("language") or "zh"),
        target_seconds=int(args.get("target_seconds") or 30),
        allow_generation=bool(args.get("allow_generation")),
    )
    try:
        return {"success": True, **await asyncio.get_event_loop().run_in_executor(None, fn)}
    except (VideoWorkflowError, GoogleModelError) as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_workflow_render(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.video_workflow import VideoWorkflowError, render_video
    from aria_code.video_workflow.google_models import GoogleModelError

    project_dir = str(args.get("project_dir", "")).strip()
    if not project_dir:
        return {"success": False, "error": "project_dir is required"}
    fn = partial(render_video, project_dir, confirm_generation=args.get("confirm_generation") is True)
    try:
        return {"success": True, **await asyncio.get_event_loop().run_in_executor(None, fn)}
    except (VideoWorkflowError, GoogleModelError) as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_generate_status(args: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(args.get("provider", "")).strip().lower()
    module_name = _VIDEO_GEN_PROVIDERS.get(provider)
    if not module_name:
        return {"success": False, "error": f"provider must be one of {sorted(_VIDEO_GEN_PROVIDERS)}"}
    task_id = str(args.get("task_id", "")).strip()
    if not task_id:
        return {"success": False, "error": "task_id is required"}

    import asyncio
    import importlib

    client = importlib.import_module(module_name)
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, client.poll_video, task_id)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_video_engine_config(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.tools.video_tools import tool_configure_video_engine
    return tool_configure_video_engine(args)


async def _call_video_produce_from_script(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial
    from aria_code.tools.video_tools import tool_produce_video_from_script
    fn = partial(tool_produce_video_from_script, args)
    return await asyncio.get_event_loop().run_in_executor(None, fn)


async def _call_video_project_create(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.tools.video_tools import tool_create_video_project
    return tool_create_video_project(args)


async def _call_video_shot_regenerate(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial
    from aria_code.tools.video_tools import tool_regenerate_video_shot
    fn = partial(tool_regenerate_video_shot, args)
    return await asyncio.get_event_loop().run_in_executor(None, fn)


async def _call_report_docx(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.artifacts import create_user_artifact
    from aria_code.report_exporters import markdown_to_docx

    md = str(args.get("markdown", ""))
    if not md.strip():
        return {"success": False, "error": "markdown is required"}
    title = str(args.get("title") or "report").strip() or "report"

    artifact = create_user_artifact("report", title, title, ".docx")
    loop = asyncio.get_event_loop()
    try:
        result_path = await loop.run_in_executor(None, markdown_to_docx, md, artifact.path, title)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not result_path:
        return {"success": False, "error": "DOCX rendering failed (python-docx not installed?)"}
    return {"success": True, "path": str(result_path)}


async def _call_report_pptx(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.artifacts import create_user_artifact
    from aria_code.report_exporters import markdown_to_pptx

    md = str(args.get("markdown", ""))
    if not md.strip():
        return {"success": False, "error": "markdown is required"}
    title = str(args.get("title") or "report").strip() or "report"

    artifact = create_user_artifact("report", title, title, ".pptx")
    loop = asyncio.get_event_loop()
    try:
        result_path = await loop.run_in_executor(None, markdown_to_pptx, md, artifact.path, title)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not result_path:
        return {"success": False, "error": "PPTX rendering failed (python-pptx not installed?)"}
    return {"success": True, "path": str(result_path)}


async def _call_estimate_image_cost(args: Dict[str, Any]) -> Dict[str, Any]:
    from aria_code.openai_image_client import estimate_cost

    return {"success": True, **estimate_cost(size=args.get("size") or "1024x1536", quality=args.get("quality") or "high")}


async def _call_generate_image(args: Dict[str, Any]) -> Dict[str, Any]:
    # Real money is spent the instant generate_image() calls OpenAI — this
    # check is the actual safety boundary, not just _WRITE_SAFE membership
    # (same pattern as _call_video_generate_submit).
    if args.get("confirmed") is not True:
        return {
            "success": False,
            "error": (
                "confirmed must be true to generate a real (paid) image. "
                "Call aria.report.estimate_image_cost first to see the cost, then resubmit with confirmed=true."
            ),
        }

    import asyncio
    from functools import partial

    from aria_code.openai_image_client import generate_image

    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}

    loop = asyncio.get_event_loop()
    try:
        fn = partial(
            generate_image, prompt,
            size=args.get("size") or "1024x1536", quality=args.get("quality") or "high",
            confirmed=True,
        )
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_edit_image(args: Dict[str, Any]) -> Dict[str, Any]:
    # Same real-money gate as _call_generate_image above.
    if args.get("confirmed") is not True:
        return {
            "success": False,
            "error": (
                "confirmed must be true to generate a real (paid) image edit. "
                "Call aria.report.estimate_image_cost first to see the cost, then resubmit with confirmed=true."
            ),
        }

    import asyncio
    from functools import partial

    from aria_code.openai_image_client import edit_image

    image_path = str(args.get("image_path", "")).strip()
    prompt = str(args.get("prompt", "")).strip()
    if not image_path or not prompt:
        return {"success": False, "error": "image_path and prompt are required"}

    mask_path = args.get("mask_path")
    mask_path = str(mask_path).strip() if mask_path else None

    loop = asyncio.get_event_loop()
    try:
        fn = partial(
            edit_image, image_path, prompt,
            size=args.get("size") or "1024x1536", quality=args.get("quality") or "high",
            mask_path=mask_path, confirmed=True,
        )
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_generate_image_local(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.local_image_provider import generate_image_local

    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}

    loop = asyncio.get_event_loop()
    fn = partial(
        generate_image_local,
        prompt,
        width=int(args.get("width", 1024) or 1024),
        height=int(args.get("height", 1024) or 1024),
        steps=int(args.get("steps", 4) or 4),
    )
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_edit_image_local(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio
    from functools import partial

    from aria_code.local_image_provider import edit_image_local

    image_path = str(args.get("image_path", "")).strip()
    prompt = str(args.get("prompt", "")).strip()
    if not image_path or not prompt:
        return {"success": False, "error": "image_path and prompt are required"}

    loop = asyncio.get_event_loop()
    fn = partial(edit_image_local, image_path, prompt, strength=float(args.get("strength", 0.6) or 0.6))
    try:
        return await loop.run_in_executor(None, fn)
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _call_canva_design(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.canva_client import autofill_design

    template_id = str(args.get("template_id", "")).strip()
    data = args.get("data") or {}
    if not template_id or not isinstance(data, dict):
        return {"success": False, "error": "template_id and data (object) are required"}

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, autofill_design, template_id, data)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return result


async def _call_canva_upload_asset(args: Dict[str, Any]) -> Dict[str, Any]:
    import asyncio

    from aria_code.canva_client import upload_asset

    file_path = str(args.get("file_path", "")).strip()
    if not file_path:
        return {"success": False, "error": "file_path is required"}

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, upload_asset, file_path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    return result


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {
    "aria.market.quote": _call_market_quote,
    "aria.agent.team": _call_agent_team,
    "aria.artifacts.list": _call_artifacts_list,
    "aria.skill.list": _call_skill_list,
    "aria.skill.get": _call_skill_get,
    "aria.broker.positions": _call_broker_positions,
    "aria.broker.list_previews": _call_broker_list_previews,
    "aria.broker.preview_order": _call_broker_preview_order,
    "aria.broker.confirm_order": _call_broker_confirm_order,
    "aria.report.chart": _call_report_chart,
    "aria.report.indicator_chart": _call_indicator_chart,
    "aria.report.comparison_chart": _call_comparison_chart,
    "aria.report.allocation_chart": _call_allocation_chart,
    "aria.report.pdf": _call_report_pdf,
    "aria.report.estimate_image_cost": _call_estimate_image_cost,
    "aria.report.generate_image": _call_generate_image,
    "aria.report.edit_image": _call_edit_image,
    "aria.report.generate_image_local": _call_generate_image_local,
    "aria.report.edit_image_local": _call_edit_image_local,
    "aria.report.canva_design": _call_canva_design,
    "aria.report.canva_upload_asset": _call_canva_upload_asset,
    "aria.report.docx": _call_report_docx,
    "aria.report.pptx": _call_report_pptx,
    "aria.figma.read_file": _call_figma_read_file,
    "aria.figma.nodes": _call_figma_nodes,
    "aria.figma.export_images": _call_figma_export_images,
    "aria.figma.variables": _call_figma_variables,
    "aria.figma.comments": _call_figma_comments,
    "aria.video.probe": _call_video_probe,
    "aria.video.trim": _call_video_trim,
    "aria.video.concat": _call_video_concat,
    "aria.video.overlay_text": _call_video_overlay_text,
    "aria.video.overlay_audio": _call_video_overlay_audio,
    "aria.video.convert": _call_video_convert,
    "aria.video.change_speed": _call_video_change_speed,
    "aria.video.transcribe": _call_video_transcribe,
    "aria.video.detect_scenes": _call_video_detect_scenes,
    "aria.video.generate_estimate": _call_video_generate_estimate,
    "aria.video.generate_submit": _call_video_generate_submit,
    "aria.video.generate_status": _call_video_generate_status,
    "aria.video.workflow_plan": _call_video_workflow_plan,
    "aria.video.workflow_render": _call_video_workflow_render,
    "aria.video.engine_config": _call_video_engine_config,
    "aria.video.produce_from_script": _call_video_produce_from_script,
    "aria.video.project_create": _call_video_project_create,
    "aria.video.shot_regenerate": _call_video_shot_regenerate,
    "aria.report.generate": _call_report_generate,
    "aria.backtest.run": _call_backtest_run,
}


def _build_tools() -> List[Dict[str, Any]]:
    """Real MCP tool descriptors — read-only exposures, plus the explicit
    _WRITE_SAFE allowlist.

    Hard filter, not just trust in the exposure list: any exposure whose
    target is skill-backed (not a clean callable in v1), or that isn't
    read_only AND isn't in _WRITE_SAFE, is dropped here — so a future
    careless edit to default_exposures() can't silently make a live-trading
    or otherwise unreviewed write tool reachable. Adding something to
    _WRITE_SAFE is a deliberate, individually-justified code change, not a
    side effect of editing the exposure list.
    """
    from aria_code.packages.aria_mcp.bridge import default_exposures

    tools = []
    for exposure in default_exposures():
        if not exposure.read_only and exposure.name not in _WRITE_SAFE:
            continue
        if exposure.target.startswith("skill:"):
            continue
        if exposure.name not in _HANDLERS:
            continue
        tools.append({
            "name": exposure.name,
            "description": exposure.description,
            "inputSchema": _INPUT_SCHEMAS.get(exposure.name, {"type": "object", "properties": {}}),
        })
    return tools


TOOLS: List[Dict[str, Any]] = _build_tools()
_TOOL_NAMES = {t["name"] for t in TOOLS}


# ---------------------------------------------------------------------------
# MCP stdio server (JSON-RPC 2.0) — same wire format as
# Arthera's packages/quant_engine/mcp_server.py
# ---------------------------------------------------------------------------

def _reply(req_id: Any, result: Any) -> None:
    msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _error(req_id: Any, code: int, message: str) -> None:
    msg = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


async def _handle(req: Dict[str, Any]) -> None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        _reply(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "aria-code", "version": "1.0.0"},
        })

    elif method == "notifications/initialized":
        pass  # no response needed for notifications

    elif method == "tools/list":
        _reply(req_id, {"tools": TOOLS})

    elif method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {}) or {}
        if name not in _TOOL_NAMES:
            _error(req_id, -32601, f"Unknown or non-exposed tool: {name}")
            return
        try:
            result = await _HANDLERS[name](arguments)
            text = json.dumps(result, ensure_ascii=False, default=str)
            _reply(req_id, {"content": [{"type": "text", "text": text}]})
        except TypeError as exc:
            _error(req_id, -32602, f"Invalid arguments for {name}: {exc}")
        except Exception as exc:
            _error(req_id, -32603, f"Tool error: {exc}\n{traceback.format_exc()[-300:]}")

    elif method == "ping":
        _reply(req_id, {})

    else:
        if req_id is not None:
            _error(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    import asyncio

    # --version/-V is not part of the MCP wire protocol — it exists purely
    # so a packaged binary (PyInstaller, `aria-code-mcp --version`) can be
    # smoke-tested the same way aria_cli.py's binary is, without having to
    # speak JSON-RPC over stdin just to prove the executable launches.
    # Reads the version via importlib.metadata rather than `from aria_cli
    # import __version__` deliberately — aria_cli.py's top-level imports
    # pull in the whole interactive-CLI stack (prompt_toolkit, bootstrap
    # hooks, ...), which is unnecessary weight for a binary whose only job
    # here is speaking JSON-RPC over stdio.
    if any(arg in ("--version", "-V") for arg in sys.argv[1:]):
        try:
            from importlib.metadata import version as _pkg_version
            v = _pkg_version("aria-code")
        except Exception:
            v = "unknown"
        print(f"aria-code-mcp {v}")
        return

    logger.info("aria-code MCP server starting (stdio transport, %d tools)", len(TOOLS))

    async def _run() -> None:
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("JSON parse error: %s", exc)
                continue
            try:
                await _handle(req)
            except Exception as exc:
                logger.error("Unhandled error: %s\n%s", exc, traceback.format_exc())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
