use super::app::App;
use super::{markdown, theme::Palette};
use ratatui::{
    layout::{Constraint, Direction, Layout, Rect},
    style::{Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, Borders, Clear, Paragraph, Wrap},
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
    let (input_rows, cursor) = app.editor.layout(area.width.saturating_sub(4));
    let input_height = (input_rows.len().min(6) as u16)
        .saturating_add(1)
        .min(area.height / 2);
    let parts = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(if area.height >= 14 {
                4 + u16::from(!app.update_notice.is_empty()) + u16::from(app.details)
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
                    usize::from(area.width)
                        .saturating_sub(11 + app.tr("Workspace", "工作区").width() + 1),
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
            let target = workspace_tail(
                &operation_target(&action.target, &app.workspace),
                usize::from(parts[1].width).saturating_sub(label.width() + 16),
            );
            let count = action
                .count
                .map(|n| format!(" · {n} {}", app.tr("items", "项")))
                .unwrap_or_default();
            let reused = if action.cancelled {
                app.tr(" · cancelled", " · 已取消")
            } else if action.reused {
                app.tr(" · reused", " · 复用结果")
            } else {
                ""
            };
            lines.push(Line::from(vec![
                Span::styled(format!("{symbol} {label}"), Style::new().fg(color)),
                Span::raw(format!(" {target}{count}{reused}")),
            ]));
            if !action.error.is_empty() {
                lines.push(Line::styled(
                    format!("  {}", action.error),
                    Style::new().fg(p.error),
                ));
            }
            if app.details {
                lines.push(Line::styled(
                    format!(
                        "  {}{}",
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
                        .map(|line| Line::styled(format!("  {line}"), Style::new().fg(p.muted))),
                );
            }
            continue;
        }
        // Space between messages, not between every operation in a batch.
        if !lines.is_empty() {
            lines.push(Line::default());
        }
        let (label, color) = match entry.role.as_str() {
            "user" => (app.tr("You", "你"), p.accent),
            "assistant" => ("Aria", p.assistant),
            "error" => (app.tr("Error", "错误"), p.error),
            _ => ("", p.muted),
        };
        if !label.is_empty() {
            lines.push(Line::styled(
                label,
                Style::new().fg(color).add_modifier(Modifier::BOLD),
            ));
        }
        if entry.role == "assistant" {
            lines.extend(markdown::render(&entry.text, p, parts[1].width));
        } else {
            lines.extend(entry.text.lines().map(|s| Line::raw(s.to_owned())));
        }
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
    let input = Block::default()
        .borders(Borders::TOP)
        .title(if app.turn.is_some() {
            app.tr(" Draft · wait to send ", " 草稿 · 任务结束后按 Enter 发送 ")
        } else {
            app.tr(" Ask Aria · Enter sends ", " 输入消息 · Enter 发送 ")
        })
        .border_style(Style::new().fg(if app.ready { p.accent } else { p.muted }));
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
    frame.render_widget(
        Paragraph::new(format!(
            "{}{elapsed}{cancel}{scroll} · {}",
            app.status,
            app.tr("F1 help · Ctrl+O details", "F1 帮助 · Ctrl+O 详情")
        ))
        .style(Style::new().fg(p.muted)),
        parts[3],
    );
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
                " Your response required · Esc declines ",
                " 需要你的选择 · Esc 拒绝 ",
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
        assert_eq!(
            operation_target("/workspace/project/src/app.rs", "/workspace/project"),
            "src/app.rs"
        );
        assert_eq!(
            operation_target("/workspace/project", "/workspace/project"),
            "."
        );
        assert_eq!(
            operation_target("/workspace/project-other/a.rs", "/workspace/project"),
            "/workspace/project-other/a.rs"
        );
        assert_eq!(
            operation_target("cargo test --all", "/workspace/project"),
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
