package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/xml"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const sampleReport = `# Quarterly review 季度复盘

> Generated 2026-11-02 | Data to 2026-10-31

Revenue grew **12%** and margins held; see *notes* and ` + "`calc.py`" + `.

## Positions

| Symbol | Weight | Signal |
|---|---|---|
| AAPL | 20% | BUY |
| MSFT | 15% | HOLD |

- Rebalance monthly
  - Cap single names at 20%
- Review risk weekly

<script>alert(1)</script>

` + "```\nprint('ok')\n```\n"

func TestMarkdownBlocks(t *testing.T) {
	blocks := markdownBlocks(sampleReport)
	kinds := []string{}
	for _, b := range blocks {
		kinds = append(kinds, b.kind)
	}
	want := "heading para para heading table bullet bullet bullet code"
	if got := strings.Join(kinds, " "); !strings.HasPrefix(got, "heading para para heading table bullet bullet bullet") || !strings.HasSuffix(got, "code") {
		t.Fatalf("blocks %q, want about %q", got, want)
	}
	var bold, code bool
	for _, r := range blocks[2].runs {
		bold = bold || (r.bold && r.text == "12%")
		code = code || (r.code && r.text == "calc.py")
	}
	if !bold || !code {
		t.Fatal("inline formatting lost", blocks[2].runs)
	}
	if table := blocks[4]; len(table.rows) != 3 || runsText(table.rows[1][2]) != "BUY" {
		t.Fatal(table.rows)
	}
	if blocks[6].level != 2 || blocks[5].level != 1 {
		t.Fatal("list depth", blocks[5].level, blocks[6].level)
	}
}

func TestDocumentHTML(t *testing.T) {
	page, err := renderDocumentHTML(sampleReport, "")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`<html lang="en"`, "Content-Security-Policy", `<div class="meta">`, `class="badge"`, "<title>Quarterly review 季度复盘</title>"} {
		if !strings.Contains(page, want) {
			t.Fatal("missing", want)
		}
	}
	if strings.Contains(page, "<script>") {
		t.Fatal("raw HTML was not escaped")
	}
	if page, _ := renderDocumentHTML("# 季度复盘\n\n收入增长百分之十二，利润率保持稳定。", ""); !strings.Contains(page, `<html lang="zh"`) || !strings.Contains(page, "仅供研究参考") {
		t.Fatal("Chinese text should get the Chinese template")
	}
	wide := "|" + strings.Repeat(" c |", 10) + "\n|" + strings.Repeat("---|", 10) + "\n|" + strings.Repeat(" 1 |", 10) + "\n"
	if page, _ := renderDocumentHTML(wide, "t"); !strings.Contains(page, `class="t-wide"`) {
		t.Fatal("wide table not narrowed")
	}
}

// wellFormed parses every XML part of an OOXML package.
func wellFormed(t *testing.T, data []byte) map[string]string {
	t.Helper()
	r, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	parts := map[string]string{}
	for _, f := range r.File {
		rc, _ := f.Open()
		body, _ := io.ReadAll(rc)
		rc.Close()
		d := xml.NewDecoder(bytes.NewReader(body))
		for {
			if _, err := d.Token(); err == io.EOF {
				break
			} else if err != nil {
				t.Fatalf("%s: %v", f.Name, err)
			}
		}
		parts[f.Name] = string(body)
	}
	return parts
}

// pythonCheck runs a python-docx or python-pptx script on a file, when installed.
func pythonCheck(t *testing.T, module, script, path string) string {
	t.Helper()
	if exec.Command("python3", "-c", "import "+module).Run() != nil {
		t.Logf("python3 %s not installed; checked well-formedness only", module)
		return ""
	}
	out, err := exec.Command("python3", "-c", script, path).CombinedOutput()
	if err != nil {
		t.Fatal(string(out), err)
	}
	return string(out)
}

func TestDocxAndPptx(t *testing.T) {
	dir := t.TempDir()
	blocks := markdownBlocks(sampleReport)
	docx, err := buildDocx("Q3 <review> & notes", blocks)
	if err != nil {
		t.Fatal(err)
	}
	parts := wellFormed(t, docx)
	if !strings.Contains(parts["word/document.xml"], "Q3 &lt;review&gt; &amp; notes") || !strings.Contains(parts["word/document.xml"], "<w:tbl>") {
		t.Fatal("title or table missing")
	}
	path := filepath.Join(dir, "r.docx")
	os.WriteFile(path, docx, 0o644)
	if out := pythonCheck(t, "docx", `import sys, docx
d = docx.Document(sys.argv[1])
print(len(d.tables), d.tables[0].cell(1, 2).text, [p.style.name for p in d.paragraphs][:3])`, path); out != "" &&
		(!strings.HasPrefix(out, "1 BUY") || !strings.Contains(out, "Heading 1")) {
		t.Fatal(out)
	}
	pptx, err := buildPptx("Deck", blocks)
	if err != nil {
		t.Fatal(err)
	}
	wellFormed(t, pptx)
	path = filepath.Join(dir, "r.pptx")
	os.WriteFile(path, pptx, 0o644)
	if out := pythonCheck(t, "pptx", `import sys, pptx
p = pptx.Presentation(sys.argv[1])
print(len(p.slides), "|".join(sh.text_frame.text.split("\n")[0] for s in p.slides for sh in s.shapes if sh.name == "Title"))`, path); out != "" &&
		!strings.HasPrefix(out, "3 Deck|Quarterly review 季度复盘|Positions") {
		t.Fatal(out)
	}
	long := strings.Repeat("- point\n", 30)
	if slides := slidesOf("t", markdownBlocks("# Many\n"+long)); len(slides) != 3 || slides[1].title != "Many (cont.)" {
		t.Fatal(len(slides))
	}
}

func TestReportTools(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Chdir(home)
	ctx := context.Background()
	for _, tool := range []func(context.Context, map[string]any) map[string]any{mcpReportHTML, mcpReportDocx, mcpReportPptx} {
		r := tool(ctx, map[string]any{"markdown": sampleReport, "title": "Q3 / review"})
		if r["success"] != true || !strings.HasPrefix(r["path"].(string), filepath.Join(home, ".aria", "artifacts", "report", "Q3_review_")) {
			t.Fatal(r)
		}
	}
	os.WriteFile(filepath.Join(home, "in.md"), []byte("# From a file\n"), 0o644)
	named := filepath.Join(home, "named.docx")
	if r := mcpReportDocx(ctx, map[string]any{"markdown_path": "in.md", "output_path": named}); r["success"] != true {
		t.Fatal(r)
	}
	for _, args := range []map[string]any{
		{"markdown": ""},
		{"markdown": "x", "markdown_path": "in.md"},
		{"markdown_path": "/etc/passwd"},
		{"markdown": "x", "output_path": named},
		{"markdown": "x", "output_path": filepath.Join(home, "wrong.pdf")},
		{"markdown": strings.Repeat("x", maxMarkdownBytes+1)},
	} {
		if r := mcpReportDocx(ctx, args); r["success"] != false {
			t.Fatal("accepted", r)
		}
	}
}

// fakePDFBrowser stands in for Chrome's --print-to-pdf=PATH: it writes a
// minimal PDF and, on Unix, keeps the page it was asked to print beside it.
// Headless Chrome needs a working sandbox, which some CI runners lack.
func fakePDFBrowser(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "fake browser")
	os.MkdirAll(dir, 0o755)
	if runtime.GOOS == "windows" {
		path := filepath.Join(dir, "browser.cmd")
		script := "@echo off\r\n:loop\r\nif \"%~1\"==\"\" goto :eof\r\nset \"a=%~1\"\r\nif \"%a:~0,15%\"==\"--print-to-pdf=\" echo %%PDF-1.4> \"%a:~15%\"\r\nshift\r\ngoto loop\r\n"
		os.WriteFile(path, []byte(script), 0o755)
		return path
	}
	path := filepath.Join(dir, "browser")
	os.WriteFile(path, []byte("#!/bin/sh\nfor a; do case \"$a\" in --print-to-pdf=*) out=\"${a#--print-to-pdf=}\";; file://*) page=\"${a#file://}\";; esac; done\n"+
		"cp \"$page\" \"$out.html\" && printf '%%PDF-1.4' > \"$out\"\n"), 0o755)
	return path
}

func TestReportPDF(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Setenv("ARIA_BROWSER", fakePDFBrowser(t))
	t.Chdir(home)
	r := mcpReportPDF(context.Background(), map[string]any{"markdown": sampleReport})
	if r["success"] != true {
		t.Fatal(r)
	}
	out := r["path"].(string)
	if data, _ := os.ReadFile(out); !bytes.HasPrefix(data, []byte("%PDF-")) {
		t.Fatal("not a PDF")
	}
	if runtime.GOOS != "windows" {
		page, _ := os.ReadFile(out + ".html")
		if !bytes.Contains(page, []byte("Content-Security-Policy")) || !bytes.Contains(page, []byte("<table>")) {
			t.Fatal("the browser was not given the rendered page")
		}
	}
	t.Setenv("ARIA_BROWSER", filepath.Join(home, "missing-browser"))
	if r := mcpReportPDF(context.Background(), map[string]any{"markdown": "# x"}); r["success"] != false {
		t.Fatal(r)
	}
}

// TestRealReportPDF prints with a real browser: ARIA_REAL_BROWSER=/path/to/chrome.
func TestRealReportPDF(t *testing.T) {
	browser := os.Getenv("ARIA_REAL_BROWSER")
	if browser == "" {
		t.Skip("set ARIA_REAL_BROWSER to a Chrome, Chromium or Edge")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Setenv("ARIA_BROWSER", browser)
	t.Chdir(home)
	r := mcpReportPDF(context.Background(), map[string]any{"markdown": sampleReport})
	if r["success"] != true {
		t.Fatal(r)
	}
	if data, _ := os.ReadFile(r["path"].(string)); !bytes.HasPrefix(data, []byte("%PDF-")) || len(data) < 2000 {
		t.Fatal("not a real PDF")
	}
}
