use super::app::App;
use super::{hang, mark, markdown, theme::Palette};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, BorderType, Clear, Padding, Paragraph, Wrap},
    Frame,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

fn operation_target<'a>(target: &'a str, workspace: &str) -> std::borrow::Cow<'a, str> {
    let path = std::path::Path::new(target);
    if path.is_absolute() && !workspace.is_empty() {
        if let Ok(relative) = path.strip_prefix(workspace) {
            if relative.as_os_str().is_empty() {
                return ".".into();
            }
            return relative.to_string_lossy().into_owned().into();
        }
    }
    target.into()
}

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
const HELP: &str = "Aria interactive interface\n\nEnter  send · Alt+Enter / Ctrl+J  newline\nArrow keys  edit / browse history · Tab  complete /commands\nCtrl+U / K / W  delete line / end / previous word\nPgUp / PgDn or mouse wheel  scroll transcript\nCtrl+O  toggle tool details · F1  this help\nEsc or Ctrl+C  cancel current task / dismiss dialog\nCtrl+D  exit (or delete character when input is non-empty)\n\n/new  start a new session · /resume ID  restore a session\n/sessions  list saved sessions · /help  all Aria commands\n/model  choose your model · /health  check configured services\n@file:path  reference a file · !command  run a shell command\n\nFile permissions, task isolation, checks and model routing are managed by Aria.\nApproval menus require your choice. Hidden reasoning and secret inputs\nare never written to the native transcript.";

const HELP_ZH: &str = "Aria 交互界面\n\nEnter 发送 · Alt+Enter / Ctrl+J 换行\n方向键编辑 / 浏览输入历史 · Tab 补全命令\nCtrl+U / K / W 删除行首 / 行尾 / 前一个词\nPgUp / PgDn 或鼠标滚轮查看对话历史\nCtrl+O 展开工具详情 · F1 打开帮助\nEsc / Ctrl+C 取消当前任务或关闭对话框\nCtrl+D 退出（输入非空时删除字符）\n\n/new 新会话 · /resume ID 恢复会话\n/sessions 会话列表 · /help 所有命令\n/model 选择模型 · /health 服务检查\n@file:path 引用文件 · !command 执行命令\naria -C <项目目录> 在指定项目中启动\n/config set ui_lang=zh 设置中文界面\n\n运行时管理文件权限、任务隔离与验证。\n审批需要你明确选择；秘密输入与隐藏推理不会写入界面记录。";

fn tool_label<'a>(tool: &'a str, app: &App) -> &'a str {
    match tool {
        "list_files" | "glob" => app.tr("List files", "查看目录"),
        "read_file" => app.tr("Read file", "读取文件"),
        "search_code" | "find_symbol" => app.tr("Search code", "搜索代码"),
        "run_command" => app.tr("Run command", "执行命令"),
        "write_file" => app.tr("Write file", "写入文件"),
        "edit_file" | "multi_edit" | "apply_patch" => app.tr("Edit file", "修改文件"),
        "repo_map" => app.tr("Map project", "查看项目结构"),
        _ => tool,
    }
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
    let p = Palette::for_theme(&app.theme);
    if area.width < 12 || area.height < 6 {
        frame.render_widget(
            Paragraph::new(app.tr("Resize terminal", "请扩大终端窗口")),
            area,
        );
        return;
    }
    // Two borders, two padding columns, and the two-column prompt gutter.
    // Wrapping and cursor placement must use the same width as the text area.
    let (input_rows, cursor) = app.editor.layout(area.width.saturating_sub(6));
    let robot = &app.robot;
    let robot_width = robot.0.first().map(|line| line.width()).unwrap_or(0) as u16;
    let show_robot = !robot.0.is_empty() && area.width >= robot_width + 36 && area.height >= 14;
    let left_padding = if show_robot { robot_width + 2 } else { 0 };
    let input_height = (input_rows.len().min(6) as u16)
        .saturating_add(2)
        .min(area.height / 2);
    let parts = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(if area.height >= 14 {
                (4 + u16::from(!app.update_notice.is_empty()) + u16::from(app.details))
                    .max(if show_robot { robot.0.len() as u16 } else { 0 })
            } else {
                1
            }),
            Constraint::Min(1),
            Constraint::Length(input_height),
            Constraint::Length(1),
        ])
        .split(area);
    let runtime = format!(
        "{} · {} {}",
        app.permission,
        app.tr("network", "网络"),
        if app.network {
            app.tr("on", "开启")
        } else {
            app.tr("off", "关闭")
        }
    );
    let header = if parts[0].height > 1 {
        let mut lines = vec![
            Line::from(vec![
                Span::styled("Aria Code", Style::new().fg(p.accent).bold()),
                Span::raw(format!(" v{}", app.version)),
            ]),
            Line::raw(format!("{} / {}", app.provider, app.model)),
            Line::raw(format!(
                "{} {}",
                app.tr("Workspace", "工作区"),
                workspace_tail(
                    &app.workspace,
                    usize::from(area.width).saturating_sub(
                        usize::from(left_padding) + app.tr("Workspace", "工作区").width() + 1
                    ),
                )
            )),
            Line::styled(runtime.to_owned(), Style::new().fg(p.muted)),
        ];
        if !app.update_notice.is_empty() {
            lines.push(Line::styled(
                app.update_notice.clone(),
                Style::new().fg(p.accent),
            ));
        }
        if app.details {
            lines.push(Line::styled(
                format!(
                    "Rust UI {} · {} {}",
                    env!("CARGO_PKG_VERSION"),
                    app.tr("session", "会话"),
                    app.session
                ),
                Style::new().fg(p.muted),
            ));
        }
        Text::from(lines)
    } else {
        Text::from(Line::styled(
            format!("Aria · {}", app.model),
            Style::new().fg(p.accent),
        ))
    };
    let mut header_area = parts[0];
    if show_robot {
        frame.render_widget(
            Paragraph::new(robot.0.clone()),
            Rect::new(
                header_area.x,
                header_area.y,
                robot_width,
                robot.0.len() as u16,
            ),
        );
        header_area.x += left_padding;
        header_area.width -= left_padding;
    }
    frame.render_widget(Paragraph::new(header), header_area);
    let mut lines = Vec::new();
    for entry in &app.entries {
        if entry.text.trim().is_empty() || (entry.detail && !app.details) {
            continue;
        }
        if let Some(action) = &entry.action {
            let label = tool_label(&action.tool, app);
            let symbol = if !action.done {
                "•"
            } else if action.cancelled {
                "–"
            } else if action.success {
                "✓"
            } else {
                "✗"
            };
            let color = if !action.done || action.cancelled {
                p.muted
            } else if action.success {
                p.success
            } else {
                p.error
            };
            let count = action
                .count
                .map(|n| format!(" · {n} {}", app.tr("items", "项")))
                .unwrap_or_default();
            let reused = if action.cancelled {
                app.tr(" · cancelled", " · 已取消")
            } else if action.staged {
                app.tr(" · staged only", " · 仅暂存")
            } else if action.reused {
                app.tr(" · reused", " · 复用结果")
            } else {
                ""
            };
            let exit_code = action
                .exit_code
                .map(|code| format!(" · {} {code}", app.tr("exit", "退出码")))
                .unwrap_or_default();
            let target = workspace_tail(
                &operation_target(&action.target, &app.workspace),
                usize::from(parts[1].width).saturating_sub(
                    label.width() + count.width() + exit_code.width() + reused.width() + 4,
                ),
            );
            lines.push(Line::from(vec![
                Span::styled(format!("{symbol} {label}"), Style::new().fg(color)),
                Span::raw(format!(" {target}{count}{exit_code}{reused}")),
            ]));
            if !action.approval.is_empty() {
                lines.push(Line::styled(
                    format!("  └ {}", action.approval),
                    Style::new().fg(p.muted),
                ));
            }
            if !action.error.is_empty() {
                lines.push(Line::styled(
                    format!("  └ {}", action.error),
                    Style::new().fg(p.error),
                ));
            }
            if app.details {
                lines.push(Line::styled(
                    format!(
                        "  └ {}{}",
                        action.tool,
                        action
                            .elapsed
                            .map(|s| format!(" · {s:.2}s"))
                            .unwrap_or_default()
                    ),
                    Style::new().fg(p.muted),
                ));
                lines.extend(
                    entry
                        .text
                        .lines()
                        .map(|line| Line::styled(format!("    {line}"), Style::new().fg(p.muted))),
                );
            }
            continue;
        }
        // Space between messages, not between every operation in a batch.
        if !lines.is_empty() {
            lines.push(Line::default());
        }
        // Who spoke is carried by the marker at the head of the message, not
        // by a name on a line of its own: the robot's eye for Aria, the prompt
        // glyph for the user, a cross for an error — the way Codex and Claude
        // Code lay a conversation out. The rest of the message hangs beneath
        // the text (chat/hang.rs), wrapped lines included.
        let width = parts[1].width;
        let plain = |text: &str, style: Style| -> Vec<Line<'static>> {
            text.lines()
                .map(|s| Line::styled(s.to_owned(), style))
                .collect()
        };
        match entry.role.as_str() {
            "assistant" => lines.extend(hang::hang(
                markdown::render(&entry.text, p, width.saturating_sub(hang::INDENT)),
                mark::prefix(None),
                width,
            )),
            "user" => lines.extend(hang::hang(
                plain(&entry.text, Style::new()),
                vec![
                    Span::styled("›", Style::new().fg(p.accent).add_modifier(Modifier::BOLD)),
                    Span::raw(" "),
                ],
                width,
            )),
            "error" => lines.extend(hang::hang(
                plain(&entry.text, Style::new().fg(p.error)),
                vec![Span::styled("✗", Style::new().fg(p.error)), Span::raw(" ")],
                width,
            )),
            "delivery" => lines.extend(delivery_lines(&entry.text, p)),
            _ => lines.extend(plain(&entry.text, Style::new())),
        }
    }
    for waiting in &app.queued {
        if !lines.is_empty() {
            lines.push(Line::default());
        }
        let first = waiting.lines().next().unwrap_or("");
        let more = waiting.lines().count().saturating_sub(1);
        let extra = if more > 0 {
            format!(" (+{more})")
        } else {
            String::new()
        };
        lines.push(Line::styled(
            format!("› {first}{extra} · {}", app.tr("queued", "排队中")),
            Style::new().fg(p.muted),
        ));
    }
    if lines.is_empty() {
        lines = vec![
            Line::raw(app.tr(
                "Describe a task, edit your project, or type /help.",
                "描述任务、修改项目，或输入 /help。",
            )),
            Line::raw(""),
            Line::styled(
                if app.ready {
                    if app.home_workspace {
                        app.tr("Home directory · use aria -C <project> to select a project. Files are read only when needed for your task.", "当前是主目录。用 aria -C <项目目录> 选择项目；仅按任务需要读取文件。")
                    } else {
                        app.tr("Enter a prompt to start.", "输入消息开始。")
                    }
                } else {
                    app.tr("The runtime is starting…", "正在启动…")
                },
                Style::new().fg(p.muted),
            ),
        ];
    }
    let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
    let total = paragraph.line_count(parts[1].width);
    let bottom = total.saturating_sub(parts[1].height as usize);
    let offset = if app.scroll == 0 {
        app.viewport_top.set(None);
        bottom
    } else {
        let top = app
            .viewport_top
            .get()
            .map(|top| {
                top.saturating_add(app.viewport_scroll.get())
                    .saturating_sub(app.scroll)
            })
            .unwrap_or_else(|| bottom.saturating_sub(app.scroll))
            .min(bottom);
        app.viewport_top.set(Some(top));
        top
    }
    .min(u16::MAX as usize) as u16;
    app.viewport_scroll.set(app.scroll);
    frame.render_widget(paragraph.scroll((offset, 0)), parts[1]);
    let busy = app.turn.is_some();
    let label = if busy {
        app.tr(" Queue ", " 排队 ")
    } else {
        " Aria "
    };
    let hint = if !app.ready {
        app.tr(" Starting… ", " 正在启动… ")
    } else if busy {
        app.tr(
            " Enter queues · sent when this task ends ",
            " Enter 排队 · 任务结束后自动发送 ",
        )
    } else if area.width >= 60 {
        app.tr(
            " Enter send · Ctrl+J newline ",
            " Enter 发送 · Ctrl+J 换行 ",
        )
    } else {
        app.tr(" Enter send ", " Enter 发送 ")
    };
    let mut input = Block::bordered()
        .border_type(BorderType::Rounded)
        .padding(Padding::horizontal(1))
        .title(Line::styled(label, Style::new().fg(p.accent)))
        .border_style(Style::new().fg(p.muted));
    if label.width() + hint.width() + 6 <= usize::from(parts[2].width) {
        input = input.title(Line::styled(hint, Style::new().fg(p.muted)).right_aligned());
    }
    let mut inside = input.inner(parts[2]);
    frame.render_widget(input, parts[2]);
    frame.render_widget(
        Paragraph::new("›").style(Style::new().fg(if app.ready { p.accent } else { p.muted })),
        Rect::new(inside.x, inside.y, 1, inside.height.min(1)),
    );
    inside.x += 2;
    inside.width = inside.width.saturating_sub(2);
    let first = usize::from(cursor.0).saturating_sub(inside.height.saturating_sub(1) as usize);
    if app.editor.is_empty() {
        let placeholder = if busy {
            app.tr(
                "Type your next message — Enter queues it…",
                "可以先写下一条消息，Enter 排队…",
            )
        } else {
            app.tr(
                "Describe a task, or type / for commands",
                "描述任务，或输入 / 查看命令",
            )
        };
        frame.render_widget(
            Paragraph::new(placeholder).style(Style::new().fg(p.muted)),
            inside,
        );
    } else {
        frame.render_widget(Paragraph::new(input_rows[first..].join("\n")), inside);
    }
    frame.set_cursor_position((
        inside.x + cursor.1.min(inside.width.saturating_sub(1)),
        inside.y
            + cursor
                .0
                .saturating_sub(first as u16)
                .min(inside.height.saturating_sub(1)),
    ));
    let elapsed = app
        .started
        .map(|start| start.elapsed())
        .or(app.elapsed)
        .map(|elapsed| format!(" · {}s", elapsed.as_secs()))
        .unwrap_or_default();
    let cancel = if app.turn.is_some() {
        app.tr(" · Esc cancel", " · Esc 取消")
    } else {
        ""
    };
    let scroll = if app.scroll > 0 {
        app.tr(" · viewing history", " · 查看历史中")
    } else {
        ""
    };
    // While a turn runs the mascot's eye breathes at the head of the status
    // line — the one thing on screen that says the model is working, and the
    // brand once the welcome banner has scrolled away. The frame comes from
    // the seconds already on display rather than a timer of its own: the view
    // redraws once a second while a turn is open (see chat/mod.rs), so the
    // mark advances with it and the whole thing stays a pure function of
    // state.
    let mut status_line: Vec<Span> = Vec::new();
    if app.turn.is_some() {
        let second = app
            .started
            .map(|start| start.elapsed().as_secs())
            .unwrap_or(0) as usize;
        status_line.extend(super::mark::prefix(Some(second)));
    }
    status_line.push(Span::styled(
        format!(
            "{}{elapsed}{cancel}{scroll} · {}",
            app.status,
            app.tr("F1 help · Ctrl+O details", "F1 帮助 · Ctrl+O 详情")
        ),
        Style::new().fg(p.muted),
    ));
    frame.render_widget(Paragraph::new(Line::from(status_line)), parts[3]);
    if app.help {
        let rect = popup(area, 88, 24);
        frame.render_widget(Clear, rect);
        frame.render_widget(
            Paragraph::new(app.tr(HELP, HELP_ZH))
                .wrap(Wrap { trim: false })
                .block(Block::bordered().title(app.tr(" Help · Esc closes ", " 帮助 · Esc 关闭 "))),
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
                    .map(|line| Line::styled(line.to_owned(), Style::new().fg(p.assistant))),
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
            .title(app.tr(
                " Your response required · PgUp/PgDn review · Esc declines ",
                " 需要你的选择 · PgUp/PgDn 查看预览 · Esc 拒绝 ",
            ))
            .border_style(Style::new().fg(p.accent));
        let inside = block.inner(rect);
        frame.render_widget(block, rect);
        // Keep the operation visible while scrolling through approval choices.
        let sections = Layout::vertical([
            Constraint::Length((heading_lines.min(8) as u16).min(inside.height.saturating_sub(3))),
            Constraint::Min(1),
        ])
        .split(inside);
        let max_scroll = heading_lines
            .saturating_sub(usize::from(sections[0].height))
            .min(usize::from(u16::MAX)) as u16;
        dialog.context_max_scroll.set(max_scroll);
        frame.render_widget(
            heading.scroll((dialog.context_scroll.min(max_scroll), 0)),
            sections[0],
        );
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
                    Style::new().fg(p.selection_fg).bg(p.accent)
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
                    rows.push(Line::styled(
                        format!("     {help}"),
                        Style::new().fg(p.muted),
                    ));
                }
            }
            rows.push(Line::raw(""));
            rows.push(Line::styled(
                "↑/↓ selects · Enter confirms · number or shortcut chooses",
                Style::new().fg(p.muted),
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

/// The delivery report, coloured as the Python REPL prints it: the status by
/// outcome, headings dim, ✓ green, ✗ red, × and ⚠ highlighted.
fn delivery_lines(text: &str, p: Palette) -> Vec<Line<'static>> {
    const HEADINGS: [&str; 8] = [
        "Changed",
        "Verified",
        "Behaviour",
        "Review",
        "Risk",
        "Contract",
        "Checkpoint",
        "Next",
    ];
    text.lines()
        .enumerate()
        .map(|(index, line)| {
            let trimmed = line.trim();
            let style = if index == 0 {
                Style::new()
                    .fg(if trimmed == "DONE" {
                        p.success
                    } else {
                        p.accent
                    })
                    .add_modifier(Modifier::BOLD)
            } else if HEADINGS.contains(&trimmed) {
                Style::new().fg(p.muted)
            } else if trimmed.starts_with('✓') {
                Style::new().fg(p.success)
            } else if trimmed.starts_with('✗') {
                Style::new().fg(p.error)
            } else if trimmed.starts_with('×') || trimmed.starts_with('⚠') {
                Style::new().fg(p.accent)
            } else {
                Style::new()
            };
            Line::styled(line.to_owned(), style)
        })
        .collect()
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
    fn operation_targets_are_relative_only_inside_the_selected_workspace() {
        // Use native absolute paths, including a drive prefix on Windows.
        let workspace = std::env::temp_dir().join("aria-target-project");
        let file = workspace.join("src").join("app.rs");
        let outside = std::env::temp_dir()
            .join("aria-target-project-other")
            .join("app.rs");
        let workspace = workspace.to_string_lossy();
        assert_eq!(
            operation_target(&file.to_string_lossy(), &workspace),
            std::path::Path::new("src").join("app.rs").to_string_lossy()
        );
        assert_eq!(operation_target(&workspace, &workspace), ".");
        assert_eq!(
            operation_target(&outside.to_string_lossy(), &workspace),
            outside.to_string_lossy()
        );
        assert_eq!(
            operation_target("cargo test --all", &workspace),
            "cargo test --all"
        );
    }
    #[test]
    fn chinese_actions_are_compact_complete_and_precede_the_answer() {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut app = App {
            ready: true,
            lang: "zh".into(),
            theme: "light".into(),
            ..App::default()
        };
        app.editor.insert("查看项目目录");
        app.submit();
        app.event(serde_json::json!({"protocol":1,"type":"output.delta","turn_id":"turn-1","text":"\n\n\n"})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"list_files","params":{"path":"/project"}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"list_files","success":true,"item_count":3})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"answer.replace","turn_id":"turn-1","text":"**目录已读取**，可以开始。"})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"turn.completed","turn_id":"turn-1","status":"ok"})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let text = display(&terminal);
        assert!(text.contains("✓ 查看目录 /project · 3 项"));
        assert!(text.find("✓ 查看目录").unwrap() < text.find("目录已读取").unwrap());
        assert!(!text.contains("Action") && !text.contains("**") && !text.contains("Rust UI"));
        assert!(text.contains("已完成") && text.contains("Enter 发送"));
        let user_line = text
            .lines()
            .position(|line| line.contains("查看项目目录"))
            .unwrap();
        let action_line = text
            .lines()
            .position(|line| line.contains("✓ 查看目录"))
            .unwrap();
        assert_eq!(
            action_line,
            user_line + 1,
            "legacy blank output must not create gaps"
        );
    }
    #[test]
    fn staged_write_is_visible_as_prepared_instead_of_an_applied_file() {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut app = App {
            ready: true,
            lang: "zh".into(),
            ..App::default()
        };
        app.editor.insert("Prepare a change for review");
        app.submit();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"write_file","params":{"path":"note.txt"}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"write_file","success":true,"staged":true,"output":"Staged change review-id; the file has not been modified."})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(display(&terminal).contains("仅暂存"));
        app.lang = "en".into();
        app.details = true;
        terminal.draw(|f| draw(f, &app)).unwrap();
        let text = display(&terminal);
        assert!(text.contains("staged only") && text.contains("review-id"));
    }
    #[test]
    fn reading_history_stays_anchored_when_new_output_arrives() {
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut app = App::default();
        app.add(
            "output",
            &(0..50)
                .map(|i| format!("History row {i}\n"))
                .collect::<String>(),
            false,
        );
        app.scroll = 20;
        terminal.draw(|f| draw(f, &app)).unwrap();
        let before = display(&terminal).lines().nth(4).unwrap().to_owned();
        app.add("output", "New output\n".repeat(10).as_str(), false);
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert_eq!(display(&terminal).lines().nth(4).unwrap(), before);
        app.scroll = 0;
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(display(&terminal).contains("New output"));
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
    fn long_change_preview_can_be_read_without_hiding_the_choices() {
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        let mut app = App {
            ready: true,
            turn: Some("turn-1".into()),
            ..App::default()
        };
        let context = (0..16)
            .map(|n| format!("change line {n}\n"))
            .collect::<String>();
        app.event(
            serde_json::json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1",
            "request_id":"approval","title":"Review changes","context":context,
            "choices":[["Allow once",""],["Decline",""]]}),
        )
        .unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(display(&terminal).contains("change line 0"));
        let max = app.dialog.as_ref().unwrap().context_max_scroll.get();
        assert!(max > 0);
        app.dialog.as_mut().unwrap().context_scroll = max;
        terminal.draw(|f| draw(f, &app)).unwrap();
        let rendered = display(&terminal);
        assert!(rendered.contains("change line 15"));
        assert!(rendered.contains("Allow once"));
        assert!(rendered.contains("Decline"));
    }
    #[test]
    fn background_update_notice_is_visible_without_replacing_the_robot() {
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        let app = App {
            ready: true,
            update_notice: "Update available · v0.138.0 · aria update".into(),
            ..App::default()
        };
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(display(&terminal).contains("Update available · v0.138.0 · aria update"));
    }
    #[test]
    fn composer_has_a_visible_prompt_and_fits_cursor_after_wide_text() {
        use ratatui::backend::Backend;
        for (width, height) in [(12, 6), (40, 12), (80, 24), (120, 40)] {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            let mut app = App {
                ready: true,
                ..App::default()
            };
            terminal.draw(|f| draw(f, &app)).unwrap();
            let empty_cursor = terminal.backend_mut().get_cursor_position().unwrap();
            assert_eq!(empty_cursor.x, 4);
            assert_eq!(empty_cursor.y, height - 3);
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(2, height - 3)].symbol(), "›");
            assert_eq!(buffer[(0, height - 2)].symbol(), "╰");
            app.editor.insert("你好👩‍💻");
            terminal.draw(|f| draw(f, &app)).unwrap();
            let cursor = terminal.backend_mut().get_cursor_position().unwrap();
            assert!(cursor.x >= 4 && cursor.x < width - 2);
            assert!(cursor.y < height - 2);
            if width > 12 {
                assert_eq!(cursor.x, 10);
            }
            // Filling the content exactly creates a new row, not a cursor on
            // the right border. A long paste scrolls only the editable area.
            app.editor.set(&"x".repeat(usize::from(width - 6)));
            terminal.draw(|f| draw(f, &app)).unwrap();
            assert_eq!(terminal.backend_mut().get_cursor_position().unwrap().x, 4);
            app.editor
                .set(&(0..20).map(|i| format!("line{i}\n")).collect::<String>());
            terminal.draw(|f| draw(f, &app)).unwrap();
            assert!(display(&terminal).contains("line19") || height == 6);
            assert_eq!(
                terminal.backend_mut().get_cursor_position().unwrap().y,
                height - 3
            );
            app.editor.clear();
            terminal.draw(|f| draw(f, &app)).unwrap();
            assert_eq!(
                terminal.backend_mut().get_cursor_position().unwrap(),
                empty_cursor
            );
        }
    }
    #[test]
    fn busy_composer_says_enter_queues_and_the_message_waits_in_view() {
        // Enter while a turn runs used to do nothing and the box was labelled
        // a draft to be sent by hand once the task ended. Codex and Claude
        // Code queue it and send it when the turn completes; so does this,
        // and the label says so.
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        let mut app = App {
            ready: true,
            lang: "zh".into(),
            turn: Some("active".into()),
            ..App::default()
        };
        terminal.draw(|f| draw(f, &app)).unwrap();
        let rendered = display(&terminal);
        assert!(rendered.contains("排队") && rendered.contains("任务结束后自动发送"));
        assert!(!rendered.contains("手动发送"));

        app.editor.insert("下一条消息");
        assert!(
            app.submit().is_none(),
            "nothing is sent while the turn runs"
        );
        assert!(
            app.editor.is_empty(),
            "the box is free for the message after"
        );
        assert_eq!(app.queued.len(), 1);
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(
            display(&terminal).contains("排队中"),
            "a waiting message stays visible"
        );
    }
    #[test]
    fn a_conversation_is_led_by_markers_not_by_name_lines() {
        // The transcript used to print "你" and "Aria" on lines of their own.
        // Codex and Claude Code carry who spoke in a marker at the head of
        // the message instead, and hang the rest beneath the text.
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.add("user", "你好", false);
        app.add(
            "assistant",
            "你好，我是 Aria Code。这一行足够长，会在窄终端里折行，折行后的部分要留在正文下面。",
            false,
        );
        app.add("error", "No model is reachable.", false);
        let mut terminal = Terminal::new(TestBackend::new(40, 30)).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let rows: Vec<String> = (0..30)
            .map(|y| {
                (0..40)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect();

        assert!(
            !rows
                .iter()
                .any(|r| r.trim() == "你" || r.trim() == "Aria" || r.trim() == "错误"),
            "a name on a line of its own: {rows:#?}"
        );
        // A wide character fills two cells and reads back with a blank after
        // it, so "你好" is matched by its first character.
        let user = rows
            .iter()
            .position(|r| r.starts_with("› 你"))
            .expect("user line");
        let aria = rows
            .iter()
            .position(|r| r.starts_with(super::super::mark::RESTING))
            .expect("aria line");
        assert!(aria > user);
        assert!(
            rows[aria + 1].starts_with("  ") && !rows[aria + 1].trim().is_empty(),
            "a wrapped line must hang under the text: {:?}",
            rows[aria + 1]
        );
        assert!(rows
            .iter()
            .any(|r| r.starts_with("✗ No model is reachable.")));
    }

    #[test]
    fn a_waiting_message_sits_under_the_conversation_in_the_users_own_marker() {
        // The queue and the marker layout were written separately. Together: a
        // message waiting to be sent reads as the user's own line (›), after
        // everything already said, and says it is still waiting.
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.add("user", "第一个问题", false);
        app.add("assistant", "正在处理。", false);
        app.turn = Some("turn-1".into());
        app.editor.insert("第二个问题");
        app.submit();
        let mut terminal = Terminal::new(TestBackend::new(60, 24)).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let rows: Vec<String> = (0..24)
            .map(|y| {
                (0..60)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect();
        // A wide character fills two cells and reads back with a blank after
        // it, so the answer is found by its marker, not by its text.
        let said = rows
            .iter()
            .position(|r| r.starts_with(super::super::mark::RESTING))
            .expect("answer");
        let waiting = rows
            .iter()
            .position(|r| r.contains("queued"))
            .expect("queued line");
        assert!(
            waiting > said,
            "the waiting message comes after what was already said"
        );
        assert!(rows[waiting].starts_with("› "), "{:?}", rows[waiting]);
    }

    #[test]
    fn the_status_line_shows_the_mascots_eye_only_while_a_turn_runs() {
        // The chat had no activity indicator at all: nothing on screen said
        // the model was working. The mark is that, and it is the brand once
        // the welcome banner has scrolled away — so it must appear while a
        // turn is open and leave the line alone when none is.
        let row = |app: &App| {
            let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
            terminal.draw(|f| draw(f, app)).unwrap();
            let buffer = terminal.backend().buffer().clone();
            (0..80)
                .map(|x| buffer[(x, 23)].symbol().to_string())
                .collect::<String>()
        };

        let working = App {
            ready: true,
            turn: Some("turn-1".into()),
            status: "Working".into(),
            ..App::default()
        };
        let idle = App {
            ready: true,
            turn: None,
            status: "Working".into(),
            ..App::default()
        };

        let drawn = row(&working);
        assert!(
            super::super::mark::BREATH.iter().any(|f| drawn.contains(f)),
            "a running turn should show the eye: {drawn:?}"
        );
        let quiet = row(&idle);
        assert!(
            !super::super::mark::BREATH.iter().any(|f| quiet.contains(f)),
            "an idle status line should carry no mark: {quiet:?}"
        );
    }

    #[test]
    fn mascot_keeps_the_same_compact_size_after_resizing() {
        for (w, h) in [(55, 18), (80, 24), (120, 40)] {
            let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
            let app = App {
                ready: true,
                robot: super::super::robot::Robot::cream("light", true),
                ..App::default()
            };
            terminal.draw(|f| draw(f, &app)).unwrap();
            let buffer = terminal.backend().buffer();
            // 9 columns wide and 4 rows tall: ears on row 1, text two columns on.
            assert_eq!(buffer[(0, 1)].symbol(), "▌");
            assert_eq!(buffer[(8, 1)].symbol(), "▐");
            assert_eq!(buffer[(11, 0)].symbol(), "A");
            assert!(display(&terminal).contains("Aria Code"));
        }
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
                context_scroll: 0,
                context_max_scroll: std::cell::Cell::new(0),
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
