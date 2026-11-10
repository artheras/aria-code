use super::app::{App, Dialog, Risk};
use super::{hang, mark, markdown, theme::Palette};
use ratatui::{
    layout::{Constraint, Direction, Layout, Margin, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, BorderType, Borders, Clear, Padding, Paragraph, Wrap},
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

/// `/Users/me/project` as `~/project`, the way a shell prompt shows it.
fn home_short(path: &str) -> String {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_default();
    home_short_in(path, &home)
}

fn home_short_in(path: &str, home: &str) -> String {
    let home = home.trim_end_matches(['/', '\\']);
    if home.len() > 1 {
        if path == home {
            return "~".into();
        }
        if let Some(rest) = path.strip_prefix(home) {
            if rest.starts_with(['/', '\\']) {
                return format!("~{rest}");
            }
        }
    }
    path.to_owned()
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
/// "gemini-3.8-flash" as "Gemini 3.8 Flash", "gpt-oss-120b" as "GPT OSS 120B".
/// Only plain lower-case ids are reworded; anything else (Ollama tags such as
/// `qwen2.5:7b`, mixed case) is shown as given. `/model` lists the exact ids.
fn display_model(id: &str) -> String {
    let id = id.rsplit('/').next().unwrap_or(id);
    if id.is_empty()
        || !id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '.')
    {
        return id.to_owned();
    }
    // Version numbers split by dashes ("sonnet-5-5") read as "5.5".
    let mut words: Vec<String> = Vec::new();
    for word in id.split('-').filter(|w| !w.is_empty()) {
        let numeric = word.chars().all(|c| c.is_ascii_digit());
        match words.last_mut() {
            Some(last) if numeric && last.chars().all(|c| c.is_ascii_digit() || c == '.') => {
                last.push('.');
                last.push_str(word);
            }
            _ => words.push(word.to_owned()),
        }
    }
    words
        .iter()
        .map(|word| match word.as_str() {
            "gpt" | "glm" | "oss" | "ai" | "vl" => word.to_ascii_uppercase(),
            w if w.ends_with('b') && w[..w.len() - 1].parse::<f64>().is_ok() => {
                format!("{}B", &w[..w.len() - 1])
            }
            w => {
                let mut chars = w.chars();
                match chars.next() {
                    Some(first) => first.to_ascii_uppercase().to_string() + chars.as_str(),
                    None => String::new(),
                }
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn display_provider(id: &str) -> String {
    match id {
        "google" => "Google".into(),
        "openai" => "OpenAI".into(),
        "anthropic" => "Anthropic".into(),
        "ollama" => "Ollama".into(),
        "deepseek" => "DeepSeek".into(),
        "xai" => "xAI".into(),
        "openrouter" => "OpenRouter".into(),
        other => {
            let mut chars = other.chars();
            match chars.next() {
                Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
                None => String::new(),
            }
        }
    }
}

const HELP: &str = "Aria interactive interface\n\nEnter  send · Alt+Enter / Ctrl+J  newline\nArrow keys  edit / browse history · /  list commands · @  pick a file · Tab  fill in\nCtrl+U / K / W  delete line / end / previous word\nPgUp / PgDn or mouse wheel  scroll transcript\nCtrl+O  toggle tool details · F1  this help\nEsc or Ctrl+C  cancel current task / dismiss dialog\nCtrl+D  exit (or delete character when input is non-empty)\n\n/new  start a new session · /resume ID  restore a session\n/sessions  list saved sessions · /help  all Aria commands\n/model  choose your model · /health  check configured services\n@file:path  reference a file · !command  run a shell command\n\nFile permissions, task isolation, checks and model routing are managed by Aria.\nApproval menus require your choice. Hidden reasoning and secret inputs\nare never written to the native transcript.";

const HELP_ZH: &str = "Aria 交互界面\n\nEnter 发送 · Alt+Enter / Ctrl+J 换行\n方向键编辑 / 浏览输入历史 · 输入 / 列出命令 · @ 选择文件 · Tab 补全\nCtrl+U / K / W 删除行首 / 行尾 / 前一个词\nPgUp / PgDn 或鼠标滚轮查看对话历史\nCtrl+O 展开工具详情 · F1 打开帮助\nEsc / Ctrl+C 取消当前任务或关闭对话框\nCtrl+D 退出（输入非空时删除字符）\n\n/new 新会话 · /resume ID 恢复会话\n/sessions 会话列表 · /help 所有命令\n/model 选择模型 · /health 服务检查\n@file:path 引用文件 · !command 执行命令\naria -C <项目目录> 在指定项目中启动\n/config set ui_lang=zh 设置中文界面\n\n运行时管理文件权限、任务隔离与验证。\n审批需要你明确选择；秘密输入与隐藏推理不会写入界面记录。";

fn tool_label<'a>(tool: &'a str, app: &App) -> &'a str {
    let name = tool.rsplit("__").next().unwrap_or(tool);
    match name {
        "list_files" | "glob" => app.tr("List files", "查看目录"),
        "read_file" => app.tr("Read file", "读取文件"),
        "search_code" | "find_symbol" => app.tr("Search code", "搜索代码"),
        "run_command" => app.tr("Run command", "执行命令"),
        "write_file" => app.tr("Write file", "写入文件"),
        "edit_file" | "multi_edit" | "apply_patch" => app.tr("Edit file", "修改文件"),
        "repo_map" => app.tr("Map project", "查看项目结构"),
        "get_market_data" | "get_crypto_data" | "get_forex_data" | "financial_quote" => {
            app.tr("Quote", "行情")
        }
        "backtest_strategy"
        | "cloud_backtest"
        | "run_portfolio_backtest"
        | "financial_backtest" => app.tr("Backtest", "回测"),
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

/// `text` cut to at most `max` display columns, with an ellipsis when it was cut.
fn fit(text: &str, max: usize) -> String {
    if text.width() <= max {
        return text.to_owned();
    }
    let mut out = String::new();
    let mut used = 0;
    for g in text.graphemes(true) {
        let w = g.width();
        if used + w + 1 > max {
            break;
        }
        out.push_str(g);
        used += w;
    }
    out.push('…');
    out
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
    let show_robot = !robot.0.is_empty() && area.width >= robot_width + 36 && area.height >= 10;
    let left_padding = if show_robot { robot_width + 2 } else { 0 };
    let input_height = (input_rows.len().min(6) as u16)
        .saturating_add(2)
        .min(area.height / 2);
    let roomy = area.height >= 10 && area.width >= 48;
    let text_rows = if area.height < 8 {
        1
    } else {
        u16::from(roomy).saturating_mul(2).saturating_add(1)
            + u16::from(!app.update_notice.is_empty())
            + u16::from(!app.service_notices.is_empty())
            + u16::from(app.details)
            + u16::from(app.turn.is_some() && !app.phase.is_empty())
    };
    let parts = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(if area.height < 8 {
                1
            } else {
                text_rows.max(if show_robot { robot.0.len() as u16 } else { 0 })
            }),
            Constraint::Min(1),
            Constraint::Length(input_height),
            Constraint::Length(1),
        ])
        .split(area);
    let header = if parts[0].height > 1 {
        // Roomy terminals match Claude Code: the mascot sits beside the name,
        // the model, and the directory. Narrow terminals keep one status line.
        let width = usize::from(area.width.saturating_sub(left_padding));
        let mut lines = if roomy {
            identity_lines(app, p, width)
        } else {
            vec![session_line(app, p, width)]
        };
        if !app.service_notices.is_empty() {
            lines.push(Line::styled(
                fit(&app.service_notices.join(" · "), width),
                Style::new().fg(p.warn),
            ));
        }
        if !app.update_notice.is_empty() {
            lines.push(Line::styled(
                app.update_notice.clone(),
                Style::new().fg(p.warn),
            ));
        }
        // Where the turn is: Inspect ✓ · Edit ● · Verify ○ — what the work is,
        // not which tools the model happens to call.
        if app.turn.is_some() && !app.phase.is_empty() {
            let mut spans = Vec::new();
            for (index, (text, state)) in app.phase.pieces(app.zh()).into_iter().enumerate() {
                if index > 0 {
                    spans.push(Span::styled(" · ", Style::new().fg(p.muted)));
                }
                let color = match state {
                    0 => p.accent,
                    -1 => p.success,
                    _ => p.muted,
                };
                spans.push(Span::styled(text, Style::new().fg(color)));
            }
            spans.push(Span::styled(
                format!("  {}", fit(&app.phase.doing(app.zh()), 60)),
                Style::new().fg(p.muted),
            ));
            lines.push(Line::from(spans));
        }
        if app.details {
            lines.push(Line::styled(
                format!(
                    "Aria Code v{} · Rust UI {} · {} {}",
                    app.version,
                    env!("CARGO_PKG_VERSION"),
                    app.tr("session", "会话"),
                    app.session
                ),
                Style::new().fg(p.muted),
            ));
        }
        Text::from(lines)
    } else {
        Text::from(session_line(app, p, usize::from(area.width)))
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
    let (group_start, grouped) = inspection_groups(app);
    for (index, entry) in app.entries.iter().enumerate() {
        if entry.text.trim().is_empty() || (entry.detail && !app.details) {
            continue;
        }
        if grouped.contains(&index) {
            continue;
        }
        if let Some((reads, others)) = group_start.get(&index) {
            lines.push(Line::from(vec![
                Span::styled("✓ ", Style::new().fg(p.success)),
                Span::raw(inspection_summary(app, *reads, *others)),
                Span::styled(
                    app.tr(" · Ctrl+O expands", " · Ctrl+O 展开"),
                    Style::new().fg(p.muted),
                ),
            ]));
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
                app.tr(
                    " · turn cancelled before result",
                    " · 本轮已取消，未收到结果",
                )
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
            let mut spans = vec![
                Span::styled(format!("{symbol} {label}"), Style::new().fg(color)),
                Span::raw(format!(" {target}{count}{exit_code}{reused}")),
            ];
            if let (Some(added), Some(removed)) = (action.added, action.removed) {
                if added == 0 && removed == 0 {
                    spans.push(Span::styled(
                        app.tr(" · unchanged", " · 无改动").to_owned(),
                        Style::new().fg(p.muted),
                    ));
                } else {
                    spans.push(Span::styled(
                        format!(" +{added}"),
                        Style::new().fg(p.success),
                    ));
                    spans.push(Span::styled(
                        format!(" −{removed}"),
                        Style::new().fg(p.error),
                    ));
                }
            }
            if !action.service_line.is_empty() {
                spans.push(Span::raw(format!(" {}", action.service_line)));
            }
            if let Some(change) = action.service_change {
                let change_color = if change < 0.0 { p.error } else { p.success };
                spans.push(Span::styled(
                    format!(" {change:+.1}%"),
                    Style::new().fg(change_color),
                ));
            }
            if let Some(fresh) = action.fresh {
                spans.push(Span::styled(
                    if fresh {
                        app.tr(" · current", " · 未过期").to_owned()
                    } else {
                        app.tr(" · stale", " · 过期").to_owned()
                    },
                    Style::new().fg(p.muted),
                ));
            }
            lines.push(Line::from(spans));
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
    // An empty conversation says nothing the input's own hint already says;
    // only a starting runtime or a home-folder workspace earns a line here.
    if lines.is_empty() {
        if !app.ready {
            lines.push(Line::styled(
                app.tr("The runtime is starting…", "正在启动…"),
                Style::new().fg(p.muted),
            ));
        } else if app.home_workspace {
            lines.push(Line::styled(
                app.tr(
                    "Home folder · start in a project with aria -C <project>. Files are read only when a task needs them.",
                    "当前是主目录。用 aria -C <项目目录> 在项目中启动；仅按任务需要读取文件。",
                ),
                Style::new().fg(p.muted),
            ));
        }
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
    // Like Claude Code's composer: a rule above and below, no box. The same
    // inner width as before (two padding columns each side), so wrapping and
    // the cursor stay where the editor computes them. The label is shown only
    // while queueing, when it says something.
    let mut input = Block::new()
        .borders(Borders::TOP | Borders::BOTTOM)
        .padding(Padding::horizontal(2))
        .border_style(Style::new().fg(p.muted));
    if busy {
        input = input.title(Line::styled(label, Style::new().fg(p.accent)));
    }
    if label.width() + hint.width() + 6 <= usize::from(parts[2].width) {
        input = input.title(Line::styled(hint, Style::new().fg(p.muted)).right_aligned());
    }
    let mut inside = input.inner(parts[2]);
    frame.render_widget(input, parts[2]);
    frame.render_widget(
        Paragraph::new("❯").style(Style::new().fg(if app.ready { p.accent } else { p.muted })),
        Rect::new(inside.x, inside.y, 1, inside.height.min(1)),
    );
    inside.x += 2;
    inside.width = inside.width.saturating_sub(2);
    let first = usize::from(cursor.0).saturating_sub(inside.height.saturating_sub(1) as usize);
    if app.editor.is_empty() {
        let placeholder: &str = if busy {
            app.tr(
                "Type your next message — Enter queues it…",
                "可以先写下一条消息，Enter 排队…",
            )
        } else {
            &example_prompt(app)
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
    // The command list: opens above the input on `/`, narrows as you type.
    let (rows_found, for_files) = app.popup_rows();
    let items: Vec<(String, String)> = if for_files {
        rows_found
            .into_iter()
            .map(|(label, _)| {
                let tag = if label.ends_with('/') {
                    app.tr("folder", "文件夹").to_owned()
                } else {
                    String::new()
                };
                (label, tag)
            })
            .collect()
    } else {
        rows_found
    };
    if !items.is_empty() && parts[2].y > area.y {
        let rows = items.len() as u16;
        let height = (rows + 2).min(parts[2].y - area.y);
        let width = parts[2].width; // as wide as the input it belongs to
        let rect = Rect::new(parts[2].x, parts[2].y - height, width, height);
        let name_width = items
            .iter()
            .map(|(name, _)| name.width())
            .max()
            .unwrap_or(0)
            .min(if for_files { 60 } else { 24 });
        let inner_width = usize::from(rect.width.saturating_sub(4));
        let selected = app.popup_index.min(items.len() - 1);
        let lines: Vec<Line> = items
            .iter()
            .enumerate()
            .take(usize::from(height.saturating_sub(2)))
            .map(|(i, (name, detail))| {
                let name = fit(name, name_width);
                let used = name_width + 2;
                let detail = fit(detail, inner_width.saturating_sub(used));
                let text = format!("{name:<name_width$}  {detail}");
                if i == selected {
                    Line::styled(
                        format!("❯ {text}"),
                        Style::new().fg(p.selection_fg).bg(p.accent),
                    )
                } else {
                    Line::from(vec![
                        Span::styled(format!("  {name:<name_width$}"), Style::new().fg(p.accent)),
                        Span::styled(format!("  {detail}"), Style::new().fg(p.muted)),
                    ])
                }
            })
            .collect();
        frame.render_widget(Clear, rect);
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::bordered()
                    .border_type(BorderType::Rounded)
                    .border_style(Style::new().fg(p.muted))
                    .title(Line::styled(
                        if for_files {
                            app.tr(" Files ", " 文件 ")
                        } else {
                            app.tr(" Commands ", " 命令 ")
                        },
                        Style::new().fg(p.accent),
                    ))
                    .title_bottom(Line::styled(
                        app.tr(
                            " ↑↓ choose · Tab fill in · Esc close ",
                            " ↑↓ 选择 · Tab 补全 · Esc 关闭 ",
                        ),
                        Style::new().fg(p.muted),
                    )),
            ),
            rect,
        );
    }
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
    let mode = permission_phrase(app);
    if !mode.is_empty() {
        status_line.push(Span::styled("▸ ", Style::new().fg(p.muted)));
        status_line.push(Span::styled(mode, Style::new().fg(p.accent)));
        status_line.push(Span::raw(" · "));
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
    if let Some(dialog) = app.dialog.as_ref().filter(|d| d.picker) {
        draw_picker(frame, app, dialog, area, p);
    } else if let Some(dialog) = &app.dialog {
        let mut heading = vec![
            Line::styled(dialog.title.clone(), Style::new().bold()),
            Line::raw(""),
        ];
        if !dialog.context.is_empty() {
            heading.extend(context_lines(&dialog.context, p));
            heading.push(Line::raw(""));
        }
        if let Some(risk) = &dialog.risk {
            heading.extend(risk_card(app, risk, p));
            heading.push(Line::raw(""));
        }
        let heading = Paragraph::new(heading).wrap(Wrap { trim: false });
        let heading_lines = heading.line_count(area.width.saturating_sub(2));
        let choice_rows: usize = dialog
            .choices
            .iter()
            .map(|(_, help)| 1 + usize::from(!help.is_empty()))
            .sum();
        // A panel along the bottom, the width of the window and as tall as
        // what it shows, like Claude Code's: the conversation above stays
        // readable instead of being cut by a box in the middle.
        let wanted = 1
            + heading_lines
            + if dialog.choices.is_empty() {
                3
            } else {
                choice_rows + 2
            };
        let height = (wanted as u16).min(area.height.saturating_sub(2)).max(4);
        let rect = Rect::new(area.x, area.bottom() - height, area.width, height);
        frame.render_widget(Clear, rect);
        let block = Block::new()
            .borders(Borders::TOP)
            .padding(Padding::horizontal(1))
            .border_style(Style::new().fg(p.accent));
        let inside = block.inner(rect);
        frame.render_widget(block, rect);
        // Keep the operation visible while scrolling through approval choices.
        // The context takes whatever the choices leave; a long diff scrolls
        // with PgUp/PgDn while the choices stay in view.
        let reserved = if dialog.choices.is_empty() {
            3
        } else {
            choice_rows + 2
        } as u16;
        // Never less than half the panel for the context: with many choices
        // those scroll instead, so the operation being approved stays in view.
        let heading_rows = heading_lines.min(usize::from(u16::MAX)) as u16;
        let context_height = heading_rows
            .min(inside.height.saturating_sub(reserved))
            .max(heading_rows.min(inside.height / 2));
        let sections = Layout::vertical([Constraint::Length(context_height), Constraint::Min(1)])
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
                    Style::new().fg(p.accent).bold()
                } else {
                    Style::default()
                };
                rows.push(Line::styled(
                    format!(
                        "{} {}. {}",
                        if i == dialog.selected { "❯" } else { " " },
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
                app.tr(
                    "Enter confirms · Esc declines · ↑/↓ or a number chooses · PgUp/PgDn review",
                    "Enter 确认 · Esc 拒绝 · ↑/↓ 或数字选择 · PgUp/PgDn 查看",
                ),
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

/// Read-only operations that may be folded into one line when several run in
/// a row: what was looked at matters less than what was changed.
const INSPECTIONS: [&str; 7] = [
    "read_file",
    "list_files",
    "glob",
    "search_code",
    "find_symbol",
    "repo_map",
    "grep",
];

/// Runs of two or more finished, successful inspections with nothing to report
/// (no approval note, no error), unless details are shown. Returns the first
/// entry of each run with its (reads, other inspections) counts, and the
/// entries folded into it.
fn inspection_groups(
    app: &App,
) -> (
    std::collections::HashMap<usize, (usize, usize)>,
    std::collections::HashSet<usize>,
) {
    let mut starts = std::collections::HashMap::new();
    let mut folded = std::collections::HashSet::new();
    if app.details {
        return (starts, folded);
    }
    let foldable = |i: usize| {
        app.entries[i].action.as_ref().is_some_and(|a| {
            a.done
                && a.success
                && !a.cancelled
                && a.approval.is_empty()
                && a.error.is_empty()
                && INSPECTIONS.contains(&a.tool.as_str())
        })
    };
    let mut i = 0;
    while i < app.entries.len() {
        if !foldable(i) {
            i += 1;
            continue;
        }
        let start = i;
        while i < app.entries.len() && foldable(i) {
            i += 1;
        }
        if i - start >= 2 {
            let reads = (start..i)
                .filter(|&k| app.entries[k].action.as_ref().unwrap().tool == "read_file")
                .count();
            starts.insert(start, (reads, i - start - reads));
            folded.extend(start + 1..i);
        }
    }
    (starts, folded)
}

fn inspection_summary(app: &App, reads: usize, others: usize) -> String {
    let files = |n: usize| {
        if app.zh() {
            format!("读取了 {n} 个文件")
        } else if n == 1 {
            "Read 1 file".to_owned()
        } else {
            format!("Read {n} files")
        }
    };
    let looked = |n: usize| {
        if app.zh() {
            format!("查看了 {n} 处")
        } else if n == 1 {
            "looked in 1 place".to_owned()
        } else {
            format!("looked in {n} places")
        }
    };
    match (reads, others) {
        (r, 0) => files(r),
        (0, o) => {
            let mut text = looked(o);
            if let Some(first) = text.get(..1) {
                text = first.to_uppercase() + &text[1..];
            }
            text
        }
        (r, o) => format!("{}{}{}", files(r), app.tr(", ", "，"), looked(o)),
    }
}

/// An approval's context, with any unified diff in it drawn the way an editor
/// shows one: line numbers, added lines on green, removed lines on red.
/// Lines outside a hunk are shown as plain context.
fn context_lines(context: &str, p: Palette) -> Vec<Line<'static>> {
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
fn risk_card(app: &App, risk: &Risk, p: Palette) -> Vec<Line<'static>> {
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
fn draw_picker(frame: &mut Frame, app: &App, dialog: &Dialog, area: Rect, p: Palette) {
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

/// The delivery report, coloured as the Python REPL prints it: the status by
/// outcome, headings dim, ✓ green, ✗ red, × and ⚠ highlighted.
/// The runtime's delivery report, read quietly: "DONE" becomes one "✓ Done"
/// line in the success colour (not a shouted heading), blank lines go, and
/// section names are muted labels with their content indented under them.
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
    let mut out = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        if index == 0 {
            out.push(if trimmed == "DONE" {
                Line::styled("✓ Done".to_owned(), Style::new().fg(p.success))
            } else {
                Line::styled(
                    line.to_owned(),
                    Style::new().fg(p.accent).add_modifier(Modifier::BOLD),
                )
            });
            continue;
        }
        let style = if HEADINGS.contains(&trimmed) {
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
        let indent = if HEADINGS.contains(&trimmed) {
            "  "
        } else {
            "    "
        };
        out.push(Line::styled(format!("{indent}{trimmed}"), style));
    }
    out
}

/// One concrete thing to try, like Claude Code's `Try "fix typecheck errors"`,
/// picked from the session id so it stays put while the session lasts.
fn example_prompt(app: &App) -> String {
    const EN: [&str; 4] = [
        "Add tests for utils.py",
        "Explain how this project is structured",
        "Fix the failing test",
        "How's NVDA this week?",
    ];
    const ZH: [&str; 4] = [
        "给 utils.py 补上测试",
        "讲讲这个项目的结构",
        "修好失败的测试",
        "宁德时代今天怎么样？",
    ];
    let pick = app.session.bytes().map(usize::from).sum::<usize>() % EN.len();
    if app.zh() {
        format!("试试“{}”", ZH[pick])
    } else {
        format!("Try \"{}\"", EN[pick])
    }
}

/// Name, model, and directory: the three lines beside Claude Code's mascot.
fn identity_lines(app: &App, p: Palette, width: usize) -> Vec<Line<'static>> {
    let mut model = display_model(&app.model);
    if !app.provider.is_empty() {
        let provider = display_provider(&app.provider);
        model = if model.is_empty() {
            provider
        } else {
            format!("{model} · {provider}")
        };
    }
    let home = if app.home_workspace {
        format!(" · {}", app.tr("home folder", "主目录"))
    } else {
        String::new()
    };
    let branch = if app.home_workspace || app.branch.is_empty() {
        String::new()
    } else {
        format!(" · {}", app.branch)
    };
    let place = workspace_tail(
        &home_short(&app.workspace),
        width.saturating_sub(home.width() + branch.width()),
    );
    let mut place_line = vec![Span::styled(
        place,
        if app.home_workspace {
            Style::new().fg(p.warn)
        } else {
            Style::new().fg(p.muted)
        },
    )];
    if app.home_workspace {
        place_line.push(Span::styled(home, Style::new().fg(p.warn)));
    } else if !branch.is_empty() {
        place_line.push(Span::styled(branch, Style::new().fg(p.muted)));
    }
    vec![
        Line::from(vec![
            Span::styled("Aria Code", Style::new().bold()),
            Span::styled(format!(" v{}", app.version), Style::new().fg(p.muted)),
        ]),
        Line::styled(model, Style::new().fg(p.muted)),
        Line::from(place_line),
    ]
}

/// Model, place, and what a later action is allowed to do, fitted to one row.
fn session_line(app: &App, p: Palette, width: usize) -> Line<'static> {
    let mut model = display_model(&app.model);
    if !app.provider.is_empty() {
        let provider = display_provider(&app.provider);
        if model.is_empty() {
            model = provider;
        } else {
            model = format!("{model} · {provider}");
        }
    }
    let place = home_short(&app.workspace);
    let home = if app.home_workspace {
        format!(" · {}", app.tr("home folder", "主目录"))
    } else {
        String::new()
    };
    let branch = if app.home_workspace || app.branch.is_empty() {
        String::new()
    } else {
        format!(" · {}", app.branch)
    };
    let permission = permission_phrase(app);
    let tail = format!("{home}{branch} · {permission}");
    let lead = if model.is_empty() {
        String::new()
    } else {
        format!("{model} · ")
    };
    let place = workspace_tail(&place, width.saturating_sub(lead.width() + tail.width()));
    let mut spans = Vec::new();
    if !lead.is_empty() {
        spans.push(Span::raw(lead));
    }
    let place_style = if app.home_workspace {
        Style::new().fg(p.warn)
    } else {
        Style::new().fg(p.muted)
    };
    spans.push(Span::styled(place, place_style));
    if app.home_workspace {
        spans.push(Span::styled(home, Style::new().fg(p.warn)));
    } else if !branch.is_empty() {
        spans.push(Span::styled(branch, Style::new().fg(p.muted)));
    }
    spans.push(Span::styled(
        format!(" · {permission}"),
        Style::new().fg(p.muted),
    ));
    Line::from(spans)
}

fn permission_phrase(app: &App) -> String {
    let mode = match app.permission.as_str() {
        "workspace-write" => app.tr("writes need approval", "写入需确认"),
        "read-only" | "plan" => app.tr("read only", "只读"),
        "accept-edits" => app.tr("edits allowed", "可直接改文件"),
        "" => "",
        other => other,
    };
    let network = if app.network {
        app.tr("network on", "网络开")
    } else {
        app.tr("network off", "网络关")
    };
    if mode.is_empty() {
        network.to_owned()
    } else {
        format!("{mode} · {network}")
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
    fn the_header_shows_where_a_running_turn_is() {
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).unwrap();
        let mut app = App {
            permission: "workspace-write".into(),
            ready: true,
            ..App::default()
        };
        app.editor.insert("fix the refresh bug");
        app.submit();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"read_file","params":{"path":"src/auth.py"}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"read_file","success":true})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"edit_file","params":{"path":"src/auth.py"}})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(shown.contains("Inspect ✓ · Edit ● · Verify ○"), "{shown}");
        assert!(shown.contains("Editing src/auth.py"), "{shown}");
        let palette = Palette::for_theme("dark");
        let buffer = terminal.backend().buffer();
        let mut saw_done = false;
        let mut saw_now = false;
        let mut saw_wait = false;
        for y in 0..buffer.area.height {
            for x in 0..buffer.area.width {
                let cell = &buffer[(x, y)];
                match cell.symbol() {
                    "✓" => {
                        assert_eq!(cell.fg, palette.success);
                        saw_done = true;
                    }
                    "●" => {
                        assert_eq!(cell.fg, palette.accent);
                        saw_now = true;
                    }
                    "○" => {
                        assert_eq!(cell.fg, palette.muted);
                        saw_wait = true;
                    }
                    _ => {}
                }
            }
        }
        assert!(saw_done && saw_now && saw_wait);
        // Once the turn ends the strip goes away; the header returns to its usual rows.
        app.event(serde_json::json!({"protocol":1,"type":"turn.completed","turn_id":"turn-1","status":"completed"})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(!display(&terminal).contains("Verify ○"));
    }

    #[test]
    fn an_approval_shows_what_the_action_reaches() {
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        let mut app = App {
            turn: Some("turn-1".into()),
            ..App::default()
        };
        app.event(serde_json::json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1","request_id":"a",
            "title":"Approve","context":"Command: npm install jose","choices":[["Allow once",""],["Deny",""]],
            "risk":{"level":2,"name":"medium","score":43,"reversible":true,"hosts":["registry.npmjs.org"],
                    "files":["package.json","package-lock.json"],"systems":[],"reasons":["Changes installed dependencies"],
                    "impact":"3 files depend on this"}})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        for want in [
            "Command: npm install jose",
            "L2 medium",
            "43/100",
            "reversible",
            "registry.npmjs.org",
            "package.json, package-lock.json",
            "Changes installed dependencies",
            "3 files depend on this",
        ] {
            assert!(shown.contains(want), "missing {want:?}\n{shown}");
        }
        // No assessment: the dialog is as it was.
        let mut plain = App {
            turn: Some("turn-1".into()),
            ..App::default()
        };
        plain.event(serde_json::json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1","request_id":"b",
            "title":"Approve","choices":[["Yes",""]]})).unwrap();
        terminal.draw(|f| draw(f, &plain)).unwrap();
        assert!(!display(&terminal).contains("Risk"));
    }

    #[test]
    fn a_service_warning_is_one_sentence_under_the_status() {
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        let app = App {
            ready: true,
            model: "qwen2.5:7b".into(),
            service_notices: vec!["商业 · 未连接，没有商家令牌".into(), "检索 · 未连接".into()],
            ..App::default()
        };
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(shown.contains("Aria Code"), "{shown}");
        assert!(shown.contains("qwen2.5:7b"), "{shown}");
        assert!(shown.contains("商家令牌"), "{shown}");
        assert!(shown.contains("检索 · 未连接"), "{shown}");
        assert!(!shown.contains("WARNING:"), "{shown}");
    }

    #[test]
    fn welcome_lines_read_like_a_prompt_not_a_form() {
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).unwrap();
        let mut app = App {
            ready: true,
            version: "0.164.0".into(),
            model: "gemini-3.8-flash".into(),
            provider: "google".into(),
            workspace: "/srv/game".into(),
            branch: "feat/boss".into(),
            permission: "workspace-write".into(),
            network: true,
            ..App::default()
        };
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(shown.contains("Gemini 3.8 Flash · Google"), "{shown}");
        assert!(shown.contains("/srv/game · feat/boss"), "{shown}");
        assert!(!shown.contains("Workspace"), "{shown}");
        // The whole home folder as workspace is called out instead of the branch.
        app.home_workspace = true;
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(display(&terminal).contains("· home folder"));
    }

    #[test]
    fn an_empty_conversation_offers_one_example_instead_of_three_hints() {
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).unwrap();
        let app = App {
            ready: true,
            session: "s1".into(),
            ..App::default()
        };
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(shown.contains("Try \""), "{shown}");
        assert!(
            !shown.contains("Describe a task, edit your project"),
            "{shown}"
        );
        assert!(!shown.contains("/ commands"), "{shown}");
        assert!(!shown.contains("@ files"), "{shown}");
    }

    #[test]
    fn the_progress_strip_names_files_relative_to_the_workspace() {
        // Native absolute paths: "/srv/game" is not absolute on Windows.
        let workspace = std::env::temp_dir().join("aria-strip-game");
        let file = workspace.join("src").join("calc.py");
        let relative = std::path::Path::new("src").join("calc.py");
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        let mut app = App {
            ready: true,
            workspace: workspace.display().to_string(),
            ..App::default()
        };
        app.editor.insert("fix it");
        app.submit();
        app.event(
            serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1",
            "tool":"edit_file","params":{"path":file.display().to_string()}}),
        )
        .unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(
            shown.contains(&format!("Editing {}", relative.display())),
            "{shown}"
        );
        assert!(!shown.contains(&file.display().to_string()), "{shown}");
    }

    #[test]
    fn the_delivery_report_is_quiet() {
        let lines = delivery_lines(
            "DONE\n\nChanged\n  M calc.py  +4 -0\n\nNext\n  Run the tests",
            Palette::for_theme("dark"),
        );
        let text: Vec<String> = lines.iter().map(|l| l.to_string()).collect();
        assert_eq!(
            text,
            [
                "✓ Done",
                "  Changed",
                "    M calc.py  +4 -0",
                "  Next",
                "    Run the tests"
            ]
        );
    }

    #[test]
    fn the_selected_command_is_marked_not_only_coloured() {
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).unwrap();
        let mut app = App {
            ready: true,
            commands: vec!["/help".into(), "/model".into()],
            ..App::default()
        };
        app.command_info.insert("/help".into(), "Show help".into());
        app.editor.insert("/");
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(shown.contains("❯ /help"), "{shown}");
        assert!(shown.contains("  /model"), "{shown}");
    }

    #[test]
    fn an_approval_is_a_panel_along_the_bottom_with_a_numbered_diff() {
        let mut terminal = Terminal::new(TestBackend::new(80, 30)).unwrap();
        let mut app = App {
            ready: true,
            workspace: "/srv/game".into(),
            ..App::default()
        };
        app.editor.insert("add subtract");
        app.submit();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"edit_file","params":{"path":"/srv/game/calc.py"}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"approval.requested","turn_id":"turn-1","request_id":"a",
            "title":"Make this edit to calc.py?",
            "context":"File: calc.py\n@@ -1,2 +1,4 @@\n def add(a, b):\n     return a + b\n+def subtract(a, b):\n+    return a - b",
            "choices":[["Allow once",""],["Decline",""]]})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        let rows: Vec<&str> = shown.lines().collect();
        // The conversation above is untouched; the panel ends at the last row.
        assert!(
            rows.iter().any(|r| r.starts_with("› add subtract")),
            "{shown}"
        );
        assert!(rows.last().unwrap().contains("Enter confirms"), "{shown}");
        // New-file line numbers, the sign kept with the text.
        assert!(
            rows.iter().any(|r| r.contains("   3 +def subtract(a, b):")),
            "{shown}"
        );
        assert!(
            rows.iter().any(|r| r.contains("   1  def add(a, b):")),
            "{shown}"
        );
        assert!(
            rows.iter().any(|r| r.contains("❯ 1. Allow once")),
            "{shown}"
        );
        // Added lines carry a background.
        let lines = context_lines("@@ -1 +1,2 @@\n a\n+b", Palette::for_theme("dark"));
        assert!(lines[2].spans[1].style.bg.is_some());
        assert!(lines[1].spans[1].style.bg.is_none());
    }

    #[test]
    fn consecutive_reads_fold_into_one_line_until_details_are_shown() {
        let mut app = App {
            ready: true,
            workspace: "/srv/game".into(),
            ..App::default()
        };
        app.editor.insert("look around");
        app.submit();
        for path in ["a.py", "b.py", "c.py"] {
            app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"read_file","params":{"path":path}})).unwrap();
            app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"read_file","success":true})).unwrap();
        }
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"search_code","params":{"query":"subtract"}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"search_code","success":true})).unwrap();
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(
            shown.contains("✓ Read 3 files, looked in 1 place · Ctrl+O expands"),
            "{shown}"
        );
        assert!(!shown.contains("b.py"), "{shown}");
        app.details = true;
        terminal.draw(|f| draw(f, &app)).unwrap();
        assert!(display(&terminal).contains("b.py"));
    }

    #[test]
    fn model_and_provider_ids_read_as_names() {
        assert_eq!(display_model("gemini-3.8-flash"), "Gemini 3.8 Flash");
        assert_eq!(display_model("openai/gpt-oss-120b"), "GPT OSS 120B");
        assert_eq!(display_model("claude-sonnet-5-5"), "Claude Sonnet 5.5");
        assert_eq!(display_model("gpt-5"), "GPT 5");
        assert_eq!(display_model("qwen2.5:7b"), "qwen2.5:7b");
        assert_eq!(display_model("GLM-4.6"), "GLM-4.6");
        assert_eq!(display_provider("google"), "Google");
        assert_eq!(display_provider("openai"), "OpenAI");
        assert_eq!(display_provider("my-gateway"), "My-gateway");
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
        let header = rendered
            .lines()
            .find(|line| line.contains('…') || line.contains("我的项目"))
            .unwrap();
        assert!(
            rendered.contains("writes need approval · network on"),
            "{rendered}"
        );
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
    fn an_edit_shows_its_size_and_a_service_result_is_one_line() {
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        let mut app = App {
            ready: true,
            lang: "zh".into(),
            ..App::default()
        };
        app.editor.insert("改一下并看行情");
        app.submit();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"edit_file","params":{"path":"src/auth.py"}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"edit_file","success":true,"brief":{"kind":"edit","added":12,"removed":3}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"mcp__arthera-finance__financial_quote","params":{}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"mcp__arthera-finance__financial_quote","success":true,"brief":{"kind":"quote","name":"宁德时代","price":182.4,"change_pct":1.2,"stale":false}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.started","turn_id":"turn-1","tool":"backtest_strategy","params":{}})).unwrap();
        app.event(serde_json::json!({"protocol":1,"type":"tool.completed","turn_id":"turn-1","tool":"backtest_strategy","success":true,"brief":{"kind":"backtest","return_pct":8.4,"drawdown_pct":3.1}})).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let shown = display(&terminal);
        assert!(shown.contains("修改文件"), "{shown}");
        assert!(shown.contains("+12"), "{shown}");
        assert!(shown.contains("−3"), "{shown}");
        assert!(shown.contains("行情"), "{shown}");
        assert!(shown.contains("宁德时代 182.40"), "{shown}");
        assert!(shown.contains("+1.2%"), "{shown}");
        assert!(shown.contains("未过期"), "{shown}");
        assert!(shown.contains("回测"), "{shown}");
        assert!(shown.contains("收益 +8.4% · 回撤 3.1%"), "{shown}");
        assert!(!shown.contains("financial_quote"), "{shown}");
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
            // A rule above and below the composer, the prompt two columns in.
            assert_eq!(buffer[(2, height - 3)].symbol(), "❯");
            assert_eq!(buffer[(0, height - 2)].symbol(), "─");
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
    fn home_is_shown_as_a_tilde() {
        assert_eq!(home_short_in("/Users/me/project", "/Users/me"), "~/project");
        assert_eq!(home_short_in("/Users/me", "/Users/me"), "~");
        assert_eq!(
            home_short_in("/Users/medal/x", "/Users/me"),
            "/Users/medal/x"
        );
        assert_eq!(home_short_in("/tmp/x", "/Users/me"), "/tmp/x");
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
            // Nine columns, three rows. The name starts two columns past the mascot.
            assert_eq!(buffer[(2, 1)].symbol(), "▀");
            assert_eq!(buffer[(5, 1)].symbol(), "▀");
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
                picker: false,
                groups: vec![],
                filter: String::new(),
                risk: None,
            });
            app.dialog.as_mut().unwrap().editor.insert("DO_NOT_DISPLAY");
            terminal.draw(|f| draw(f, &app)).unwrap();
            assert!(!format!("{:?}", terminal.backend().buffer()).contains("DO_NOT_DISPLAY"));
        }
    }

    #[test]
    fn the_command_list_is_drawn_above_the_input_with_the_highlight_on_the_first_row() {
        let mut app = App {
            ready: true,
            ..App::default()
        };
        app.commands = vec!["/help".into(), "/model".into()];
        app.command_order = app.commands.clone();
        app.command_info.insert("/help".into(), "Show help".into());
        app.command_info
            .insert("/model".into(), "Choose a model".into());
        app.editor.insert("/");
        let mut terminal = Terminal::new(TestBackend::new(70, 24)).unwrap();
        terminal.draw(|f| draw(f, &app)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let rows: Vec<String> = (0..24)
            .map(|y| {
                (0..70)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect::<String>()
            })
            .collect();
        let help = rows
            .iter()
            .position(|r| r.contains("/help") && r.contains("Show help"))
            .expect("first row");
        let model = rows
            .iter()
            .position(|r| r.contains("/model") && r.contains("Choose a model"))
            .expect("second row");
        let input = rows
            .iter()
            .rposition(|r| r.trim_end().ends_with("❯ /"))
            .expect("the composer with the typed slash");
        assert!(
            help < model && model < input,
            "the list sits above the input, in order: {rows:#?}"
        );
        let highlighted =
            (0..70).any(|x| buffer[(x, help as u16)].bg != ratatui::style::Color::Reset);
        assert!(highlighted, "the first row is highlighted");
        // Closing it removes it.
        app.editor.insert("zzz");
        terminal.draw(|f| draw(f, &app)).unwrap();
        let buffer = terminal.backend().buffer().clone();
        let text: String = (0..24)
            .flat_map(|y| (0..70).map(move |x| (x, y)))
            .map(|(x, y)| buffer[(x, y)].symbol().to_string())
            .collect();
        assert!(!text.contains("Show help"));
    }
}
