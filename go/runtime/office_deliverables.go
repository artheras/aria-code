package main

import (
	"archive/zip"
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"
)

type docxBuildOptions struct {
	Title    string
	Subtitle string
	Scope    *scope
}

type pptxBuildOptions struct {
	Title    string
	Subtitle string
	Scope    *scope
}

type embeddedImage struct {
	RelID       string
	Filename    string
	ContentType string
	Ext         string
	Data        []byte
	WidthEMU    int
	HeightEMU   int
}

func parseImageDimensions(data []byte) (int, int, string, string) {
	// PNG: 8-byte signature + IHDR chunk (width at 16..20, height at 20..24)
	if len(data) >= 24 && bytes.Equal(data[:8], []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}) {
		w := int(binary.BigEndian.Uint32(data[16:20]))
		h := int(binary.BigEndian.Uint32(data[20:24]))
		if w > 0 && h > 0 {
			return w, h, "png", "image/png"
		}
	}
	// JPEG: scan markers for SOF0/SOF2 (0xFFC0..0xFFC2)
	if len(data) >= 4 && data[0] == 0xFF && data[1] == 0xD8 {
		i := 2
		for i+9 < len(data) {
			if data[i] != 0xFF {
				i++
				continue
			}
			marker := data[i+1]
			if marker == 0xFF {
				i++
				continue
			}
			if marker == 0xD9 || marker == 0xDA {
				break
			}
			segLen := int(binary.BigEndian.Uint16(data[i+2 : i+4]))
			if segLen < 2 || i+2+segLen > len(data) {
				break
			}
			if marker >= 0xC0 && marker <= 0xC3 {
				h := int(binary.BigEndian.Uint16(data[i+5 : i+7]))
				w := int(binary.BigEndian.Uint16(data[i+7 : i+9]))
				if w > 0 && h > 0 {
					return w, h, "jpeg", "image/jpeg"
				}
			}
			i += 2 + segLen
		}
		return 800, 450, "jpeg", "image/jpeg"
	}
	return 0, 0, "", ""
}

func resolveEmbeddedImage(src string, sc *scope, seq int, relID string, maxWidthEMU, maxHeightEMU int) (*embeddedImage, error) {
	trimmed := strings.TrimSpace(src)
	if trimmed == "" {
		return nil, errors.New("empty image source")
	}
	var raw []byte
	if strings.HasPrefix(trimmed, "data:image/") {
		comma := strings.IndexByte(trimmed, ',')
		if comma < 0 {
			return nil, errors.New("malformed data URI")
		}
		meta := trimmed[:comma]
		payload := trimmed[comma+1:]
		if !strings.Contains(meta, ";base64") {
			return nil, errors.New("only base64 image data URIs are supported in Office documents")
		}
		decoded, err := base64.StdEncoding.DecodeString(strings.TrimSpace(payload))
		if err != nil {
			return nil, err
		}
		raw = decoded
	} else {
		if sc != nil {
			p, err := sc.open(trimmed, false)
			if err != nil {
				return nil, err
			}
			defer p.Close()
			data, err := readRegular(p, 8<<20)
			if err != nil {
				return nil, err
			}
			raw = data
		} else {
			full, err := mcpPathIn(trimmed, true)
			if err != nil {
				return nil, err
			}
			data, err := os.ReadFile(full)
			if err != nil {
				return nil, err
			}
			raw = data
		}
	}
	if len(raw) > 8<<20 {
		return nil, errors.New("embedded image exceeds 8 MiB limit")
	}
	pxW, pxH, ext, mime := parseImageDimensions(raw)
	if ext == "" {
		return nil, errors.New("unsupported image format for Office embedding (use PNG or JPEG)")
	}
	// 1 px at 96 DPI = 9525 EMU
	wEMU := pxW * 9525
	hEMU := pxH * 9525
	if wEMU > maxWidthEMU && wEMU > 0 {
		scale := float64(maxWidthEMU) / float64(wEMU)
		wEMU = maxWidthEMU
		hEMU = int(float64(hEMU) * scale)
	}
	if hEMU > maxHeightEMU && hEMU > 0 {
		scale := float64(maxHeightEMU) / float64(hEMU)
		hEMU = maxHeightEMU
		wEMU = int(float64(wEMU) * scale)
	}
	if wEMU < 457200 {
		wEMU = 457200
	}
	if hEMU < 304800 {
		hEMU = 304800
	}
	return &embeddedImage{
		RelID:       relID,
		Filename:    fmt.Sprintf("image%d.%s", seq, ext),
		ContentType: mime,
		Ext:         ext,
		Data:        raw,
		WidthEMU:    wEMU,
		HeightEMU:   hEMU,
	}, nil
}

func docxDrawingInlineXML(docPrID int, img *embeddedImage, alt string) string {
	safeAlt := xmlText(alt)
	if safeAlt == "" {
		safeAlt = img.Filename
	}
	return fmt.Sprintf(
		`<w:p><w:pPr><w:jc w:val="center"/><w:spacing w:before="120" w:after="160"/></w:pPr><w:r><w:drawing>`+
			`<wp:inline distT="0" distB="0" distL="0" distR="0">`+
			`<wp:extent cx="%d" cy="%d"/>`+
			`<wp:docPr id="%d" name="Picture %d" descr="%s"/>`+
			`<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>`+
			`<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">`+
			`<pic:pic><pic:nvPicPr><pic:cNvPr id="%d" name="%s"/><pic:cNvPicPr/></pic:nvPicPr>`+
			`<pic:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>`+
			`<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>`+
			`</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>`,
		img.WidthEMU, img.HeightEMU, docPrID, docPrID, safeAlt, docPrID, xmlText(img.Filename), img.RelID, img.WidthEMU, img.HeightEMU,
	)
}

func buildDocxHeaderXML(title, subtitle string) string {
	var parts []string
	if strings.TrimSpace(title) != "" {
		parts = append(parts, strings.TrimSpace(title))
	}
	if strings.TrimSpace(subtitle) != "" {
		parts = append(parts, strings.TrimSpace(subtitle))
	}
	if owner := strings.TrimSpace(os.Getenv("ARIA_OWNER_SCOPE")); owner != "" {
		parts = append(parts, "Tenant: "+owner)
	}
	label := strings.Join(parts, "  ·  ")
	if label == "" {
		label = "Aria Deliverable"
	}
	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
		`<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
		`<w:p><w:pPr><w:jc w:val="right"/><w:pBdr><w:bottom w:val="single" w:sz="4" w:space="4" w:color="E2E8F0"/></w:pBdr></w:pPr>` +
		`<w:r><w:rPr><w:color w:val="64748B"/><w:sz w:val="17"/></w:rPr><w:t xml:space="preserve">` + xmlText(label) + `</w:t></w:r></w:p>` +
		`</w:hdr>`
}

func buildDocxFooterXML() string {
	stamp := fmt.Sprintf("Generated by Aria · %s", time.Now().Format("2006-01-02"))
	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
		`<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
		`<w:p><w:pPr><w:pBdr><w:top w:val="single" w:sz="4" w:space="4" w:color="E2E8F0"/></w:pBdr>` +
		`<w:tabs><w:tab w:val="right" w:pos="9638"/></w:tabs></w:pPr>` +
		`<w:r><w:rPr><w:color w:val="94A3B8"/><w:sz w:val="16"/></w:rPr><w:t xml:space="preserve">` + xmlText(stamp) + `</w:t></w:r>` +
		`<w:r><w:tab/></w:r>` +
		`<w:r><w:rPr><w:color w:val="64748B"/><w:sz w:val="16"/></w:rPr><w:t xml:space="preserve">Page </w:t></w:r>` +
		`<w:fldSimple w:instr=" PAGE "><w:r><w:rPr><w:color w:val="64748B"/><w:sz w:val="16"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>` +
		`<w:r><w:rPr><w:color w:val="64748B"/><w:sz w:val="16"/></w:rPr><w:t xml:space="preserve"> of </w:t></w:r>` +
		`<w:fldSimple w:instr=" NUMPAGES "><w:r><w:rPr><w:color w:val="64748B"/><w:sz w:val="16"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>` +
		`</w:p></w:ftr>`
}

func zipBinaryFiles(entries map[string][]byte, order []string) ([]byte, error) {
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for _, name := range order {
		f, err := w.Create(name)
		if err != nil {
			return nil, err
		}
		if _, err := f.Write(entries[name]); err != nil {
			return nil, err
		}
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

var docxRunGapRe = regexp.MustCompile(`</w:t></w:r>(?:<w:proofErr[^/]*/>)*<w:r>(?:<w:rPr>.*?</w:rPr>)?<w:t[^>]*>`)

// fillDocxTemplate replaces {{key}} placeholders across document, headers, and footers
// while keeping every style, logo, relationship, and layout in the enterprise template intact.
func fillDocxTemplate(templateBytes []byte, vars map[string]string) ([]byte, error) {
	if len(vars) == 0 {
		return nil, errors.New("variables map is required when template_path is provided")
	}
	r, err := zip.NewReader(bytes.NewReader(templateBytes), int64(len(templateBytes)))
	if err != nil {
		return nil, errors.New("invalid .docx template archive")
	}
	if len(r.File) > 5000 {
		return nil, errors.New("template archive contains too many entries")
	}

	normalized := map[string]string{}
	for k, v := range vars {
		clean := strings.TrimSpace(k)
		if clean == "" {
			continue
		}
		if !strings.HasPrefix(clean, "{{") {
			clean = "{{" + clean + "}}"
		}
		normalized[clean] = xmlText(v)
	}
	keys := make([]string, 0, len(normalized))
	for k := range normalized {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	entries := map[string][]byte{}
	var order []string
	budget := 32 << 20
	for _, f := range r.File {
		rc, err := f.Open()
		if err != nil {
			return nil, err
		}
		data, err := io.ReadAll(io.LimitReader(rc, int64(min(16<<20, budget))+1))
		rc.Close()
		if err != nil {
			return nil, err
		}
		budget -= len(data)
		if budget < 0 {
			return nil, errors.New("template exceeds decompression budget")
		}

		if strings.HasPrefix(f.Name, "word/") && strings.HasSuffix(f.Name, ".xml") {
			xmlStr := string(data)
			// Coalesce adjacent runs inside paragraphs if Word split {{placeholder}} across runs
			if strings.Contains(xmlStr, "{") && strings.Contains(xmlStr, "}") {
				xmlStr = mergeSplitPlaceholderRuns(xmlStr)
			}
			for _, placeholder := range keys {
				xmlStr = strings.ReplaceAll(xmlStr, placeholder, normalized[placeholder])
			}
			data = []byte(xmlStr)
		}
		entries[f.Name] = data
		order = append(order, f.Name)
	}
	return zipBinaryFiles(entries, order)
}

func mergeSplitPlaceholderRuns(xmlStr string) string {
	// Only merge adjacent <w:t> runs inside <w:p> blocks that actually contain '{{' and '}}'
	var out strings.Builder
	rest := xmlStr
	for {
		pStart := strings.Index(rest, "<w:p")
		if pStart < 0 {
			out.WriteString(rest)
			break
		}
		out.WriteString(rest[:pStart])
		rest = rest[pStart:]
		pEnd := strings.Index(rest, "</w:p>")
		if pEnd < 0 {
			out.WriteString(rest)
			break
		}
		para := rest[:pEnd+6]
		rest = rest[pEnd+6:]
		if strings.Count(para, "{") >= 2 && strings.Count(para, "}") >= 2 {
			para = docxRunGapRe.ReplaceAllString(para, "")
		}
		out.WriteString(para)
	}
	return out.String()
}

func compileDocxFromParams(sc *scope, params map[string]any) ([]byte, error) {
	title := capRunes(strings.TrimSpace(str(params, "title", "")), 200)
	subtitle := capRunes(strings.TrimSpace(str(params, "subtitle", "")), 200)

	if tmplPath := strings.TrimSpace(str(params, "template_path", "")); tmplPath != "" {
		var tmplBytes []byte
		if sc != nil {
			p, err := sc.open(tmplPath, false)
			if err != nil {
				return nil, err
			}
			defer p.Close()
			tmplBytes, err = readRegular(p, 16<<20)
			if err != nil {
				return nil, err
			}
		} else {
			full, err := mcpPathIn(tmplPath, true)
			if err != nil {
				return nil, err
			}
			var errRead error
			tmplBytes, errRead = os.ReadFile(full)
			if errRead != nil {
				return nil, errRead
			}
		}
		rawVars, _ := params["variables"].(map[string]any)
		vars := map[string]string{}
		for k, v := range rawVars {
			if v != nil {
				vars[k] = fmt.Sprint(v)
			}
		}
		return fillDocxTemplate(tmplBytes, vars)
	}

	md := strings.TrimSpace(str(params, "markdown", ""))
	if md == "" {
		if mdPath := strings.TrimSpace(str(params, "markdown_path", "")); mdPath != "" {
			if sc != nil {
				p, err := sc.open(mdPath, false)
				if err != nil {
					return nil, err
				}
				defer p.Close()
				data, err := readRegular(p, maxMarkdownBytes)
				if err != nil {
					return nil, err
				}
				md = string(data)
			} else {
				full, err := mcpPathIn(mdPath, true)
				if err != nil {
					return nil, err
				}
				data, err := os.ReadFile(full)
				if err != nil {
					return nil, err
				}
				md = string(data)
			}
		}
	}
	if md == "" {
		return nil, errors.New("markdown, markdown_path, or template_path is required")
	}
	return buildDocxWithOptions(docxBuildOptions{Title: title, Subtitle: subtitle, Scope: sc}, markdownBlocks(md))
}

func compilePptxFromParams(sc *scope, params map[string]any) ([]byte, error) {
	title := capRunes(strings.TrimSpace(str(params, "title", "")), 200)
	subtitle := capRunes(strings.TrimSpace(str(params, "subtitle", "")), 200)
	md := strings.TrimSpace(str(params, "markdown", ""))
	if md == "" {
		if mdPath := strings.TrimSpace(str(params, "markdown_path", "")); mdPath != "" {
			if sc != nil {
				p, err := sc.open(mdPath, false)
				if err != nil {
					return nil, err
				}
				defer p.Close()
				data, err := readRegular(p, maxMarkdownBytes)
				if err != nil {
					return nil, err
				}
				md = string(data)
			} else {
				full, err := mcpPathIn(mdPath, true)
				if err != nil {
					return nil, err
				}
				data, err := os.ReadFile(full)
				if err != nil {
					return nil, err
				}
				md = string(data)
			}
		}
	}
	if md == "" {
		return nil, errors.New("markdown or markdown_path is required")
	}
	return buildPptxWithOptions(pptxBuildOptions{Title: title, Subtitle: subtitle, Scope: sc}, markdownBlocks(md))
}

// pptxTableFrame renders a real native DrawingML <a:tbl> graphicFrame on a slide.
func pptxTableFrame(id int, x, y, cx, cy int, rows [][][]docRun) string {
	if len(rows) == 0 {
		return ""
	}
	cols := 1
	for _, r := range rows {
		if len(r) > cols {
			cols = len(r)
		}
	}
	colW := cx / cols
	var grid strings.Builder
	for i := 0; i < cols; i++ {
		fmt.Fprintf(&grid, `<a:gridCol w="%d"/>`, colW)
	}
	rowH := min(457200, max(280000, cy/max(1, len(rows))))
	var trs strings.Builder
	for rIdx, r := range rows {
		if rIdx >= 14 {
			break
		}
		fmt.Fprintf(&trs, `<a:tr h="%d">`, rowH)
		for cIdx := 0; cIdx < cols; cIdx++ {
			var cellRuns []docRun
			if cIdx < len(r) {
				cellRuns = r[cIdx]
			}
			fillHex := "FFFFFF"
			textHex := "1A202C"
			bold := false
			if rIdx == 0 {
				fillHex = "1F2937"
				textHex = "FFFFFF"
				bold = true
			} else if rIdx%2 == 0 {
				fillHex = "F8FAFC"
			}
			var runXML strings.Builder
			if len(cellRuns) == 0 {
				fmt.Fprintf(&runXML, `<a:endParaRPr lang="en-US" sz="1300"/>`)
			} else {
				for _, cr := range cellRuns {
					bAttr := ""
					if bold || cr.bold {
						bAttr = ` b="1"`
					}
					iAttr := ""
					if cr.italic {
						iAttr = ` i="1"`
					}
					fmt.Fprintf(&runXML, `<a:r><a:rPr lang="en-US" sz="1300"%s%s dirty="0"><a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:rPr><a:t>%s</a:t></a:r>`,
						bAttr, iAttr, textHex, xmlText(strings.ReplaceAll(cr.text, "\n", " ")))
				}
			}
			border := `<a:ln w="9525"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:ln>`
			fmt.Fprintf(&trs,
				`<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>%s</a:p></a:txBody>`+
					`<a:tcPr marL="91440" marR="91440" marT="45720" marB="45720" anchor="ctr">`+
					`%s%s%s%s<a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:tcPr></a:tc>`,
				strings.ReplaceAll(border, "<a:ln ", "<a:lnL "), // placeholder replaced below
				"", "", "", "", fillHex,
			)
			_ = border
		}
		trs.WriteString(`</a:tr>`)
	}

	// Rebuild cleanly with explicit lnL/lnR/lnT/lnB borders per ECMA-376
	trs.Reset()
	for rIdx, r := range rows {
		if rIdx >= 14 {
			break
		}
		fmt.Fprintf(&trs, `<a:tr h="%d">`, rowH)
		for cIdx := 0; cIdx < cols; cIdx++ {
			var cellRuns []docRun
			if cIdx < len(r) {
				cellRuns = r[cIdx]
			}
			fillHex := "FFFFFF"
			textHex := "1A202C"
			bold := false
			if rIdx == 0 {
				fillHex = "1F2937"
				textHex = "FFFFFF"
				bold = true
			} else if rIdx%2 == 0 {
				fillHex = "F8FAFC"
			}
			var runXML strings.Builder
			if len(cellRuns) == 0 {
				runXML.WriteString(`<a:endParaRPr lang="en-US" sz="1300"/>`)
			} else {
				for _, cr := range cellRuns {
					bAttr := ""
					if bold || cr.bold {
						bAttr = ` b="1"`
					}
					iAttr := ""
					if cr.italic {
						iAttr = ` i="1"`
					}
					fmt.Fprintf(&runXML, `<a:r><a:rPr lang="en-US" sz="1300"%s%s dirty="0"><a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:rPr><a:t>%s</a:t></a:r>`,
						bAttr, iAttr, textHex, xmlText(strings.ReplaceAll(cr.text, "\n", " ")))
				}
			}
			borders := `<a:lnL w="9525"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:lnL>` +
				`<a:lnR w="9525"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:lnR>` +
				`<a:lnT w="9525"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:lnT>` +
				`<a:lnB w="9525"><a:solidFill><a:srgbClr val="E2E8F0"/></a:solidFill></a:lnB>`
			fmt.Fprintf(&trs,
				`<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>%s</a:p></a:txBody>`+
					`<a:tcPr marL="91440" marR="91440" marT="45720" marB="45720" anchor="ctr">%s<a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:tcPr></a:tc>`,
				runXML.String(), borders, fillHex,
			)
		}
		trs.WriteString(`</a:tr>`)
	}

	return fmt.Sprintf(
		`<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="%d" name="Table %d"/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>`+
			`<p:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></p:xfrm>`+
			`<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">`+
			`<a:tbl><a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>%s</a:tblGrid>%s</a:tbl>`+
			`</a:graphicData></a:graphic></p:graphicFrame>`,
		id, id, x, y, cx, cy, grid.String(), trs.String(),
	)
}

func pptxPicShape(id int, x, y int, img *embeddedImage) string {
	return fmt.Sprintf(
		`<p:pic><p:nvPicPr><p:cNvPr id="%d" name="%s"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>`+
			`<p:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>`+
			`<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>`,
		id, xmlText(img.Filename), img.RelID, x, y, img.WidthEMU, img.HeightEMU,
	)
}

func pptxSolidBar(id int, name string, x, y, cx, cy int, hexColor string) string {
	return fmt.Sprintf(
		`<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>`+
			`<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>`+
			`<a:solidFill><a:srgbClr val="%s"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>`,
		id, name, x, y, cx, cy, hexColor,
	)
}
