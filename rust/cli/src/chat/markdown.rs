//! CommonMark source stays in the app; the viewport receives styled lines.
use super::theme::Palette;
use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::Style,
    text::{Line, Span},
};
use unicode_width::UnicodeWidthStr;

fn is_cjk(c: char) -> bool {
    ('\u{4E00}'..='\u{9FFF}').contains(&c) || ('\u{3400}'..='\u{4DBF}').contains(&c)
}

fn is_inline_fragment(s: &str) -> bool {
    let t = s.trim();
    if t.is_empty() || t.len() > 24 {
        return false;
    }
    if t.starts_with('#') || t.starts_with('-') || t.starts_with('*') || t.starts_with('|') {
        return false;
    }
    let all_num_or_ticker = t
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '%' | ',' | '+' | '-' | '$' | '¥'));
    let short_cjk_or_punct = t.chars().count() <= 3
        && t.chars().all(|c| {
            is_cjk(c)
                || matches!(
                    c,
                    '。' | '，'
                        | '：'
                        | '；'
                        | '（'
                        | '）'
                        | '('
                        | ')'
                        | '！'
                        | '？'
                        | '.'
                        | ','
                        | ':'
                        | ';'
                )
        });
    all_num_or_ticker || short_cjk_or_punct
}

fn join_inline_fragment(prev: &str, next: &str) -> String {
    let p = prev.trim_end();
    let n = next.trim_start();
    let last = p.chars().last();
    let first = n.chars().next();
    let need_space = matches!(
        (last, first),
        (Some(a), Some(b))
            if (a.is_ascii_alphanumeric() && b.is_ascii_alphanumeric())
                || (is_cjk(a) && b.is_ascii_alphanumeric())
                || (a.is_ascii_alphanumeric() && is_cjk(b))
    );
    if need_space {
        format!("{p} {n}")
    } else {
        format!("{p}{n}")
    }
}

/// Normalize weak LLM Markdown (e.g. `###标题`, `。###`, `-中文`, fragmented `\n\n125.53\n\n`)
/// outside fenced code blocks so CommonMark renders clean headings, lists, and paragraphs.
fn normalize_llm_markdown(source: &str) -> String {
    let mut out_lines: Vec<String> = Vec::new();
    let mut in_fence = false;
    let raw_lines: Vec<&str> = source.lines().collect();

    // Pass 1: collapse fragmented single-token blank-line splits outside fences.
    let mut collapsed: Vec<String> = Vec::new();
    let mut i = 0;
    while i < raw_lines.len() {
        let line = raw_lines[i];
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_fence = !in_fence;
            collapsed.push(line.to_owned());
            i += 1;
            continue;
        }
        if in_fence {
            collapsed.push(line.to_owned());
            i += 1;
            continue;
        }
        if line.trim().is_empty()
            && !collapsed.is_empty()
            && i + 1 < raw_lines.len()
            && !raw_lines[i + 1].trim().is_empty()
        {
            let prev = collapsed.last().unwrap().as_str();
            let next = raw_lines[i + 1];
            let prev_ends_open = prev.trim_end().chars().last().is_some_and(|c| {
                matches!(c, '（' | '(' | '为' | '是' | '的' | '：' | ':') || is_cjk(c)
            });
            let next_starts_close = next.trim_start().chars().next().is_some_and(|c| {
                matches!(
                    c,
                    '）' | ')' | '。' | '，' | '：' | '；' | '美' | '元' | '%' | '.'
                )
            });
            let prev_ends_num = prev
                .trim_end()
                .chars()
                .last()
                .is_some_and(|c| c.is_ascii_alphanumeric());
            if (is_inline_fragment(next) && prev_ends_open)
                || ((is_inline_fragment(prev) || prev_ends_num) && next_starts_close)
                || (is_inline_fragment(prev) && is_inline_fragment(next))
            {
                let merged = join_inline_fragment(prev, next);
                *collapsed.last_mut().unwrap() = merged;
                i += 2;
                continue;
            }
        }
        collapsed.push(line.to_owned());
        i += 1;
    }

    // Pass 2: split glued headings (`。###标题`) / glued bullets (` -中文`) and add missing space.
    in_fence = false;
    for line in collapsed {
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_fence = !in_fence;
            out_lines.push(line);
            continue;
        }
        if in_fence {
            out_lines.push(line);
            continue;
        }

        // Split glued inline `###` headings (2-6 `#` preceded by non-`#` / non-whitespace or space).
        let mut expanded = String::with_capacity(line.len() + 16);
        let chars: Vec<char> = line.chars().collect();
        let mut k = 0;
        while k < chars.len() {
            if k > 0 && chars[k] == '#' {
                let mut hashes = 0;
                while k + hashes < chars.len() && chars[k + hashes] == '#' {
                    hashes += 1;
                }
                if (2..=6).contains(&hashes) {
                    let prev = chars[k - 1];
                    if matches!(
                        prev,
                        '。' | '！' | '？' | '.' | '!' | '?' | ' ' | '；' | ';'
                    ) {
                        if !expanded.ends_with('\n') {
                            expanded.push('\n');
                        }
                        for _ in 0..hashes {
                            expanded.push('#');
                        }
                        if k + hashes < chars.len() && chars[k + hashes] != ' ' {
                            expanded.push(' ');
                        }
                        k += hashes;
                        continue;
                    }
                }
            }
            if k > 1
                && chars[k] == '-'
                && k + 1 < chars.len()
                && (is_cjk(chars[k + 1]) || chars[k + 1] == '*')
            {
                let prev = chars[k - 1];
                if matches!(prev, ' ' | '。' | '！' | '？' | '；' | '：') {
                    expanded.push('\n');
                    expanded.push_str("- ");
                    k += 1;
                    continue;
                }
            }
            expanded.push(chars[k]);
            k += 1;
        }

        for sub in expanded.split('\n') {
            let mut s = sub.to_owned();
            // Fix leading `###标题` -> `### 标题`
            let hash_count = s.chars().take_while(|&c| c == '#').count();
            if (1..=6).contains(&hash_count) {
                let rest = &s[hash_count..];
                if rest.chars().next().is_some_and(|c| c != ' ' && c != '#') {
                    s = format!("{} {}", "#".repeat(hash_count), rest);
                }
            } else if let Some(rest) = s.strip_prefix('-') {
                // Fix leading `-动量因子` or `-**加粗**` -> `- 动量因子` (do not touch `--` or `-0.4`)
                if rest.chars().next().is_some_and(|c| is_cjk(c) || c == '*') {
                    s = format!("- {rest}");
                }
            }
            out_lines.push(s);
        }
    }

    out_lines.join("\n")
}

pub fn render(source: &str, palette: Palette, width: u16) -> Vec<Line<'static>> {
    let normalized = normalize_llm_markdown(source);
    let source = normalized.as_str();
    let mut out = Vec::new();
    let mut spans = Vec::new();
    let mut styles = vec![Style::default()];
    let mut lists: Vec<Option<u64>> = Vec::new();
    let mut code = false;
    let mut table: Option<Vec<Vec<Vec<Span<'static>>>>> = None;
    let mut row = Vec::new();
    let mut cell = Vec::new();
    let mut in_cell = false;
    let mut links = Vec::new();
    for event in Parser::new_ext(
        source,
        Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS,
    ) {
        let style = *styles.last().unwrap();
        match event {
            Event::Start(Tag::Table(_)) => {
                flush(&mut out, &mut spans);
                table = Some(Vec::new());
            }
            Event::Start(Tag::TableCell) => {
                in_cell = true;
                cell.clear();
            }
            Event::End(TagEnd::TableCell) => {
                in_cell = false;
                row.push(std::mem::take(&mut cell));
            }
            Event::End(TagEnd::TableHead | TagEnd::TableRow) => {
                if let Some(table) = &mut table {
                    table.push(std::mem::take(&mut row));
                }
            }
            Event::End(TagEnd::Table) => {
                if let Some(table) = table.take() {
                    out.extend(render_table(table, width, palette));
                }
                blank(&mut out);
            }
            Event::Start(Tag::Strong) => styles.push(style.bold()),
            Event::Start(Tag::Emphasis) => styles.push(style.italic()),
            Event::Start(Tag::Strikethrough) => styles.push(style.crossed_out()),
            Event::Start(Tag::Heading { .. }) => {
                flush(&mut out, &mut spans);
                styles.push(style.bold().fg(palette.accent));
            }
            Event::Start(Tag::CodeBlock(kind)) => {
                flush(&mut out, &mut spans);
                let language = match kind {
                    CodeBlockKind::Fenced(s) => s.into_string(),
                    _ => String::new(),
                };
                out.push(Line::styled(
                    if language.is_empty() {
                        "── code".into()
                    } else {
                        format!("── {language}")
                    },
                    Style::new().fg(palette.muted),
                ));
                styles.push(style.fg(palette.assistant).bg(palette.code_bg));
                code = true;
            }
            Event::Start(Tag::List(start)) => {
                flush(&mut out, &mut spans);
                lists.push(start);
            }
            Event::Start(Tag::Item) => {
                flush(&mut out, &mut spans);
                let indent = "  ".repeat(lists.len().saturating_sub(1));
                let marker = if let Some(Some(n)) = lists.last_mut() {
                    let text = format!("{n}. ");
                    *n += 1;
                    text
                } else {
                    "• ".into()
                };
                spans.push(Span::raw(format!("{indent}{marker}")));
            }
            Event::Start(Tag::BlockQuote(_)) => {
                flush(&mut out, &mut spans);
                styles.push(style.fg(palette.muted));
                spans.push(Span::raw("│ "));
            }
            Event::Start(Tag::Link { dest_url, .. }) => {
                links.push(dest_url.into_string());
                styles.push(style.underlined().fg(palette.assistant));
            }
            Event::End(TagEnd::Link) => {
                styles.pop();
                // A labelled link must remain usable in terminals without OSC links.
                if let Some(url) = links.pop().filter(|s| !s.is_empty()) {
                    let destination =
                        Span::styled(format!(" ({url})"), Style::new().fg(palette.muted));
                    if in_cell {
                        cell.push(destination);
                    } else {
                        spans.push(destination);
                    }
                }
            }
            Event::Text(text) | Event::Html(text) | Event::InlineHtml(text) => {
                if code {
                    for line in text.split_inclusive('\n') {
                        spans.push(Span::styled(line.trim_end_matches('\n').to_owned(), style));
                        if line.ends_with('\n') {
                            flush(&mut out, &mut spans);
                        }
                    }
                } else if in_cell {
                    cell.push(Span::styled(text.into_string(), style));
                } else {
                    spans.push(Span::styled(text.into_string(), style));
                }
            }
            Event::Code(text) => {
                let span = Span::styled(
                    text.into_string(),
                    style.fg(palette.assistant).bg(palette.code_bg),
                );
                if in_cell {
                    cell.push(span);
                } else {
                    spans.push(span);
                }
            }
            Event::TaskListMarker(done) => {
                spans.push(Span::raw(if done { "☑ " } else { "☐ " }))
            }
            Event::SoftBreak => {
                if in_cell {
                    cell.push(Span::raw(" "));
                } else {
                    flush(&mut out, &mut spans);
                }
            }
            Event::HardBreak => flush(&mut out, &mut spans),
            Event::Rule => {
                flush(&mut out, &mut spans);
                out.push(Line::styled(
                    "─".repeat(usize::from(width.min(40))),
                    Style::new().fg(palette.muted),
                ));
            }
            Event::End(TagEnd::CodeBlock) => {
                flush(&mut out, &mut spans);
                styles.pop();
                code = false;
                blank(&mut out);
            }
            Event::End(TagEnd::Heading(_)) => {
                flush(&mut out, &mut spans);
                styles.pop();
            }
            Event::End(TagEnd::Strong | TagEnd::Emphasis | TagEnd::Strikethrough) => {
                styles.pop();
            }
            Event::End(TagEnd::BlockQuote(_)) => {
                flush(&mut out, &mut spans);
                styles.pop();
            }
            Event::End(TagEnd::Paragraph) => {
                flush(&mut out, &mut spans);
                if lists.is_empty() {
                    blank(&mut out);
                }
            }
            Event::End(TagEnd::Item) => flush(&mut out, &mut spans),
            Event::End(TagEnd::List(_)) => {
                lists.pop();
                if lists.is_empty() {
                    blank(&mut out);
                }
            }
            _ => (),
        }
    }
    flush(&mut out, &mut spans);
    while out.last().is_some_and(|l| l.spans.is_empty()) {
        out.pop();
    }
    out
}
fn flush(out: &mut Vec<Line<'static>>, spans: &mut Vec<Span<'static>>) {
    if !spans.is_empty() {
        out.push(Line::from(std::mem::take(spans)));
    }
}
fn blank(out: &mut Vec<Line<'static>>) {
    if out.last().is_some_and(|l| !l.spans.is_empty()) {
        out.push(Line::default());
    }
}
fn render_table(
    rows: Vec<Vec<Vec<Span<'static>>>>,
    width: u16,
    palette: Palette,
) -> Vec<Line<'static>> {
    let Some(header) = rows.first() else {
        return Vec::new();
    };
    let mut widths = vec![0; header.len()];
    for row in &rows {
        for (i, cell) in row.iter().enumerate().take(widths.len()) {
            widths[i] = widths[i].max(cell.iter().map(|s| s.content.width()).sum::<usize>());
        }
    }
    let mut out = Vec::new();
    if widths.iter().sum::<usize>() + widths.len().saturating_sub(1) * 3 <= usize::from(width) {
        for (r, row) in rows.iter().enumerate() {
            let mut spans = Vec::new();
            for (i, w) in widths.iter().enumerate() {
                if i > 0 {
                    spans.push(Span::styled(" │ ", Style::new().fg(palette.muted)));
                }
                let cell = row.get(i).cloned().unwrap_or_default();
                let used: usize = cell.iter().map(|s| s.content.width()).sum();
                spans.extend(cell.into_iter().map(|s| {
                    if r == 0 {
                        s.style(Style::new().bold())
                    } else {
                        s
                    }
                }));
                spans.push(Span::raw(" ".repeat(w.saturating_sub(used))));
            }
            out.push(Line::from(spans));
        }
    } else {
        for row in rows.iter().skip(1) {
            for (i, cell) in row.iter().enumerate() {
                let label: String = header
                    .get(i)
                    .into_iter()
                    .flatten()
                    .map(|s| s.content.as_ref())
                    .collect();
                let mut spans = vec![Span::styled(format!("{label}: "), Style::new().bold())];
                spans.extend(cell.clone());
                out.push(Line::from(spans));
            }
            blank(&mut out);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    fn text(lines: &[Line<'_>]) -> String {
        lines
            .iter()
            .map(|l| {
                l.spans
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
    #[test]
    fn prose_is_styled_without_literal_markup_and_bullets_are_neutral() {
        let p = Palette::for_theme("light");
        let lines = render(
            "## 结果\n\n**完成** `你好.rs`\n\n- 文件已读\n- 测试通过",
            p,
            80,
        );
        let body = text(&lines);
        assert!(!body.contains("##") && !body.contains('`') && !body.contains("**"));
        assert!(body.contains("• 文件已读"));
        assert!(!lines
            .iter()
            .flat_map(|l| &l.spans)
            .any(|s| s.style.fg == Some(p.error)));
    }
    #[test]
    fn fenced_source_preserves_newlines_spaces_and_syntax() {
        let lines = render(
            "```rust\n  let s = \"**你好**\";\n\n  s\n```",
            Palette::for_theme("dark"),
            80,
        );
        assert_eq!(text(&lines), "── rust\n  let s = \"**你好**\";\n\n  s");
    }
    #[test]
    fn tables_fall_back_to_labelled_values_at_small_widths() {
        let source = "| 文件 | 结果 |\n|---|---|\n| src/你好.rs | 已完成 |";
        assert!(
            text(&render(source, Palette::for_theme("light"), 10)).contains("文件: src/你好.rs")
        );
        assert!(text(&render(source, Palette::for_theme("light"), 80)).contains('│'));
    }
    #[test]
    fn normalizes_glued_headings_bullets_and_fragmented_numbers() {
        let raw = "此外，英伟达的最新价格为\n\n125.53\n\n美元，数据点总数为\n\n250\n\n。接下来分析。###动量因子\n-动量因子（12个月至1个月）：0.0978 -这个因子表明趋势。###波动率\n-年化波动率：0.4125";
        let rendered = text(&render(raw, Palette::for_theme("light"), 80));
        assert!(rendered.contains("125.53 美元"), "got:\n{rendered}");
        assert!(rendered.contains("250。"), "got:\n{rendered}");
        assert!(!rendered.contains("###"), "bare ### leaked:\n{rendered}");
        assert!(rendered.contains("动量因子"), "got:\n{rendered}");
        assert!(rendered.contains("• 动量因子"), "got:\n{rendered}");
        assert!(rendered.contains("• 年化波动率"), "got:\n{rendered}");
    }
}
