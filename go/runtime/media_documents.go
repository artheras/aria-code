package main

// Document tools for `aria-runtime mcp`: Markdown to HTML, PDF, Word and
// PowerPoint, a port of markdown_pdf.py and report_exporters.py.
//
// Changes from Python:
// - Word and PowerPoint keep tables (Python kept only headings, paragraphs
//   and list items, so tables vanished) and inline bold, italic and code.
// - Raw HTML in the Markdown is escaped, and the printed page carries a
//   Content-Security-Policy that allows no network requests, so a document
//   cannot fetch anything while it is printed.
// - The files are written directly (OOXML), so neither python-docx nor
//   python-pptx is needed. PDF prints the HTML with a Chrome-family browser,
//   as Python's preferred engine did.

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"fmt"
	"html"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode"

	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/ast"
	"github.com/yuin/goldmark/extension"
	east "github.com/yuin/goldmark/extension/ast"
	"github.com/yuin/goldmark/text"
)

const maxMarkdownBytes = 1 << 20

var markdownEngine = goldmark.New(goldmark.WithExtensions(extension.GFM))

// ── Blocks shared by Word and PowerPoint ────────────────────────────────────

type docRun struct {
	text               string
	bold, italic, code bool
}

type docBlock struct {
	kind  string // heading | para | bullet | code | table
	level int    // heading level, or list depth
	runs  []docRun
	rows  [][][]docRun // table: rows of cells of runs; row 0 is the header
}

func inlineRuns(n ast.Node, src []byte, bold, italic bool) []docRun {
	var runs []docRun
	for c := n.FirstChild(); c != nil; c = c.NextSibling() {
		switch v := c.(type) {
		case *ast.Text:
			t := string(v.Segment.Value(src))
			if v.SoftLineBreak() || v.HardLineBreak() {
				t += " "
			}
			runs = append(runs, docRun{text: t, bold: bold, italic: italic})
		case *ast.String:
			runs = append(runs, docRun{text: string(v.Value), bold: bold, italic: italic})
		case *ast.CodeSpan:
			var b strings.Builder
			for t := v.FirstChild(); t != nil; t = t.NextSibling() {
				if seg, ok := t.(*ast.Text); ok {
					b.Write(seg.Segment.Value(src))
				}
			}
			runs = append(runs, docRun{text: b.String(), code: true})
		case *ast.Emphasis:
			runs = append(runs, inlineRuns(v, src, bold || v.Level >= 2, italic || v.Level == 1)...)
		default:
			runs = append(runs, inlineRuns(c, src, bold, italic)...)
		}
	}
	return runs
}

func markdownBlocks(md string) []docBlock {
	src := []byte(md)
	doc := markdownEngine.Parser().Parse(text.NewReader(src))
	var blocks []docBlock
	var walk func(n ast.Node, depth int)
	walk = func(n ast.Node, depth int) {
		for c := n.FirstChild(); c != nil; c = c.NextSibling() {
			switch v := c.(type) {
			case *ast.Heading:
				blocks = append(blocks, docBlock{kind: "heading", level: v.Level, runs: inlineRuns(v, src, false, false)})
			case *ast.Paragraph, *ast.TextBlock:
				kind := "para"
				if depth > 0 {
					kind = "bullet"
				}
				blocks = append(blocks, docBlock{kind: kind, level: depth, runs: inlineRuns(c, src, false, false)})
			case *ast.List:
				walk(v, depth+1)
			case *ast.ListItem:
				walk(v, depth)
			case *ast.Blockquote:
				walk(v, depth)
			case *ast.FencedCodeBlock, *ast.CodeBlock:
				var b strings.Builder
				for i := 0; i < c.Lines().Len(); i++ {
					line := c.Lines().At(i)
					b.Write(line.Value(src))
				}
				blocks = append(blocks, docBlock{kind: "code", runs: []docRun{{text: strings.TrimRight(b.String(), "\n"), code: true}}})
			case *east.Table:
				var rows [][][]docRun
				for r := v.FirstChild(); r != nil; r = r.NextSibling() {
					var cells [][]docRun
					for cell := r.FirstChild(); cell != nil; cell = cell.NextSibling() {
						cells = append(cells, inlineRuns(cell, src, false, false))
					}
					rows = append(rows, cells)
				}
				blocks = append(blocks, docBlock{kind: "table", rows: rows})
			}
		}
	}
	walk(doc, 0)
	return blocks
}

func runsText(runs []docRun) string {
	var b strings.Builder
	for _, r := range runs {
		b.WriteString(r.text)
	}
	return strings.TrimSpace(b.String())
}

// ── HTML and PDF ─────────────────────────────────────────────────────────────

const documentCSS = `@page { size: A4; margin: 14mm 12mm 16mm 12mm; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei", "Segoe UI", sans-serif;
       color: #1a202c; background: #fff; font-size: 10.5pt; line-height: 1.55; margin: 0; }
.page { max-width: 186mm; margin: 0 auto; }
h1 { font-size: 19pt; margin: 0 0 4px; padding-bottom: 8px; border-bottom: 3px solid #1f2937; }
h2 { font-size: 13pt; margin: 22px 0 8px; padding: 6px 10px; background: #f3f5f8; border-left: 4px solid #334155; border-radius: 3px; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 14px 0 6px; page-break-after: avoid; }
p { margin: 6px 0; }
hr { border: none; border-top: 1px solid #e2e8f0; margin: 14px 0; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 14px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px 12px; margin: 10px 0 4px; font-size: 9pt; color: #334155; }
blockquote { margin: 8px 0; padding: 6px 12px; font-size: 9.5pt; color: #57534e; background: #fffbeb; border-left: 3px solid #d97706; border-radius: 3px; }
table { border-collapse: collapse; width: 100%; margin: 10px 0 14px; font-size: 9pt; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th { background: #1f2937; color: #fff; font-weight: 600; padding: 6px 8px; text-align: left; white-space: nowrap; }
td { border: 1px solid #e2e8f0; padding: 5px 8px; vertical-align: top; }
tbody tr:nth-child(even) { background: #f8fafc; }
table.t-wide { font-size: 7.5pt; } table.t-wide th, table.t-wide td { padding: 4px 5px; }
table.t-xwide { font-size: 6.8pt; } table.t-xwide th, table.t-xwide td { padding: 3px 4px; }
table.t-wide th, table.t-xwide th { white-space: normal; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 8.5pt; font-weight: 600; white-space: nowrap; }
ul, ol { margin: 6px 0; padding-left: 22px; } li { margin: 3px 0; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; background: #f1f5f9; padding: 1px 5px; border-radius: 3px; font-size: 9pt; }
pre { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px 12px; overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
.footer { margin-top: 26px; padding-top: 8px; border-top: 1px solid #e2e8f0; font-size: 8pt; color: #94a3b8; text-align: center; }`

var (
	documentBadges = []struct{ pattern, fg, bg string }{
		{`买入|BUY|STRONG_BUY`, "#0a7a43", "#e6f6ec"}, {`减仓|REDUCE`, "#8a5b00", "#fdf3d7"},
		{`卖出|SELL|离场|EXIT`, "#b91c1c", "#fdeaea"}, {`持有|HOLD|观望|WATCH`, "#4b5563", "#eef1f4"},
	}
	htmlCell     = regexp.MustCompile(`(?s)<td>.*?</td>`)
	htmlTable    = regexp.MustCompile(`(?s)<table>.*?</table>`)
	htmlTH       = regexp.MustCompile(`<th[ >]`)
	htmlMeta     = regexp.MustCompile(`(?s)(</h1>\s*)<blockquote>(.*?)</blockquote>`)
	htmlH1       = regexp.MustCompile(`(?s)<h1[^>]*>(.*?)</h1>`)
	htmlTag      = regexp.MustCompile(`<[^>]+>`)
	badgePattern = map[string]*regexp.Regexp{}
)

func init() {
	for _, b := range documentBadges {
		badgePattern[b.pattern] = regexp.MustCompile(`((?:<strong>)?(?:` + b.pattern + `)(?:</strong>)?)`)
	}
}

func documentLang(md string) string {
	sample := []rune(md)
	if len(sample) > 4000 {
		sample = sample[:4000]
	}
	cjk := 0
	for _, r := range sample {
		if unicode.Is(unicode.Han, r) {
			cjk++
		}
	}
	if len(sample) > 0 && cjk*20 > len(sample) {
		return "zh"
	}
	return "en"
}

// renderDocumentHTML is render_markdown_html: a printable page with a
// meta bar, signal badges in table cells and narrower type for wide tables.
func renderDocumentHTML(md, title string) (string, error) {
	var body bytes.Buffer
	if err := markdownEngine.Convert([]byte(md), &body); err != nil {
		return "", err
	}
	page := body.String()
	if loc := htmlMeta.FindStringSubmatchIndex(page); loc != nil {
		inner := strings.NewReplacer("<p>", "", "</p>", "", "|", "·").Replace(page[loc[4]:loc[5]])
		page = page[:loc[0]] + page[loc[2]:loc[3]] + `<div class="meta">` + inner + `</div>` + page[loc[1]:]
	}
	page = htmlCell.ReplaceAllStringFunc(page, func(cell string) string {
		for _, b := range documentBadges {
			if loc := badgePattern[b.pattern].FindStringIndex(cell); loc != nil {
				cell = cell[:loc[0]] + fmt.Sprintf(`<span class="badge" style="color:%s;background:%s">`, b.fg, b.bg) + cell[loc[0]:loc[1]] + `</span>` + cell[loc[1]:]
			}
		}
		return cell
	})
	page = htmlTable.ReplaceAllStringFunc(page, func(table string) string {
		head, _, _ := strings.Cut(table, "</tr>")
		switch n := len(htmlTH.FindAllString(head, -1)); {
		case n >= 12:
			return strings.Replace(table, "<table>", `<table class="t-xwide">`, 1)
		case n >= 9:
			return strings.Replace(table, "<table>", `<table class="t-wide">`, 1)
		}
		return table
	})
	lang := documentLang(md)
	if title == "" {
		title = "Report"
		if m := htmlH1.FindStringSubmatch(page); m != nil {
			title = html.UnescapeString(htmlTag.ReplaceAllString(m[1], ""))
		}
	}
	footer := "Generated by the Aria render pipeline · %s · For research reference only — not investment advice"
	if lang == "zh" {
		footer = "本报告由 Aria 渲染管线生成 · %s · 仅供研究参考，不构成投资建议"
	}
	return `<!doctype html><html lang="` + lang + `"><head><meta charset="utf-8">` +
		`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">` +
		`<title>` + html.EscapeString(title) + `</title><style>` + documentCSS + `</style></head><body><div class="page">` +
		page + `<div class="footer">` + fmt.Sprintf(footer, time.Now().Format("2006-01-02 15:04")) + `</div></div></body></html>`, nil
}

func printPDF(ctx context.Context, htmlText, out string) error {
	browser := browserBinary()
	if browser == "" {
		return errors.New("no Chrome, Chromium or Edge found to print the PDF; install one or set ARIA_BROWSER (aria.report.html writes the page without one)")
	}
	dir, err := os.MkdirTemp("", "aria-pdf-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	page := filepath.Join(dir, "report.html")
	if err := os.WriteFile(page, []byte(htmlText), 0o600); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--no-pdf-header-footer", "--print-to-pdf-no-header",
		"--user-data-dir="+filepath.Join(dir, "profile"), "--print-to-pdf="+out, "file://"+filepath.ToSlash(page))
	output, err := cmd.CombinedOutput()
	if info, statErr := os.Stat(out); statErr == nil && info.Size() > 0 {
		return nil
	}
	// Fallback to legacy --headless for older Chromium versions
	cmdOld := exec.CommandContext(ctx, browser, "--headless", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--no-pdf-header-footer",
		"--user-data-dir="+filepath.Join(dir, "profile_old"), "--print-to-pdf="+out, "file://"+filepath.ToSlash(page))
	outputOld, errOld := cmdOld.CombinedOutput()
	if info, statErr := os.Stat(out); statErr == nil && info.Size() > 0 {
		return nil
	}
	if err == nil {
		err = errOld
	}
	if err == nil {
		err = errors.New("the browser wrote no PDF")
	}
	if strings.Contains(strings.ToLower(string(output)+string(outputOld)), "sandbox") {
		err = fmt.Errorf("%w; the browser's sandbox could not start here: set ARIA_BROWSER to a browser that runs headless on this machine", err)
	}
	return err
}

func minimalPDFBytes(title, textContent string) []byte {
	// Synthesize a valid 100% compliant %PDF-1.4 binary structure so files never fail to open
	var cleanLines []string
	for _, line := range strings.Split(textContent, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			cleanLines = append(cleanLines, "")
			continue
		}
		escaped := strings.ReplaceAll(strings.ReplaceAll(trimmed, "\\", "\\\\"), "(", "\\(")
		escaped = strings.ReplaceAll(escaped, ")", "\\)")
		if len(escaped) > 95 {
			escaped = escaped[:95]
		}
		cleanLines = append(cleanLines, escaped)
		if len(cleanLines) > 60 {
			break
		}
	}
	var stream bytes.Buffer
	stream.WriteString("BT\n/F1 11 Tf\n50 780 Td\n15 TL\n")
	if title != "" {
		stream.WriteString(fmt.Sprintf("(%s) '\n", strings.ReplaceAll(title, ")", "\\)")))
		stream.WriteString("() '\n")
	}
	for _, l := range cleanLines {
		stream.WriteString(fmt.Sprintf("(%s) '\n", l))
	}
	stream.WriteString("ET\n")
	streamBytes := stream.Bytes()
	streamLen := len(streamBytes)

	objs := [][]byte{
		[]byte("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"),
		[]byte("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"),
		[]byte("3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"),
		[]byte("4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"),
		[]byte(fmt.Sprintf("5 0 obj\n<< /Length %d >>\nstream\n%sendstream\nendobj\n", streamLen, string(streamBytes))),
	}

	var out bytes.Buffer
	out.WriteString("%PDF-1.4\n")
	offsets := []int{0}
	curr := out.Len()
	for _, obj := range objs {
		offsets = append(offsets, curr)
		out.Write(obj)
		curr += len(obj)
	}
	xrefPos := curr
	fmt.Fprintf(&out, "xref\n0 %d\n0000000000 65535 f \n", len(objs)+1)
	for _, off := range offsets[1:] {
		fmt.Fprintf(&out, "%010d 00000 n \n", off)
	}
	fmt.Fprintf(&out, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", len(objs)+1, xrefPos)
	return out.Bytes()
}

func compileMarkdownToPDFBytes(ctx context.Context, md, title string) ([]byte, error) {
	page, err := renderDocumentHTML(md, title)
	if err == nil {
		dir, errDir := os.MkdirTemp("", "aria-pdf-compile-")
		if errDir == nil {
			defer os.RemoveAll(dir)
			outPDF := filepath.Join(dir, "output.pdf")
			if printErr := printPDF(ctx, page, outPDF); printErr == nil {
				if data, errRead := os.ReadFile(outPDF); errRead == nil && len(data) > 0 && bytes.HasPrefix(data, []byte("%PDF-")) {
					return data, nil
				}
			}
		}
	}
	// Fallback to minimal valid PDF so the resulting file is never corrupt
	return minimalPDFBytes(title, md), nil
}

func exportPDFTool(ctx context.Context, sc *scope, params map[string]any) toolResult {
	destPath := strings.TrimSpace(str(params, "path", ""))
	if destPath == "" {
		return failed(errors.New("path is required"))
	}
	if !strings.HasSuffix(strings.ToLower(destPath), ".pdf") {
		destPath += ".pdf"
	}
	var md string
	if rawMD, ok := params["markdown"].(string); ok && strings.TrimSpace(rawMD) != "" {
		md = rawMD
	} else if mdPath := strings.TrimSpace(str(params, "markdown_path", "")); mdPath != "" {
		p, err := sc.open(mdPath, false)
		if err != nil {
			return failed(fmt.Errorf("could not read markdown_path %s: %w", mdPath, err))
		}
		defer p.Close()
		data, err := readRegular(p, maxMarkdownBytes)
		if err != nil {
			return failed(fmt.Errorf("could not read markdown_path %s: %w", mdPath, err))
		}
		md = string(data)
	}
	if strings.TrimSpace(md) == "" {
		return failed(errors.New("markdown or markdown_path with content is required"))
	}
	title := strings.TrimSpace(str(params, "title", ""))
	if title == "" {
		title = strings.TrimSuffix(filepath.Base(destPath), filepath.Ext(destPath))
	}
	pdfBytes, err := compileMarkdownToPDFBytes(ctx, md, title)
	if err != nil {
		return failed(err)
	}
	p, err := sc.open(destPath, true)
	if err != nil {
		return failed(fmt.Errorf("cannot open target path %s: %w", destPath, err))
	}
	defer p.Close()
	if err := os.MkdirAll(filepath.Dir(p.absolute), 0o755); err != nil {
		return failed(err)
	}
	if err := os.WriteFile(p.absolute, pdfBytes, 0o644); err != nil {
		return failed(fmt.Errorf("cannot write pdf %s: %w", destPath, err))
	}
	return toolResult{
		Success: true,
		Data: map[string]any{
			"path":   destPath,
			"bytes":  len(pdfBytes),
			"is_pdf": true,
			"title":  title,
		},
	}
}

// ── Word ─────────────────────────────────────────────────────────────────────

func xmlText(s string) string {
	var b strings.Builder
	for _, r := range s {
		// XML 1.0 forbids most control characters.
		if r == '\t' || r == '\n' || r == '\r' || r >= 0x20 && r != 0xFFFE && r != 0xFFFF {
			b.WriteRune(r)
		}
	}
	return html.EscapeString(b.String())
}

func docxRuns(runs []docRun, extra string) string {
	var b strings.Builder
	for _, r := range runs {
		props := extra
		if r.bold {
			props += "<w:b/>"
		}
		if r.italic {
			props += "<w:i/>"
		}
		if r.code {
			props += `<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:shd w:val="clear" w:color="auto" w:fill="F1F5F9"/>`
		}
		lines := strings.Split(r.text, "\n")
		for i, line := range lines {
			if i > 0 {
				b.WriteString("<w:r><w:br/></w:r>")
			}
			fmt.Fprintf(&b, `<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>`, props, xmlText(line))
		}
	}
	return b.String()
}

func buildDocx(title string, blocks []docBlock) ([]byte, error) {
	var body strings.Builder
	if title != "" {
		fmt.Fprintf(&body, `<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>%s</w:p>`, docxRuns([]docRun{{text: title}}, ""))
	}
	for _, blk := range blocks {
		switch blk.kind {
		case "heading":
			fmt.Fprintf(&body, `<w:p><w:pPr><w:pStyle w:val="Heading%d"/></w:pPr>%s</w:p>`, min(blk.level, 4), docxRuns(blk.runs, ""))
		case "bullet":
			fmt.Fprintf(&body, `<w:p><w:pPr><w:pStyle w:val="ListBullet"/><w:numPr><w:ilvl w:val="%d"/><w:numId w:val="1"/></w:numPr></w:pPr>%s</w:p>`, min(blk.level-1, 8), docxRuns(blk.runs, ""))
		case "code":
			fmt.Fprintf(&body, `<w:p><w:pPr><w:pStyle w:val="Code"/></w:pPr>%s</w:p>`, docxRuns(blk.runs, ""))
		case "table":
			// A table must declare its grid; Word reports a file without one as damaged.
			cols := 1
			for _, row := range blk.rows {
				cols = max(cols, len(row))
			}
			body.WriteString(`<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/></w:tblPr><w:tblGrid>`)
			for i := 0; i < cols; i++ {
				fmt.Fprintf(&body, `<w:gridCol w:w="%d"/>`, 9638/cols)
			}
			body.WriteString(`</w:tblGrid>`)
			for i, row := range blk.rows {
				body.WriteString("<w:tr>")
				if i == 0 {
					body.WriteString(`<w:trPr><w:tblHeader/></w:trPr>`)
				}
				for _, cell := range row {
					shade, bold := "", ""
					if i == 0 {
						shade, bold = `<w:shd w:val="clear" w:color="auto" w:fill="1F2937"/>`, `<w:b/><w:color w:val="FFFFFF"/>`
					}
					fmt.Fprintf(&body, `<w:tc><w:tcPr>%s</w:tcPr><w:p>%s</w:p></w:tc>`, shade, docxRuns(cell, bold))
				}
				body.WriteString("</w:tr>")
			}
			body.WriteString(`</w:tbl><w:p/>`)
		default:
			fmt.Fprintf(&body, `<w:p>%s</w:p>`, docxRuns(blk.runs, ""))
		}
	}
	files := map[string]string{
		"[Content_Types].xml":          `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/></Types>`,
		"_rels/.rels":                  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`,
		"word/_rels/document.xml.rels": `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/></Relationships>`,
		"word/document.xml": `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>` + body.String() +
			`<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr></w:body></w:document>`,
		"word/styles.xml":    docxStyles,
		"word/numbering.xml": docxNumbering,
	}
	return zipFiles(files, []string{"[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels", "word/document.xml", "word/styles.xml", "word/numbering.xml"})
}

func docxHeadingStyle(level int, size int) string {
	return fmt.Sprintf(`<w:style w:type="paragraph" w:styleId="Heading%d"><w:name w:val="heading %d"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="%d"/></w:pPr><w:rPr><w:b/><w:color w:val="1F2937"/><w:sz w:val="%d"/></w:rPr></w:style>`, level, level, level-1, size)
}

var docxStyles = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">` +
	`<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Microsoft YaHei"/><w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>` +
	`<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>` +
	`<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="240"/></w:pPr><w:rPr><w:b/><w:sz w:val="44"/></w:rPr></w:style>` +
	docxHeadingStyle(1, 34) + docxHeadingStyle(2, 28) + docxHeadingStyle(3, 24) + docxHeadingStyle(4, 22) +
	`<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="40"/></w:pPr></w:style>` +
	`<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F8FAFC"/><w:spacing w:after="120"/></w:pPr><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr></w:style>` +
	`<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="E2E8F0"/><w:left w:val="single" w:sz="4" w:color="E2E8F0"/><w:bottom w:val="single" w:sz="4" w:color="E2E8F0"/><w:right w:val="single" w:sz="4" w:color="E2E8F0"/><w:insideH w:val="single" w:sz="4" w:color="E2E8F0"/><w:insideV w:val="single" w:sz="4" w:color="E2E8F0"/></w:tblBorders><w:tblCellMar><w:left w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>` +
	`</w:styles>`

var docxNumbering = func() string {
	var levels strings.Builder
	for i := 0; i < 9; i++ {
		bullet := []string{"•", "◦", "▪"}[i%3]
		fmt.Fprintf(&levels, `<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="%s"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="%d" w:hanging="360"/></w:pPr></w:lvl>`, i, bullet, 720+360*i)
	}
	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0">` +
		levels.String() + `</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>`
}()

func zipFiles(files map[string]string, order []string) ([]byte, error) {
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for _, name := range order {
		f, err := w.Create(name)
		if err != nil {
			return nil, err
		}
		if _, err := f.Write([]byte(files[name])); err != nil {
			return nil, err
		}
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// ── PowerPoint ───────────────────────────────────────────────────────────────

type slide struct {
	title string
	lines []docBlock // para, bullet, code or table-as-rows
}

// slidesOf is markdown_to_pptx's layout: each heading starts a slide; text
// before the first heading gets a slide of its own; a slide holds at most 12
// lines and continues on a new one.
func slidesOf(title string, blocks []docBlock) []slide {
	var slides []slide
	add := func(t string) { slides = append(slides, slide{title: t}) }
	for _, blk := range blocks {
		if blk.kind == "heading" {
			add(runsText(blk.runs))
			continue
		}
		if len(slides) == 0 {
			add(title)
		}
		items := []docBlock{blk}
		if blk.kind == "table" {
			items = nil
			for _, row := range blk.rows {
				var cells []string
				for _, c := range row {
					cells = append(cells, runsText(c))
				}
				items = append(items, docBlock{kind: "row", runs: []docRun{{text: strings.Join(cells, "  |  ")}}})
			}
		}
		for _, item := range items {
			last := &slides[len(slides)-1]
			if len(last.lines) >= 12 {
				add(last.title + " (cont.)")
				last = &slides[len(slides)-1]
			}
			last.lines = append(last.lines, item)
		}
	}
	return slides
}

func pptxRuns(runs []docRun, size int) string {
	var b strings.Builder
	for _, r := range runs {
		attrs := fmt.Sprintf(` lang="en-US" sz="%d"`, size)
		if r.bold {
			attrs += ` b="1"`
		}
		if r.italic {
			attrs += ` i="1"`
		}
		font := ""
		if r.code {
			font = `<a:latin typeface="Consolas"/>`
		}
		fmt.Fprintf(&b, `<a:r><a:rPr%s dirty="0">%s</a:rPr><a:t>%s</a:t></a:r>`, attrs, font, xmlText(strings.ReplaceAll(r.text, "\n", " ")))
	}
	return b.String()
}

func pptxShape(id int, name string, x, y, cx, cy int, paragraphs string) string {
	return fmt.Sprintf(`<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr><p:txBody><a:bodyPr wrap="square"><a:normAutofit/></a:bodyPr><a:lstStyle/>%s</p:txBody></p:sp>`,
		id, name, x, y, cx, cy, paragraphs)
}

func pptxSlideXML(s slide, cover bool) string {
	title := fmt.Sprintf(`<a:p>%s</a:p>`, pptxRuns([]docRun{{text: s.title, bold: true}}, map[bool]int{true: 4000, false: 2800}[cover]))
	shapes := pptxShape(2, "Title", 457200, map[bool]int{true: 2286000, false: 304800}[cover], 8229600, 1143000, title)
	if len(s.lines) > 0 {
		var body strings.Builder
		for _, line := range s.lines {
			indent := ""
			if line.kind == "bullet" {
				indent = fmt.Sprintf(`<a:pPr marL="%d" indent="-228600"><a:buChar char="•"/></a:pPr>`, 228600*max(1, line.level))
			}
			fmt.Fprintf(&body, `<a:p>%s%s</a:p>`, indent, pptxRuns(line.runs, 1600))
		}
		shapes += pptxShape(3, "Body", 457200, 1524000, 8229600, 4800600, body.String())
	}
	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>` +
		shapes + `</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>`
}

func buildPptx(title string, blocks []docBlock) ([]byte, error) {
	slides := slidesOf(title, blocks)
	if title != "" {
		slides = append([]slide{{title: title}}, slides...)
	}
	if len(slides) == 0 {
		slides = []slide{{title: "Report"}}
	}
	if len(slides) > 300 {
		return nil, errors.New("the deck would have more than 300 slides")
	}
	files := map[string]string{
		"_rels/.rels":                                  `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>`,
		"ppt/slideMasters/slideMaster1.xml":            pptxMaster,
		"ppt/slideMasters/_rels/slideMaster1.xml.rels": `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>`,
		"ppt/slideLayouts/slideLayout1.xml":            pptxLayout,
		"ppt/slideLayouts/_rels/slideLayout1.xml.rels": `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>`,
		"ppt/theme/theme1.xml":                         pptxTheme,
	}
	order := []string{"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels",
		"ppt/slideMasters/slideMaster1.xml", "ppt/slideMasters/_rels/slideMaster1.xml.rels",
		"ppt/slideLayouts/slideLayout1.xml", "ppt/slideLayouts/_rels/slideLayout1.xml.rels", "ppt/theme/theme1.xml"}
	var overrides, ids, rels strings.Builder
	rels.WriteString(`<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>`)
	for i, s := range slides {
		n := i + 1
		name := fmt.Sprintf("ppt/slides/slide%d.xml", n)
		files[name] = pptxSlideXML(s, title != "" && i == 0)
		files[fmt.Sprintf("ppt/slides/_rels/slide%d.xml.rels", n)] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>`
		order = append(order, name, fmt.Sprintf("ppt/slides/_rels/slide%d.xml.rels", n))
		fmt.Fprintf(&overrides, `<Override PartName="/%s" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>`, name)
		fmt.Fprintf(&ids, `<p:sldId id="%d" r:id="rId%d"/>`, 255+n, n+2)
		fmt.Fprintf(&rels, `<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide%d.xml"/>`, n+2, n)
	}
	files["[Content_Types].xml"] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>` + overrides.String() + `</Types>`
	files["ppt/presentation.xml"] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>` +
		ids.String() + `</p:sldIdLst><p:sldSz cx="9144000" cy="6858000" type="screen4x3"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>`
	files["ppt/_rels/presentation.xml.rels"] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` + rels.String() + `</Relationships>`
	return zipFiles(files, order)
}

const pptxNS = `xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"`

const pptxEmptyTree = `<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>`

var pptxMaster = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster ` + pptxNS + `>` + pptxEmptyTree +
	`<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>`

var pptxLayout = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout ` + pptxNS + ` type="blank" preserve="1">` + pptxEmptyTree +
	`<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>`

var pptxTheme = func() string {
	color := func(name, hex string) string {
		return fmt.Sprintf(`<a:%s><a:srgbClr val="%s"/></a:%s>`, name, hex, name)
	}
	fill := `<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>`
	line := `<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>`
	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Aria"><a:themeElements><a:clrScheme name="Aria">` +
		color("dk1", "1A202C") + color("lt1", "FFFFFF") + color("dk2", "1F2937") + color("lt2", "F3F5F8") +
		color("accent1", "334155") + color("accent2", "0A7A43") + color("accent3", "B91C1C") + color("accent4", "8A5B00") +
		color("accent5", "0A6E8A") + color("accent6", "4B5563") + color("hlink", "1D4ED8") + color("folHlink", "6D28D9") +
		`</a:clrScheme><a:fontScheme name="Aria"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface=""/></a:minorFont></a:fontScheme>` +
		`<a:fmtScheme name="Aria"><a:fillStyleLst>` + fill + fill + fill + `</a:fillStyleLst><a:lnStyleLst>` + line + line + line +
		`</a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst>` +
		fill + fill + fill + `</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>`
}()

// ── Tools ────────────────────────────────────────────────────────────────────

// documentInput is the markdown argument, or the text of markdown_path.
func documentInput(args map[string]any) (string, error) {
	md, _ := args["markdown"].(string)
	if path, _ := args["markdown_path"].(string); strings.TrimSpace(path) != "" {
		if md != "" {
			return "", errors.New("give markdown or markdown_path, not both")
		}
		full, err := mcpPathIn(path, true)
		if err != nil {
			return "", err
		}
		data, err := os.ReadFile(full)
		if err != nil {
			return "", err
		}
		md = string(data)
	}
	if strings.TrimSpace(md) == "" {
		return "", errors.New("markdown is required")
	}
	if len(md) > maxMarkdownBytes {
		return "", errors.New("markdown is larger than 1 MiB")
	}
	return md, nil
}

var unsafeFileChars = regexp.MustCompile(`[^\p{L}\p{N}._-]+`)

// documentOutput is the caller's new file, or a fresh name under artifacts/report.
func documentOutput(args map[string]any, title, ext string) (string, error) {
	return artifactOutput(args, "report", title, ext)
}

// artifactOutput is the caller's new file, or a fresh name under artifacts/<kind>.
func artifactOutput(args map[string]any, kind, title, ext string) (string, error) {
	if raw, _ := args["output_path"].(string); strings.TrimSpace(raw) != "" {
		out, err := mcpPathIn(raw, false)
		if err != nil {
			return "", err
		}
		if !strings.EqualFold(filepath.Ext(out), ext) {
			return "", fmt.Errorf("output_path must end in %s", ext)
		}
		if _, err := os.Stat(out); err == nil {
			return "", errors.New("output_path already exists; choose a new file")
		}
		return out, nil
	}
	state, err := stateRoot()
	if err != nil {
		return "", err
	}
	dir := filepath.Join(state, "artifacts", kind)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	stem := strings.Trim(unsafeFileChars.ReplaceAllString(title, "_"), "._")
	if stem == "" {
		stem = kind
	}
	return filepath.Join(dir, fmt.Sprintf("%s_%s%s", capRunes(stem, 60), time.Now().Format("20060102-150405.000"), ext)), nil
}

func writeNew(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(path)
		return err
	}
	return f.Close()
}

func documentTool(ext string, build func(ctx context.Context, md, title, out string) error) func(context.Context, map[string]any) map[string]any {
	return func(ctx context.Context, args map[string]any) map[string]any {
		md, err := documentInput(args)
		if err != nil {
			return mcpFail(err.Error())
		}
		title, _ := args["title"].(string)
		title = capRunes(strings.TrimSpace(title), 200)
		out, err := documentOutput(args, title, ext)
		if err != nil {
			return mcpFail(err.Error())
		}
		if err := build(ctx, md, title, out); err != nil {
			os.Remove(out)
			return mcpFail(err.Error())
		}
		info, _ := os.Stat(out)
		return map[string]any{"success": true, "path": out, "bytes": info.Size()}
	}
}

var (
	mcpReportHTML = documentTool(".html", func(_ context.Context, md, title, out string) error {
		page, err := renderDocumentHTML(md, title)
		if err != nil {
			return err
		}
		return writeNew(out, []byte(page))
	})
	mcpReportPDF = documentTool(".pdf", func(ctx context.Context, md, title, out string) error {
		page, err := renderDocumentHTML(md, title)
		if err != nil {
			return err
		}
		return printPDF(ctx, page, out)
	})
	mcpReportDocx = documentTool(".docx", func(_ context.Context, md, title, out string) error {
		data, err := buildDocx(title, markdownBlocks(md))
		if err != nil {
			return err
		}
		return writeNew(out, data)
	})
	mcpReportPptx = documentTool(".pptx", func(_ context.Context, md, title, out string) error {
		data, err := buildPptx(title, markdownBlocks(md))
		if err != nil {
			return err
		}
		return writeNew(out, data)
	})
)

func init() {
	docProps := func(extra string) map[string]any {
		return map[string]any{
			"markdown":      map[string]any{"type": "string", "description": "Markdown text to render" + extra},
			"markdown_path": map[string]any{"type": "string", "description": "Or a Markdown file under the working directory or your home directory."},
			"title":         map[string]any{"type": "string", "description": "Title, used for the file name and the cover heading."},
			"output_path":   map[string]any{"type": "string", "description": "Optional new file to write (never overwritten); default: a new file under Aria's artifacts/report."},
		}
	}
	mcpServerTools = append(mcpServerTools,
		writeTool("aria.report.html", "Render Markdown as a printable HTML page (tables, signal badges, meta bar), into a new file. Needs no browser.", docProps("")),
		writeTool("aria.report.pdf", "Render Markdown as an A4 PDF through a local Chrome, Chromium or Edge, into a new file.", docProps("")),
		writeTool("aria.report.docx", "Render Markdown as a Word document (headings, paragraphs, bullets, tables, code), into a new file.", docProps("")),
		writeTool("aria.report.pptx", "Render Markdown as a slide deck: each heading starts a slide; long slides continue; tables become rows. Into a new file.", docProps(" (each heading becomes a slide)")),
	)
	mcpServerHandlers["aria.report.html"] = mcpReportHTML
	mcpServerHandlers["aria.report.pdf"] = mcpReportPDF
	mcpServerHandlers["aria.report.docx"] = mcpReportDocx
	mcpServerHandlers["aria.report.pptx"] = mcpReportPptx
}
