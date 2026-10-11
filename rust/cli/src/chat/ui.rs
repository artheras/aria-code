use super::app::App;
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
    Frame,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

const ACCENT: Color = Color::Rgb(225, 164, 103);
const MUTED: Color = Color::DarkGray;

fn workspace_tail(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_owned();
    }
    if width == 0 {
        return String::new();
    }
    let mut kept = Vec::new();
    let mut used = 1;
    for grapheme in text.graphemes(true).rev() {
        if used + grapheme.width() > width {
            break;
        }
        used += grapheme.width();
        kept.push(grapheme);
    }
    format!("…{}", kept.into_iter().rev().collect::<String>())
}
const HELP: &str = "Rust interactive interface\n\nEnter  send · Alt+Enter / Ctrl+J  newline\nArrow keys  edit / browse history · Tab  complete /commands\nCtrl+U / K / W  delete line / end / previous word\nPgUp / PgDn or mouse wheel  scroll transcript\nCtrl+O  toggle tool details · F1  this help\nEsc or Ctrl+C  cancel current task / dismiss dialog\nCtrl+D  exit (or delete character when input is non-empty)\n\n/new  start a new session · /resume ID  restore a session\n/sessions  list saved sessions · /help  all Aria commands\n/model  choose your model · /health  check configured services\n@file:path  reference a file · !command  run a shell command\n\nFile permissions, task isolation, checks and model routing are managed by Aria.\nApproval menus require your choice. Hidden reasoning and secret inputs\nare never written to the native transcript.";

fn markdown(text: &str) -> Vec<Line<'static>> {
    let mut code = false;
    text.lines()
        .map(|line| {
            let style = if line.starts_with("```") {
                code = !code;
                Style::new().fg(MUTED)
            } else if code {
                Style::new().fg(Color::Cyan)
            } else if line.starts_with('#') {
                Style::new().fg(ACCENT).add_modifier(Modifier::BOLD)
            } else if line.starts_with("+ ") {
                Style::new().fg(Color::Green)
            } else if line.starts_with("- ") {
                Style::new().fg(Color::Red)
            } else {
                Style::default()
            };
            Line::styled(line.to_owned(), style)
        })
        .collect()
}
fn popup(area: Rect, width: u16, height: u16) -> Rect {
    let w = width.min(area.width);
    let h = height.min(area.height);
    Rect::new(
        area.x + (area.width - w) / 2,
        area.y + (area.height - h) / 2,
        w,
        h,
    )
}

pub fn draw(frame: &mut Frame, app: &App) {
    let area = frame.area();
    if area.width < 12 || area.height < 6 {
        frame.render_widget(Paragraph::new("Resize terminal"), area);
        return;
    }
    let (input_rows, cursor) = app.editor.layout(area.width.saturating_sub(4));
    let input_height = (input_rows.len().min(6) as u16)
        .saturating_add(2)
        .min(area.height / 2);
    let parts = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(if area.height >= 14 { 5 } else { 1 }),
            Constraint::Min(1),
            Constraint::Length(input_height),
            Constraint::Length(1),
        ])
        .split(area);
    let runtime = format!(
        "{} · network {} · ",
        app.permission,
        if app.network { "on" } else { "off" }
    );
    let header = if parts[0].height > 1 {
        Text::from(vec![
            Line::from(vec![
                Span::styled("Aria Code", Style::new().fg(ACCENT).bold()),
                Span::raw(format!(
                    " v{} · Rust UI {}",
                    app.version,
                    env!("CARGO_PKG_VERSION")
                )),
            ]),
            Line::raw(format!(
                "{} / {} · session {}",
                app.provider, app.model, app.session
            )),
            Line::raw(workspace_tail(
                &app.workspace,
                usize::from(area.width).saturating_sub(11),
            )),
            Line::styled(
                runtime.trim_end_matches(" · ").to_owned(),
                Style::new().fg(MUTED),
            ),
        ])
    } else {
        Text::from(Line::styled(
            format!("Aria · {} · {}", app.model, app.session),
            Style::new().fg(ACCENT),
        ))
    };
    let mut header_area = parts[0];
    if !app.robot.0.is_empty() && header_area.height >= 4 && header_area.width >= 45 {
        frame.render_widget(
            Paragraph::new(app.robot.0.clone()),
            Rect::new(header_area.x, header_area.y, 9, 4),
        );
        header_area.x += 11;
        header_area.width -= 11;
    }
    frame.render_widget(Paragraph::new(header), header_area);
    let mut lines = Vec::new();
    for entry in &app.entries {
        if entry.text.is_empty() || (entry.detail && !app.details) {
            continue;
        }
        let (label, color) = match entry.role.as_str() {
            "user" => ("You", ACCENT),
            "assistant" => ("Aria", Color::Cyan),
            "error" => ("Error", Color::Red),
            "action" => ("Action", Color::Green),
            _ => ("", MUTED),
        };
        if !label.is_empty() {
            lines.push(Line::styled(
                label,
                Style::new().fg(color).add_modifier(Modifier::BOLD),
            ));
        }
        lines.extend(markdown(&entry.text));
        lines.push(Line::raw(""));
    }
    if lines.is_empty() {
        lines = vec![
            Line::raw("Describe a task, edit your project, or type /help."),
            Line::raw(""),
            Line::styled(
                if app.ready {
                    "Enter a prompt to start."
                } else {
                    "The runtime is starting…"
                },
                Style::new().fg(MUTED),
            ),
        ];
    }
    let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
    let total = paragraph.line_count(parts[1].width);
    let bottom = total.saturating_sub(parts[1].height as usize);
    let offset = bottom.saturating_sub(app.scroll).min(u16::MAX as usize) as u16;
    frame.render_widget(paragraph.scroll((offset, 0)), parts[1]);
    let input = Block::default()
        .borders(Borders::ALL)
        .title(if app.turn.is_some() {
            " Draft · Esc cancels task "
        } else {
            " Ask Aria · Enter sends "
        })
        .border_style(Style::new().fg(if app.ready { ACCENT } else { MUTED }));
    let inside = input.inner(parts[2]);
    frame.render_widget(input, parts[2]);
    let first = usize::from(cursor.0).saturating_sub(inside.height.saturating_sub(1) as usize);
    frame.render_widget(Paragraph::new(input_rows[first..].join("\n")), inside);
    frame.set_cursor_position((
        inside.x + cursor.1.min(inside.width.saturating_sub(1)),
        inside.y
            + cursor
                .0
                .saturating_sub(first as u16)
                .min(inside.height.saturating_sub(1)),
    ));
    frame.render_widget(
        Paragraph::new(format!("{} · F1 help · Ctrl+O details", app.status))
            .style(Style::new().fg(MUTED)),
        parts[3],
    );
    if app.help {
        let rect = popup(area, 88, 24);
        frame.render_widget(Clear, rect);
        frame.render_widget(
            Paragraph::new(HELP)
                .wrap(Wrap { trim: false })
                .block(Block::bordered().title(" Help · Esc closes ")),
            rect,
        );
    }
    if let Some(dialog) = &app.dialog {
        let mut heading = vec![
            Line::styled(dialog.title.clone(), Style::new().bold()),
            Line::raw(""),
        ];
        if !dialog.context.is_empty() {
            heading.extend(
                dialog
                    .context
                    .lines()
                    .map(|line| Line::styled(line.to_owned(), Style::new().fg(Color::Cyan))),
            );
            heading.push(Line::raw(""));
        }
        let heading = Paragraph::new(heading).wrap(Wrap { trim: false });
        let heading_lines = heading.line_count(area.width.min(90).saturating_sub(2));
        let height = (heading_lines
            + if dialog.choices.is_empty() {
                6
            } else {
                dialog.choices.len() * 3 + 3
            })
        .min(25) as u16;
        let rect = popup(area, 90, height);
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(" Your response required · Esc declines ")
            .border_style(Style::new().fg(ACCENT));
        let inside = block.inner(rect);
        frame.render_widget(block, rect);
        // Keep the operation visible while scrolling through approval choices.
        let sections = Layout::vertical([
            Constraint::Length((heading_lines.min(8) as u16).min(inside.height.saturating_sub(3))),
            Constraint::Min(1),
        ])
        .split(inside);
        frame.render_widget(heading, sections[0]);
        let inside = sections[1];
        let mut scroll = 0;
        let mut rows = Vec::new();
        if dialog.choices.is_empty() {
            let (text, cursor) = dialog.editor.layout_text(inside.width, dialog.secret);
            let start = rows.len() as u16;
            let cursor_row = start.saturating_add(cursor.0);
            scroll = cursor_row.saturating_sub(inside.height.saturating_sub(1));
            rows.extend(text.into_iter().map(Line::raw));
            frame.set_cursor_position((
                inside.x + cursor.1.min(inside.width.saturating_sub(1)),
                inside.y
                    + cursor_row
                        .saturating_sub(scroll)
                        .min(inside.height.saturating_sub(1)),
            ));
        } else {
            for (i, (label, help)) in dialog.choices.iter().enumerate() {
                if i == dialog.selected {
                    let before = Paragraph::new(rows.clone()).wrap(Wrap { trim: false });
                    scroll = (before.line_count(inside.width) as u16)
                        .saturating_sub(inside.height.saturating_sub(2));
                }
                let style = if i == dialog.selected {
                    Style::new().fg(Color::Black).bg(ACCENT)
                } else {
                    Style::default()
                };
                rows.push(Line::styled(
                    format!(
                        "{} {}. {}",
                        if i == dialog.selected { "›" } else { " " },
                        i + 1,
                        label
                    ),
                    style,
                ));
                if !help.is_empty() {
                    rows.push(Line::styled(format!("     {help}"), Style::new().fg(MUTED)));
                }
            }
            rows.push(Line::raw(""));
            rows.push(Line::styled(
                "↑/↓ selects · Enter confirms · number or shortcut chooses",
                Style::new().fg(MUTED),
            ));
        }
        frame.render_widget(
            Paragraph::new(rows)
                .wrap(Wrap { trim: false })
                .scroll((scroll, 0)),
            inside,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{backend::TestBackend, Terminal};
    fn display(terminal: &Terminal<TestBackend>) -> String {
        let buffer = terminal.backend().buffer();
        (0..buffer.area.height)
            .map(|y| {
                let mut row = String::new();
                let mut x = 0;
                while x < buffer.area.width {
                    let symbol = buffer[(x, y)].symbol();
                    row.push_str(symbol);
                    x += symbol.width().max(1) as u16;
                }
                row
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
    #[test]
    fn long_workspaces_leave_policy_and_project_name_visible() {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let app = App {
            permission: "workspace-write".into(),
            network: true,
            workspace: format!("/Users/项目/{}/我的项目", "很长的目录/".repeat(20)),
            ..App::default()
        };
        terminal.draw(|f| draw(f, &app)).unwrap();
        let rendered = display(&terminal);
        let header = rendered.lines().nth(2).unwrap();
        assert!(rendered
            .lines()
            .nth(3)
            .unwrap()
            .contains("workspace-write · network on"));
        assert!(header.contains('…'));
        assert!(header.contains("我的项目"));
        assert!(header.width() <= 80);
        assert_eq!(workspace_tail("a/👩‍💻/界", 6), "…👩‍💻/界");
        assert_eq!(workspace_tail("界", 0), "");
    }
    #[test]
    fn approval_target_stays_visible_when_choices_scroll() {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut app = App {
            ready: true,
            turn: Some("turn-1".into()),
            ..App::default()
        };
        app.event(
            serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1",
            "tool":"shell","params":{"command":"git status","directory":"/project/review"}}),
        )
        .unwrap();
        let choices: Vec<_> = (0..12)
            .map(|i| vec![format!("Choice {i}"), "Help text".into()])
            .collect();
        app.event(
            serde_json::json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"approval","title":"Approval","choices":choices,"selected":11}),
        )
        .unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let rendered = display(&terminal);
        assert!(rendered.contains("Command: git status"));
        assert!(rendered.contains("Directory: /project/review"));
        assert!(rendered.contains("12. Choice 11"));
        assert!(!app.details);
    }
    #[test]
    fn renders_resize_unicode_code_and_secrets() {
        for (w, h) in [(1, 1), (12, 6), (40, 12), (80, 24), (120, 40)] {
            let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
            let mut app = App {
                model: "Google Cloud".into(),
                status: "Ready".into(),
                ..App::default()
            };
            app.add("assistant", "你好\n```rust\nfn main() {}\n```", false);
            app.editor.insert("👩‍💻你好");
            terminal.draw(|f| draw(f, &app)).unwrap();
            if w >= 80 {
                assert!(format!("{:?}", terminal.backend().buffer()).contains("fn main()"));
            }
            app.help = true;
            terminal.draw(|f| draw(f, &app)).unwrap();
            app.help = false;
            app.dialog = Some(super::super::app::Dialog {
                id: "id".into(),
                title: "API key".into(),
                context: String::new(),
                choices: vec![],
                selected: 0,
                secret: true,
                editor: super::super::editor::Editor::default(),
                shortcuts: serde_json::Value::Null,
            });
            app.dialog.as_mut().unwrap().editor.insert("DO_NOT_DISPLAY");
            terminal.draw(|f| draw(f, &app)).unwrap();
            assert!(!format!("{:?}", terminal.backend().buffer()).contains("DO_NOT_DISPLAY"));
        }
    }
}
