"""Describe how Aria packages should be exposed over MCP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class MCPExposure:
    name: str
    target: str
    description: str
    read_only: bool = True

    def to_tool_descriptor(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "target": self.target,
            "read_only": self.read_only,
        }


def default_exposures() -> List[MCPExposure]:
    """Initial MCP server surface for Aria Code.

    These names are stable contracts. The server implementation can bind them
    to ToolRegistry/AgentRegistry without changing client-facing tool names.
    """

    return [
        MCPExposure("aria.market.quote", "tool:get_market_data", "Fetch quote and technical market snapshot."),
        MCPExposure("aria.agent.team", "agent:team", "Run multi-agent financial research."),
        MCPExposure("aria.report.generate", "tool:report_generate", "Fetch data + generate a full HTML research report artifact for a symbol.", read_only=False),
        MCPExposure("aria.backtest.run", "tool:backtest_run", "Run a historical strategy simulation (buy_hold or sma_cross) against real price history.", read_only=False),
        MCPExposure("aria.artifacts.list", "infra:artifacts", "List local generated artifacts."),
        MCPExposure(
            "aria.skill.list",
            "infra:skills",
            "List the installed portable SKILL.md workflows — reusable expert playbooks "
            "for design, research, and engineering tasks (e.g. UI design systems, "
            "anti-generic-AI-look UI critique, trading-app patterns, equity research, "
            "backtest validation). Pass `query` to rank by relevance to a task "
            "description instead of listing everything.",
        ),
        MCPExposure(
            "aria.skill.get",
            "infra:skills",
            "Fetch one skill's full instructions by name (from aria.skill.list). Returns "
            "the SKILL.md workflow text for you to follow yourself — skills are "
            "instruction documents, not executable tools, so nothing runs server-side. "
            "The response lists any bundled `references` docs; when the instructions tell "
            "you to read one, call this again with `reference` set to that filename.",
        ),
        MCPExposure("aria.broker.positions", "tool:broker_positions", "Read account info + positions for a configured broker."),
        MCPExposure("aria.broker.list_previews", "tool:broker_list_previews", "List recent order previews, including TradingView-triggered ones."),
        MCPExposure(
            "aria.broker.preview_order",
            "tool:broker_preview_order",
            "Build a risk-checked order preview. Never executes a trade by itself — "
            "actual execution needs either a human running /trade confirm in the aria-code "
            "terminal, or aria.broker.confirm_order if the user explicitly opted that broker "
            "into chat-confirmed execution (off by default; see aria.broker.confirm_order).",
            read_only=False,
        ),
        MCPExposure(
            "aria.broker.confirm_order",
            "tool:broker_confirm_order",
            "Execute a previously built order preview — REAL MONEY. Refuses unless the user "
            "already ran /trade allow-chat-confirm <broker_id> in the aria-code terminal "
            "themselves (a real trade cannot be confirmed for the first time from chat; that "
            "opt-in can only happen at the terminal keyboard) AND this call passes confirmed=true.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.chart",
            "tool:report_chart",
            "Generate a candlestick + moving-average chart PNG for a symbol.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.indicator_chart",
            "tool:report_indicator_chart",
            "Generate a candlestick + volume + RSI(14) + MACD(12,26,9) multi-panel chart "
            "PNG for a symbol — for when you need the indicators plotted, not just price.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.comparison_chart",
            "tool:report_comparison_chart",
            "Generate a normalized (base=100) % return comparison chart PNG across 2+ symbols.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.allocation_chart",
            "tool:report_allocation_chart",
            "Generate a pie chart PNG of a broker account's position weights by market value.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.pdf",
            "tool:report_pdf",
            "Render Markdown text to a styled PDF report.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.canva_upload_asset",
            "tool:canva_upload_asset",
            "Upload a local image (e.g. a chart PNG from aria.report.chart) to Canva and "
            "return its asset_id, for use in aria.report.canva_design's data field. "
            "Requires /canva connect to have been run first.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.estimate_image_cost",
            "tool:openai_estimate_image_cost",
            "Get an illustrative per-image cost estimate (USD) for OpenAI's gpt-image-1 "
            "at a given size/quality, before generating. Free, no API call to OpenAI.",
        ),
        MCPExposure(
            "aria.report.generate_image",
            "tool:openai_generate_image",
            "Generate a new image from a text prompt (e.g. minimal-editorial-poster's "
            "compiled prompt) via OpenAI's gpt-image-1. Real per-call cost, billed by "
            "OpenAI the instant this succeeds — requires confirmed=true, and calling "
            "aria.report.estimate_image_cost first is strongly recommended. Requires an "
            "OpenAI API key (/apikey set openai sk-... or OPENAI_API_KEY).",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.edit_image",
            "tool:openai_edit_image",
            "Transform an existing local photo per a text prompt (duotone, background "
            "simplification, texture overlay, etc.) via OpenAI's gpt-image-1 edits "
            "endpoint. Pass mask_path for inpainting — a local mask PNG where transparent "
            "areas mark what to edit and opaque areas are left untouched — to constrain "
            "the edit to part of the image instead of the whole thing. Real per-call cost, "
            "billed by OpenAI the instant this succeeds — requires confirmed=true, and "
            "calling aria.report.estimate_image_cost first is strongly recommended. "
            "Requires an OpenAI API key.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.generate_image_local",
            "tool:local_generate_image",
            "Generate a new image from a text prompt entirely locally via a self-hosted "
            "open-weight model (SDXL-Turbo by default) — no API key, no per-call cost, "
            "runs on this machine. Needs the optional 'image_gen' extra installed. "
            "First call for a given model downloads its weights (several GB).",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.edit_image_local",
            "tool:local_edit_image",
            "Transform an existing local photo per a text prompt, entirely locally "
            "(image-to-image, no API key). Needs the optional 'image_gen' extra installed.",
            read_only=False,
        ),
        MCPExposure(
            "aria.report.canva_design",
            "tool:canva_autofill",
            "Fill a Canva brand template with data and export the design draft. "
            "Requires /canva connect to have been run first.",
            read_only=False,
        ),
        MCPExposure("aria.report.docx", "tool:report_docx", "Render Markdown text to an editable Word document.", read_only=False),
        MCPExposure("aria.report.pptx", "tool:report_pptx", "Render Markdown text to an editable slide deck (one slide per heading).", read_only=False),
        MCPExposure("aria.figma.read_file", "tool:figma_read_file", "Read a Figma file's page/frame structure (depth-limited summary, not the full node tree). Read-only — Figma has no API for writing designs."),
        MCPExposure("aria.figma.nodes", "tool:figma_nodes", "Inspect detailed node structure, layout, typography, fills, strokes, and effects for specific node IDs or a Figma URL."),
        MCPExposure("aria.figma.export_images", "tool:figma_export_images", "Export and render Figma components/frames/vectors as image URLs (SVG, PNG, JPG, PDF)."),
        MCPExposure("aria.figma.variables", "tool:figma_variables", "Extract local Figma variables and token collections (colors, typography, dimensions)."),
        MCPExposure("aria.figma.comments", "tool:figma_comments", "List comments on a Figma file."),
        MCPExposure("aria.video.probe", "tool:video_probe", "Read a video's duration/resolution/codec info."),
        MCPExposure("aria.video.trim", "tool:video_trim", "Cut a [start, end] second range out of a local video.", read_only=False),
        MCPExposure("aria.video.concat", "tool:video_concat", "Concatenate multiple local videos in order.", read_only=False),
        MCPExposure("aria.video.overlay_text", "tool:video_overlay_text", "Burn a text overlay onto a local video (top/bottom/center).", read_only=False),
        MCPExposure("aria.video.overlay_audio", "tool:video_overlay_audio", "Add or replace a local video's audio track.", read_only=False),
        MCPExposure("aria.video.convert", "tool:video_convert", "Convert a local video's format and/or reframe to a target aspect ratio (e.g. 9:16 for vertical).", read_only=False),
        MCPExposure("aria.video.change_speed", "tool:video_change_speed", "Speed up or slow down a local video (pitch-corrected audio).", read_only=False),
        MCPExposure("aria.video.transcribe", "tool:video_transcribe", "Transcribe a local video's speech track entirely locally (faster-whisper, no API key). Needs the optional 'video_analysis' extra installed."),
        MCPExposure("aria.video.detect_scenes", "tool:video_detect_scenes", "Detect scene-cut timestamps in a local video via frame-histogram comparison. Needs the optional 'video' extra (opencv) installed."),
        MCPExposure(
            "aria.video.generate_estimate",
            "tool:video_generate_estimate",
            "Get an illustrative cost estimate (USD) for a cloud AI video generation job on "
            "'kling' or 'runway', before submitting. Free, no API call to the provider.",
        ),
        MCPExposure(
            "aria.video.generate_submit",
            "tool:video_generate_submit",
            "Submit a real cloud AI text-to-video generation job on 'kling' or 'runway'. "
            "Real per-request cost (billed by the provider the instant this succeeds) — requires "
            "confirmed=true, and calling aria.video.generate_estimate first is strongly recommended. "
            "Returns a task_id immediately; poll with aria.video.generate_status (generation takes minutes).",
            read_only=False,
        ),
        MCPExposure(
            "aria.video.generate_status",
            "tool:video_generate_status",
            "Check a submitted cloud video generation job's status; downloads the result once "
            "succeeded. Checking status costs nothing beyond the original submission.",
        ),
        MCPExposure(
            "aria.video.workflow_plan",
            "tool:video_workflow_plan",
            "Plan a short video from a script and image/clip files with Google Cloud Gemini: describes every "
            "asset, then writes an edit plan (beats with narration, captions, camera moves) into project_dir. "
            "Show the plan to the user before rendering; they may edit edit_plan.json. Creates nothing paid.",
            read_only=False,
        ),
        MCPExposure(
            "aria.video.workflow_render",
            "tool:video_workflow_render",
            "Render the edit plan in project_dir into a finished video: Gemini TTS narration, ffmpeg render, "
            "captions, and file checks (duration, frame size, audio, black frames, silence). Beats marked "
            "source=generate use Veo, billed per second — without confirm_generation=true it returns "
            "needs_confirmation with the seconds instead of rendering.",
            read_only=False,
        ),
        MCPExposure(
            "aria.video.engine_config",
            "tool:video_engine_config",
            "Inspect, switch, or configure the active video generation engine (Google Cloud Veo, custom private model endpoint, or local).",
            read_only=False,
        ),
        MCPExposure(
            "aria.video.produce_from_script",
            "tool:video_produce_from_script",
            "Produce a finished video from script and images end-to-end via the active video engine, with storyboard planning, audio ducking, and subtitles.",
            read_only=False,
        ),
    ]
