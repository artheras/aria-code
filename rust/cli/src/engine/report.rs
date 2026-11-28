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
                "用法: `/export-report <input.md> [--pdf|--html|--docx|--pptx|--xlsx]`".to_owned()
            } else {
                "Usage: `/export-report <input.md> [--pdf|--html|--docx|--pptx|--xlsx]`".to_owned()
            },
            "completed",
        );
    };
    let as_pdf = tokens.contains(&"--pdf");
    let as_docx = tokens.contains(&"--docx");
    let as_pptx = tokens.contains(&"--pptx");
    let as_xlsx = tokens.contains(&"--xlsx");
    let in_path = Path::new(first);
    let ext = if as_xlsx {
        "xlsx"
    } else if as_pptx {
        "pptx"
    } else if as_docx {
        "docx"
    } else if as_pdf {
        "pdf"
    } else {
        "html"
    };
    let stem = in_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("report");
    let out_path = workspace
        .join(".aria")
        .join("exports")
        .join(format!("{stem}.{ext}"));

    let result = if as_xlsx {
        export_markdown_xlsx(workspace, in_path, &out_path)
    } else if as_pptx {
        export_markdown_pptx(workspace, in_path, &out_path)
    } else if as_docx {
        export_markdown_docx(workspace, in_path, &out_path)
    } else {
        export_markdown_report(workspace, in_path, &out_path, as_pdf, zh)
    };
    match result {
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

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

pub fn render_markdown_docx_xml(md: &str) -> String {
    let mut body = String::new();
    let mut cur_text = String::new();
    let mut cur_heading: Option<u8> = None;
    let mut in_list_item = false;
    let mut in_para = false;

    let flush_para = |body: &mut String, text: &mut String, style: Option<&str>| {
        let t = text.trim();
        if !t.is_empty() {
            body.push_str("<w:p>");
            if let Some(st) = style {
                body.push_str(&format!("<w:pPr><w:pStyle w:val=\"{st}\"/></w:pPr>"));
            }
            body.push_str("<w:r><w:t xml:space=\"preserve\">");
            body.push_str(&xml_escape(t));
            body.push_str("</w:t></w:r></w:p>");
        }
        text.clear();
    };

    let mut in_table = false;
    let mut table_rows: Vec<Vec<String>> = Vec::new();
    let mut cur_row: Vec<String> = Vec::new();
    let mut in_cell = false;

    for ev in Parser::new_ext(md, Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH) {
        match ev {
            Event::Start(Tag::Heading { level, .. }) => {
                cur_heading = Some(level as u8);
                cur_text.clear();
            }
            Event::End(TagEnd::Heading(_)) => {
                let lvl = cur_heading.take().unwrap_or(1).clamp(1, 4);
                let style = format!("Heading{lvl}");
                flush_para(&mut body, &mut cur_text, Some(&style));
            }
            Event::Start(Tag::Table(_)) => {
                in_table = true;
                table_rows.clear();
            }
            Event::End(TagEnd::Table) => {
                in_table = false;
                if !table_rows.is_empty() {
                    let cols = table_rows.iter().map(|r| r.len()).max().unwrap_or(1).max(1);
                    body.push_str("<w:tbl><w:tblPr><w:tblW w:w=\"5000\" w:type=\"pct\"/></w:tblPr><w:tblGrid>");
                    for _ in 0..cols {
                        body.push_str(&format!("<w:gridCol w:w=\"{}\"/>", 9638 / cols));
                    }
                    body.push_str("</w:tblGrid>");
                    for (r_idx, row) in table_rows.iter().enumerate() {
                        body.push_str("<w:tr>");
                        for cell in row {
                            let shade = if r_idx == 0 {
                                "<w:tcPr><w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"1F2937\"/></w:tcPr>"
                            } else {
                                ""
                            };
                            let rpr = if r_idx == 0 {
                                "<w:rPr><w:b/><w:color w:val=\"FFFFFF\"/></w:rPr>"
                            } else {
                                ""
                            };
                            body.push_str(&format!(
                                "<w:tc>{shade}<w:p><w:r>{rpr}<w:t xml:space=\"preserve\">{}</w:t></w:r></w:p></w:tc>",
                                xml_escape(cell)
                            ));
                        }
                        body.push_str("</w:tr>");
                    }
                    body.push_str("</w:tbl><w:p/>");
                }
                table_rows.clear();
            }
            Event::Start(Tag::TableHead) | Event::Start(Tag::TableRow) => {
                cur_row.clear();
            }
            Event::End(TagEnd::TableHead) | Event::End(TagEnd::TableRow) => {
                if !cur_row.is_empty() {
                    table_rows.push(std::mem::take(&mut cur_row));
                }
            }
            Event::Start(Tag::TableCell) => {
                in_cell = true;
                cur_text.clear();
            }
            Event::End(TagEnd::TableCell) => {
                in_cell = false;
                cur_row.push(cur_text.trim().to_owned());
                cur_text.clear();
            }
            Event::Start(Tag::Item) => {
                in_list_item = true;
                cur_text.push_str("• ");
            }
            Event::End(TagEnd::Item) => {
                in_list_item = false;
                flush_para(&mut body, &mut cur_text, None);
            }
            Event::Start(Tag::Paragraph) => {
                in_para = true;
            }
            Event::End(TagEnd::Paragraph) => {
                in_para = false;
                if !in_list_item && cur_heading.is_none() && !in_table {
                    flush_para(&mut body, &mut cur_text, None);
                }
            }
            Event::Text(t) | Event::Code(t) => {
                if cur_heading.is_some() || in_list_item || in_para || in_cell {
                    cur_text.push_str(&t);
                }
            }
            Event::SoftBreak | Event::HardBreak if !cur_text.is_empty() => {
                cur_text.push(' ');
            }
            _ => {}
        }
    }
    flush_para(&mut body, &mut cur_text, None);

    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">\
<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
}

fn write_store_zip(entries: &[(&str, &[u8])]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut central = Vec::new();

    for &(name, data) in entries {
        let name_bytes = name.as_bytes();
        let mut crc = flate2::Crc::new();
        crc.update(data);
        let sum = crc.sum();
        let size = data.len() as u32;
        let offset = out.len() as u32;

        // Local file header
        out.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes()); // version needed
        out.extend_from_slice(&0u16.to_le_bytes()); // flags
        out.extend_from_slice(&0u16.to_le_bytes()); // method: STORE
        out.extend_from_slice(&0u16.to_le_bytes()); // mod time
        out.extend_from_slice(&0u16.to_le_bytes()); // mod date
        out.extend_from_slice(&sum.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // extra len
        out.extend_from_slice(name_bytes);
        out.extend_from_slice(data);

        // Central directory header
        central.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        central.extend_from_slice(&20u16.to_le_bytes()); // version made by
        central.extend_from_slice(&20u16.to_le_bytes()); // version needed
        central.extend_from_slice(&0u16.to_le_bytes()); // flags
        central.extend_from_slice(&0u16.to_le_bytes()); // method: STORE
        central.extend_from_slice(&0u16.to_le_bytes()); // mod time
        central.extend_from_slice(&0u16.to_le_bytes()); // mod date
        central.extend_from_slice(&sum.to_le_bytes());
        central.extend_from_slice(&size.to_le_bytes());
        central.extend_from_slice(&size.to_le_bytes());
        central.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
        central.extend_from_slice(&0u16.to_le_bytes()); // extra len
        central.extend_from_slice(&0u16.to_le_bytes()); // comment len
        central.extend_from_slice(&0u16.to_le_bytes()); // disk number
        central.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
        central.extend_from_slice(&0u32.to_le_bytes()); // external attrs
        central.extend_from_slice(&offset.to_le_bytes());
        central.extend_from_slice(name_bytes);
    }

    let cd_offset = out.len() as u32;
    let cd_size = central.len() as u32;
    let count = entries.len() as u16;
    out.extend_from_slice(&central);

    // End of central directory
    out.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes()); // disk
    out.extend_from_slice(&0u16.to_le_bytes()); // cd disk
    out.extend_from_slice(&count.to_le_bytes());
    out.extend_from_slice(&count.to_le_bytes());
    out.extend_from_slice(&cd_size.to_le_bytes());
    out.extend_from_slice(&cd_offset.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes()); // comment len
    out
}

pub fn export_markdown_docx(
    workspace: &Path,
    in_path: &Path,
    out_path: &Path,
) -> Result<PathBuf, String> {
    let abs_in = if in_path.is_absolute() {
        in_path.to_path_buf()
    } else {
        workspace.join(in_path)
    };
    let md = fs::read_to_string(&abs_in)
        .map_err(|e| format!("failed to read {}: {e}", abs_in.display()))?;
    let doc_xml = render_markdown_docx_xml(&md);

    let content_types = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"#;

    let rels = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"#;

    let zip_bytes = write_store_zip(&[
        ("[Content_Types].xml", content_types.as_bytes()),
        ("_rels/.rels", rels.as_bytes()),
        ("word/document.xml", doc_xml.as_bytes()),
    ]);

    let abs_out = if out_path.is_absolute() {
        out_path.to_path_buf()
    } else {
        workspace.join(out_path)
    };
    if let Some(parent) = abs_out.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed to create output dir: {e}"))?;
    }
    fs::write(&abs_out, zip_bytes)
        .map_err(|e| format!("failed to write {}: {e}", abs_out.display()))?;
    Ok(abs_out)
}

fn split_pptx_slides(md: &str) -> Vec<(String, Vec<String>)> {
    let mut slides: Vec<(String, Vec<String>)> = Vec::new();
    let mut cur_title = String::from("Report");
    let mut cur_lines: Vec<String> = Vec::new();
    let mut cur_text = String::new();
    let mut in_heading = false;
    let mut in_item = false;
    let mut in_para = false;

    for ev in Parser::new_ext(md, Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH) {
        match ev {
            Event::Start(Tag::Heading { .. }) => {
                in_heading = true;
                cur_text.clear();
            }
            Event::End(TagEnd::Heading(_)) => {
                in_heading = false;
                let heading = cur_text.trim().to_owned();
                cur_text.clear();
                if !heading.is_empty() {
                    if !cur_lines.is_empty() || cur_title != "Report" {
                        slides.push((cur_title.clone(), std::mem::take(&mut cur_lines)));
                    }
                    cur_title = heading;
                }
            }
            Event::Start(Tag::Item) => {
                in_item = true;
                cur_text.clear();
            }
            Event::End(TagEnd::Item) => {
                in_item = false;
                let line = cur_text.trim().to_owned();
                cur_text.clear();
                if !line.is_empty() {
                    cur_lines.push(format!("• {line}"));
                }
            }
            Event::Start(Tag::Paragraph) => {
                in_para = true;
            }
            Event::End(TagEnd::Paragraph) => {
                in_para = false;
                if !in_item && !in_heading {
                    let line = cur_text.trim().to_owned();
                    cur_text.clear();
                    if !line.is_empty() {
                        cur_lines.push(line);
                    }
                }
            }
            Event::Text(t) | Event::Code(t) => {
                if in_heading || in_item || in_para {
                    cur_text.push_str(&t);
                }
            }
            Event::SoftBreak | Event::HardBreak if !cur_text.is_empty() => {
                cur_text.push(' ');
            }
            _ => {}
        }
    }
    if !cur_lines.is_empty() || slides.is_empty() {
        slides.push((cur_title, cur_lines));
    }
    slides
}

fn render_slide_xml(title: &str, lines: &[String]) -> String {
    let mut body_paras = String::new();
    for line in lines {
        body_paras.push_str(&format!(
            "<a:p><a:r><a:rPr lang=\"en-US\" sz=\"1800\"/><a:t>{}</a:t></a:r></a:p>",
            xml_escape(line)
        ));
    }
    if body_paras.is_empty() {
        body_paras.push_str("<a:p><a:endParaRPr lang=\"en-US\"/></a:p>");
    }
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
<p:sld xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">\
<p:cSld><p:spTree>\
<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>\
<p:sp><p:nvSpPr><p:cNvPr id=\"2\" name=\"Title\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>\
<p:spPr><a:xfrm><a:off x=\"457200\" y=\"274638\"/><a:ext cx=\"8229600\" cy=\"1143000\"/></a:xfrm></p:spPr>\
<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang=\"en-US\" sz=\"3200\" b=\"1\"/><a:t>{}</a:t></a:r></a:p></p:txBody></p:sp>\
<p:sp><p:nvSpPr><p:cNvPr id=\"3\" name=\"Content\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>\
<p:spPr><a:xfrm><a:off x=\"457200\" y=\"1600200\"/><a:ext cx=\"8229600\" cy=\"4525963\"/></a:xfrm></p:spPr>\
<p:txBody><a:bodyPr/><a:lstStyle/>{body_paras}</p:txBody></p:sp>\
</p:spTree></p:cSld></p:sld>",
        xml_escape(title)
    )
}

pub fn export_markdown_pptx(
    workspace: &Path,
    in_path: &Path,
    out_path: &Path,
) -> Result<PathBuf, String> {
    let abs_in = if in_path.is_absolute() {
        in_path.to_path_buf()
    } else {
        workspace.join(in_path)
    };
    let md = fs::read_to_string(&abs_in)
        .map_err(|e| format!("failed to read {}: {e}", abs_in.display()))?;
    let slides = split_pptx_slides(&md);

    let mut overrides = String::from(
        "  <Override PartName=\"/ppt/presentation.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml\"/>\n",
    );
    let mut pres_rels = String::new();
    let mut sld_id_lst = String::new();
    let mut slide_files: Vec<(String, Vec<u8>)> = Vec::with_capacity(slides.len());

    for (idx, (title, lines)) in slides.iter().enumerate() {
        let n = idx + 1;
        let path = format!("ppt/slides/slide{n}.xml");
        overrides.push_str(&format!(
            "  <Override PartName=\"/{path}\" ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.slide+xml\"/>\n"
        ));
        pres_rels.push_str(&format!(
            "  <Relationship Id=\"rId{n}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide\" Target=\"slides/slide{n}.xml\"/>\n"
        ));
        sld_id_lst.push_str(&format!("<p:sldId id=\"{}\" r:id=\"rId{n}\"/>", 256 + idx));
        slide_files.push((path, render_slide_xml(title, lines).into_bytes()));
    }

    let content_types = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n\
<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">\n\
  <Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>\n\
  <Default Extension=\"xml\" ContentType=\"application/xml\"/>\n\
{overrides}</Types>"
    );
    let root_rels = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"#;
    let pres_xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
<p:presentation xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">\
<p:sldIdLst>{sld_id_lst}</p:sldIdLst><p:sldSz cx=\"12192000\" cy=\"6858000\"/></p:presentation>"
    );
    let pres_rels_xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n\
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">\n\
{pres_rels}</Relationships>"
    );

    let mut entries: Vec<(&str, &[u8])> = vec![
        ("[Content_Types].xml", content_types.as_bytes()),
        ("_rels/.rels", root_rels.as_bytes()),
        ("ppt/presentation.xml", pres_xml.as_bytes()),
        ("ppt/_rels/presentation.xml.rels", pres_rels_xml.as_bytes()),
    ];
    for (path, bytes) in &slide_files {
        entries.push((path.as_str(), bytes.as_slice()));
    }
    let zip_bytes = write_store_zip(&entries);

    let abs_out = if out_path.is_absolute() {
        out_path.to_path_buf()
    } else {
        workspace.join(out_path)
    };
    if let Some(parent) = abs_out.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed to create output dir: {e}"))?;
    }
    fs::write(&abs_out, zip_bytes)
        .map_err(|e| format!("failed to write {}: {e}", abs_out.display()))?;
    Ok(abs_out)
}

fn xlsx_col_ref(col: usize) -> String {
    let mut n = col + 1;
    let mut chars = Vec::new();
    while n > 0 {
        n -= 1;
        chars.push((b'A' + (n % 26) as u8) as char);
        n /= 26;
    }
    chars.into_iter().rev().collect()
}

/// Export GFM tables (or summary sections) from Markdown into a valid `.xlsx` workbook.
pub fn export_markdown_xlsx(
    workspace: &Path,
    in_path: &Path,
    out_path: &Path,
) -> Result<PathBuf, String> {
    let abs_in = if in_path.is_absolute() {
        in_path.to_path_buf()
    } else {
        workspace.join(in_path)
    };
    let md = fs::read_to_string(&abs_in)
        .map_err(|e| format!("failed to read {}: {e}", abs_in.display()))?;

    let mut sheets: Vec<(String, Vec<Vec<String>>)> = Vec::new();
    let mut cur_heading = String::from("Summary");
    let mut cur_text = String::new();
    let mut in_heading = false;
    let mut table_rows: Vec<Vec<String>> = Vec::new();
    let mut cur_row: Vec<String> = Vec::new();
    let mut in_cell = false;

    for ev in Parser::new_ext(&md, Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH) {
        match ev {
            Event::Start(Tag::Heading { .. }) => {
                in_heading = true;
                cur_text.clear();
            }
            Event::End(TagEnd::Heading(_)) => {
                in_heading = false;
                let h = cur_text.trim().to_owned();
                if !h.is_empty() {
                    cur_heading = h;
                }
                cur_text.clear();
            }
            Event::Start(Tag::Table(_)) => {
                table_rows.clear();
            }
            Event::End(TagEnd::Table) => {
                if !table_rows.is_empty() {
                    let clean_name: String = cur_heading
                        .chars()
                        .filter(|c| !matches!(c, ':' | '\\' | '/' | '?' | '*' | '[' | ']'))
                        .take(28)
                        .collect();
                    let name = if clean_name.trim().is_empty() {
                        format!("Sheet{}", sheets.len() + 1)
                    } else {
                        clean_name.trim().to_owned()
                    };
                    sheets.push((name, std::mem::take(&mut table_rows)));
                }
            }
            Event::Start(Tag::TableHead) | Event::Start(Tag::TableRow) => {
                cur_row.clear();
            }
            Event::End(TagEnd::TableHead) | Event::End(TagEnd::TableRow) => {
                if !cur_row.is_empty() {
                    table_rows.push(std::mem::take(&mut cur_row));
                }
            }
            Event::Start(Tag::TableCell) => {
                in_cell = true;
                cur_text.clear();
            }
            Event::End(TagEnd::TableCell) => {
                in_cell = false;
                cur_row.push(cur_text.trim().to_owned());
                cur_text.clear();
            }
            Event::Text(t) | Event::Code(t) => {
                if in_heading || in_cell {
                    cur_text.push_str(&t);
                }
            }
            _ => {}
        }
    }

    if sheets.is_empty() {
        let mut fallback_rows = vec![vec!["Line".to_owned(), "Content".to_owned()]];
        for (i, line) in md.lines().filter(|l| !l.trim().is_empty()).enumerate() {
            fallback_rows.push(vec![(i + 1).to_string(), line.trim().to_owned()]);
        }
        sheets.push(("Summary".to_owned(), fallback_rows));
    }

    let mut shared: Vec<String> = Vec::new();
    let mut shared_map = std::collections::HashMap::<String, usize>::new();
    let mut intern = |s: &str| -> usize {
        if let Some(&idx) = shared_map.get(s) {
            return idx;
        }
        let idx = shared.len();
        shared.push(s.to_owned());
        shared_map.insert(s.to_owned(), idx);
        idx
    };

    let mut overrides = String::from(
        "  <Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>\n\
         <Override PartName=\"/xl/styles.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml\"/>\n\
         <Override PartName=\"/xl/sharedStrings.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml\"/>\n",
    );
    let mut wb_sheets = String::new();
    let mut wb_rels = String::new();
    let mut sheet_files: Vec<(String, Vec<u8>)> = Vec::new();

    for (idx, (name, rows)) in sheets.iter().enumerate() {
        let n = idx + 1;
        let path = format!("xl/worksheets/sheet{n}.xml");
        overrides.push_str(&format!(
            "  <Override PartName=\"/{path}\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>\n"
        ));
        wb_sheets.push_str(&format!(
            "<sheet name=\"{}\" sheetId=\"{n}\" r:id=\"rId{n}\"/>",
            xml_escape(name)
        ));
        wb_rels.push_str(&format!(
            "<Relationship Id=\"rId{n}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet{n}.xml\"/>"
        ));

        let mut rows_xml = String::new();
        for (r_idx, row) in rows.iter().enumerate() {
            let r_num = r_idx + 1;
            rows_xml.push_str(&format!("<row r=\"{r_num}\">"));
            for (c_idx, val) in row.iter().enumerate() {
                let cell_ref = format!("{}{}", xlsx_col_ref(c_idx), r_num);
                let trimmed = val.trim();
                let style = if r_idx == 0 { "1" } else { "0" };
                if r_idx > 0 && trimmed.starts_with('=') && trimmed.len() > 1 {
                    rows_xml.push_str(&format!(
                        "<c r=\"{cell_ref}\" s=\"{style}\"><f>{}</f></c>",
                        xml_escape(trimmed.trim_start_matches('='))
                    ));
                } else if r_idx > 0 && trimmed.parse::<f64>().is_ok() {
                    rows_xml.push_str(&format!(
                        "<c r=\"{cell_ref}\" s=\"{style}\"><v>{trimmed}</v></c>"
                    ));
                } else {
                    let s_id = intern(trimmed);
                    rows_xml.push_str(&format!(
                        "<c r=\"{cell_ref}\" s=\"{style}\" t=\"s\"><v>{s_id}</v></c>"
                    ));
                }
            }
            rows_xml.push_str("</row>");
        }
        let sheet_xml = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
             <worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">\
             <sheetData>{rows_xml}</sheetData></worksheet>"
        );
        sheet_files.push((path, sheet_xml.into_bytes()));
    }

    let style_rid = sheets.len() + 1;
    let ss_rid = sheets.len() + 2;
    wb_rels.push_str(&format!(
        "<Relationship Id=\"rId{style_rid}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles\" Target=\"styles.xml\"/>\
         <Relationship Id=\"rId{ss_rid}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings\" Target=\"sharedStrings.xml\"/>"
    ));

    let content_types = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
         <Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">\
         <Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>\
         <Default Extension=\"xml\" ContentType=\"application/xml\"/>\
         {overrides}</Types>"
    );
    let root_rels = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"#;
    let wb_xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
         <workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">\
         <sheets>{wb_sheets}</sheets><calcPr calcId=\"191029\" fullCalcOnLoad=\"1\"/></workbook>"
    );
    let wb_rels_xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
         <Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">{wb_rels}</Relationships>"
    );
    let styles_xml = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs></styleSheet>"#;

    let mut ss_xml = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" count=\"{}\" uniqueCount=\"{}\">",
        shared.len(),
        shared.len()
    );
    for s in &shared {
        ss_xml.push_str(&format!(
            "<si><t xml:space=\"preserve\">{}</t></si>",
            xml_escape(s)
        ));
    }
    ss_xml.push_str("</sst>");

    let mut entries: Vec<(&str, &[u8])> = vec![
        ("[Content_Types].xml", content_types.as_bytes()),
        ("_rels/.rels", root_rels.as_bytes()),
        ("xl/workbook.xml", wb_xml.as_bytes()),
        ("xl/_rels/workbook.xml.rels", wb_rels_xml.as_bytes()),
        ("xl/styles.xml", styles_xml.as_bytes()),
        ("xl/sharedStrings.xml", ss_xml.as_bytes()),
    ];
    for (path, bytes) in &sheet_files {
        entries.push((path.as_str(), bytes.as_slice()));
    }
    let zip_bytes = write_store_zip(&entries);

    let abs_out = if out_path.is_absolute() {
        out_path.to_path_buf()
    } else {
        workspace.join(out_path)
    };
    if let Some(parent) = abs_out.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed to create output dir: {e}"))?;
    }
    fs::write(&abs_out, zip_bytes)
        .map_err(|e| format!("failed to write {}: {e}", abs_out.display()))?;
    Ok(abs_out)
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

    #[test]
    fn exports_valid_docx_archive_from_markdown() {
        let dir = tempfile::tempdir().expect("tempdir");
        let md = "# Weekly Brief\n\nRevenue rose **18%**.\n\n- Margin expanded\n- Cash positive\n";
        fs::write(dir.path().join("brief.md"), md).expect("write md");

        let (msg, status) =
            handle_export_report_command(dir.path(), "brief.md --docx", true, false);
        assert_eq!(status, "completed");
        assert!(msg.contains("brief.docx"));

        let bytes = fs::read(dir.path().join(".aria").join("exports").join("brief.docx"))
            .expect("read docx");
        assert!(bytes.starts_with(b"PK\x03\x04"));
        let raw = String::from_utf8_lossy(&bytes);
        assert!(raw.contains("[Content_Types].xml"));
        assert!(raw.contains("word/document.xml"));
        assert!(raw.contains("Weekly Brief"));
        assert!(raw.contains("Revenue rose 18%."));
    }

    #[test]
    fn exports_valid_pptx_deck_from_markdown() {
        let dir = tempfile::tempdir().expect("tempdir");
        let md = "# Slide One\n\n- Key metric up\n\n# Slide Two\n\nSummary text.\n";
        fs::write(dir.path().join("deck.md"), md).expect("write md");

        let (msg, status) = handle_export_report_command(dir.path(), "deck.md --pptx", true, false);
        assert_eq!(status, "completed");
        assert!(msg.contains("deck.pptx"));

        let bytes = fs::read(dir.path().join(".aria").join("exports").join("deck.pptx"))
            .expect("read pptx");
        assert!(bytes.starts_with(b"PK\x03\x04"));
        let raw = String::from_utf8_lossy(&bytes);
        assert!(raw.contains("ppt/presentation.xml"));
        assert!(raw.contains("ppt/slides/slide1.xml"));
        assert!(raw.contains("ppt/slides/slide2.xml"));
        assert!(raw.contains("Slide One"));
        assert!(raw.contains("Slide Two"));
    }

    #[test]
    fn exports_and_reads_valid_xlsx_workbook_from_markdown() {
        let dir = tempfile::tempdir().expect("tempdir");
        let md = "# Valuation\n\n| Metric | Value |\n| --- | --- |\n| Revenue | 1200 |\n| Growth | =B2*0.1 |\n";
        fs::write(dir.path().join("model.md"), md).expect("write md");

        let (msg, status) =
            handle_export_report_command(dir.path(), "model.md --xlsx", true, false);
        assert_eq!(status, "completed");
        assert!(msg.contains("model.xlsx"));

        let bytes = fs::read(dir.path().join(".aria").join("exports").join("model.xlsx"))
            .expect("read xlsx");
        assert!(bytes.starts_with(b"PK\x03\x04"));
        let extracted =
            crate::engine::office_read::extract_office_text(&bytes, "xlsx").expect("extract xlsx");
        assert!(extracted.contains("Valuation"));
        assert!(extracted.contains("Revenue"));
        assert!(extracted.contains("1200"));
        assert!(extracted.contains("=B2*0.1"));
    }
}
