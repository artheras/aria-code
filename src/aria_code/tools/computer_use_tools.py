"""
computer_use_tools.py — Browser automation + desktop control tools for Aria Code.

Provides three LOCAL_TOOLS-compatible tool functions:
  browser_navigate     Open a URL and return page text + links (Playwright with requests fallback)
  browser_screenshot   Navigate to URL and capture a full-page screenshot
  computer_screenshot  Capture a screenshot of the current desktop
  computer_action      Control mouse / keyboard (click, type, scroll, move, hotkey)

Screenshot images travel with each tool result; concurrent sessions never share them.

Install:
    pip install playwright mss pyautogui pillow
    playwright install chromium
"""

from __future__ import annotations

import base64
import io
import logging
import os
import subprocess
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

# ── Browser navigate ──────────────────────────────────────────────────────────

def _tool_browser_navigate(params: dict) -> dict:
    """
    Open a URL in a headless browser and return the page text content + links.
    Uses Playwright if installed; falls back to requests+BeautifulSoup.
    """
    url = params.get("url", "").strip()
    if not url:
        return {"success": False, "error": "Missing 'url' parameter"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    max_chars = min(int(params.get("max_chars", 12000)), 40000)
    wait_for = params.get("wait_for", "domcontentloaded")  # or "networkidle"

    # --- Playwright path ---
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                )
            )
            page.goto(url, wait_until=wait_for, timeout=30000)
            title = page.title()
            text = page.inner_text("body")[:max_chars]
            # Collect visible links
            links = []
            for a in page.query_selector_all("a[href]")[:30]:
                href = a.get_attribute("href") or ""
                label = (a.inner_text() or "").strip()[:60]
                if href.startswith("http") and label:
                    links.append(f"[{label}]({href})")
            browser.close()
        return {
            "success": True,
            "data": {
                "url": url,
                "title": title,
                "text": text,
                "links": links[:20],
                "length": len(text),
                "engine": "playwright",
            },
        }
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("playwright navigate failed: %s — falling back to requests", exc)

    # --- requests fallback ---
    try:
        import re
        import requests
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 Chrome/124.0"},
            timeout=15,
            verify=True,
        )
        r.raise_for_status()
        raw = r.text
        text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"\s{3,}", "\n", text).strip()
        title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.DOTALL)
        title = title_m.group(1).strip() if title_m else url
        return {
            "success": True,
            "data": {
                "url": url,
                "title": title,
                "text": text[:max_chars],
                "links": [],
                "length": len(text),
                "engine": "requests",
            },
        }
    except Exception as exc:
        return {"success": False, "error": f"Navigation failed: {exc}"}


# ── Browser screenshot ────────────────────────────────────────────────────────

def _tool_browser_screenshot(params: dict) -> dict:
    """
    Navigate to a URL and capture a full-page screenshot.
    Returns the image with this tool result for vision-model injection.
    """
    url = params.get("url", "").strip()
    if not url:
        return {"success": False, "error": "Missing 'url' parameter"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    wait_for = params.get("wait_for", "networkidle")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until=wait_for, timeout=30000)
            title = page.title()
            png_bytes = page.screenshot(full_page=False)
            browser.close()

        b64 = base64.b64encode(png_bytes).decode()
        size_kb = len(png_bytes) // 1024
        return {
            "success": True,
            "_vision_image": b64,
            "data": {
                "url": url,
                "title": title,
                "size_kb": size_kb,
                "width": 1280,
                "height": 900,
                "note": f"Screenshot captured ({size_kb} KB). Image attached to next message.",
            },
        }
    except ImportError:
        return {
            "success": False,
            "error": "Playwright not installed. Run: pip install playwright && playwright install chromium",
        }
    except Exception as exc:
        return {"success": False, "error": f"Browser screenshot failed: {exc}"}


# ── Desktop screenshot ────────────────────────────────────────────────────────

_LAST_CAPTURE_META: dict = {
    "image_width": 1920,
    "image_height": 1080,
    "logical_width": 1920,
    "logical_height": 1080,
    "scale_x": 1.0,
    "scale_y": 1.0,
}


def _apply_grid_overlay(img):
    """Draw a subtle 10x10 coordinate ruler grid onto a PIL Image."""
    try:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        w, h = img.size
        for step in range(1, 10):
            gx = int(w * step / 10)
            gy = int(h * step / 10)
            draw.line([(gx, 0), (gx, h)], fill=(120, 160, 240), width=1)
            draw.line([(0, gy), (w, gy)], fill=(120, 160, 240), width=1)
            draw.text((gx + 3, 4), f"{gx}", fill=(220, 50, 50))
            draw.text((4, gy + 3), f"{gy}", fill=(220, 50, 50))
    except Exception:
        pass
    return img


def _record_capture_meta(img_w: int, img_h: int, logical_w: int, logical_h: int) -> dict:
    sx = round(logical_w / max(1, img_w), 4)
    sy = round(logical_h / max(1, img_h), 4)
    _LAST_CAPTURE_META.update({
        "image_width": img_w,
        "image_height": img_h,
        "logical_width": logical_w,
        "logical_height": logical_h,
        "scale_x": sx,
        "scale_y": sy,
    })
    return dict(_LAST_CAPTURE_META)


def _resolve_xy(x: int, y: int, coord_space: str = "screen") -> tuple[int, int]:
    """Map coordinates from ('screen' | 'screenshot' | 'normalized') to logical screen coordinates."""
    space = (coord_space or "screen").strip().lower()
    if space == "screenshot":
        sx = float(_LAST_CAPTURE_META.get("scale_x", 1.0) or 1.0)
        sy = float(_LAST_CAPTURE_META.get("scale_y", 1.0) or 1.0)
        return int(round(x * sx)), int(round(y * sy))
    if space == "normalized":
        lw = int(_LAST_CAPTURE_META.get("logical_width", 1920) or 1920)
        lh = int(_LAST_CAPTURE_META.get("logical_height", 1080) or 1080)
        return int(round(x * lw / 1000.0)), int(round(y * lh / 1000.0))
    return x, y


def _tool_computer_screenshot(params: dict) -> dict:
    """
    Capture a screenshot of the current desktop screen.
    Returns the image with this tool result for vision-model injection.
    """
    try:
        monitor_idx = int(params.get("monitor", 1))
        max_dim = int(params.get("max_dim", 1920))
        mark_grid = bool(params.get("mark_grid", False))
        if monitor_idx < 0 or not 64 <= max_dim <= 4096:
            raise ValueError("monitor must be nonnegative; max_dim must be 64..4096")
    except (ValueError, TypeError) as exc:
        return {"success": False, "error": str(exc)}

    # --- mss path (fast, cross-platform) ---
    try:
        import mss
        from PIL import Image

        with mss.mss() as sct:
            monitors = sct.monitors
            if monitor_idx >= len(monitors):
                return {"success": False, "error": "Monitor is unavailable"}
            mon = monitors[monitor_idx]
            logical_w = int(mon.get("width", 0))
            logical_h = int(mon.get("height", 0))
            raw = sct.grab(mon)
            img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)

        # Resize if too large (keep aspect ratio)
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        if mark_grid:
            img = _apply_grid_overlay(img)
        meta = _record_capture_meta(img.width, img.height, logical_w or w, logical_h or h)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        size_kb = len(buf.getvalue()) // 1024
        return {
            "success": True,
            "_vision_image": b64,
            "data": {
                "width": img.width,
                "height": img.height,
                "logical_width": meta["logical_width"],
                "logical_height": meta["logical_height"],
                "scale_x": meta["scale_x"],
                "scale_y": meta["scale_y"],
                "size_kb": size_kb,
                "note": f"Desktop screenshot captured ({img.width}×{img.height}, {size_kb} KB). Image attached.",
            },
        }
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("mss screenshot failed: %s", exc)

    # --- screencapture fallback (macOS primary screen only) ---
    if sys.platform != "darwin" or monitor_idx != 1:
        return {"success": False, "error": "Requested monitor capture unavailable. Install mss pillow."}
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = f.name
        subprocess.run(["screencapture", "-x", tmp], check=True, timeout=10)
        from PIL import Image
        img = Image.open(tmp)
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        if mark_grid:
            img = _apply_grid_overlay(img)
        meta = _record_capture_meta(img.width, img.height, w, h)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {
            "success": True,
            "_vision_image": b64,
            "data": {
                "width": img.width,
                "height": img.height,
                "logical_width": meta["logical_width"],
                "logical_height": meta["logical_height"],
                "scale_x": meta["scale_x"],
                "scale_y": meta["scale_y"],
                "size_kb": len(buf.getvalue()) // 1024,
                "note": f"Screenshot captured ({img.width}×{img.height}). Image attached.",
            },
        }
    except Exception as exc:
        return {"success": False, "error": f"Screenshot failed: {exc}. Install: pip install mss pillow"}
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


# ── Computer action ───────────────────────────────────────────────────────────

def _tool_computer_action(params: dict) -> dict:
    """
    Control the mouse or keyboard. Supported actions:
      click (x, y)         — left-click at screen coordinates
      right_click (x, y)   — right-click
      double_click (x, y)  — double-click
      move (x, y)          — move mouse without clicking
      type (text)          — type text via keyboard
      key (key)            — press a key or combo: "enter", "ctrl+c", "cmd+space"
      scroll (x, y, dy)    — scroll wheel: dy > 0 = down, dy < 0 = up
      drag (x, y, ex, ey)  — drag from (x,y) to (ex,ey)
      batch (steps)        — execute a list of actions sequentially
    """
    action = str(params.get("action", "") or "").lower()
    if action == "batch":
        steps = params.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
            return {"success": False, "error": "batch requires 'steps' as a list of 1..10 actions"}
        executed = []
        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or str(step.get("action", "")).lower() == "batch":
                return {"success": False, "error": f"Invalid batch step {idx}"}
            res = _tool_computer_action(step)
            if not res.get("success"):
                return {"success": False, "error": f"Step {idx} failed: {res.get('error')}", "completed": executed}
            executed.append(res.get("data"))
        return {"success": True, "data": {"action": "batch", "steps_completed": len(executed), "results": executed}}

    try:
        coord_space = str(params.get("coord_space", "screen") or "screen").lower()
        if coord_space not in {"screen", "screenshot", "normalized"}:
            return {"success": False, "error": f"Invalid coord_space '{coord_space}'"}
        raw_x = int(params.get("x", 0))
        raw_y = int(params.get("y", 0))
        x, y = _resolve_xy(raw_x, raw_y, coord_space)
        text = params.get("text", "")
        key = params.get("key", "")
        dy = int(params.get("dy", 3))
        raw_ex = int(params.get("ex", raw_x))
        raw_ey = int(params.get("ey", raw_y))
        ex, ey = _resolve_xy(raw_ex, raw_ey, coord_space)
    except (ValueError, TypeError, AttributeError):
        return {"success": False, "error": "Invalid action parameters"}

    if not action:
        return {"success": False, "error": "Missing 'action' parameter"}

    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05

        if action == "click":
            pyautogui.click(x, y)
            return {"success": True, "data": {"action": "click", "x": x, "y": y}}

        elif action == "right_click":
            pyautogui.rightClick(x, y)
            return {"success": True, "data": {"action": "right_click", "x": x, "y": y}}

        elif action == "double_click":
            pyautogui.doubleClick(x, y)
            return {"success": True, "data": {"action": "double_click", "x": x, "y": y}}

        elif action == "move":
            pyautogui.moveTo(x, y, duration=0.2)
            return {"success": True, "data": {"action": "move", "x": x, "y": y}}

        elif action == "type":
            if not text:
                return {"success": False, "error": "Missing 'text' for type action"}
            import pyperclip
            previous = pyperclip.paste()
            try:
                pyperclip.copy(text)
                pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
                time.sleep(0.15)
            finally:
                pyperclip.copy(previous)
            return {"success": True, "data": {"action": "type", "chars": len(text)}}

        elif action == "key":
            if not key:
                return {"success": False, "error": "Missing 'key' for key action"}
            # Handle combos like "ctrl+c", "cmd+space"
            parts = [{"cmd": "command", "return": "enter", "esc": "escape"}.get(k.lower(), k.lower())
                     for k in key.replace("+", " ").split() if k.strip()]
            if not parts:
                return {"success": False, "error": "Missing key"}
            if len(parts) > 1:
                pyautogui.hotkey(*parts)
            else:
                pyautogui.press(parts[0])
            return {"success": True, "data": {"action": "key", "key": key}}

        elif action == "scroll":
            pyautogui.scroll(-dy, x=x, y=y)
            return {"success": True, "data": {"action": "scroll", "x": x, "y": y, "dy": dy}}

        elif action == "drag":
            pyautogui.moveTo(x, y)
            pyautogui.dragTo(ex, ey, duration=0.5, button="left")
            return {"success": True, "data": {"action": "drag", "from": [x, y], "to": [ex, ey]}}

        else:
            return {
                "success": False,
                "error": (
                    f"Unknown action '{action}'. "
                    "Valid: click, right_click, double_click, move, type, key, scroll, drag"
                ),
            }
    except ImportError:
        return {
            "success": False,
            "error": "Desktop dependency missing. Run: pip install 'aria-code[desktop]'",
        }
    except pyautogui.FailSafeException:
        return {
            "success": False,
            "error": "Fail-safe triggered: mouse moved to screen corner. Move away and retry.",
        }
    except Exception as exc:
        return {"success": False, "error": f"computer_action failed: {exc}"}


# ── Tool schemas (Ollama / OpenAI function-calling format) ────────────────────

COMPUTER_USE_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": (
                "Open a URL in a headless browser and return the full page text content and links. "
                "Use this to read web pages, documentation, news articles, financial data, or any URL. "
                "Supports JavaScript-rendered pages (via Playwright) with fallback to requests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to navigate to, e.g. https://example.com",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters of page text to return (default 12000, max 40000)",
                    },
                    "wait_for": {
                        "type": "string",
                        "enum": ["domcontentloaded", "networkidle", "load"],
                        "description": "When to consider navigation done (default: domcontentloaded)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": (
                "Navigate to a URL and capture a visual screenshot of the page. "
                "The screenshot is automatically attached to the next message for visual analysis. "
                "Use when you need to SEE the visual layout, charts, images, or UI of a web page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL to screenshot",
                    },
                    "wait_for": {
                        "type": "string",
                        "enum": ["domcontentloaded", "networkidle", "load"],
                        "description": "Wait condition before capturing (default: networkidle)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_screenshot",
            "description": (
                "Capture a screenshot of the current desktop screen. "
                "The image is automatically attached to the next message so you can see what's on screen. "
                "Use this before computer_action to understand where to click."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor": {
                        "type": "integer",
                        "description": "Monitor index: 1 = primary (default), 0 = all monitors",
                    },
                    "max_dim": {
                        "type": "integer",
                        "description": "Max pixel dimension for resize (default 1920)",
                    },
                    "mark_grid": {
                        "type": "boolean",
                        "description": "Overlay a 10x10 coordinate ruler grid onto the screenshot",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_action",
            "description": (
                "Control the mouse and keyboard to interact with the desktop. "
                "Always take a computer_screenshot first to see the current screen state. "
                "Actions: click, right_click, double_click, move, type, key, scroll, drag, batch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "click", "right_click", "double_click", "move",
                            "type", "key", "scroll", "drag", "batch",
                        ],
                        "description": "Action to perform",
                    },
                    "coord_space": {
                        "type": "string",
                        "enum": ["screen", "screenshot", "normalized"],
                        "description": "Coordinate space: 'screen' (default), 'screenshot' (auto-scaled for Retina/resized captures), or 'normalized' (0..1000)",
                    },
                    "steps": {
                        "type": "array",
                        "description": "List of action objects when action='batch'",
                    },
                    "x": {
                        "type": "integer",
                        "description": "Screen X coordinate (pixels from left)",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Screen Y coordinate (pixels from top)",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type (for 'type' action)",
                    },
                    "key": {
                        "type": "string",
                        "description": "Key or combo to press, e.g. 'enter', 'ctrl+c', 'cmd+space'",
                    },
                    "dy": {
                        "type": "integer",
                        "description": "Scroll amount: positive = down, negative = up (for 'scroll' action)",
                    },
                    "ex": {
                        "type": "integer",
                        "description": "Drag end X coordinate (for 'drag' action)",
                    },
                    "ey": {
                        "type": "integer",
                        "description": "Drag end Y coordinate (for 'drag' action)",
                    },
                },
                "required": ["action"],
            },
        },
    },
]


# ── Tool registry for LOCAL_TOOLS ─────────────────────────────────────────────

COMPUTER_USE_TOOLS = {
    "browser_navigate":   (_tool_browser_navigate,   "Open a URL and return page text content"),
    "browser_screenshot": (_tool_browser_screenshot, "Navigate to URL and capture a screenshot"),
    "computer_screenshot":(_tool_computer_screenshot,"Capture a screenshot of the current desktop"),
    "computer_action":    (_tool_computer_action,    "Control mouse/keyboard (click, type, scroll)"),
}

