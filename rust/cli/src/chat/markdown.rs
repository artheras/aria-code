//! CommonMark source stays in the app; the viewport receives styled lines.
use super::theme::Palette;
use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
use ratatui::{
    style::Style,
    text::{Line, Span},
};
use unicode_width::UnicodeWidthStr;

pub fn render(source: &str, palette: Palette, width: u16) -> Vec<Line<'static>> {
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
                    spans.push(Span::raw(" "));
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
}
