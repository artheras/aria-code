"""Generic HTML/Markdown-to-PDF export engine + compatibility stubs.

Vertical equity/Bloomberg report & chart generation has moved to
``artheras/financial-service`` over MCP. This module retains the generic PDF
rendering chain (``export_pdf``, ``html_string_to_pdf``, ``update_reports_index``,
``_md_to_html``, ``_strip_emoji``) used by ``markdown_pdf.py`` and ``pdf_report.py``.
"""

from __future__ import annotations

import html
import logging
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_EMOJI_RE = _re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0001F100-\U0001F1FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+"
)


def _strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", str(s or "")).strip()


def _md_to_html(text: str, max_chars: int = 0) -> str:
    """Convert LLM-generated markdown to safe HTML for embedding in reports."""
    if not text:
        return ""
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "…"

    text = _strip_emoji(text)
    t = html.escape(text)
    t = t.replace("&lt;br&gt;", "\n")

    raw_lines = t.split("\n")
    merged: List[str] = []
    for raw_line in raw_lines:
        if merged and merged[-1].lstrip().startswith("|") and raw_line and not raw_line.lstrip().startswith("|"):
            merged[-1] += "<br>" + raw_line
        else:
            merged.append(raw_line)

    out_lines: List[str] = []
    i = 0
    while i < len(merged):
        line = merged[i]
        if (
            "|" in line
            and i + 1 < len(merged)
            and _re.match(r"^\s*\|[\s\-:|]+\|", merged[i + 1])
        ):
            header_cells = [c.strip() for c in line.strip("|").split("|")]
            th = "".join(f"<th>{c}</th>" for c in header_cells)
            i += 2
            tbody_rows = ""
            while i < len(merged) and "|" in merged[i] and merged[i].lstrip().startswith("|"):
                cells = [c.strip() for c in merged[i].strip("|").split("|")]
                tbody_rows += "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
                i += 1
            out_lines.append(
                f'<table class="md-table">'
                f"<thead><tr>{th}</tr></thead>"
                f"<tbody>{tbody_rows}</tbody>"
                f"</table>"
            )
        else:
            out_lines.append(line)
            i += 1
    t = "\n".join(out_lines)

    t = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t, flags=_re.DOTALL)
    t = _re.sub(r"__(.+?)__", r"<strong>\1</strong>", t, flags=_re.DOTALL)
    t = _re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", t)
    t = _re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = _re.sub(r"^#{1,3}\s+(.+)$", r'<strong class="md-h">\1</strong>', t, flags=_re.MULTILINE)
    t = _re.sub(r"\n[-*]{3,}\n", '\n<hr class="md-hr">\n', t)
    return t.replace("\n", "<br>\n")


_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome", "chromium", "chromium-browser", "msedge",
)


def _find_chrome() -> Optional[str]:
    import shutil
    for cand in _CHROME_CANDIDATES:
        if "/" in cand:
            if Path(cand).exists():
                return cand
        elif shutil.which(cand):
            return shutil.which(cand)
    return None


def _export_pdf_via_chrome(html_path: Path, pdf_path: Path) -> bool:
    chrome = _find_chrome()
    if not chrome:
        return False
    import subprocess as _sp
    import tempfile

    with tempfile.TemporaryDirectory(prefix="aria_pdf_") as tmp:
        cmd = [
            chrome, "--headless=new", "--disable-gpu",
            "--no-sandbox", "--disable-dev-shm-usage",
            f"--user-data-dir={tmp}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.resolve().as_uri(),
        ]
        try:
            r = _sp.run(cmd, capture_output=True, timeout=90)
            if r.returncode != 0 or not pdf_path.exists():
                cmd[1] = "--headless"
                r = _sp.run(cmd, capture_output=True, timeout=90)
            if r.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
                logger.info("[report] pdf via headless chrome: %s", pdf_path)
                return True
        except Exception as e:
            logger.debug("[report] headless chrome failed: %s", e)
    return False


def _export_pdf_via_webkit(html_path: Path, pdf_path: Path) -> bool:
    import sys
    if sys.platform != "darwin":
        return False
    try:
        import objc  # noqa: F401
        from AppKit import NSApplication
        from Foundation import NSDate, NSMakeRect, NSRunLoop, NSURL
        from WebKit import WKPDFConfiguration, WKWebView, WKWebViewConfiguration
    except ImportError:
        return False

    try:
        NSApplication.sharedApplication()
        view = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 1024, 1400), WKWebViewConfiguration.alloc().init()
        )
        url = NSURL.fileURLWithPath_(str(html_path.resolve()))
        view.loadFileURL_allowingReadAccessToURL_(
            url, NSURL.fileURLWithPath_(str(html_path.resolve().parent))
        )

        runloop = NSRunLoop.currentRunLoop()
        deadline = 8.0
        while view.isLoading() and deadline > 0:
            runloop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            deadline -= 0.05
        if view.isLoading():
            return False
        for _ in range(6):
            runloop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))

        result: dict = {}

        def _done(data, error):
            result["data"], result["error"] = data, error

        view.createPDFWithConfiguration_completionHandler_(
            WKPDFConfiguration.alloc().init(), _done
        )
        deadline = 8.0
        while "data" not in result and deadline > 0:
            runloop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
            deadline -= 0.05

        data = result.get("data")
        if data is None or result.get("error") is not None:
            return False
        data.writeToFile_atomically_(str(pdf_path), True)
        return pdf_path.exists() and pdf_path.stat().st_size > 0
    except Exception as e:
        logger.debug("[report] webkit engine failed: %s", e)
        return False


def export_pdf(html_path: Path) -> Optional[Path]:
    """Convert an HTML file to PDF alongside the source file."""
    pdf_path = html_path.with_suffix(".pdf")
    if _export_pdf_via_chrome(html_path, pdf_path):
        return pdf_path
    if _export_pdf_via_webkit(html_path, pdf_path):
        return pdf_path
    try:
        import weasyprint
        weasyprint.HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return pdf_path
    except Exception:
        pass
    import shutil
    import subprocess as _sp
    if shutil.which("wkhtmltopdf"):
        try:
            r = _sp.run(
                ["wkhtmltopdf", "--quiet", "--print-media-type", str(html_path), str(pdf_path)],
                capture_output=True, timeout=60,
            )
            if r.returncode == 0 and pdf_path.exists():
                return pdf_path
        except Exception:
            pass
    return None


def html_string_to_pdf(html_body: str, pdf_path: Path) -> Optional[Path]:
    """Render an arbitrary HTML string to PDF via the shared engine chain."""
    import tempfile
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html_body)
        tmp_html = Path(f.name)
    try:
        out = export_pdf(tmp_html)
        if out and out.exists():
            out.replace(pdf_path)
            return pdf_path
        return None
    finally:
        tmp_html.unlink(missing_ok=True)


def update_reports_index(reports_root: Path) -> Optional[Path]:
    """Scan reports_root recursively for *_report_*.html files and write index.html."""
    index_path = reports_root / "index.html"
    entries: List[Dict[str, Any]] = []
    for f in sorted(reports_root.rglob("*_report_*.html"), reverse=True):
        if f.name == "index.html":
            continue
        m = _re.match(r"^(.+?)_report_(\d{8})_(\d{4})\.html$", f.name)
        if not m:
            continue
        sym, ds, ts_ = m.group(1), m.group(2), m.group(3)
        dt_str = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]} {ts_[:2]}:{ts_[2:]}"
        size_kb = max(1, f.stat().st_size // 1024)
        try:
            rel = str(f.relative_to(reports_root)).replace("\\", "/")
        except ValueError:
            rel = f.name
        entries.append({"symbol": sym.upper(), "datetime": dt_str, "size_kb": size_kb, "href": rel})

    rows_html = "".join(
        f'<tr><td><a href="{html.escape(e["href"])}">{html.escape(e["symbol"])}</a></td>'
        f'<td>{html.escape(e["datetime"])}</td><td>{e["size_kb"]}KB</td></tr>\n'
        for e in entries
    )
    idx_html = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Aria Code Reports</title></head>"
        f"<body><h1>Reports ({len(entries)}) · {datetime.now().strftime('%Y-%m-%d %H:%M')}</h1>"
        f"<table><tbody>{rows_html}</tbody></table></body></html>"
    )
    index_path.write_text(idx_html, encoding="utf-8")
    return index_path


# ── Financial report/chart compatibility stubs (moved to financial-service) ──

def _fetch_report_data_sync(_symbol: str) -> Tuple[Any, Any, Dict[str, Any]]:
    return None, None, {}


def generate_price_chart(*_args: Any, **_kwargs: Any) -> Optional[str]:
    return None


def generate_indicator_chart(*_args: Any, **_kwargs: Any) -> Optional[str]:
    return None


def generate_comparison_chart(*_args: Any, **_kwargs: Any) -> Optional[str]:
    return None


def generate_allocation_chart(*_args: Any, **_kwargs: Any) -> Optional[str]:
    return None


async def generate_report(*_args: Any, **_kwargs: Any) -> Optional[Path]:
    return None
