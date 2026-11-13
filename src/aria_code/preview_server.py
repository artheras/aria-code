"""preview_server.py — local, opt-in live-preview server for generated
artifacts (the aria-code half of the "Canvas/Artifacts" feature).

Off by default: nothing in this module runs until a human types `/canvas`
at the terminal (see apps/cli/commands/canvas_cmds.py). Binds to 127.0.0.1
only — never 0.0.0.0 — since this exists purely to open a local browser tab
next to the terminal, not to serve anything over the network.

Wire-up: artifacts.write_artifact_metadata() calls notify_new_version()
on the active session (a no-op when no session is running, which is the
overwhelmingly common case — every headless MCP tool call goes through that
function without a preview session ever existing). Versions are grouped by
a thread_id derived from the artifact's category+topic, so re-running the
same report for the same symbol appends to one thread's history instead of
starting a new one each time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("aria_preview_server")

_DEFAULT_HOST = "127.0.0.1"
_PORT_CANDIDATES = list(range(8765, 8775))

_active_session: Optional["PreviewSession"] = None


def discovery_path() -> Path:
    """Where a running canvas advertises its URL.

    The port is picked from a range at bind time, so a *separate* process
    (notably Arthera's Electron terminal, which embeds this server's output
    in its artifact preview panel) cannot guess it. This file is the
    handshake: written on start, removed on stop.
    """
    from aria_code.brokers.config import BROKERS_CONFIG_PATH  # same ~/.aria-code|.arthera home

    return BROKERS_CONFIG_PATH.parent / "canvas.json"


def _write_discovery(url: str) -> None:
    try:
        path = discovery_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": url, "pid": os.getpid(), "started_at": time.time()}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # discovery is a convenience, never fatal
        logger.warning("Could not write canvas discovery file: %s", exc)


def _clear_discovery() -> None:
    try:
        discovery_path().unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Could not remove canvas discovery file: %s", exc)


def read_discovery() -> Optional[Dict[str, Any]]:
    """Read another process's advertised canvas URL, or None.

    Returns None for a stale file — one whose PID is no longer alive, which
    is what a crash or `kill -9` leaves behind. Without that check a consumer
    would keep pointing an iframe at a dead port indefinitely.
    """
    try:
        path = discovery_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, 0)  # signal 0 = liveness probe, does not kill
            except ProcessLookupError:
                return None
            except PermissionError:
                pass  # alive, owned by another user
        return data
    except Exception:
        return None


def get_active_session() -> Optional["PreviewSession"]:
    return _active_session


def thread_id_for(record: Any) -> str:
    """Group versions of "the same logical artifact" together — same
    category+topic (e.g. a market report for AAPL) is one thread across
    repeated runs, not a new thread every time."""
    return f"{record.category}:{record.topic}"


@dataclass
class _Version:
    index: int
    path: Path
    created_at: str
    kind: str


@dataclass
class _Thread:
    id: str
    versions: List[_Version] = field(default_factory=list)
    updated_at: float = 0.0


class PreviewSession:
    """One running local preview server. Create via `start()`, not directly."""

    def __init__(self) -> None:
        self.threads: Dict[str, _Thread] = {}
        self.active_thread_id: Optional[str] = None
        self.url: str = ""
        self._runner = None
        self._sse_clients: List[Any] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self, host: str = _DEFAULT_HOST) -> str:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/events", self._handle_events)
        app.router.add_get("/state", self._handle_state)
        app.router.add_get("/artifact/{thread_id:.*}", self._handle_artifact)

        runner = web.AppRunner(app)
        await runner.setup()

        last_error: Optional[Exception] = None
        for port in _PORT_CANDIDATES:
            site = web.TCPSite(runner, host, port)
            try:
                await site.start()
            except OSError as exc:
                last_error = exc
                continue
            self._runner = runner
            self.url = f"http://{host}:{port}/"
            return self.url
        await runner.cleanup()
        raise RuntimeError(
            f"Could not bind any port in {_PORT_CANDIDATES[0]}-{_PORT_CANDIDATES[-1]} "
            f"for the preview server: {last_error}"
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self.url = ""
        self.threads.clear()
        self.active_thread_id = None
        self._sse_clients.clear()

    # -- artifact notification ----------------------------------------------

    async def notify_new_version(self, thread_id: str, record: Any) -> None:
        thread = self.threads.setdefault(thread_id, _Thread(id=thread_id))
        version = _Version(
            index=len(thread.versions),
            path=Path(record.path),
            created_at=datetime.now().isoformat(timespec="seconds"),
            kind=_kind_for_suffix(Path(record.path).suffix),
        )
        thread.versions.append(version)
        thread.updated_at = time.time()
        self.active_thread_id = thread_id
        await self._broadcast({
            "type": "new-version",
            "thread_id": thread_id,
            "version_index": version.index,
            "url": f"/artifact/{thread_id}?v={version.index}",
        })

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        if not self._sse_clients:
            return
        data = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        dead = []
        for resp in self._sse_clients:
            try:
                await resp.write(data)
            except Exception:
                dead.append(resp)
        for resp in dead:
            self._sse_clients.remove(resp)

    # -- HTTP handlers --------------------------------------------------------

    async def _handle_index(self, request):
        from aiohttp import web
        return web.Response(text=_SHELL_HTML, content_type="text/html")

    async def _handle_events(self, request):
        from aiohttp import web

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        self._sse_clients.append(resp)
        try:
            # Keep the connection open; aiohttp closes it when the client
            # disconnects, which surfaces here as a ConnectionResetError on
            # the next write() from _broadcast (handled there, not here).
            while True:
                await asyncio.sleep(15)
                try:
                    await resp.write(b": ping\n\n")
                except Exception:
                    break
        finally:
            if resp in self._sse_clients:
                self._sse_clients.remove(resp)
        return resp

    async def _handle_state(self, request):
        from aiohttp import web

        threads_payload = {
            tid: {
                "versions": [
                    {"index": v.index, "created_at": v.created_at, "kind": v.kind}
                    for v in thread.versions
                ],
            }
            for tid, thread in self.threads.items()
        }
        return web.json_response({
            "threads": threads_payload,
            "active_thread_id": self.active_thread_id,
        })

    async def _handle_artifact(self, request):
        from aiohttp import web

        thread_id = request.match_info["thread_id"]
        thread = self.threads.get(thread_id)
        if thread is None or not thread.versions:
            return web.Response(status=404, text=f"Unknown thread: {thread_id}")

        v_param = request.query.get("v")
        try:
            idx = int(v_param) if v_param is not None else len(thread.versions) - 1
        except ValueError:
            idx = len(thread.versions) - 1
        idx = max(0, min(idx, len(thread.versions) - 1))
        version = thread.versions[idx]

        if not version.path.exists():
            return web.Response(status=404, text=f"Artifact file missing: {version.path}")

        if version.kind == "markdown":
            try:
                from aria_code.markdown_pdf import render_markdown_html
                raw_text = version.path.read_text(encoding="utf-8")
                rendered_html = render_markdown_html(raw_text, lang="auto", title=version.path.stem)
                return web.Response(
                    text=rendered_html,
                    content_type="text/html",
                    headers=_ARTIFACT_HEADERS,
                )
            except Exception as exc:
                logger.warning("Could not render markdown artifact as HTML: %s", exc)

        content_type = mimetypes.guess_type(str(version.path))[0] or "application/octet-stream"
        return web.Response(
            body=version.path.read_bytes(),
            content_type=content_type,
            headers=_ARTIFACT_HEADERS,
        )


# Sent with every artifact response. The iframe sandbox already gives the
# document an opaque origin; this is the second half — it stops the page from
# reaching the network at all.
#
# `connect-src 'none'` is the important line: without it, model-generated
# JavaScript in an artifact can POST the contents of the page (which may hold
# whatever data the agent just analysed) to any host on the internet, and an
# opaque origin does nothing to prevent that. Google Fonts is the one exception
# because a dashboard that silently loses its typeface looks broken rather than
# secured; nothing else is reachable.
_ARTIFACT_HEADERS = {
    "Content-Security-Policy": "; ".join((
        "default-src 'none'",
        "script-src 'unsafe-inline' 'unsafe-eval'",
        "style-src 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com data:",
        "img-src data: blob:",
        "media-src data: blob:",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
    )),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # A generated report is a snapshot of one moment; a cached copy shown after
    # the next version lands is worse than no preview.
    "Cache-Control": "no-store",
}


def _kind_for_suffix(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix in (".html", ".htm"):
        return "html"
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        return "image"
    if suffix in (".md", ".markdown"):
        return "markdown"
    return "file"


async def start_session() -> "PreviewSession":
    """Start (or return the already-running) preview session. Idempotent —
    calling /canvas twice does not open a second server."""
    global _active_session
    if _active_session is not None and _active_session.url:
        return _active_session
    session = PreviewSession()
    await session.start()
    _active_session = session
    _write_discovery(session.url)
    return session


async def stop_session() -> None:
    global _active_session
    if _active_session is not None:
        await _active_session.stop()
        _active_session = None
    _clear_discovery()


def open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:
        logger.warning("Could not auto-open browser for %s: %s", url, exc)


_SHELL_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>aria-code canvas</title>
<style>
  :root { color-scheme: dark; --bg-canvas: #ffffff; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d1117; color: #c9d1d9; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
  header { display: flex; align-items: center; gap: 12px; padding: 8px 16px;
           border-bottom: 1px solid #21262d; background: #161b22; user-select: none; flex-wrap: wrap; }
  header .brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; color: #58a6ff; }
  header .brand svg { width: 16px; height: 16px; fill: currentColor; }
  .thread-picker { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
                   border-radius: 6px; padding: 4px 8px; font-size: 12px; outline: none; max-width: 280px; }
  .thread-picker:focus { border-color: #58a6ff; }
  header .empty { color: #8b949e; font-size: 13px; }
  .stepper { display: flex; align-items: center; gap: 6px; }
  .stepper button, .btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
                          border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 12px;
                          display: inline-flex; align-items: center; gap: 4px; transition: all 0.15s ease; }
  .stepper button:hover:not(:disabled), .btn:hover:not(:disabled) { background: #30363d; border-color: #8b949e; }
  .stepper button:disabled, .btn:disabled { opacity: 0.4; cursor: default; }
  .stepper span { font-size: 12px; color: #8b949e; min-width: 48px; text-align: center; }
  .toolbar { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .v-time { font-size: 11px; color: #8b949e; margin-right: 4px; }
  main { flex: 1; position: relative; background: var(--bg-canvas); }
  iframe, img { width: 100%; height: 100%; border: none; display: block; object-fit: contain; }
  .placeholder { display: flex; flex-direction: column; align-items: center; justify-content: center;
                 height: 100%; color: #8b949e; font-size: 14px; background: #0d1117; gap: 10px; }
  .placeholder code { background: #161b22; padding: 4px 8px; border-radius: 4px; border: 1px solid #30363d; color: #58a6ff; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg viewBox="0 0 16 16"><path d="M8 0a8 8 0 1 0 0 16A8 8 0 0 0 8 0Zm0 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13ZM6.5 4.75a.75.75 0 0 1 1.5 0v3.5a.75.75 0 0 1-1.5 0v-3.5ZM8 11.5a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z"/></svg>
    <span>aria-code canvas</span>
  </div>
  <select id="thread-select" class="thread-picker" style="display:none"></select>
  <span class="empty" id="status">waiting for the first artifact&hellip;</span>
  <div class="stepper" id="stepper" style="display:none">
    <button id="prev" title="Previous version">&lsaquo;</button>
    <span id="pos"></span>
    <button id="next" title="Next version">&rsaquo;</button>
    <span class="v-time" id="v-time"></span>
  </div>
  <div class="toolbar" id="toolbar" style="display:none">
    <button class="btn" id="btn-theme" title="Toggle Canvas background (Paper white / Dark)">&#9681; Theme</button>
    <button class="btn" id="btn-print" title="Print or save as PDF">&#9113; Print</button>
    <button class="btn" id="btn-raw" title="Open in new window">&#8599; Open</button>
    <button class="btn" id="btn-fullscreen" title="Toggle Fullscreen">&#x26F6; Full</button>
  </div>
</header>
<main id="main"><div class="placeholder"><span>Run a report/chart command in aria-code — it will show up here automatically.</span><code>/report AAPL</code> or <code>/dashboard</code></div></main>
<script>
let state = { threads: {}, active_thread_id: null };
let activeIndex = 0;
let darkCanvas = false;

function render() {
  const main = document.getElementById('main');
  const status = document.getElementById('status');
  const stepper = document.getElementById('stepper');
  const toolbar = document.getElementById('toolbar');
  const select = document.getElementById('thread-select');
  
  const threadIds = Object.keys(state.threads);
  if (threadIds.length === 0 || !state.active_thread_id || !state.threads[state.active_thread_id]) {
    main.innerHTML = '<div class="placeholder"><span>Run a report/chart command in aria-code — it will show up here automatically.</span><code>/report AAPL</code> or <code>/dashboard</code></div>';
    status.style.display = 'inline';
    status.textContent = 'waiting for the first artifact\\u2026';
    stepper.style.display = 'none';
    toolbar.style.display = 'none';
    select.style.display = 'none';
    return;
  }
  
  status.style.display = 'none';
  select.style.display = 'inline-block';
  select.innerHTML = '';
  for (const tid of threadIds) {
    const opt = document.createElement('option');
    opt.value = tid;
    opt.textContent = tid;
    if (tid === state.active_thread_id) opt.selected = true;
    select.appendChild(opt);
  }
  
  const thread = state.threads[state.active_thread_id];
  stepper.style.display = 'flex';
  toolbar.style.display = 'flex';
  activeIndex = Math.max(0, Math.min(activeIndex, thread.versions.length - 1));
  document.getElementById('pos').textContent = (activeIndex + 1) + ' / ' + thread.versions.length;
  document.getElementById('prev').disabled = activeIndex <= 0;
  document.getElementById('next').disabled = activeIndex >= thread.versions.length - 1;
  
  const version = thread.versions[activeIndex];
  const timeSpan = document.getElementById('v-time');
  if (version && version.created_at) {
    timeSpan.textContent = version.created_at.slice(11, 19);
  } else {
    timeSpan.textContent = '';
  }
  
  const url = '/artifact/' + encodeURIComponent(state.active_thread_id) + '?v=' + version.index + '&_=' + Date.now();
  if (version.kind === 'image') {
    main.innerHTML = '<img id="preview-frame" src="' + url + '">';
  } else {
    // allow-scripts WITHOUT allow-same-origin keeps sandbox isolated
    main.innerHTML = '<iframe id="preview-frame" src="' + url + '" sandbox="allow-scripts allow-modals"></iframe>';
  }
}

async function refreshState(jumpToLatest) {
  const resp = await fetch('/state');
  state = await resp.json();
  if (jumpToLatest && state.active_thread_id) {
    const thread = state.threads[state.active_thread_id];
    activeIndex = thread ? thread.versions.length - 1 : 0;
  }
  render();
}

document.getElementById('thread-select').onchange = (e) => {
  state.active_thread_id = e.target.value;
  const thread = state.threads[state.active_thread_id];
  activeIndex = thread ? thread.versions.length - 1 : 0;
  render();
};

document.getElementById('prev').onclick = () => { activeIndex--; render(); };
document.getElementById('next').onclick = () => { activeIndex++; render(); };

document.getElementById('btn-print').onclick = () => {
  const frame = document.getElementById('preview-frame');
  if (frame && frame.tagName === 'IFRAME' && frame.contentWindow) {
    frame.contentWindow.focus();
    frame.contentWindow.print();
  } else {
    window.print();
  }
};

document.getElementById('btn-raw').onclick = () => {
  if (state.active_thread_id && state.threads[state.active_thread_id]) {
    const thread = state.threads[state.active_thread_id];
    const version = thread.versions[activeIndex];
    const url = '/artifact/' + encodeURIComponent(state.active_thread_id) + '?v=' + version.index;
    window.open(url, '_blank');
  }
};

document.getElementById('btn-theme').onclick = () => {
  darkCanvas = !darkCanvas;
  document.documentElement.style.setProperty('--bg-canvas', darkCanvas ? '#0d1117' : '#ffffff');
};

document.getElementById('btn-fullscreen').onclick = () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen();
  } else {
    document.exitFullscreen();
  }
};

const es = new EventSource('/events');
es.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === 'new-version') {
    refreshState(true);
  }
};

refreshState(true);
</script>
</body>
</html>
"""
