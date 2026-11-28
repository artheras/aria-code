//! Zero-dependency OOXML text & formula extractor for `.xlsx`, `.xlsm`, `.docx`, and `.pptx`.
//!
//! Uses `flate2::read::DeflateDecoder` (already in `aria-native`) to read standard
//! ZIP archives (both STORE method 0 and DEFLATE method 8) without external zip crates.

use flate2::read::DeflateDecoder;
use std::collections::HashMap;
use std::io::Read;

const MAX_XML_ENTRY_BYTES: usize = 8 * 1024 * 1024;

pub fn is_office_extension(ext: &str) -> bool {
    matches!(
        ext.to_ascii_lowercase().as_str(),
        "xlsx" | "xlsm" | "docx" | "pptx"
    )
}

pub fn extract_office_text(bytes: &[u8], ext: &str) -> Result<String, String> {
    let entries = read_zip_entries(bytes)?;
    match ext.to_ascii_lowercase().as_str() {
        "xlsx" | "xlsm" => extract_xlsx(&entries),
        "docx" => {
            let xml = entries
                .get("word/document.xml")
                .ok_or_else(|| "Missing word/document.xml in DOCX archive".to_string())?;
            Ok(extract_word_or_slide_text(xml, "w:p", "w:t"))
        }
        "pptx" => {
            let mut slide_nums: Vec<(usize, &str)> = entries
                .keys()
                .filter_map(|name| {
                    let rest = name
                        .strip_prefix("ppt/slides/slide")?
                        .strip_suffix(".xml")?;
                    let n: usize = rest.parse().ok()?;
                    Some((n, name.as_str()))
                })
                .collect();
            slide_nums.sort_by_key(|(n, _)| *n);
            if slide_nums.is_empty() {
                return Err("PPTX archive contains no slides".into());
            }
            let mut out = String::new();
            for (n, name) in slide_nums {
                if let Some(xml) = entries.get(name) {
                    if !out.is_empty() {
                        out.push('\n');
                    }
                    out.push_str(&format!("[Slide {n}]\n"));
                    out.push_str(&extract_word_or_slide_text(xml, "a:p", "a:t"));
                }
            }
            Ok(out)
        }
        _ => Err(format!("Unsupported Office extension: {ext}")),
    }
}

fn read_u16_le(buf: &[u8], off: usize) -> Option<u16> {
    buf.get(off..off + 2)
        .map(|s| u16::from_le_bytes([s[0], s[1]]))
}

fn read_u32_le(buf: &[u8], off: usize) -> Option<u32> {
    buf.get(off..off + 4)
        .map(|s| u32::from_le_bytes([s[0], s[1], s[2], s[3]]))
}

fn read_zip_entries(bytes: &[u8]) -> Result<HashMap<String, String>, String> {
    // Locate End of Central Directory (EOCD: 0x06054b50) from the end of the file.
    let min_eocd = 22usize;
    if bytes.len() < min_eocd {
        return Err("Invalid or truncated Office ZIP archive".into());
    }
    let search_start = bytes.len().saturating_sub(min_eocd + 65535);
    let mut eocd_off = None;
    for i in (search_start..=bytes.len() - min_eocd).rev() {
        if read_u32_le(bytes, i) == Some(0x0605_4b50) {
            eocd_off = Some(i);
            break;
        }
    }
    let eocd = eocd_off.ok_or_else(|| "Not a valid ZIP/OOXML archive".to_string())?;
    let total_entries = read_u16_le(bytes, eocd + 10).unwrap_or(0) as usize;
    let cd_offset = read_u32_le(bytes, eocd + 16).unwrap_or(0) as usize;
    if total_entries > 10_000 || cd_offset >= bytes.len() {
        return Err("Invalid ZIP central directory bounds".into());
    }

    let mut out = HashMap::new();
    let mut pos = cd_offset;
    for _ in 0..total_entries {
        if read_u32_le(bytes, pos) != Some(0x0201_4b50) {
            break;
        }
        let method = read_u16_le(bytes, pos + 10).unwrap_or(0);
        let comp_size = read_u32_le(bytes, pos + 20).unwrap_or(0) as usize;
        let uncomp_size = read_u32_le(bytes, pos + 24).unwrap_or(0) as usize;
        let name_len = read_u16_le(bytes, pos + 28).unwrap_or(0) as usize;
        let extra_len = read_u16_le(bytes, pos + 30).unwrap_or(0) as usize;
        let comment_len = read_u16_le(bytes, pos + 32).unwrap_or(0) as usize;
        let local_off = read_u32_le(bytes, pos + 42).unwrap_or(0) as usize;

        let name_start = pos + 46;
        let Some(name_bytes) = bytes.get(name_start..name_start + name_len) else {
            break;
        };
        let name = String::from_utf8_lossy(name_bytes).into_owned();
        pos = name_start + name_len + extra_len + comment_len;

        if !name.ends_with(".xml") && !name.ends_with(".rels") {
            continue;
        }
        if uncomp_size > MAX_XML_ENTRY_BYTES {
            return Err(format!("Office XML entry {name} exceeds size limit"));
        }
        if read_u32_le(bytes, local_off) != Some(0x0403_4b50) {
            continue;
        }
        let l_name_len = read_u16_le(bytes, local_off + 26).unwrap_or(0) as usize;
        let l_extra_len = read_u16_le(bytes, local_off + 28).unwrap_or(0) as usize;
        let data_start = local_off + 30 + l_name_len + l_extra_len;
        let Some(comp_slice) = bytes.get(data_start..data_start + comp_size) else {
            continue;
        };

        let xml_bytes = match method {
            0 => comp_slice.to_vec(),
            8 => {
                let decoder = DeflateDecoder::new(comp_slice);
                let mut buf = Vec::new();
                decoder
                    .take((MAX_XML_ENTRY_BYTES + 1) as u64)
                    .read_to_end(&mut buf)
                    .map_err(|e| format!("Failed to inflate {name}: {e}"))?;
                if buf.len() > MAX_XML_ENTRY_BYTES {
                    return Err(format!(
                        "Office XML entry {name} exceeds decompression limit"
                    ));
                }
                buf
            }
            _ => continue,
        };
        if let Ok(text) = String::from_utf8(xml_bytes) {
            out.insert(name, text);
        }
    }
    Ok(out)
}

fn unescape_xml(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

fn extract_attr<'a>(tag_head: &'a str, attr: &str) -> Option<&'a str> {
    let pat_dq = format!("{attr}=\"");
    if let Some(idx) = tag_head.find(&pat_dq) {
        let rest = &tag_head[idx + pat_dq.len()..];
        if let Some(end) = rest.find('"') {
            return Some(&rest[..end]);
        }
    }
    let pat_sq = format!("{attr}='");
    if let Some(idx) = tag_head.find(&pat_sq) {
        let rest = &tag_head[idx + pat_sq.len()..];
        if let Some(end) = rest.find('\'') {
            return Some(&rest[..end]);
        }
    }
    None
}

fn extract_word_or_slide_text(xml: &str, para_tag: &str, text_tag: &str) -> String {
    let mut out = String::new();
    let open_t = format!("<{text_tag}");
    let close_t = format!("</{text_tag}>");
    let close_p = format!("</{para_tag}>");

    let mut pos = 0;
    while pos < xml.len() {
        let next_t = xml[pos..].find(&open_t).map(|i| pos + i);
        let next_p = xml[pos..].find(&close_p).map(|i| pos + i);
        match (next_t, next_p) {
            (Some(ti), Some(pi)) if pi < ti => {
                if !out.ends_with('\n') && !out.is_empty() {
                    out.push('\n');
                }
                pos = pi + close_p.len();
            }
            (Some(ti), _) => {
                let after_open = ti + open_t.len();
                let valid_tag = xml
                    .as_bytes()
                    .get(after_open)
                    .is_some_and(|b| matches!(b, b'>' | b' ' | b'/'));
                if !valid_tag {
                    pos = after_open;
                    continue;
                }
                let Some(gt_rel) = xml[after_open..].find('>') else {
                    break;
                };
                let gt = after_open + gt_rel;
                if xml[ti..=gt].ends_with("/>") {
                    pos = gt + 1;
                    continue;
                }
                let Some(end_rel) = xml[gt + 1..].find(&close_t) else {
                    break;
                };
                let inner = &xml[gt + 1..gt + 1 + end_rel];
                out.push_str(&unescape_xml(inner));
                pos = gt + 1 + end_rel + close_t.len();
            }
            (None, _) => break,
        }
    }
    out.trim_end().to_string()
}

fn col_index(cell_ref: &str) -> Option<usize> {
    let mut col = 0usize;
    let mut found = false;
    for b in cell_ref.bytes() {
        if b == b'$' {
            continue;
        }
        if b.is_ascii_uppercase() {
            col = col * 26 + (b - b'A' + 1) as usize;
            found = true;
        } else if b.is_ascii_lowercase() {
            col = col * 26 + (b - b'a' + 1) as usize;
            found = true;
        } else {
            break;
        }
    }
    if found && col > 0 {
        Some(col - 1)
    } else {
        None
    }
}

fn parse_shared_strings(xml: &str) -> Vec<String> {
    let mut out = Vec::new();
    for chunk in xml.split("<si").skip(1) {
        let Some(gt) = chunk.find('>') else { continue };
        let body = match chunk[gt + 1..].find("</si>") {
            Some(end) => &chunk[gt + 1..gt + 1 + end],
            None => &chunk[gt + 1..],
        };
        let mut item = String::new();
        let mut p = 0;
        while let Some(rel) = body[p..].find("<t") {
            let ti = p + rel;
            let after = ti + 2;
            if !body
                .as_bytes()
                .get(after)
                .is_some_and(|b| matches!(b, b'>' | b' ' | b'/'))
            {
                p = after;
                continue;
            }
            let Some(t_gt_rel) = body[after..].find('>') else {
                break;
            };
            let t_gt = after + t_gt_rel;
            if body[ti..=t_gt].ends_with("/>") {
                p = t_gt + 1;
                continue;
            }
            let Some(end_rel) = body[t_gt + 1..].find("</t>") else {
                break;
            };
            item.push_str(&unescape_xml(&body[t_gt + 1..t_gt + 1 + end_rel]));
            p = t_gt + 1 + end_rel + 4;
        }
        out.push(item);
    }
    out
}

fn tag_inner<'a>(xml: &'a str, tag: &str) -> Option<&'a str> {
    let open = format!("<{tag}");
    let close = format!("</{tag}>");
    let mut p = 0;
    while let Some(rel) = xml[p..].find(&open) {
        let idx = p + rel;
        let after = idx + open.len();
        if !xml
            .as_bytes()
            .get(after)
            .is_some_and(|b| matches!(b, b'>' | b' ' | b'/'))
        {
            p = after;
            continue;
        }
        let gt = after + xml[after..].find('>')?;
        if xml[idx..=gt].ends_with("/>") {
            return Some("");
        }
        let end = gt + 1 + xml[gt + 1..].find(&close)?;
        return Some(&xml[gt + 1..end]);
    }
    None
}

fn extract_xlsx(entries: &HashMap<String, String>) -> Result<String, String> {
    let shared = entries
        .get("xl/sharedStrings.xml")
        .map(|s| parse_shared_strings(s))
        .unwrap_or_default();

    // Map rId -> target from xl/_rels/workbook.xml.rels
    let mut rel_targets = HashMap::new();
    if let Some(rels_xml) = entries.get("xl/_rels/workbook.xml.rels") {
        for part in rels_xml.split("<Relationship").skip(1) {
            let head = part.split('>').next().unwrap_or("");
            if let (Some(id), Some(target)) =
                (extract_attr(head, "Id"), extract_attr(head, "Target"))
            {
                let norm = if let Some(stripped) = target.strip_prefix('/') {
                    stripped.to_string()
                } else {
                    format!("xl/{}", target.trim_start_matches("./"))
                };
                rel_targets.insert(id.to_string(), norm);
            }
        }
    }

    let mut sheets: Vec<(String, String)> = Vec::new();
    if let Some(wb_xml) = entries.get("xl/workbook.xml") {
        for part in wb_xml.split("<sheet ").skip(1) {
            let head = part.split('>').next().unwrap_or("");
            let name = extract_attr(head, "name")
                .map(unescape_xml)
                .unwrap_or_else(|| format!("Sheet{}", sheets.len() + 1));
            let rid = extract_attr(head, "r:id").or_else(|| extract_attr(head, "id"));
            if let Some(rid) = rid {
                if let Some(target) = rel_targets.get(rid) {
                    sheets.push((name, target.clone()));
                }
            }
        }
    }
    if sheets.is_empty() {
        let mut fallback: Vec<String> = entries
            .keys()
            .filter(|k| k.starts_with("xl/worksheets/sheet") && k.ends_with(".xml"))
            .cloned()
            .collect();
        fallback.sort();
        for (idx, path) in fallback.into_iter().enumerate() {
            sheets.push((format!("Sheet{}", idx + 1), path));
        }
    }
    if sheets.is_empty() {
        return Err("Excel workbook contains no worksheets".into());
    }

    let mut out = String::new();
    for (idx, (sheet_name, target)) in sheets.iter().enumerate() {
        let Some(sheet_xml) = entries.get(target) else {
            continue;
        };
        let mut rows: Vec<Vec<String>> = Vec::new();
        let mut formulas: Vec<String> = Vec::new();
        let mut formula_count = 0usize;

        for row_chunk in sheet_xml.split("<row").skip(1) {
            let Some(r_gt) = row_chunk.find('>') else {
                continue;
            };
            if row_chunk[..=r_gt].ends_with("/>") {
                continue;
            }
            let row_body = match row_chunk[r_gt + 1..].find("</row>") {
                Some(end) => &row_chunk[r_gt + 1..r_gt + 1 + end],
                None => &row_chunk[r_gt + 1..],
            };
            let mut row_vals: Vec<String> = Vec::new();
            let mut next_col = 0usize;
            let mut non_empty = false;

            for cell_chunk in row_body.split("<c").skip(1) {
                if !cell_chunk
                    .as_bytes()
                    .first()
                    .is_some_and(|b| matches!(b, b' ' | b'>' | b'/'))
                {
                    continue;
                }
                let Some(c_gt) = cell_chunk.find('>') else {
                    continue;
                };
                let c_head = &cell_chunk[..c_gt];
                let cell_ref = extract_attr(c_head, "r").unwrap_or("");
                let cell_type = extract_attr(c_head, "t").unwrap_or("");
                let col = col_index(cell_ref).unwrap_or(next_col);
                next_col = col + 1;
                if col > 512 {
                    continue;
                }
                while row_vals.len() <= col {
                    row_vals.push(String::new());
                }
                if cell_chunk[..=c_gt].ends_with("/>") {
                    continue;
                }
                let c_body = match cell_chunk[c_gt + 1..].find("</c>") {
                    Some(end) => &cell_chunk[c_gt + 1..c_gt + 1 + end],
                    None => &cell_chunk[c_gt + 1..],
                };
                let formula = tag_inner(c_body, "f").map(unescape_xml).unwrap_or_default();
                if !formula.trim().is_empty() {
                    formula_count += 1;
                    if formulas.len() < 10 {
                        formulas.push(format!(
                            "{}={}",
                            if cell_ref.is_empty() { "?" } else { cell_ref },
                            formula.trim().trim_start_matches('=')
                        ));
                    }
                }
                let raw_v = tag_inner(c_body, "v").map(str::trim).unwrap_or("");
                let val = match cell_type {
                    "s" => raw_v
                        .parse::<usize>()
                        .ok()
                        .and_then(|i| shared.get(i).cloned())
                        .unwrap_or_default(),
                    "inlineStr" => tag_inner(c_body, "is")
                        .and_then(|is_xml| tag_inner(is_xml, "t"))
                        .map(unescape_xml)
                        .unwrap_or_default(),
                    "b" => {
                        if raw_v == "1" || raw_v.eq_ignore_ascii_case("true") {
                            "TRUE".into()
                        } else {
                            "FALSE".into()
                        }
                    }
                    _ => unescape_xml(raw_v),
                };
                let final_val = if val.is_empty() && !formula.trim().is_empty() {
                    format!("={}", formula.trim().trim_start_matches('='))
                } else {
                    val
                };
                if !final_val.trim().is_empty() {
                    non_empty = true;
                }
                row_vals[col] = final_val;
            }
            if non_empty {
                rows.push(row_vals);
            }
        }

        if rows.is_empty() && formula_count == 0 {
            continue;
        }
        if idx > 0 && !out.is_empty() {
            out.push('\n');
        }
        out.push_str(&format!(
            "[Sheet {}: {} ({} rows)]\n",
            idx + 1,
            sheet_name,
            rows.len()
        ));
        if formula_count > 0 {
            out.push_str(&format!(
                "[Formulas: {} cells; sample: {}]\n",
                formula_count,
                formulas.join("; ")
            ));
        }
        for r in rows {
            out.push_str(&r.join("\t"));
            out.push('\n');
        }
    }
    if out.trim().is_empty() {
        return Err("Excel workbook has no extractable cell values".into());
    }
    Ok(out)
}
