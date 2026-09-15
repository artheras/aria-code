#!/usr/bin/env python3
"""generate_demo_video.py — Universal AI Coding Agent Demo Video Generator for Aria Code.

Showcases Aria Code as a general-purpose AI product reviewer and software engineering agent
(like Claude Code / Codex / OpenAI Operator) across any stack and industry:
  - Repository understanding & architectural analysis
  - Multi-file code generation, refactoring, and bug fixes
  - Automated unit test generation & test suite execution
  - Live Shell mode and typed context references (@ and !)
  - 100% authentic Aria Code light terminal UI with pixel mascot & session sync

Outputs:
  - docs/assets/aria_code_demo.mp4 (1080p 60fps / 30fps H.264 video with English narration)
  - docs/assets/aria_code_demo.gif (High-quality GIF for README and documentation)
"""

import os
import sys
import subprocess
import time
import shutil
from typing import List, Tuple
from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
FPS = 30
OUTPUT_MP4 = "docs/assets/aria_code_demo.mp4"
OUTPUT_GIF = "docs/assets/aria_code_demo.gif"

# ── Color Palette (Exact match to Aria Code Light Theme) ─────────────────────
BG_CANVAS       = (255, 255, 255)    # Clean terminal white
TEXT_DARK       = (31, 35, 40)       # Primary dark text #1F2328
TEXT_MUTED      = (87, 96, 106)      # Secondary gray #57606A
TEXT_SUBTLE     = (110, 119, 129)    # Subtle gray #6E7781
TEXT_DIM        = (140, 149, 159)    # Dim placeholders #8C959F
BORDER_LINE     = (208, 215, 222)    # Border line #D0D7DE
BOX_BORDER      = (140, 149, 159)    # Dashboard box border #8C959F

# Accent Colors
COPPER_ACCENT   = (192, 128, 80)     # Aria Mascot copper #C08050
AMBER_ACCENT    = (154, 103, 0)      # Suggestions #9A6700
GREEN_SUCCESS   = (26, 127, 55)      # Positive change #1A7F37
RED_NEGATIVE    = (207, 34, 46)      # Negative change #CF222E
CYAN_HIGHLIGHT  = (9, 105, 218)      # Blue/Cyan links #0969DA
PURPLE_PROMPT   = (130, 80, 223)     # Prompt symbol
BADGE_BG        = (234, 238, 242)    # Badge background #EAEEF2
DIFF_ADD_BG     = (220, 255, 220)    # Diff add light green
DIFF_DEL_BG     = (255, 225, 225)    # Diff delete light red

# Robot Mascot Colors
ROBOT_SHELL     = (232, 226, 212)    # #E8E2D4
ROBOT_SCREEN    = (13, 17, 23)       # #0D1117
ROBOT_EYE_WHITE = (246, 242, 234)    # #F6F2EA
ROBOT_EYE_COPPER= (192, 128, 80)     # #C08050
ROBOT_EAR       = (157, 148, 136)    # #9D9488
ROBOT_LEG       = (138, 129, 118)    # #8A8176

# ── Fonts ────────────────────────────────────────────────────────────────────
FONT_TEXT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_CODE_PATH = "/System/Library/Fonts/Menlo.ttc"

FONT_TITLE_MD  = ImageFont.truetype(FONT_BOLD_PATH, 22)
FONT_TEXT_BASE = ImageFont.truetype(FONT_TEXT_PATH, 19)
FONT_TEXT_BOLD = ImageFont.truetype(FONT_BOLD_PATH, 19)
FONT_TEXT_SM   = ImageFont.truetype(FONT_TEXT_PATH, 16)
FONT_CODE_BASE = ImageFont.truetype(FONT_CODE_PATH, 18)
FONT_CODE_BOLD = ImageFont.truetype(FONT_CODE_PATH, 18)
FONT_CODE_SM   = ImageFont.truetype(FONT_CODE_PATH, 15)


def draw_robot(draw: ImageDraw.ImageDraw, rx: int, ry: int):
    """Draws the authentic Aria pixel mascot."""
    # Ears
    draw.rectangle([rx - 12, ry + 16, rx, ry + 38], fill=ROBOT_EAR)
    draw.rectangle([rx - 8, ry + 23, rx - 4, ry + 31], fill=ROBOT_EYE_COPPER)
    draw.rectangle([rx + 104, ry + 16, rx + 116, ry + 38], fill=ROBOT_EAR)
    draw.rectangle([rx + 108, ry + 23, rx + 112, ry + 31], fill=ROBOT_EYE_COPPER)

    # Shell Body
    draw.rectangle([rx, ry, rx + 104, ry + 56], fill=ROBOT_SHELL)

    # Dark Screen
    draw.rectangle([rx + 12, ry + 9, rx + 92, ry + 46], fill=ROBOT_SCREEN)

    # Left Eye (White Square)
    draw.rectangle([rx + 24, ry + 22, rx + 38, ry + 34], fill=ROBOT_EYE_WHITE)

    # Right Eye (Copper Dash)
    draw.rectangle([rx + 68, ry + 26, rx + 80, ry + 32], fill=ROBOT_EYE_COPPER)

    # Bottom Copper Strip
    draw.rectangle([rx, ry + 54, rx + 104, ry + 62], fill=ROBOT_EYE_COPPER)

    # 4 Legs
    for i in range(4):
        lx = rx + 12 + i * 22
        draw.rectangle([lx, ry + 70, lx + 12, ry + 82], fill=ROBOT_LEG)


class AriaGeneralAgentRenderer:
    def __init__(self):
        self.term_x = 80
        self.term_y = 50
        self.term_w = 1760
        self.term_h = 980

        # State controls
        self.current_prompt = ""
        self.prompt_mode = "Ask"
        self.status_dot_color = AMBER_ACCENT
        self.terminal_output_lines: List[Tuple[str, tuple, ImageFont.FreeTypeFont, bool]] = []
        self.show_diff_card = False

    def add_output(self, text: str, color=TEXT_DARK, font=FONT_CODE_BASE, bold=False):
        self.terminal_output_lines.append((text, color, font, bold))

    def clear_output(self):
        self.terminal_output_lines = []

    def render(self, frame_num: int) -> Image.Image:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG_CANVAS)
        draw = ImageDraw.Draw(img)

        # ── 1. Top Shell Header ──────────────────────────────────────────────
        draw.text((self.term_x, self.term_y), "Last login: Wed Aug 26 14:48:45 on ttys002", fill=TEXT_DARK, font=FONT_CODE_SM)
        
        # Shell Prompt: [-> ~ aria]
        prompt_y = self.term_y + 26
        draw.text((self.term_x, prompt_y), "-> ", fill=GREEN_SUCCESS, font=FONT_CODE_BOLD)
        draw.text((self.term_x + 30, prompt_y), "~ ", fill=CYAN_HIGHLIGHT, font=FONT_CODE_BOLD)
        draw.text((self.term_x + 55, prompt_y), "aria", fill=TEXT_DARK, font=FONT_CODE_BOLD)

        curr_y = prompt_y + 40

        # ── 2. Startup Dashboard Box ─────────────────────────────────────────
        box_x = self.term_x + 10
        box_y = curr_y
        box_w = 1480
        box_h = 195

        # Rounded border
        draw.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h], radius=10, outline=BOX_BORDER, width=1)
        
        # Title Cutout in top border
        draw.rectangle([box_x + 24, box_y - 8, box_x + 260, box_y + 8], fill=BG_CANVAS)
        draw.text((box_x + 30, box_y - 12), "Aria Code", fill=COPPER_ACCENT, font=FONT_TITLE_MD)
        draw.text((box_x + 155, box_y - 10), "v4.4.1", fill=TEXT_DIM, font=FONT_TEXT_BASE)

        # Left side: Mascot & Identity
        robot_x = box_x + 35
        robot_y = box_y + 35
        draw_robot(draw, robot_x, robot_y)

        # Text next to Mascot
        text_x = robot_x + 145
        draw.text((text_x, box_y + 38), "Welcome to Aria!", fill=TEXT_DARK, font=FONT_TEXT_BOLD)
        draw.text((text_x, box_y + 68), "Gemini 2.5 Pro", fill=TEXT_DARK, font=FONT_TEXT_BASE)
        draw.text((text_x + 140, box_y + 68), "local", fill=TEXT_MUTED, font=FONT_TEXT_BASE)
        draw.text((text_x, box_y + 96), "~/my-app", fill=TEXT_MUTED, font=FONT_TEXT_BASE)

        # Center divider line
        div_x = box_x + 640
        draw.line([div_x, box_y + 25, div_x, box_y + box_h - 25], fill=BORDER_LINE, width=1)

        # Right side: Runtime status
        rt_x = div_x + 28
        draw.text((rt_x, box_y + 35), "Runtime", fill=TEXT_DARK, font=FONT_TEXT_BOLD)
        draw.text((rt_x, box_y + 65), "workspace-write · network on", fill=TEXT_DARK, font=FONT_TEXT_BASE)
        draw.text((rt_x, box_y + 95), "local retention · Local: Ollama online", fill=TEXT_MUTED, font=FONT_TEXT_BASE)
        draw.text((rt_x, box_y + 125), "MCP 1 · 105 tools · 14 skills", fill=TEXT_MUTED, font=FONT_TEXT_BASE)

        curr_y += box_h + 16

        # ── 3. Try Suggestions (General-purpose Engineering) ─────────────────
        draw.text((box_x + 15, curr_y), "Try", fill=TEXT_MUTED, font=FONT_TEXT_BASE)
        sugg_text = "  Review repository architecture   ·   Refactor auth service & add tests   ·   Audit security risks"
        draw.text((box_x + 55, curr_y), sugg_text, fill=AMBER_ACCENT, font=FONT_TEXT_BASE)
        curr_y += 30

        # ── 4. Session Continuity Card ───────────────────────────────────────
        card_x = box_x + 15
        draw.line([card_x, curr_y, card_x, curr_y + 55], fill=TEXT_DARK, width=2)
        draw.text((card_x + 16, curr_y), "Keep working from anywhere", fill=TEXT_DARK, font=FONT_TEXT_BOLD)
        draw.text((card_x + 16, curr_y + 24), "Check progress or reply to any session from mobile app, desktop app, or", fill=TEXT_MUTED, font=FONT_TEXT_SM)
        
        link_text = "https://arthera.ai/sessions/736097dc."
        draw.text((card_x + 16, curr_y + 45), link_text, fill=TEXT_DARK, font=FONT_CODE_SM)
        draw.line([card_x + 16, curr_y + 62, card_x + 16 + int(draw.textlength(link_text, font=FONT_CODE_SM)), curr_y + 62], fill=TEXT_DARK, width=1)
        draw.text((card_x + 375, curr_y + 45), "To keep session in this terminal only, run  /remote-control", fill=TEXT_MUTED, font=FONT_CODE_SM)
        curr_y += 80

        # ── 5. Terminal Interaction & Tool Output Area ───────────────────────
        for text, color, font, bold in self.terminal_output_lines:
            if text == "---DIVIDER---":
                draw.line([self.term_x + 10, curr_y + 6, self.term_x + 1500, curr_y + 6], fill=BORDER_LINE, width=1)
                curr_y += 22
            elif text.startswith("[TOOL]"):
                t_msg = text.replace("[TOOL]", "").strip()
                draw.rounded_rectangle([self.term_x + 10, curr_y - 2, self.term_x + 1480, curr_y + 28], radius=6, fill=BADGE_BG)
                draw.text((self.term_x + 24, curr_y + 3), "✓ " + t_msg, fill=CYAN_HIGHLIGHT, font=FONT_CODE_BASE)
                curr_y += 36
            elif text.startswith("[DIFF_ADD]"):
                diff_line = text.replace("[DIFF_ADD]", "")
                draw.rectangle([self.term_x + 10, curr_y, self.term_x + 1480, curr_y + 24], fill=DIFF_ADD_BG)
                draw.text((self.term_x + 20, curr_y + 2), "+ " + diff_line, fill=GREEN_SUCCESS, font=FONT_CODE_BASE)
                curr_y += 26
            elif text.startswith("[DIFF_DEL]"):
                diff_line = text.replace("[DIFF_DEL]", "")
                draw.rectangle([self.term_x + 10, curr_y, self.term_x + 1480, curr_y + 24], fill=DIFF_DEL_BG)
                draw.text((self.term_x + 20, curr_y + 2), "- " + diff_line, fill=RED_NEGATIVE, font=FONT_CODE_BASE)
                curr_y += 26
            else:
                draw.text((self.term_x + 10, curr_y), text, fill=color, font=font)
                curr_y += 27

        # ── 6. Input Prompt Bar (Fixed near bottom) ──────────────────────────
        input_bar_y = HEIGHT - 110
        draw.line([self.term_x + 10, input_bar_y - 12, self.term_x + 1500, input_bar_y - 12], fill=BORDER_LINE, width=1)

        # Prompt symbol '>' (copper)
        draw.text((self.term_x + 12, input_bar_y), ">", fill=COPPER_ACCENT, font=FONT_TITLE_MD)

        # Badge '[Ask]' (gray rounded badge)
        badge_x = self.term_x + 36
        draw.rounded_rectangle([badge_x, input_bar_y + 2, badge_x + 48, input_bar_y + 26], radius=4, fill=BADGE_BG)
        draw.text((badge_x + 8, input_bar_y + 4), "Ask", fill=TEXT_MUTED, font=FONT_TEXT_SM)

        # Current Prompt / Typing
        prompt_text_x = badge_x + 58
        if self.current_prompt:
            draw.text((prompt_text_x, input_bar_y + 2), self.current_prompt, fill=TEXT_DARK, font=FONT_TEXT_BASE)
            if (frame_num // 12) % 2 == 0:
                cur_x = prompt_text_x + int(draw.textlength(self.current_prompt, font=FONT_TEXT_BASE))
                draw.rectangle([cur_x + 2, input_bar_y + 4, cur_x + 11, input_bar_y + 24], fill=TEXT_DARK)
        else:
            # Placeholder and shortcut hints
            draw.text((prompt_text_x, input_bar_y + 2), "Ask Aria, edit files, or run commands…", fill=TEXT_DIM, font=FONT_TEXT_BASE)
            draw.text((prompt_text_x + 370, input_bar_y + 2), "/ commands    @ context    ! shell", fill=TEXT_DIM, font=FONT_TEXT_BASE)

        # ── 7. Bottom Status Line ────────────────────────────────────────────
        status_y = HEIGHT - 55
        draw.ellipse([self.term_x + 12, status_y + 4, self.term_x + 24, status_y + 16], fill=self.status_dot_color)
        
        status_text = "  google/gemini-3.8-flash ·   ~/my-app   ·   rw   ·   Context <1%"
        draw.text((self.term_x + 28, status_y), status_text, fill=TEXT_MUTED, font=FONT_TEXT_BASE)

        return img


def build_general_agent_scenes(pipe_stdin):
    renderer = AriaGeneralAgentRenderer()
    frame_counter = 0

    def emit(n: int):
        nonlocal frame_counter
        for _ in range(n):
            f = renderer.render(frame_counter)
            pipe_stdin.write(f.tobytes())
            frame_counter += 1

    def type_cmd(cmd: str, speed: int = 2):
        renderer.current_prompt = ""
        emit(6)
        for ch in cmd:
            renderer.current_prompt += ch
            emit(speed)
        emit(10)
        renderer.current_prompt = ""

    # ── Scene 1: Authentic Startup Dashboard (4.5s) ──────────────────────────
    renderer.status_dot_color = AMBER_ACCENT
    emit(120)

    # ── Scene 2: Codebase Understanding & Architecture Review (6.0s) ─────────
    type_cmd("@src/ analyze repository architecture and identify performance bottlenecks")
    renderer.status_dot_color = GREEN_SUCCESS
    renderer.add_output("[TOOL]scan_codebase: analyzed 48 files across Python & TypeScript (24ms)")
    renderer.add_output("[TOOL]map_dependencies: FastAPI, SQLAlchemy, Redis, PostgreSQL (12ms)")
    renderer.add_output("Repository Architecture & Performance Audit:", color=TEXT_DARK, font=FONT_TEXT_BOLD)
    renderer.add_output("  • Architecture: Async Layered Clean Architecture (API -> Service -> Repository)", color=CYAN_HIGHLIGHT)
    renderer.add_output("  • Identified Bottleneck: Blocking sync DB queries found in auth middleware route handlers", color=AMBER_ACCENT)
    renderer.add_output("  • Recommendation: Convert JWT validation to async redis token cache and add connection pooling", color=TEXT_DARK)
    renderer.add_output("---DIVIDER---")
    emit(110)

    # ── Scene 3: Multi-File Refactoring & Code Generation (7.0s) ──────────────
    type_cmd("Refactor auth middleware to async JWT validation and generate unit tests")
    renderer.clear_output()
    renderer.add_output("[TOOL]edit_file: src/middleware/auth.py (+34/-12 lines) (22ms)")
    renderer.add_output("[TOOL]create_file: tests/test_auth.py (18ms)")
    renderer.add_output("Generated Diff: src/middleware/auth.py", color=TEXT_DARK, font=FONT_TEXT_BOLD)
    renderer.add_output("[DIFF_DEL]def authenticate_user(token: str = Depends(oauth2_scheme)):")
    renderer.add_output("[DIFF_DEL]    user = db.query(User).filter(User.token == token).first()")
    renderer.add_output("[DIFF_ADD]async def authenticate_user(token: str = Depends(oauth2_scheme)):")
    renderer.add_output("[DIFF_ADD]    payload = await jwt_service.verify_async(token, cache=redis_client)")
    renderer.add_output("[DIFF_ADD]    return UserPrincipal(id=payload['sub'], roles=payload['roles'])")
    renderer.add_output("✓ Created tests/test_auth.py with 6 comprehensive test fixtures (100% coverage)", color=GREEN_SUCCESS, font=FONT_TEXT_BOLD)
    renderer.add_output("---DIVIDER---")
    emit(130)

    # ── Scene 4: Interactive Shell Mode & Test Verification (6.5s) ───────────
    type_cmd("! pytest tests/ -v && git status -s")
    renderer.clear_output()
    renderer.add_output("[TOOL]shell_execute: pytest tests/ -v (output streamed to AI context) (42ms)")
    renderer.add_output("tests/test_auth.py::test_valid_jwt_token PASSED                       [ 16%]", color=GREEN_SUCCESS)
    renderer.add_output("tests/test_auth.py::test_expired_token_rejection PASSED               [ 33%]", color=GREEN_SUCCESS)
    renderer.add_output("tests/test_auth.py::test_redis_cache_hit PASSED                       [ 50%]", color=GREEN_SUCCESS)
    renderer.add_output("tests/test_auth.py::test_rate_limit_exceeded PASSED                   [ 66%]", color=GREEN_SUCCESS)
    renderer.add_output("tests/test_auth.py::test_invalid_signature PASSED                     [ 83%]", color=GREEN_SUCCESS)
    renderer.add_output("tests/test_auth.py::test_user_principal_claims PASSED                 [100%]", color=GREEN_SUCCESS)
    renderer.add_output("============================= 6 passed in 0.24s =============================", color=GREEN_SUCCESS, font=FONT_TEXT_BOLD)
    renderer.add_output("M  src/middleware/auth.py", color=CYAN_HIGHLIGHT)
    renderer.add_output("?? tests/test_auth.py", color=GREEN_SUCCESS)
    renderer.add_output("---DIVIDER---")
    emit(120)

    # ── Scene 5: Outro & Universal Agent Highlights (6.0s) ───────────────────
    type_cmd("/remote-control status")
    renderer.clear_output()
    renderer.add_output("[TOOL]session_sync: connected to https://arthera.ai/sessions/736097dc (8ms)")
    renderer.add_output("Aria Code — Universal AI Product Reviewer & Coding Agent:", color=COPPER_ACCENT, font=FONT_TITLE_MD)
    renderer.add_output("  1. General-Purpose Coding Agent: Understand, refactor, write, and verify code across any stack", color=TEXT_DARK)
    renderer.add_output("  2. 100% Local-First & Air-Gapped Privacy: Ollama local LLMs with zero telemetry leakage", color=TEXT_DARK)
    renderer.add_output("  3. Transparent Tool Execution: `✓ action (ms)` with real-time feedback and per-turn metrics", color=TEXT_DARK)
    renderer.add_output("  4. Multi-Surface Continuity: Seamless handoff between Terminal, Web, Mobile, and Feishu", color=TEXT_DARK)
    renderer.add_output("---DIVIDER---")
    renderer.add_output("Ready for every industry · Python · TypeScript · Rust · Go · Cloud Infrastructure", color=AMBER_ACCENT, font=FONT_TEXT_BOLD)
    emit(140)


def generate_english_narration() -> str | None:
    """Synthesizes studio-grade English voiceover using macOS Samantha."""
    audio_aiff = "docs/assets/narration_universal.aiff"
    audio_wav = "docs/assets/narration_universal.wav"

    script = (
        "Welcome to Aria Code. The terminal-first general-purpose AI coding agent and product reviewer for every software stack. "
        "Instantly understand complex repositories, inspect system architecture, and locate performance bottlenecks in milliseconds. "
        "Refactor multi-file codebases, implement features, and automatically generate unit tests. "
        "Execute shell commands and test suites directly in your workspace with full context awareness. "
        "With one hundred percent local-first privacy, zero data leakage, and seamless multi-device continuity, "
        "Aria Code empowers developers everywhere."
    )

    try:
        subprocess.run(["say", "-v", "Samantha", "-r", "175", "-o", audio_aiff, script], check=True)
        subprocess.run(["ffmpeg", "-y", "-i", audio_aiff, "-ar", "44100", "-ac", "2", audio_wav],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(audio_aiff):
            os.remove(audio_aiff)
        return audio_wav
    except Exception as err:
        print(f"TTS audio synthesis warning: {err}")
        return None


def main():
    os.makedirs("docs/assets", exist_ok=True)
    print("🎬 Rendering Universal AI Coding Agent Demo Video for Aria Code...")

    print("🎙️ Generating English studio narration...")
    audio_file = generate_english_narration()

    print("🖼️ Piping 1080p frames to FFmpeg...")
    temp_video = "docs/assets/temp_universal.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-pix_fmt", "rgb24",
        "-r", str(FPS),
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "17",
        "-pix_fmt", "yuv420p",
        temp_video
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        build_general_agent_scenes(proc.stdin)
        proc.stdin.close()
        proc.wait()
    except Exception as exc:
        print(f"Error during video rendering: {exc}")
        proc.kill()
        return

    print("🎧 Merging audio track into final MP4...")
    if audio_file and os.path.exists(audio_file):
        final_cmd = [
            "ffmpeg", "-y",
            "-i", temp_video,
            "-i", audio_file,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            OUTPUT_MP4
        ]
        subprocess.run(final_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(temp_video):
            os.remove(temp_video)
        if os.path.exists(audio_file):
            os.remove(audio_file)
    else:
        if os.path.exists(temp_video):
            shutil.move(temp_video, OUTPUT_MP4)

    print("🎞️ Rendering optimized preview GIF...")
    gif_cmd = [
        "ffmpeg", "-y",
        "-i", OUTPUT_MP4,
        "-vf", "fps=14,scale=840:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
        OUTPUT_GIF
    ]
    subprocess.run(gif_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"\n🎉 Success! Universal AI Agent Demo Video Generated:")
    print(f"  • Video (1080p MP4): {OUTPUT_MP4}")
    print(f"  • Preview (GIF):     {OUTPUT_GIF}")


if __name__ == "__main__":
    main()
