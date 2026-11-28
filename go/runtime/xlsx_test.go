package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 1x1 transparent PNG
const testTinyPNGBase64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

func TestXlsxWriteAndReadRoundTrip(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Chdir(home)

	ctx := context.Background()
	outPath := filepath.Join(home, "dcf_model.xlsx")

	res := mcpReportXlsx(ctx, map[string]any{
		"title":       "ACME DCF & Inventory",
		"output_path": outPath,
		"provenance": map[string]any{
			"source":      "SEC 10-K & ACME WMS",
			"as_of":       "2026-11-27",
			"owner_id":    "ACME",
			"assumptions": "WACC 9.5%, terminal growth 2.5%",
			"limitations": "Historical demand only",
		},
		"sheets": []any{
			map[string]any{
				"name": "Assumptions",
				"columns": []any{
					map[string]any{"header": "Driver", "width": 24},
					map[string]any{"header": "Value", "format": "percent"},
				},
				"rows": []any{
					[]any{"WACC", 0.095},
					[]any{"Terminal Growth", 0.025},
				},
			},
			map[string]any{
				"name": "DCF Summary",
				"columns": []any{
					"Metric",
					map[string]any{"header": "2026E", "format": "currency"},
					map[string]any{"header": "2027E", "format": "currency"},
				},
				"rows": []any{
					[]any{"Revenue", "$1,200.00", "=B2*(1+'Assumptions'!B3)"},
					[]any{"EBITDA", 360.0, "=C2*0.30"},
					[]any{"Total EV", "=B3*10", "=C3*10"},
				},
			},
		},
	})
	if res["success"] != true {
		t.Fatalf("mcpReportXlsx failed: %v", res)
	}
	if res["sheet_count"] != 3 {
		t.Fatalf("expected 3 sheets (Assumptions, DCF Summary, Audit_Provenance), got %v", res["sheet_count"])
	}
	if res["formula_count"] != 4 {
		t.Fatalf("expected 4 formulas, got %v", res["formula_count"])
	}

	raw, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatal(err)
	}
	parts := wellFormed(t, raw)
	if !strings.Contains(parts["xl/workbook.xml"], `fullCalcOnLoad="1"`) {
		t.Fatal("workbook missing fullCalcOnLoad=1")
	}
	// Blue font (#FF0000FF) for hardcoded inputs, green (#FF008000) for cross-sheet links
	if !strings.Contains(parts["xl/styles.xml"], `rgb="FF0000FF"`) || !strings.Contains(parts["xl/styles.xml"], `rgb="FF008000"`) {
		t.Fatal("styles.xml missing financial modeling color palette")
	}

	// Verify native Go reader & summarizer can read the generated .xlsx back
	sc, err := newScope(home, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	sp, err := sc.open("dcf_model.xlsx", false)
	if err != nil {
		t.Fatal(err)
	}
	defer sp.Close()

	txt, err := readReferenceDocument(ctx, sp, "dcf_model.xlsx")
	if err != nil {
		t.Fatalf("readReferenceDocument xlsx failed: %v", err)
	}
	textStr := string(txt)
	if !strings.Contains(textStr, "Audit_Provenance") || !strings.Contains(textStr, "SEC 10-K & ACME WMS") {
		t.Fatalf("extracted xlsx text missing Audit_Provenance: %s", textStr)
	}

	prof := inspectDataTool(ctx, sc, map[string]any{"path": "dcf_model.xlsx", "sheet": "DCF Summary"})
	if !prof.Success {
		t.Fatalf("inspectDataTool xlsx failed: %s", prof.Error)
	}
	if prof.Data["formula_count"] != 4 {
		t.Fatalf("expected formula_count=4 in DCF Summary, got %v", prof.Data["formula_count"])
	}
}

func TestXlsxRejectsFormulaErrors(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Chdir(home)

	res := mcpReportXlsx(context.Background(), map[string]any{
		"title": "BadModel",
		"sheets": []any{
			map[string]any{
				"name":    "Model",
				"columns": []any{"Item", "Val"},
				"rows": []any{
					[]any{"Broken", "=A1+#REF!"},
				},
			},
		},
	})
	if res["success"] != false {
		t.Fatalf("expected #REF! formula to be rejected, got %v", res)
	}
	if !strings.Contains(res["error"].(string), "#REF!") {
		t.Fatalf("unexpected error: %v", res["error"])
	}
}

func TestDocxTemplateAndPptxWidescreenWithImage(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("ARIA_HOME", filepath.Join(home, ".aria"))
	t.Chdir(home)

	imgBytes, err := base64.StdEncoding.DecodeString(testTinyPNGBase64)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(home, "chart.png"), imgBytes, 0o644); err != nil {
		t.Fatal(err)
	}

	sc, err := newScope(home, nil, nil, false)
	if err != nil {
		t.Fatal(err)
	}

	md := "# Executive Summary\n\n> Audited Q3 performance\n\n- Revenue +18%\n\n| Metric | Value |\n|---|---|\n| Margin | 24% |\n\n![Revenue Chart](chart.png)\n"
	docxBytes, err := buildDocxWithOptions(docxBuildOptions{Title: "IC Memo", Subtitle: "Confidential", Scope: sc}, markdownBlocks(md))
	if err != nil {
		t.Fatal(err)
	}
	docxParts := wellFormed(t, docxBytes)
	if _, ok := docxParts["word/media/image1.png"]; !ok {
		t.Fatal("expected embedded chart.png in word/media/")
	}
	if !strings.Contains(docxParts["word/header1.xml"], "IC Memo") || !strings.Contains(docxParts["word/footer1.xml"], "NUMPAGES") {
		t.Fatal("missing executive header or page-number footer")
	}

	// Test enterprise .docx template placeholder filling (including split runs)
	tmplBytes, err := zipBinaryFiles(map[string][]byte{
		"[Content_Types].xml":          []byte(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-officedocument.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>`),
		"_rels/.rels":                  []byte(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`),
		"word/_rels/document.xml.rels": []byte(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>`),
		"word/document.xml":            []byte(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{{client_</w:t></w:r><w:r><w:t>name}}</w:t></w:r></w:p></w:body></w:document>`),
	}, []string{"[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels", "word/document.xml"})
	if err != nil {
		t.Fatal(err)
	}
	filled, err := fillDocxTemplate(tmplBytes, map[string]string{"client_name": "ACME & Co"})
	if err != nil {
		t.Fatal(err)
	}
	filledParts := wellFormed(t, filled)
	if !strings.Contains(filledParts["word/document.xml"], "ACME &amp; Co") {
		t.Fatalf("split placeholder not replaced: %s", filledParts["word/document.xml"])
	}

	// Test 16:9 widescreen PPTX with native <a:tbl> and embedded image
	pptxBytes, err := buildPptxWithOptions(pptxBuildOptions{Title: "Q3 Deck", Subtitle: "Board Review", Scope: sc}, markdownBlocks(md))
	if err != nil {
		t.Fatal(err)
	}
	pptxParts := wellFormed(t, pptxBytes)
	if !strings.Contains(pptxParts["ppt/presentation.xml"], `cx="12192000" cy="6858000"`) {
		t.Fatal("expected 16:9 widescreen slide dimensions")
	}
	if !bytes.Contains([]byte(pptxParts["ppt/slides/slide2.xml"]), []byte("<a:tbl>")) {
		t.Fatal("expected native DrawingML <a:tbl> table on slide 2")
	}
	if _, ok := pptxParts["ppt/media/image1.png"]; !ok {
		t.Fatal("expected embedded chart.png in ppt/media/")
	}
}
