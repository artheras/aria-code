"""pdf_report.py — 通用"结构化报告 → 设计过的 PDF"渲染引擎。

背景：`report_generator.py` 已经有 `export_pdf()`（weasyprint/wkhtmltopdf 通用
HTML→PDF 转换，直接复用，不重写）和 `_md_to_html()`，但它的 `_build_html()`
是写死给单股票分析报告用的；`ui.py` 的 `get_ui_css_base()` 是给 LLM 现场生成
HTML 用的 Bloomberg 黑橙配色 token；`backtest_report.py` 又是第三套配色。三者
互不复用，也都没有"任意报告 → 好看的 PDF"的通用能力。

这个模块提供的是中间层：一个与具体报告内容无关的 `Document`（title + 若干
`Section`），每个 Section 有一个 `kind`（今天先实现短线报告实际用到的几种：
stats/badge_table/change_columns/card_grid/row_list/data_table/detail_cards/
news_cards/text），配合一个 `Theme`（配色/字体/密度）渲染成 HTML，再交给
report_generator.export_pdf() 转 PDF。新增一种报告类型，只需要写一个"解析成
Document"的函数（参考 parse_shortterm_report），不需要重新设计 CSS。

用法（命令行侧见 apps/cli/commands/pdf_export_cmds.py 的 /export-pdf）：
    doc = parse_shortterm_report(md_text)
    html = render_document(doc, THEMES["institutional"])
    Path("out.html").write_text(html)
    from report_generator import export_pdf
    export_pdf(Path("out.html"))
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Any, Callable


def esc(s: str) -> str:
    return _html.escape(str(s or ""), quote=False)


def strip_md_bold(s: str) -> str:
    return re.sub(r"\*\*(.*?)\*\*", r"\1", s or "")


# weasyprint (Pango/cairo) has no color-emoji glyph support and no emoji font
# in this theme's font stack — when an emoji leaks into plain rendered text,
# cairo substitutes a glyph from whatever monochrome fallback font is on the
# system, with different font metrics than the surrounding text, so it
# renders visibly offset from the baseline ("floating" above/beside the
# line). The fix everywhere in this file is either (a) convert the emoji to
# its structured {tone, label} form and render it as a `.badge`/`.tag` pill
# (see _signal_tone/_badge_html — the correct path, used by sections that
# render cleanly), or (b) strip it outright from any freeform text that has
# no such structured equivalent. This regex is the fallback for (b): a broad
# Unicode emoji-block range rather than an enumerated character class, so it
# doesn't silently miss new emoji the report starts using later.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # misc symbols/pictographs, emoticons, transport, supplemental symbols
    "\U00002600-\U000027BF"  # misc symbols, dingbats (⚠️☀✅ etc.)
    "\U00002B00-\U00002BFF"  # misc symbols and arrows — NOT the same block as the
                              # single-arrow ↑/↓/→ (U+2190-21FF) this report's
                              # "均线" trend column relies on; this block is stars/
                              # squares (⭐U+2B50, ⬜U+2B1C) only, safe to strip
    "\U0001F100-\U0001F1FF"  # enclosed alphanumeric supplement (🆕🆓🆙🆒🆗 etc.)
                              # + regional indicators (flag pairs) — same block,
                              # found missing 2026-07-21 when 🆕 (U+1F195) leaked
                              # through into a section-column label and dragged
                              # in an Apple-Color-Emoji-Bold font embed even
                              # though every other emoji in the doc was clean
    "\U0000FE0F"              # variation selector-16 (emoji presentation)
    "\U0000200D"              # zero-width joiner
    "]+"
)


def strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", str(s or "")).strip()


# ── 数据模型 ──────────────────────────────────────────────────────────────────

@dataclass
class Section:
    kind: str
    title: str
    icon: str = "•"
    note: str = ""
    content: Any = None
    new_page: bool = True  # 大部分 section 独立起一页；小的（如 stats）可设 False


@dataclass
class Document:
    title: str
    meta: str = ""
    warning: str = ""
    sections: list[Section] = field(default_factory=list)
    footer: str = ""

    def select(self, include: list[str] | None = None, exclude: list[str] | None = None) -> "Document":
        """按 section 标题子串过滤（内容取舍）。include 为空表示全选。"""
        secs = self.sections
        if include:
            secs = [s for s in secs if any(k.lower() in s.title.lower() for k in include)]
        if exclude:
            secs = [s for s in secs if not any(k.lower() in s.title.lower() for k in exclude)]
        return Document(title=self.title, meta=self.meta, warning=self.warning,
                         sections=secs, footer=self.footer)


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    surface: str          # 卡片/表头以外的浅底色
    text: str
    text_muted: str
    border: str
    accent: str            # section 图标底色 / 表头底色
    buy: str; sell: str; hold: str; reduce: str
    buy_bg: str; sell_bg: str; hold_bg: str; reduce_bg: str
    font: str
    mono: str
    radius: str = "3px"


THEMES: dict[str, Theme] = {
    # 浅色研报风格 —— 面向对外交付（尽调材料/合规文档同调性）
    "institutional": Theme(
        name="institutional",
        bg="#ffffff", surface="#f8f9fb", text="#1c2230", text_muted="#6b7280", border="#e7e9ee",
        accent="#1f3a5f",
        buy="#16803c", sell="#c81e1e", hold="#5b6472", reduce="#b4650a",
        buy_bg="#e8f8ee", sell_bg="#fde8e8", hold_bg="#eef0f3", reduce_bg="#fef3e0",
        # STHeiti first, not PingFang SC: PingFang ships as a multi-face .ttc
        # (TrueType Collection) — see fc-list — and weasyprint has to extract/
        # resubset a single face out of that container into a standalone
        # CID-keyed CFF font for PDF embedding. That conversion isn't something
        # Apple's own font ever shipped as; poppler-based readers parse the
        # result fine, but some other PDF viewers mis-render it as substituted/
        # wrong glyphs (coherent-looking nonsense, not blank characters — the
        # signature of a glyph-index mismatch, not missing content). STHeiti
        # is a single-file font with no .ttc-extraction step, avoiding the
        # whole failure class. Less refined-looking than PingFang, but correct
        # everywhere beats good-looking in one viewer and broken in another.
        font='"STHeiti", "PingFang SC", "Helvetica Neue", Arial, sans-serif',
        mono='"SF Mono", Menlo, Consolas, monospace',
    ),
    # 深色 Bloomberg 终端风格 —— 沿用 apps/cli/prompts/ui.py 的黑橙配色 token，
    # 跟 /ui 生成的仪表盘、dashboard_generator.py 保持视觉一致
    "bloomberg": Theme(
        name="bloomberg",
        bg="#000000", surface="#111111", text="#E8E9EA", text_muted="#808080", border="#2A2A2A",
        accent="#F5A623",
        buy="#00CC66", sell="#FF3B3B", hold="#4A9EFF", reduce="#FFB800",
        buy_bg="#00331a", sell_bg="#330d0d", hold_bg="#0d1a26", reduce_bg="#332400",
        font='"IBM Plex Sans", "STHeiti", "PingFang SC", "Helvetica Neue", sans-serif',
        mono='"IBM Plex Mono", "SF Mono", Menlo, monospace',
    ),
}


# ── CSS（按 Theme 参数化，两套主题共用同一份布局规则） ──────────────────────────

def build_css(t: Theme) -> str:
    return f"""
/* @page 的 margin 区域（页眉页脚跑马灯文字所在的地方）默认不继承 body 的
   背景色 —— 深色主题下会在页面上下各留一道刺眼的白边，这里显式把整张纸
   （html 根元素）的背景设成主题色，margin 区域才会跟内容区一致。 */
html {{ background: {t.bg}; }}
@page {{
    size: A4;
    margin: 20mm 16mm 18mm 16mm;
    background: {t.bg};
    @top-center {{ content: "{{TITLE}}"; font-family: {t.font}; font-size: 8.5pt; color: {t.text_muted}; }}
    @bottom-right {{ content: counter(page) " / " counter(pages); font-family: {t.font}; font-size: 8.5pt; color: {t.text_muted}; }}
}}
* {{ box-sizing: border-box; }}
body {{ font-family: {t.font}; color: {t.text}; background: {t.bg}; font-size: 9.5pt; line-height: 1.55; }}
.mono {{ font-family: {t.mono}; }}

.cover-title {{ font-size: 24pt; font-weight: 700; color: {t.text}; margin: 0 0 3mm 0; letter-spacing: -0.3px; }}
.cover-meta {{ font-size: 9pt; color: {t.text_muted}; margin-bottom: 3mm; }}
.warning-banner {{ background: {t.reduce_bg}; border-left: 3px solid {t.reduce}; color: {t.reduce};
    padding: 2.5mm 4mm; border-radius: 2px; font-size: 8.8pt; margin-bottom: 6mm; }}

.section {{ margin-top: 8mm; }}
.section.new-page {{ break-before: page; }}
.section-head {{ display: flex; align-items: center; gap: 2.5mm; border-bottom: 1.5px solid {t.accent};
    padding-bottom: 2mm; margin-bottom: 4mm; }}
.section-icon {{ width: 5.5mm; height: 5.5mm; border-radius: 50%; background: {t.accent}; color: {t.bg};
    font-size: 7.5pt; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
.section-title {{ font-size: 13pt; font-weight: 700; color: {t.text}; }}
.section-note {{ font-size: 8.3pt; color: {t.text_muted}; background: {t.surface}; border-radius: 2px;
    padding: 2mm 3mm; margin-bottom: 4mm; border-left: 2px solid {t.border}; }}

.stat-grid {{ display: flex; flex-wrap: wrap; gap: 3mm; margin-bottom: 4mm; }}
.stat-card {{ flex: 1 1 27mm; min-width: 27mm; background: {t.surface}; border: 1px solid {t.border};
    border-radius: {t.radius}; padding: 3mm 3.5mm; break-inside: avoid; }}
.stat-card .label {{ font-size: 7.8pt; color: {t.text_muted}; margin-bottom: 1mm; }}
.stat-card .value {{ font-size: 13pt; font-weight: 700; color: {t.text}; }}
.stat-card.buy .value {{ color: {t.buy}; }}
.stat-card.sell .value {{ color: {t.sell}; }}
.meta-table {{ width: 100%; border-collapse: collapse; font-size: 8.6pt; margin-bottom: 2mm; }}
.meta-table td {{ padding: 1.6mm 2mm; border-bottom: 1px solid {t.border}; vertical-align: top; }}
.meta-table td:first-child {{ color: {t.text_muted}; width: 32mm; white-space: nowrap; }}

.badge {{ display: inline-block; padding: 0.6mm 2.2mm; border-radius: 8px; font-size: 8pt; font-weight: 600; white-space: nowrap; }}
.badge.buy {{ background: {t.buy_bg}; color: {t.buy}; }}
.badge.buy2 {{ background: {t.buy}; color: {t.bg}; }}
.badge.hold {{ background: {t.hold_bg}; color: {t.hold}; }}
.badge.reduce {{ background: {t.reduce_bg}; color: {t.reduce}; }}
.badge.sell {{ background: {t.sell_bg}; color: {t.sell}; }}
.tag {{ display: inline-block; padding: 0.4mm 1.8mm; border-radius: 6px; font-size: 7.6pt; font-weight: 600; }}
.tag.up {{ background: {t.sell_bg}; color: {t.sell}; }}
.tag.down {{ background: {t.sell_bg}; color: {t.sell}; }}
.tag.pos {{ background: {t.buy_bg}; color: {t.buy}; }}
.tag.neutral {{ background: {t.hold_bg}; color: {t.hold}; }}
.tag.confirm {{ background: {t.hold_bg}; color: {t.hold}; }}

table.gtable {{ width: 100%; border-collapse: collapse; font-size: 8.5pt; }}
table.gtable th {{ background: {t.accent}; color: {t.bg}; text-align: left; font-weight: 600; padding: 2mm 2.2mm; font-size: 8pt; }}
table.gtable td {{ padding: 1.9mm 2.2mm; border-bottom: 1px solid {t.border}; vertical-align: middle; }}
table.gtable tr:nth-child(even) td {{ background: {t.surface}; }}
table.gtable tr {{ break-inside: avoid; }}
table.gtable .name-cell {{ font-weight: 600; color: {t.text}; }}
table.gtable .code {{ color: {t.text_muted}; font-size: 7.6pt; }}
table.gtable.compact {{ font-size: 7.4pt; }}
table.gtable.compact th {{ padding: 1.6mm 1.3mm; font-size: 7.1pt; text-align: center; }}
table.gtable.compact td {{ padding: 1.4mm 1.3mm; text-align: center; }}
table.gtable .up-txt {{ color: {t.sell}; }}
table.gtable .down-txt {{ color: {t.buy}; }}

.change-cols {{ display: flex; gap: 4mm; }}
.change-col {{ flex: 1; }}
.change-col h4 {{ font-size: 9.3pt; margin: 0 0 2mm 0; color: {t.text}; }}
.change-col ul {{ margin: 0; padding-left: 4mm; font-size: 8.4pt; }}
.change-col li {{ margin-bottom: 1.3mm; break-inside: avoid; }}

/* 卡片网格用 CSS columns 而不是 flex-wrap —— weasyprint 的 flexbox 分页/
   分片实现有个已验证过的真实 bug：同样内容前面只要有一段稍微复杂点的封面区块
   （标题+meta+警示条），紧跟着的 flex-wrap 卡片网格就会被整体错误地推到下一页，
   哪怕当前页还有大片空白。CSS columns 是印刷排版的标准工具，weasyprint 对它的
   分页支持成熟得多，同样的内容不会触发这个问题（用 /tmp 下 15 轮最小复现实验
   二分定位到的，不是瞎猜的）。*/
.card-grid {{ columns: 2; column-gap: 2.5mm; }}
.card {{ border: 1px solid {t.border}; border-left: 3px solid {t.border};
    border-radius: 2px; padding: 2.2mm 3mm; break-inside: avoid; font-size: 8.2pt; background: {t.bg};
    margin-bottom: 2.5mm; }}
.card.buy {{ border-left-color: {t.buy}; }}
.card.sell {{ border-left-color: {t.sell}; }}
.card.reduce {{ border-left-color: {t.reduce}; }}
.card .head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.3mm; }}
.card .head .name {{ font-weight: 700; color: {t.text}; }}
.card .head .score {{ color: {t.text_muted}; font-size: 7.8pt; }}
.card .line {{ color: {t.text_muted}; margin-top: 0.8mm; }}

/* 同样避开 flex：当 .detail 文本长到要换行时，flex 行内的换行会在分页边界
   被从中间切开（子元素各自独立分页，不是整行一起移动）。改用普通块级 +
   inline-block 定宽标签，避免这个问题。 */
.row-list {{ font-size: 8.2pt; }}
.row-list .row {{ padding: 1.6mm 0; border-bottom: 1px solid {t.border}; break-inside: avoid; }}
.row-list .row .name {{ font-weight: 600; display: inline-block; width: 30mm; vertical-align: top; }}
.row-list .row .meta {{ color: {t.text_muted}; display: inline-block; width: 14mm; vertical-align: top; }}
.row-list .row .detail {{ color: {t.text_muted}; display: inline; }}

.detail-grid {{ columns: 2; column-gap: 3.5mm; }}
.detail-card {{ border: 1px solid {t.border}; border-radius: 3px;
    overflow: hidden; break-inside: avoid; margin-bottom: 3.5mm; }}
.detail-card .dc-head {{ background: {t.buy_bg}; padding: 2.2mm 3.5mm; display: flex; justify-content: space-between;
    align-items: center; border-bottom: 1px solid {t.border}; }}
.detail-card .dc-head .name {{ font-size: 10.5pt; font-weight: 700; color: {t.text}; }}
.detail-card .dc-head .code {{ font-size: 8pt; color: {t.text_muted}; margin-left: 1.5mm; }}
.detail-card .dc-body {{ padding: 2.5mm 3.5mm; }}
.kv-grid {{ display: flex; flex-wrap: wrap; }}
.kv-grid .item {{ width: 50%; padding: 1mm 0; font-size: 8.3pt; }}
.kv-grid .item .k {{ color: {t.text_muted}; font-size: 7.6pt; }}
.kv-grid .item .v {{ color: {t.text}; font-weight: 600; }}
.callout {{ margin-top: 1.5mm; background: {t.reduce_bg}; color: {t.reduce}; font-size: 7.9pt;
    padding: 1.8mm 2.5mm; border-radius: 2px; }}

.news-item {{ border: 1px solid {t.border}; border-radius: 2px; padding: 2.5mm 3.5mm; margin-bottom: 2.5mm; break-inside: avoid; }}
.news-item .n-head {{ display: flex; align-items: center; gap: 2mm; margin-bottom: 1.3mm; }}
.news-item .n-head .name {{ font-weight: 700; color: {t.text}; font-size: 9.3pt; }}
.news-item .kw {{ font-size: 7.8pt; color: {t.text_muted}; margin-bottom: 1.3mm; }}
.news-item .article {{ font-size: 7.9pt; margin: 0.8mm 0; color: {t.text}; }}
.news-item .article .src {{ color: {t.text_muted}; }}
.news-item .article a {{ color: {t.text}; }}

.footer-note {{ text-align: center; color: {t.text_muted}; font-size: 8pt; margin-top: 10mm; font-style: italic; }}

.text-block {{ color: {t.text}; font-size: 9pt; line-height: 1.7; }}
.text-block strong {{ color: {t.text}; font-weight: 700; }}
"""


# ── 各 section kind 的渲染器 ──────────────────────────────────────────────────

def _r_stats(content: dict, t: Theme) -> str:
    cards = "".join(
        f'<div class="stat-card {c.get("tone","")}"><div class="label">{esc(c["label"])}</div>'
        f'<div class="value">{esc(c["value"])}</div></div>'
        for c in content.get("cards", [])
    )
    meta = "".join(f"<tr><td>{esc(m['label'])}</td><td>{esc(m['value'])}</td></tr>" for m in content.get("meta", []))
    return f'<div class="stat-grid">{cards}</div><table class="meta-table">{meta}</table>'


def _badge_html(tone: str, label: str) -> str:
    cls = "buy2" if tone == "buy2" else tone
    return f'<span class="badge {cls}">{esc(label)}</span>'


def _tone_cell_html(v: dict, as_badge: bool) -> str:
    if as_badge:
        return _badge_html(v["tone"], v["label"])
    return f'<span class="tag {v["tone"]}">{esc(v["label"])}</span>'


def _r_badge_table(content: dict, t: Theme) -> str:
    cols = content["columns"]
    ths = "".join(f'<th style="{c.get("style","")}">{esc(c["label"])}</th>' for c in cols)
    trs = []
    for row in content["rows"]:
        tds = [f'<td class="name-cell">{esc(row["name"])} <span class="code">{esc(row.get("code",""))}</span></td>']
        for c in cols[1:]:
            v = row.get(c["key"])
            if isinstance(v, dict) and "tone" in v:
                tds.append(f'<td>{_tone_cell_html(v, bool(c.get("badge")))}</td>')
            else:
                tds.append(f'<td>{esc(v)}</td>')
        trs.append(f"<tr>{''.join(tds)}</tr>")
    return f'<table class="gtable"><tr>{ths}</tr>{"".join(trs)}</table>'


def _r_change_columns(content: dict, t: Theme) -> str:
    cols = []
    for col in content["columns"]:
        items = col.get("items", [])
        if not items:
            continue
        lis = "".join(f"<li>{esc(it)}</li>" for it in items)
        cols.append(f'<div class="change-col"><h4>{esc(col["label"])} ({len(items)})</h4><ul>{lis}</ul></div>')
    if not cols:
        return '<div class="section-note">无显著变化</div>'
    return f'<div class="change-cols">{"".join(cols)}</div>'


def _r_card_grid(content: dict, t: Theme) -> str:
    cards = []
    for it in content["cards"]:
        lines = "".join(f'<div class="line">{esc(ln)}</div>' for ln in it.get("lines", []))
        cards.append(f"""<div class="card {it.get('tone','')}">
            <div class="head"><span class="name">{esc(it['title'])} <span class="code">{esc(it.get('code',''))}</span></span>
            <span class="score">{esc(it.get('subtitle',''))}</span></div>{lines}</div>""")
    return f'<div class="card-grid">{"".join(cards)}</div>'


def _r_row_list(content: dict, t: Theme) -> str:
    rows = []
    for it in content["rows"]:
        badge = _badge_html(it["tone"], it["badge_label"]) if it.get("badge_label") else ""
        rows.append(f"""<div class="row">
            <span class="name">{esc(it['name'])} {badge}</span>
            <span class="meta">{esc(it.get('meta',''))}</span>
            <span class="detail">{esc(it.get('detail',''))}</span></div>""")
    return f'<div class="row-list">{"".join(rows)}</div>'


def _r_data_table(content: dict, t: Theme) -> str:
    cols = content["columns"]
    compact = " compact" if content.get("compact") else ""
    ths = "".join(f"<th>{esc(c['label'])}</th>" for c in cols)
    trs = []
    for row in content["rows"]:
        tds = []
        for c in cols:
            v = row.get(c["key"], "")
            cls = []
            if c.get("numeric"):
                cls.append("mono")
            if c.get("align") == "left":
                cls.append("name-cell")
            if c.get("color_rule") == "cn_updown":
                sv = str(v).strip()
                if sv.startswith("+"):
                    cls.append("up-txt")
                elif sv.startswith("-"):
                    cls.append("down-txt")
            if c.get("badge") and _has_signal_emoji(str(v)):
                tone, label = _signal_tone(str(v))
                cell = _badge_html(tone, label)
            elif c.get("badge"):
                # badge column but this particular row has no recognized signal
                # emoji (e.g. a subtotal/footer row mixed into a signal table) —
                # forcing it through _signal_tone would default to a fake "持有
                # 观望" badge that misrepresents a row that was never a signal.
                cell = esc(strip_emoji(str(v)))
            else:
                cell = esc(v)
            tds.append(f'<td class="{" ".join(cls)}">{cell}</td>')
        trs.append(f"<tr>{''.join(tds)}</tr>")
    return f'<table class="gtable{compact}"><tr>{ths}</tr>{"".join(trs)}</table>'


def _r_detail_cards(content: dict, t: Theme) -> str:
    cards = []
    for it in content["cards"]:
        items = "".join(
            f'<div class="item"><div class="k">{esc(kv["k"])}</div><div class="v">{esc(kv["v"])}</div></div>'
            for kv in it.get("kv", [])
        )
        callout = f'<div class="callout">{esc(it["callout"])}</div>' if it.get("callout") else ""
        badge = _badge_html(it["tone"], it["badge_label"])
        cards.append(f"""<div class="detail-card">
            <div class="dc-head"><span><span class="name">{esc(it['title'])}</span><span class="code">{esc(it.get('code',''))}</span></span>{badge}</div>
            <div class="dc-body"><div class="kv-grid">{items}</div>{callout}</div></div>""")
    return f'<div class="detail-grid">{"".join(cards)}</div>'


def _r_news_cards(content: dict, t: Theme) -> str:
    cards = []
    for it in content["cards"]:
        arts = "".join(
            f'<div class="article"><a href="{esc(a["url"])}">{esc(a["title"])}</a> <span class="src">{esc(a.get("meta",""))}</span></div>'
            for a in it.get("articles", [])
        )
        kw = f'<div class="kw">{esc(it["kw"])}</div>' if it.get("kw") else ""
        cards.append(f"""<div class="news-item">
            <div class="n-head"><span class="name">{esc(it['title'])}</span><span class="code">{esc(it.get('code',''))}</span>
            <span class="tag {it['tone']}">{esc(it['tag_label'])}</span></div>{kw}{arts}</div>""")
    return "".join(cards)


def _r_text(content: dict, t: Theme) -> str:
    # Split on **bold** runs first so each piece can be escaped independently,
    # then wrap the bold pieces in <strong> — escaping after adding the tags
    # would turn the tags themselves into visible text.
    text = content.get("text", "")
    parts = re.split(r"(\*\*.+?\*\*)", text)
    html_parts = [
        f"<strong>{esc(p[2:-2])}</strong>" if p.startswith("**") and p.endswith("**")
        else esc(p)
        for p in parts
    ]
    body = "".join(html_parts).replace("\n\n", "<br><br>").replace("\n", "<br>")
    return f'<div class="text-block">{body}</div>'


RENDERERS: dict[str, Callable[[Any, Theme], str]] = {
    "stats": _r_stats,
    "badge_table": _r_badge_table,
    "change_columns": _r_change_columns,
    "card_grid": _r_card_grid,
    "row_list": _r_row_list,
    "data_table": _r_data_table,
    "detail_cards": _r_detail_cards,
    "news_cards": _r_news_cards,
    "text": _r_text,
}


def render_document(doc: Document, theme: Theme) -> str:
    css = build_css(theme).replace("{TITLE}", esc(doc.title))
    parts = [f"""<div class="cover">
        <div class="cover-title">{esc(doc.title)}</div>
        <div class="cover-meta">{esc(doc.meta)}</div>
        {f'<div class="warning-banner">{esc(doc.warning)}</div>' if doc.warning else ""}
    </div>"""]
    for i, sec in enumerate(doc.sections):
        body = RENDERERS.get(sec.kind, _r_text)(sec.content, theme)
        note_html = f'<div class="section-note">{esc(sec.note)}</div>' if sec.note else ""
        page_cls = " new-page" if (sec.new_page and i > 0) else ""
        parts.append(f"""<div class="section{page_cls}">
            <div class="section-head"><span class="section-icon">{esc(sec.icon)}</span><span class="section-title">{esc(sec.title)}</span></div>
            {note_html}{body}</div>""")
    if doc.footer:
        parts.append(f'<div class="footer-note">{esc(doc.footer)}</div>')
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{"".join(parts)}</body></html>'


# ── 报告类型解析器：短线分析报告（阿萨阿股短线报告的 .md 结构） ────────────────
# 新增报告类型时，照这个函数的样子写一个新的 parse_xxx_report(text) -> Document，
# 不用碰上面的渲染逻辑。

def _parse_md_table(block: str) -> list[dict]:
    lines = [ln for ln in block.strip().splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    headers = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def _parse_name_code(cell: str) -> tuple[str, str]:
    m = re.match(r"\*\*(.+?)\*\*\s*\((\d+)\)", cell.strip())
    return (m.group(1), m.group(2)) if m else (cell.strip(), "")


def _signal_tone(cell: str) -> tuple[str, str]:
    if "🟢🟢" in cell:
        return "buy2", "强力买入"
    if "🟢" in cell:
        return "buy", "买入"
    if "🟡" in cell:
        return "reduce", "减仓"
    if "🔴" in cell:
        return "sell", "卖出"
    return "hold", "持有观望"


def _has_signal_emoji(cell: str) -> bool:
    return any(m in cell for m in ("🟢", "🟡", "🔴", "⬜"))


def parse_shortterm_report(md_text: str) -> Document:
    title_m = re.search(r"^# (.+)$", md_text, re.M)
    meta_m = re.search(r"^> \*\*生成时间\*\*:(.+)$", md_text, re.M)
    warn_m = re.search(r"^> ⚠️ (.+)$", md_text, re.M)

    sections_raw = re.split(r"^## ", md_text, flags=re.M)[1:]
    sec_map = {}
    for s in sections_raw:
        heading, _, body = s.partition("\n")
        sec_map[heading.strip()] = body

    doc = Document(
        title=title_m.group(1).strip() if title_m else "分析报告",
        meta=strip_md_bold(meta_m.group(1)).strip() if meta_m else "",
        warning=warn_m.group(1).strip() if warn_m else "",
    )

    # ① 今日概览
    ov_key = next((k for k in sec_map if "今日概览" in k), None)
    if ov_key:
        rows = _parse_md_table(sec_map[ov_key])
        stat_keys = {"分析标的": "hold", "🟢 买入信号": "buy", "⬜ 持有观望": "hold", "🔴 减仓/离场": "sell",
                     "平均综合评分": "hold", "深度数据覆盖": "hold"}
        cards, meta = [], []
        for r in rows:
            k, v = r.get("项目", ""), r.get("数值", "")
            if k in stat_keys:
                cards.append({"label": strip_emoji(k), "value": v, "tone": stat_keys[k]})
            else:
                meta.append({"label": strip_emoji(k), "value": strip_emoji(v)})
        doc.sections.append(Section("stats", "今日概览", "1", content={"cards": cards, "meta": meta}, new_page=False))

    # ② 综合研判
    vd_key = next((k for k in sec_map if "综合研判" in k), None)
    if vd_key:
        raw = _parse_md_table(sec_map[vd_key])
        rows = []
        for r in raw:
            name, code = _parse_name_code(r.get("标的", ""))
            tone, label = _signal_tone(r.get("🚦 综合研判", r.get("综合研判", "")))
            news_cell = r.get("新闻面", "").strip()
            score_m = re.search(r"[+-][\d.]+", news_cell)
            score_txt = score_m.group(0) if score_m else ""
            if "利好" in news_cell:
                news = {"tone": "pos", "label": f"利好 {score_txt}".strip()}
            elif "利空" in news_cell:
                news = {"tone": "up", "label": f"利空 {score_txt}".strip()}
            elif "中性" in news_cell:
                news = {"tone": "neutral", "label": "中性 +0.00"}
            else:
                news = {"tone": "neutral", "label": "无"}
            confirm_cell = r.get("印证", "").strip()
            confirm = {"tone": "confirm" if "共振" in confirm_cell else "neutral",
                       "label": "共振" if "共振" in confirm_cell else ("中性" if "中性" in confirm_cell else "—")}
            rows.append({"name": name, "code": code, "signal": {"tone": tone, "label": label}, "news": news,
                         "confirm": confirm, "action": strip_md_bold(r.get("一句话操作", "")).strip()})
        doc.sections.append(Section("badge_table", "综合研判 · 一眼看清买卖", "2", content={
            "columns": [
                {"key": "name", "label": "标的", "style": "width:26%"},
                {"key": "signal", "label": "综合研判", "badge": True, "style": "width:12%"},
                {"key": "news", "label": "新闻面", "style": "width:14%"},
                {"key": "confirm", "label": "印证", "style": "width:9%"},
                {"key": "action", "label": "一句话操作"},
            ],
            "rows": rows,
        }, note="综合研判以已回测的量化技术/基本面信号为主导，新闻面情绪仅作共振/分歧提示，分歧不等于反向。"))

    # ③ 信号变化
    ch_key = next((k for k in sec_map if "信号变化" in k), None)
    if ch_key:
        body = sec_map[ch_key]
        cols = []
        for pat, label in [(r"\*\*🆕 新增买入\*\*.*?(?=\n\*\*|\Z)", "🆕 新增买入"),
                            (r"\*\*🚪 退出买入\*\*.*?(?=\n\*\*|\Z)", "🚪 退出买入"),
                            (r"\*\*⬇️ 信号下调\*\*.*?(?=\n\*\*|\Z)", "⬇️ 信号下调"),
                            (r"\*\*⬆️ 信号上调\*\*.*?(?=\n\*\*|\Z)", "⬆️ 信号上调")]:
            m = re.search(pat, body, re.S)
            if not m:
                continue
            items = [strip_emoji(strip_md_bold(ln.strip()[2:])) for ln in m.group(0).splitlines() if ln.strip().startswith("- ")]
            if items:
                cols.append({"label": strip_emoji(label), "items": items})
        if cols:
            date_m = re.search(r"vs 上一交易日 ([\d\-]+)", ch_key)
            doc.sections.append(Section("change_columns", f"信号变化（vs 上一交易日 {date_m.group(1) if date_m else ''}）",
                                         "3", content={"columns": cols}, new_page=False))

    # ④ 今日特殊行情
    sp_key = next((k for k in sec_map if "今日特殊行情" in k), None)
    if sp_key:
        blocks = re.split(r"\n(?=- \*\*)", sec_map[sp_key].strip())
        cards = []
        for b in blocks:
            hm = re.match(r"- \*\*(.+?)\*\* \((\d+)\) (\S+)\s*评分(\d+)", b.strip())
            if not hm:
                continue
            name, code, badge_emoji, score = hm.groups()
            tone, _ = _signal_tone(badge_emoji)
            # 第一行是标题行本身（- **name** (code) ...），之后每一行缩进的
            # "  - 详情" 才是风险提示，用 splitlines 按行处理，不猜整块的缩进规律
            alerts = [strip_emoji(ln.strip()[2:]) for ln in b.splitlines()[1:] if ln.strip().startswith("- ")]
            cards.append({"title": name, "code": code, "subtitle": f"评分 {score}", "tone": tone,
                          "lines": [a.strip() for a in alerts]})
        if cards:
            doc.sections.append(Section("card_grid", "今日特殊行情 & 风险提示", "4", content={"cards": cards}))

    # ⑤ 今日优选短线标的
    pk_key = next((k for k in sec_map if "今日优选短线标的" in k), None)
    if pk_key:
        rows = _parse_md_table(sec_map[pk_key])
        cols_spec = [
            {"key": "代码", "label": "代码"}, {"key": "名称", "label": "名称", "align": "left"},
            {"key": "分", "label": "分", "numeric": True}, {"key": "信号", "label": "信号", "badge": True},
            {"key": "现价", "label": "现价", "numeric": True}, {"key": "今涨", "label": "今涨", "numeric": True, "color_rule": "cn_updown"},
            {"key": "5日", "label": "5日", "numeric": True, "color_rule": "cn_updown"}, {"key": "量比", "label": "量比", "numeric": True},
            {"key": "目标/参考", "label": "目标/参考", "numeric": True}, {"key": "止损/参考", "label": "止损/参考", "numeric": True},
            {"key": "R/R", "label": "R/R", "numeric": True}, {"key": "RSI", "label": "RSI", "numeric": True},
            {"key": "MACD", "label": "MACD"}, {"key": "均线", "label": "均线"}, {"key": "形态", "label": "形态"},
        ]
        # "信号" carries its raw 🟢/⬜/🔴 emoji through untouched — _r_data_table's
        # badge column path converts it via _signal_tone()/_badge_html() the same
        # way the ②⑥⑦ sections already do. Every other column gets emoji-stripped:
        # source data appends decorative suffix emoji straight onto plain values
        # (e.g. "建设银行⭐", RSI "77🔴") that have no badge equivalent and were
        # rendering as floating glyphs (see strip_emoji's docstring above).
        clean_rows = [
            {
                c["key"]: strip_md_bold(r.get(c["key"], "")) if c["key"] == "信号"
                else strip_emoji(strip_md_bold(r.get(c["key"], "")))
                for c in cols_spec
            }
            for r in rows
        ]
        doc.sections.append(Section("data_table", "今日优选短线标的", "5", content={
            "columns": cols_spec, "rows": clean_rows, "compact": True,
        }, note="~¥ 前缀 = 持有标的参考区间，非买入入场价。"))

    # ⑥ 买入/加仓候选
    buy_key = next((k for k in sec_map if k.startswith("🟢 买入")), None)
    if buy_key:
        blocks = re.split(r"\n(?=### )", sec_map[buy_key].strip())
        cards = []
        kv_order = ["现价", "建议入场", "目标价", "止损价", "盈亏比", "今日涨幅", "RSI(14)", "MACD状态", "信号类型"]
        for b in blocks:
            hm = re.match(r"### (.+?) \((\d+)\)\s*(\S+)", b)
            if not hm:
                continue
            name, code, badge = hm.groups()
            tone, label = _signal_tone(badge)
            rows = _parse_md_table(b)
            kv = {r["项目"]: r["数值"] for r in rows if "项目" in r}
            risk = strip_emoji(kv.get("⚠️ 追高风险", ""))
            cards.append({"title": name, "code": code, "tone": tone, "badge_label": label,
                          "kv": [{"k": strip_emoji(k), "v": strip_emoji(kv[k])} for k in kv_order if k in kv],
                          "callout": f"注意：{risk}" if risk else None})
        if cards:
            doc.sections.append(Section("detail_cards", "买入/加仓候选", "6", content={"cards": cards}))

    # ⑦ 减仓/观察
    rd_key = next((k for k in sec_map if k.startswith("🔴 减仓")), None)
    if rd_key:
        rows = []
        for line in sec_map[rd_key].splitlines():
            m = re.match(r"- \*\*(.+?)\*\* \((\d+)\) — (\S+)\s*评分\s*(\d+)\s*\|\s*(.+)", line.strip())
            if not m:
                continue
            name, code, badge, score, detail = m.groups()
            tone, label = _signal_tone(badge)
            rows.append({"name": name, "tone": tone, "badge_label": label, "meta": f"评分{score}", "detail": strip_emoji(detail)})
        if rows:
            doc.sections.append(Section("row_list", "减仓/观察", "7", content={"rows": rows}))

    # ⑧ 新闻面预测
    nw_key = next((k for k in sec_map if "新闻面预测" in k), None)
    if nw_key:
        blocks = re.split(r"\n(?=### )", sec_map[nw_key].strip())
        cards = []
        for b in blocks:
            hm = re.match(r"### (\S+) (.+?) \((\d+)\) · (\S+) ([+\-][\d.]+)（(\d+) 条相关）", b)
            if not hm:
                continue
            _e, name, code, label, score, count = hm.groups()
            tone = {"利好": "pos", "利空": "up", "中性": "neutral"}.get(label, "neutral")
            kw_m = re.search(r"- 情绪关键词: (.+)$", b, re.M)
            articles = [{"title": t, "url": u, "meta": f"{s} · {d}"}
                       for t, u, s, d in re.findall(r"- \[(.+?)\]\((.+?)\)\s*<sub>(.+?) · (.+?)</sub>", b)]
            cards.append({"title": name, "code": code, "tone": tone, "tag_label": f"{label} {score}（{count}条）",
                          "kw": f"情绪关键词: {kw_m.group(1).strip()}" if kw_m else "", "articles": articles})
        if cards:
            doc.sections.append(Section("news_cards", "新闻面预测（近两周资讯情绪）", "8", content={"cards": cards},
                                        note="情绪分 ∈ [−1, +1]：> +0.15 偏多、< −0.15 偏空，其间为中性。"))

    # ⑨ 短线交易纪律
    rl_key = next((k for k in sec_map if "短线交易纪律" in k), None)
    if rl_key:
        raw = _parse_md_table(sec_map[rl_key])
        rows = [{"规则": strip_emoji(strip_md_bold(r.get("规则", ""))),
                 "要求": strip_emoji(strip_md_bold(r.get("要求", "")))} for r in raw]
        doc.sections.append(Section("data_table", "短线交易纪律", "9", content={
            "columns": [{"key": "规则", "label": "规则", "align": "left"}, {"key": "要求", "label": "要求", "align": "left"}],
            "rows": rows,
        }))

    doc.footer = "Arthera Quant — 短线分析，严格止损，风险自担 · 每日盘后自动更新"
    return doc


def parse_longterm_report(md_text: str) -> Document:
    """Compatibility alias — longterm report generation moved to Arthera backend."""
    return parse_shortterm_report(md_text)


PARSERS: dict[str, Callable[[str], Document]] = {
    "shortterm": parse_shortterm_report,
    "longterm": parse_longterm_report,
}
