//! Native Rust Markdown -> styled HTML / PDF report exporter.
//!
//! Uses `pulldown-cmark` (already compiled into `aria-native`) to render
//! clean, print-ready HTML reports and optionally convert them to PDF via
//! system headless Chrome/Chromium (`--headless --print-to-pdf`).

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};

const REPORT_CSS: &str = r#"
  @page { size: A4; margin: 18mm 16mm; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif; color: #111827; line-height: 1.6; max-width: 860px; margin: 32px auto; padding: 0 20px; }
  h1, h2, h3 { color: #0f172a; break-after: avoid; }
  h1 { border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px; }
  th { background: #1e293b; color: #ffffff; text-align: left; padding: 8px 10px; }
  td { border-bottom: 1px solid #e5e7eb; padding: 7px 10px; }
  tr:nth-child(even) td { background: #f8fafc; }
  pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #f1f5f9; border-radius: 4px; }
  pre { padding: 12px; overflow-x: auto; }
  blockquote { border-left: 3px solid #3b82f6; margin: 12px 0; padding: 6px 12px; background: #eff6ff; color: #1e3a8a; }
  footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #6b7280; }
"#;

fn escape_html(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn push_events_html(out: &mut String, parser: Parser<'_>) {
    let mut in_table_head = false;
    for ev in parser {
        match ev {
            Event::Start(tag) => match tag {
                Tag::Paragraph => out.push_str("<p>"),
                Tag::Heading { level, .. } => out.push_str(&format!("<{level}>")),
                Tag::BlockQuote(_) => out.push_str("<blockquote>"),
                Tag::CodeBlock(_) => out.push_str("<pre><code>"),
                Tag::List(Some(start)) => out.push_str(&format!("<ol start=\"{start}\">")),
                Tag::List(None) => out.push_str("<ul>"),
                Tag::Item => out.push_str("<li>"),
                Tag::Table(_) => out.push_str("<table>"),
                Tag::TableHead => {
                    in_table_head = true;
                    out.push_str("<thead><tr>");
                }
                Tag::TableRow => out.push_str("<tr>"),
                Tag::TableCell => out.push_str(if in_table_head { "<th>" } else { "<td>" }),
                Tag::Emphasis => out.push_str("<em>"),
                Tag::Strong => out.push_str("<strong>"),
                Tag::Strikethrough => out.push_str("<del>"),
                Tag::Link { dest_url, .. } => {
                    out.push_str(&format!("<a href=\"{}\">", escape_html(&dest_url)));
                }
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Paragraph => out.push_str("</p>\n"),
                TagEnd::Heading(level) => out.push_str(&format!("</{level}>\n")),
                TagEnd::BlockQuote(_) => out.push_str("</blockquote>\n"),
                TagEnd::CodeBlock => out.push_str("</code></pre>\n"),
                TagEnd::List(ordered) => out.push_str(if ordered { "</ol>\n" } else { "</ul>\n" }),
                TagEnd::Item => out.push_str("</li>\n"),
                TagEnd::Table => out.push_str("</tbody></table>\n"),
                TagEnd::TableHead => {
                    in_table_head = false;
                    out.push_str("</tr></thead><tbody>\n");
                }
                TagEnd::TableRow => out.push_str("</tr>\n"),
                TagEnd::TableCell => out.push_str(if in_table_head { "</th>" } else { "</td>" }),
                TagEnd::Emphasis => out.push_str("</em>"),
                TagEnd::Strong => out.push_str("</strong>"),
                TagEnd::Strikethrough => out.push_str("</del>"),
                TagEnd::Link => out.push_str("</a>"),
                _ => {}
            },
            Event::Text(t) => out.push_str(&escape_html(&t)),
            Event::Code(c) => out.push_str(&format!("<code>{}</code>", escape_html(&c))),
            Event::SoftBreak => out.push('\n'),
            Event::HardBreak => out.push_str("<br />\n"),
            Event::Rule => out.push_str("<hr />\n"),
            _ => {}
        }
    }
}

/// Convert Markdown text to a self-contained print-ready HTML document.
pub fn render_markdown_html(markdown: &str, title: Option<&str>, zh: bool) -> String {
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_TABLES);
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    opts.insert(Options::ENABLE_TASKLISTS);

    let parser = Parser::new_ext(markdown, opts);
    let mut body_html = String::new();
    push_events_html(&mut body_html, parser);

    let doc_title = escape_html(title.unwrap_or("Aria Report"));
    let footer_text = if zh {
        "本报告由 Aria 原生 Rust 渲染管线生成 · 仅供参考"
    } else {
        "Generated by Aria native Rust render pipeline · For reference only"
    };

    format!(
        "<!doctype html>\n<html lang=\"{lang}\">\n<head>\n  <meta charset=\"utf-8\" />\n  <title>{doc_title}</title>\n  <style>{REPORT_CSS}</style>\n</head>\n<body>\n{body_html}\n<footer>{footer_text}</footer>\n</body>\n</html>\n",
        lang = if zh { "zh" } else { "en" }
    )
}

fn find_headless_chrome() -> Option<PathBuf> {
    let candidates: &[&str] = &[
        #[cfg(target_os = "macos")]
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        #[cfg(target_os = "macos")]
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        #[cfg(target_os = "linux")]
        "/usr/bin/google-chrome",
        #[cfg(target_os = "linux")]
        "/usr/bin/chromium",
        #[cfg(target_os = "linux")]
        "/usr/bin/chromium-browser",
    ];
    for c in candidates {
        let p = Path::new(c);
        if p.is_file() {
            return Some(p.to_path_buf());
        }
    }
    None
}

pub fn export_markdown_report(
    workspace: &Path,
    input_path: &Path,
    out_path: &Path,
    as_pdf: bool,
    zh: bool,
) -> Result<PathBuf, String> {
    let abs_in = if input_path.is_absolute() {
        input_path.to_path_buf()
    } else {
        workspace.join(input_path)
    };
    let md = fs::read_to_string(&abs_in)
        .map_err(|e| format!("failed to read {}: {e}", abs_in.display()))?;
    let title = abs_in
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("Aria Report");
    let html_doc = render_markdown_html(&md, Some(title), zh);

    let abs_out = if out_path.is_absolute() {
        out_path.to_path_buf()
    } else {
        workspace.join(out_path)
    };
    if let Some(parent) = abs_out.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed to create output dir: {e}"))?;
    }

    if !as_pdf {
        fs::write(&abs_out, html_doc)
            .map_err(|e| format!("failed to write {}: {e}", abs_out.display()))?;
        return Ok(abs_out);
    }

    let chrome = find_headless_chrome()
        .ok_or_else(|| "Headless Chrome/Chromium not found; use HTML export instead".to_owned())?;
    let tmp_dir = tempfile::tempdir().map_err(|e| format!("tempdir failed: {e}"))?;
    let tmp_html = tmp_dir.path().join("report.html");
    fs::write(&tmp_html, html_doc).map_err(|e| format!("write temp html failed: {e}"))?;

    let pdf_arg = format!("--print-to-pdf={}", abs_out.display());
    let file_url = format!("file://{}", tmp_html.display());
    let out = Command::new(chrome)
        .args([
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            &pdf_arg,
            &file_url,
        ])
        .output()
        .map_err(|e| format!("failed to launch headless Chrome: {e}"))?;
    if !out.status.success() || !abs_out.is_file() {
        return Err(format!(
            "Chrome PDF export failed: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(abs_out)
}

pub fn handle_export_report_command(
    workspace: &Path,
    arg: &str,
    allow_write: bool,
    zh: bool,
) -> (String, &'static str) {
    if !allow_write {
        return (
            if zh {
                "当前处于只读模式；请先开启 `/write-mode on` 再导出报告。".to_owned()
            } else {
                "Read-only mode active; enable `/write-mode on` before exporting reports."
                    .to_owned()
            },
            "failed",
        );
    }
    let tokens: Vec<&str> = arg.split_whitespace().collect();
    let Some(first) = tokens.first() else {
        return (
            if zh {
                "用法: `/export-report <input.md> [--pdf|--html]`".to_owned()
            } else {
                "Usage: `/export-report <input.md> [--pdf|--html]`".to_owned()
            },
            "completed",
        );
    };
    let as_pdf = tokens.contains(&"--pdf");
    let in_path = Path::new(first);
    let ext = if as_pdf { "pdf" } else { "html" };
    let stem = in_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("report");
    let out_path = workspace
        .join(".aria")
        .join("exports")
        .join(format!("{stem}.{ext}"));

    match export_markdown_report(workspace, in_path, &out_path, as_pdf, zh) {
        Ok(written) => (
            if zh {
                format!("✓ 已导出原生报告: `{}`", written.display())
            } else {
                format!("✓ Exported native report to `{}`", written.display())
            },
            "completed",
        ),
        Err(err) => (err, "failed"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_markdown_table_and_exports_html_report() {
        let md = "# Q3 Summary\n\n| Metric | Value |\n| --- | --- |\n| Margin | 24% |\n";
        let html_out = render_markdown_html(md, Some("Q3 Summary"), false);
        assert!(html_out.contains("<h1>Q3 Summary</h1>"));
        assert!(html_out.contains("<table>"));
        assert!(html_out.contains("<th>Metric</th>"));
        assert!(html_out.contains("<td>24%</td>"));

        let dir = tempfile::tempdir().expect("tempdir");
        let in_file = dir.path().join("summary.md");
        fs::write(&in_file, md).expect("write md");
        let (msg, status) =
            handle_export_report_command(dir.path(), "summary.md --html", true, false);
        assert_eq!(status, "completed");
        assert!(msg.contains("summary.html"));
        let exported = dir
            .path()
            .join(".aria")
            .join("exports")
            .join("summary.html");
        assert!(exported.is_file());
    }
}
