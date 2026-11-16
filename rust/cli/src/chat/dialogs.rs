use super::app::{App, Dialog, Risk};
use super::theme::Palette;
use super::ui::{fit, popup};
use ratatui::{
    layout::{Constraint, Layout, Margin, Rect},
    style::{Color, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Clear, Paragraph, Wrap},
    Frame,
};
use unicode_width::UnicodeWidthStr;

/// Lines outside a hunk are shown as plain context.
pub(super) fn context_lines(context: &str, p: Palette) -> Vec<Line<'static>> {
    let (add_bg, del_bg) = if p.selection_fg == Color::White {
        (Color::Rgb(218, 251, 225), Color::Rgb(255, 235, 233))
    } else {
        (Color::Rgb(18, 58, 30), Color::Rgb(74, 22, 26))
    };
    let mut out = Vec::new();
    let mut hunk: Option<(usize, usize)> = None; // (old line, new line)
    let width = 4;
    for line in context.lines() {
        if let Some(rest) = line.strip_prefix("@@ ") {
            // @@ -a,b +c,d @@
            let mut parts = rest.split_whitespace();
            let old = parts
                .next()
                .and_then(|t| t.trim_start_matches('-').split(',').next()?.parse().ok());
            let new = parts
                .next()
                .and_then(|t| t.trim_start_matches('+').split(',').next()?.parse().ok());
            hunk = old.zip(new);
            out.push(Line::styled(line.to_owned(), Style::new().fg(p.muted)));
            continue;
        }
        let Some((old, new)) = hunk.as_mut() else {
            out.push(Line::styled(line.to_owned(), Style::new().fg(p.assistant)));
            continue;
        };
        let (number, sign, body, bg) = if let Some(body) = line.strip_prefix('+') {
            let n = *new;
            *new += 1;
            (n, "+", body, Some(add_bg))
        } else if let Some(body) = line.strip_prefix('-') {
            let n = *old;
            *old += 1;
            (n, "-", body, Some(del_bg))
        } else {
            let n = *new;
            *old += 1;
            *new += 1;
            (n, " ", line.strip_prefix(' ').unwrap_or(line), None)
        };
        let mut style = Style::new();
        if let Some(bg) = bg {
            style = style.bg(bg);
        }
        out.push(Line::from(vec![
            Span::styled(format!("{number:>width$} "), Style::new().fg(p.muted)),
            Span::styled(format!("{sign}{body}"), style),
        ]));
    }
    out
}

/// What an action reaches, drawn above its approval: a coloured level, then
/// the hosts, files and systems it touches and why it is rated that way.
pub(super) fn risk_card(app: &App, risk: &Risk, p: Palette) -> Vec<Line<'static>> {
    let level_color = match risk.level {
        0 | 1 => p.assistant,
        2 => Color::Yellow,
        _ => Color::Red,
    };
    let undo = if risk.reversible {
        app.tr("reversible", "可撤销")
    } else {
        app.tr("not reversible", "不可撤销")
    };
    let mut lines = vec![Line::from(vec![
        Span::styled(
            format!("{:<9}", app.tr("Risk", "风险")),
            Style::new().fg(p.muted),
        ),
        Span::styled(
            format!("L{} {}", risk.level, risk.name),
            Style::new().fg(level_color).bold(),
        ),
        Span::styled(
            format!(" · {}/100 · {undo}", risk.score),
            Style::new().fg(p.muted),
        ),
    ])];
    let mut row = |label: &str, values: &[String]| {
        if values.is_empty() {
            return;
        }
        let shown: Vec<&str> = values.iter().take(4).map(String::as_str).collect();
        let more = values.len().saturating_sub(4);
        let tail = if more > 0 {
            format!(" +{more}")
        } else {
            String::new()
        };
        lines.push(Line::from(vec![
            Span::styled(format!("{label:<9}"), Style::new().fg(p.muted)),
            Span::raw(format!("{}{tail}", shown.join(", "))),
        ]));
    };
    row(app.tr("Network", "网络"), &risk.hosts);
    row(app.tr("Files", "文件"), &risk.files);
    if !risk.impact.is_empty() {
        row(app.tr("Impact", "影响"), std::slice::from_ref(&risk.impact));
    }
    row(app.tr("Affects", "涉及"), &risk.systems);
    let why: Vec<String> = risk.reasons.iter().take(2).cloned().collect();
    row(app.tr("Why", "原因"), &why);
    lines
}

/// The model picker: a searchable list, one row per model under group
/// headings, with the highlighted model's details beneath it.
pub(super) fn draw_picker(frame: &mut Frame, app: &App, dialog: &Dialog, area: Rect, p: Palette) {
    let visible = dialog.visible();
    // Rows to draw: (group heading, or the choice index).
    let mut lines_spec: Vec<(Option<&str>, usize)> = Vec::new();
    let mut last_group: Option<&str> = None;
    for &i in &visible {
        if let Some(group) = dialog.groups.get(i).map(String::as_str) {
            if last_group != Some(group) && !group.is_empty() {
                lines_spec.push((Some(group), i));
                last_group = Some(group);
            }
        }
        lines_spec.push((None, i));
    }
    let list_rows = lines_spec.len().clamp(1, 20);
    let height = (list_rows + 6 + 2).min(usize::from(area.height)) as u16;
    let rect = popup(area, 100, height);
    frame.render_widget(Clear, rect);
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .border_style(Style::new().fg(p.accent))
        .title(Line::styled(
            app.tr(" Choose ", " 选择 "),
            Style::new().fg(p.accent),
        ))
        .title_bottom(Line::styled(
            app.tr(
                " ↑↓ choose · Enter select · type to search · Esc close ",
                " ↑↓ 选择 · Enter 确认 · 输入即搜索 · Esc 关闭 ",
            ),
            Style::new().fg(p.muted),
        ));
    let inside = block.inner(rect).inner(Margin::new(1, 0));
    frame.render_widget(block, rect);
    let width = usize::from(inside.width);
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(3),
    ])
    .split(inside);
    frame.render_widget(
        Paragraph::new(Line::styled(
            fit(dialog.title.lines().next().unwrap_or(""), width),
            Style::new().bold(),
        )),
        rows[0],
    );
    // Search box, with how many models match.
    let count = format!("{}/{}", visible.len(), dialog.choices.len());
    let label = app.tr("Search: ", "搜索：");
    let typed = if dialog.filter.is_empty() {
        Span::styled(
            app.tr("type a name, size or provider", "输入名称、规格或服务商"),
            Style::new().fg(p.muted),
        )
    } else {
        Span::raw(dialog.filter.clone())
    };
    let pad = width.saturating_sub(label.width() + typed.width() + count.width());
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(label, Style::new().fg(p.accent)),
            typed,
            Span::raw(" ".repeat(pad)),
            Span::styled(count, Style::new().fg(p.muted)),
        ])),
        rows[1],
    );
    frame.set_cursor_position((
        rows[1].x + (label.width() + dialog.filter.width()).min(width.saturating_sub(1)) as u16,
        rows[1].y,
    ));
    // The list, scrolled so the highlighted row stays in view.
    let list = rows[2];
    let room = usize::from(list.height);
    let at = lines_spec
        .iter()
        .position(|(group, i)| group.is_none() && *i == dialog.selected)
        .unwrap_or(0);
    let start = at
        .saturating_sub(room / 2)
        .min(lines_spec.len().saturating_sub(room));
    let name_width = visible
        .iter()
        .map(|&i| dialog.choices[i].0.width())
        .max()
        .unwrap_or(0)
        .min(36);
    let mut drawn: Vec<Line> = Vec::new();
    for (group, i) in lines_spec.iter().skip(start).take(room) {
        if let Some(group) = group {
            drawn.push(Line::styled(
                format!(" {}", fit(group, width.saturating_sub(1))),
                Style::new().fg(p.muted).bold(),
            ));
            continue;
        }
        let (name, help) = &dialog.choices[*i];
        let name = fit(name, name_width);
        let detail = fit(help, width.saturating_sub(name_width + 6));
        if *i == dialog.selected {
            drawn.push(Line::styled(
                format!(" › {name:<name_width$}  {detail}"),
                Style::new().fg(p.selection_fg).bg(p.accent),
            ));
        } else {
            drawn.push(Line::from(vec![
                Span::raw(format!("   {name:<name_width$}")),
                Span::styled(format!("  {detail}"), Style::new().fg(p.muted)),
            ]));
        }
    }
    if visible.is_empty() {
        drawn.push(Line::styled(
            format!(
                " {}",
                app.tr("No model matches that search.", "没有匹配的模型。")
            ),
            Style::new().fg(p.muted),
        ));
    }
    frame.render_widget(Paragraph::new(drawn), list);
    // The full line for the highlighted model, which the row may have cut.
    if let Some((name, help)) = dialog
        .choices
        .get(dialog.selected)
        .filter(|_| !visible.is_empty())
    {
        frame.render_widget(
            Paragraph::new(vec![
                Line::styled(fit(name, width), Style::new().fg(p.accent)),
                Line::styled(help.clone(), Style::new().fg(p.muted)),
            ])
            .wrap(Wrap { trim: true }),
            rows[3],
        );
    }
}
