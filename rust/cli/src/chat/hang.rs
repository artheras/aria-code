//! A message with a marker in front and its lines hanging beneath it.
//!
//! Codex and Claude Code both lay a conversation out this way: one glyph at
//! the head of whatever the assistant says, the prompt glyph at the head of
//! what the user says, and every following or wrapped line indented to sit
//! under the text rather than under the glyph. Neither prints a name line.
//!
//! The indent has to survive wrapping, and the terminal's own wrapping does
//! not know about it — a wrapped tail would start at column 0, under the
//! marker. So lines are wrapped here, to the width left after the indent,
//! before the paragraph ever sees them; they then fit and are not re-wrapped.

use ratatui::{
    style::Style,
    text::{Line, Span},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

/// Columns every line is indented by; the marker and its space fill them.
pub const INDENT: u16 = 2;

/// `lines` with `marker` (two columns wide, including its space) before the
/// first, and every other line — wrapped or not — indented to match.
pub fn hang(
    lines: Vec<Line<'static>>,
    marker: Vec<Span<'static>>,
    width: u16,
) -> Vec<Line<'static>> {
    let avail = usize::from(width.saturating_sub(INDENT)).max(8);
    let mut out = Vec::new();
    let mut marker = Some(marker);
    for line in lines {
        for row in wrap(line, avail) {
            let mut spans = match marker.take() {
                Some(head) => head,
                None => vec![Span::raw(" ".repeat(usize::from(INDENT)))],
            };
            spans.extend(row);
            out.push(Line::from(spans));
        }
    }
    if let Some(head) = marker {
        // An empty message still says who spoke.
        out.push(Line::from(head));
    }
    out
}

/// One logical line as rows no wider than `avail`, styles kept. Breaks after
/// a space when there is one in the back half of the row — English stays in
/// words — and between graphemes otherwise, which is how CJK text wraps.
fn wrap(line: Line<'static>, avail: usize) -> Vec<Vec<Span<'static>>> {
    let line_style = line.style;
    let cells: Vec<(String, Style)> = line
        .spans
        .iter()
        .flat_map(|span| {
            let style = line_style.patch(span.style);
            span.content
                .graphemes(true)
                .map(move |g| (g.to_owned(), style))
                .collect::<Vec<_>>()
        })
        .collect();
    if cells.is_empty() {
        return vec![Vec::new()];
    }
    let mut rows = Vec::new();
    let mut start = 0;
    while start < cells.len() {
        let mut used = 0;
        let mut end = start;
        while end < cells.len() {
            let w = cells[end].0.width();
            if used + w > avail && end > start {
                break;
            }
            used += w;
            end += 1;
        }
        if end < cells.len() {
            if let Some(space) = (start..end).rev().find(|&i| cells[i].0 == " ") {
                if space > start + (end - start) / 2 {
                    end = space + 1;
                }
            }
        }
        rows.push(join(&cells[start..end]));
        start = end;
    }
    rows
}

fn join(cells: &[(String, Style)]) -> Vec<Span<'static>> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    for (text, style) in cells {
        match spans.last_mut() {
            Some(last) if last.style == *style => last.content.to_mut().push_str(text),
            _ => spans.push(Span::styled(text.clone(), *style)),
        }
    }
    spans
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::style::Color;

    fn text(line: &Line) -> String {
        line.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    fn marker() -> Vec<Span<'static>> {
        vec![Span::raw("▄"), Span::raw(" ")]
    }

    #[test]
    fn the_marker_heads_the_first_line_only() {
        let out = hang(vec![Line::raw("one"), Line::raw("two")], marker(), 40);
        assert_eq!(text(&out[0]), "▄ one");
        assert_eq!(text(&out[1]), "  two");
    }

    #[test]
    fn a_wrapped_tail_stays_under_the_text_not_under_the_marker() {
        // The terminal would start it at column 0; that is the reason this
        // module exists.
        let out = hang(vec![Line::raw("alpha beta gamma delta")], marker(), 14);
        assert!(out.len() > 1);
        for row in &out[1..] {
            assert!(text(row).starts_with("  "), "{:?}", text(row));
        }
    }

    #[test]
    fn no_row_is_wider_than_the_width() {
        let out = hang(
            vec![Line::raw("一二三四五六七八九十一二三四五六七八九十")],
            marker(),
            12,
        );
        for row in &out {
            assert!(text(row).width() <= 12, "{:?}", text(row));
        }
    }

    #[test]
    fn english_breaks_between_words() {
        let out = hang(vec![Line::raw("hello wonderful world")], marker(), 18);
        assert_eq!(text(&out[0]).trim_end(), "▄ hello wonderful");
        assert_eq!(text(&out[1]), "  world");
    }

    #[test]
    fn styles_survive_the_wrap() {
        let bold = Style::new().fg(Color::Red);
        let line = Line::from(vec![
            Span::raw("plain "),
            Span::styled("redredredred", bold),
        ]);
        let out = hang(vec![line], marker(), 12);
        assert!(out.iter().flat_map(|l| &l.spans).any(|s| s.style == bold));
    }

    #[test]
    fn a_blank_line_inside_a_message_stays_blank() {
        let out = hang(
            vec![Line::raw("a"), Line::raw(""), Line::raw("b")],
            marker(),
            20,
        );
        assert_eq!(text(&out[1]).trim(), "");
        assert_eq!(text(&out[2]), "  b");
    }

    #[test]
    fn an_empty_message_still_shows_its_marker() {
        let out = hang(Vec::new(), marker(), 20);
        assert_eq!(text(&out[0]), "▄ ");
    }
}
