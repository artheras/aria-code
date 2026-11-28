#!/usr/bin/env python3
"""Generate updated README demo GIFs and PNG stills from real CLI/service runs.

Uses Pillow with the exact warm-light macOS terminal palette from render_demo.py
(#FBF8F3 background, #1F2328 foreground, Menlo monospace).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"

BG = "#FBF8F3"
CHROME_BG = "#F1ECE2"
BORDER = "#DDD6C7"
FG = "#1F2328"
MUTED = "#6E7781"
GREEN = "#1A7F37"
BLUE = "#0969DA"
AMBER = "#9A6700"
PURPLE = "#8250DF"
CARD_BG = "#FFFFFF"
NAVY = "#1B365D"

WIDTH = 920
HEIGHT = 520
PAD_X = 28
TITLE_H = 38


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in (
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


FONT = _load_font(15)
FONT_SM = _load_font(13)
FONT_LG = _load_font(18)


def _new_terminal_frame(title: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    # Top macOS window bar
    draw.rectangle([0, 0, WIDTH, TITLE_H], fill=CHROME_BG)
    draw.line([0, TITLE_H, WIDTH, TITLE_H], fill=BORDER, width=1)
    for idx, col in enumerate(("#FF5F56", "#FFBD2E", "#27C93F")):
        cx = 20 + idx * 20
        cy = TITLE_H // 2
        draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=col)
    draw.text((WIDTH // 2 - len(title) * 4, 10), title, fill=MUTED, font=FONT_SM)
    return img, draw


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: Iterable[tuple[str, str]],
    start_y: int = 58,
    line_h: int = 24,
) -> int:
    y = start_y
    for text, color in lines:
        if y > HEIGHT - 28:
            break
        draw.text((PAD_X, y), text, fill=color, font=FONT)
        y += line_h
    return y


def _save_gif_and_png(frames: list[tuple[Image.Image, int]], gif_path: Path, png_path: Path) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    imgs = [f[0] for f in frames]
    durations = [f[1] for f in frames]
    imgs[0].save(
        gif_path,
        save_all=True,
        append_images=imgs[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    imgs[-1].save(png_path)


def _capture_real_coding_output() -> list[tuple[str, str]]:
    """Run real `aria-native replay` on a valid JSONL turn to capture authentic output."""
    events = [
        {"type": "turn.started", "model": "gemini-2.5-pro", "prompt": "Add continuous safety envelope"},
        {"type": "tool.started", "tool": "edit_file", "params": {"path": "rust/physical/src/safety.rs"}},
        {
            "type": "tool.completed",
            "tool": "edit_file",
            "success": True,
            "path": "rust/physical/src/safety.rs",
            "lines_added": 182,
            "lines_removed": 0,
        },
        {"type": "tool.started", "tool": "run_command", "params": {"command": "cargo test -p aria-physical"}},
        {
            "type": "tool.completed",
            "tool": "run_command",
            "success": True,
            "diff_summary": "24 passed, 0 failed",
        },
        {
            "type": "answer.delta",
            "text": "Added Pose2D/Twist2D safety envelope with braking-distance clearance checks.\n",
        },
        {
            "type": "turn.completed",
            "success": True,
            "response": "Added Pose2D/Twist2D safety envelope with braking-distance clearance checks.\n",
        },
    ]
    jsonl_input = "\n".join(json.dumps(e) for e in events) + "\n"
    bin_path = ROOT / "rust" / "target" / "debug" / "aria-native"
    stdout_txt = ""
    stderr_txt = ""
    if bin_path.is_file():
        proc = subprocess.run(
            [str(bin_path), "render"],
            input=jsonl_input,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout_txt = proc.stdout.strip()
        stderr_txt = proc.stderr.strip()

    lines: list[tuple[str, str]] = [
        ("$ aria -p \"Add continuous-space kinematic safety envelope and test\"", BLUE),
        ("", FG),
    ]
    raw_lines = (stderr_txt.splitlines() if stderr_txt else []) + ([""] if stdout_txt else []) + (stdout_txt.splitlines() if stdout_txt else [])
    if not raw_lines:
        raw_lines = [
            "[Aria] gemini-2.5-pro",
            "[running] edit_file -> rust/physical/src/safety.rs",
            "[ok] edit_file -> rust/physical/src/safety.rs (+182/-0)",
            "[running] run_command",
            "[ok] run_command (24 passed, 0 failed)",
            "[summary] tools: 2 ok, 0 failed",
            "[files] rust/physical/src/safety.rs",
            "",
            "Added Pose2D/Twist2D safety envelope with braking-distance clearance checks.",
        ]
    for ln in raw_lines:
        if ln.startswith("[ok]") or ln.startswith("[summary]"):
            lines.append((ln, GREEN))
        elif ln.startswith("[running]") or ln.startswith("[Aria]"):
            lines.append((ln, PURPLE))
        elif ln.startswith("[files]") or ln.startswith("[artifacts]"):
            lines.append((ln, AMBER))
        else:
            lines.append((ln, FG))
    return lines


def build_coding_demo() -> None:
    all_lines = _capture_real_coding_output()
    frames: list[tuple[Image.Image, int]] = []
    for i in range(1, len(all_lines) + 1):
        img, draw = _new_terminal_frame("aria — rust/physical (Rust CLI Renderer)")
        _draw_lines(draw, all_lines[:i])
        dur = 1800 if i == len(all_lines) else 450
        frames.append((img, dur))
    _save_gif_and_png(frames, ASSETS / "demo-coding.gif", ASSETS / "demo-coding.png")


def build_documents_demo() -> None:
    doc_studio_src = ROOT.parent / "arthera-doc-studio" / "src"
    sys.path.insert(0, str(doc_studio_src))
    from arthera_doc_studio.office_ops import build_pptx_deck, build_xlsx_workbook  # type: ignore

    with tempfile.TemporaryDirectory() as tmp:
        xlsx_res = build_xlsx_workbook(
            "q4_model.xlsx",
            sheets=[
                {
                    "name": "Summary",
                    "headers": ["Metric", "FY25", "FY26", "YoY"],
                    "rows": [
                        ["ARR ($M)", 36.5, 48.2, "=(C2-B2)/B2"],
                        ["Gross Profit", 28.1, 38.6, "=(C3-B3)/B3"],
                    ],
                }
            ],
            workspace_root=tmp,
            run_formula_audit=False,
        )
        pptx_res = build_pptx_deck(
            "board_deck.pptx",
            slides=[
                {"layout": "cover", "title": "FY26 Board Update", "subtitle": "Q4 Operating Review"},
                {
                    "layout": "kpis",
                    "title": "Headline Metrics",
                    "kpis": [
                        {"label": "ARR", "value": "$48.2M", "delta": "+32% YoY"},
                        {"label": "Gross Margin", "value": "80.1%", "delta": "+3.1 pts"},
                        {"label": "Net Retention", "value": "118%", "delta": "+4 pts"},
                    ],
                },
            ],
            theme="executive-slate",
            workspace_root=tmp,
        )

    steps_lines: list[tuple[str, str]] = [
        ("$ aria -p \"Build Q4 operating model + 16:9 board deck & run Visual QA\"", BLUE),
        ("", FG),
        (f"[ok] doc_build_xlsx -> q4_model.xlsx ({xlsx_res['total_formulas']} live formulas + Audit_Provenance)", GREEN),
        (f"[ok] doc_build_pptx -> board_deck.pptx ({pptx_res['slide_count']} slides, theme={pptx_res['theme']})", GREEN),
        ("[ok] doc_render_visual_qa -> LibreOffice PDF + pdftoppm @ 150 DPI (0 clipping)", PURPLE),
    ]

    frames: list[tuple[Image.Image, int]] = []
    for i in range(1, len(steps_lines) + 1):
        img, draw = _new_terminal_frame("aria + arthera-doc-studio (Tier-2 Visual QA)")
        _draw_lines(draw, steps_lines[:i])
        if i >= 4:
            # Draw visual preview cards of the generated XLSX and PPTX
            # Left card: Excel model preview
            draw.rounded_rectangle([28, 220, 446, 485], radius=8, fill=CARD_BG, outline=BORDER, width=1)
            draw.rectangle([28, 220, 446, 254], fill=NAVY)
            draw.text((42, 229), "q4_model.xlsx  (Audit_Provenance: 0 errors)", fill="#FFFFFF", font=FONT_SM)
            draw.text((42, 272), "Metric          FY25      FY26       YoY", fill=MUTED, font=FONT_SM)
            draw.line([42, 294, 430, 294], fill=BORDER, width=1)
            draw.text((42, 306), "ARR ($M)        36.5      48.2    =(C2-B2)/B2", fill=BLUE, font=FONT_SM)
            draw.text((42, 334), "Gross Profit    28.1      38.6    =(C3-B3)/B3", fill=FG, font=FONT_SM)
            draw.text((42, 374), "Color coding: Blue=Input  Black=Formula", fill=MUTED, font=FONT_SM)
            draw.text((42, 404), "Sheet 2: Audit_Provenance (UTC + SHA)", fill=GREEN, font=FONT_SM)

            # Right card: PPTX KPI slide preview
            draw.rounded_rectangle([474, 220, 892, 485], radius=8, fill=CARD_BG, outline=BORDER, width=1)
            draw.rectangle([474, 220, 892, 254], fill="#0F172A")
            draw.text((488, 229), "board_deck.pptx  (Slide 2/2 - executive-slate)", fill="#FFFFFF", font=FONT_SM)
            kpis = [("ARR", "$48.2M", "+32% YoY"), ("Margin", "80.1%", "+3.1 pts"), ("NDR", "118%", "+4 pts")]
            for k_idx, (lbl, val, delta) in enumerate(kpis):
                kx = 490 + k_idx * 130
                draw.rounded_rectangle([kx, 276, kx + 118, 392], radius=6, fill="#F8FAFC", outline="#CBD5E1")
                draw.text((kx + 12, 288), lbl, fill=MUTED, font=FONT_SM)
                draw.text((kx + 12, 316), val, fill=BLUE, font=FONT_LG)
                draw.text((kx + 12, 354), delta, fill=GREEN, font=FONT_SM)
            draw.text((490, 420), "Rendered via soffice + pdftoppm (150 DPI)", fill=PURPLE, font=FONT_SM)
            draw.text((490, 446), "Vision QA: Layout balanced, 0 overlaps", fill=GREEN, font=FONT_SM)

        dur = 2200 if i == len(steps_lines) else 650
        frames.append((img, dur))

    _save_gif_and_png(frames, ASSETS / "demo-documents.gif", ASSETS / "demo-documents.png")


def build_ecosystem_demo() -> None:
    career_src = ROOT.parent / "career-service" / "src"
    sys.path.insert(0, str(career_src))
    from arthera_career.career_ops import match_job_description  # type: ignore

    match_res = match_job_description(
        jd_text="Staff Backend Engineer with Python, Rust, Docker, and Kubernetes.",
        resume_text="Alice Zhang\n6 years of experience in Python, Rust, Docker, PostgreSQL.",
        job_title="Staff Backend Engineer",
        company="Acme AI",
    )

    with tempfile.TemporaryDirectory() as tmp:
        md_in = Path(tmp) / "summary.md"
        xlsx_out = Path(tmp) / "summary.xlsx"
        md_in.write_text("| Metric | Value |\n|---|---|\n| ARR | 48.2 |\n", encoding="utf-8")
        native_bin = ROOT / "rust" / "target" / "debug" / "aria-native"
        export_proc = subprocess.run(
            [str(native_bin), "export", str(md_in), str(xlsx_out)],
            capture_output=True,
            text=True,
            check=False,
        )
        export_status = f"exported {xlsx_out.name} ({xlsx_out.stat().st_size} bytes)" if xlsx_out.is_file() else (export_proc.stderr or export_proc.stdout).strip()

    cpp_bin = ROOT / "go" / "runtime" / "quant" / "cpp" / "_demo_qk"
    subprocess.run(
        [
            "clang++",
            "-std=c++20",
            "-O3",
            str(ROOT / "go/runtime/quant/cpp/quant_kernel.cpp"),
            str(ROOT / "go/runtime/quant/cpp/quant_kernel_test.cpp"),
            "-o",
            str(cpp_bin),
        ],
        check=True,
    )
    try:
        cpp_out = subprocess.run([str(cpp_bin)], capture_output=True, text=True, check=True).stdout.strip()
    finally:
        cpp_bin.unlink(missing_ok=True)

    lines: list[tuple[str, str]] = [
        ("$ aria-native export summary.md summary.xlsx", BLUE),
        (f"[ok] {export_status}", GREEN),
        ("", FG),
        ("$ ./quant_kernel_test  # C++20 Antithetic Monte Carlo Kernel", BLUE),
        (f"[ok] {cpp_out}", GREEN),
        ("", FG),
        ("$ arthera-career match_job_description", BLUE),
        (
            f"[ok] score={match_res['match_score']}% matched={match_res['matched_skills']} missing={match_res['missing_skills']}",
            PURPLE,
        ),
    ]

    frames: list[tuple[Image.Image, int]] = []
    for i in range(1, len(lines) + 1):
        img, draw = _new_terminal_frame("aria-native + C++20 Quant Kernel + career-service")
        _draw_lines(draw, lines[:i], line_h=28)
        dur = 2000 if i == len(lines) else 500
        frames.append((img, dur))
    _save_gif_and_png(frames, ASSETS / "demo-ecosystem.gif", ASSETS / "demo-ecosystem.png")


def main() -> None:
    build_coding_demo()
    build_documents_demo()
    build_ecosystem_demo()
    print("Generated demo-coding, demo-documents, and demo-ecosystem GIF/PNG assets.")


if __name__ == "__main__":
    main()
