package main

import (
	"context"
	"fmt"
	"google.golang.org/genai"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func pdfFixture() []byte {
	stream := "BT /F1 18 Tf 40 200 Td (PDF_NATIVE_EVIDENCE) Tj ET"
	objects := []string{
		"<< /Type /Catalog /Pages 2 0 R >>",
		"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
		"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
		fmt.Sprintf("<< /Length %d >>\nstream\n%s\nendstream", len(stream), stream),
	}
	return pdfFromObjects(objects)
}

func pdfFromObjects(objects []string) []byte {
	var b strings.Builder
	b.WriteString("%PDF-1.4\n")
	offsets := []int{0}
	for i, obj := range objects {
		offsets = append(offsets, b.Len())
		fmt.Fprintf(&b, "%d 0 obj\n%s\nendobj\n", i+1, obj)
	}
	xref := b.Len()
	fmt.Fprintf(&b, "xref\n0 %d\n0000000000 65535 f \n", len(offsets))
	for _, offset := range offsets[1:] {
		fmt.Fprintf(&b, "%010d 00000 n \n", offset)
	}
	fmt.Fprintf(&b, "trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n", len(offsets), xref)
	return []byte(b.String())
}

func TestPDFRealConverter(t *testing.T) {
	if _, err := exec.LookPath("pdftotext"); err != nil {
		t.Skip("Poppler not installed")
	}
	body, err := pdfText(context.Background(), pdfFixture())
	if err != nil || !strings.Contains(string(body), "[Page 1]\nPDF_NATIVE_EVIDENCE") {
		t.Fatalf("%s: %v", body, err)
	}
	if _, err := pdfText(context.Background(), []byte("%PDF-broken")); err == nil {
		t.Fatal("damaged PDF accepted")
	}
	if _, err := exec.LookPath("pdftoppm"); err == nil {
		var parts []*genai.Part
		if _, err := pdfText(context.Background(), pdfFixture(), documentOptions{Images: &parts, Source: "chart.pdf"}); err != nil {
			t.Fatal(err)
		}
		if len(parts) != 2 || parts[1].InlineData == nil || !strings.Contains(parts[0].Text, "chart.pdf PDF page 1") {
			t.Fatal("PDF visual evidence or citation lost")
		}
	}
	s := approvalServer(t, config{}, appOptions{allowedTools: []string{"read_file", "run_command"}})
	if err := os.WriteFile(filepath.Join(s.scope.workspace, "report.pdf"), pdfFixture(), 0600); err != nil {
		t.Fatal(err)
	}
	var completed bool
	hooks := agentHooks{approve: s.approve, emit: func(kind string, fields map[string]any) {
		if kind == "tool.completed" {
			completed = fields["success"] == true
		}
	}}
	snapshot, err := resolveReferences(context.Background(), s.scope, "@file:report.pdf", hooks)
	if err != nil || !completed || !strings.Contains(snapshot, "PDF_NATIVE_EVIDENCE") || !strings.Contains(snapshot, "OCR output") {
		t.Fatal(snapshot, err)
	}
}

func TestPDFBoundsAndMissingDependency(t *testing.T) {
	for _, input := range [][]byte{[]byte("not pdf"), make([]byte, (32<<20)+1)} {
		if _, err := pdfText(context.Background(), input); err == nil {
			t.Fatal("invalid input accepted")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := pdfText(ctx, pdfFixture()); err == nil {
		t.Fatal("ignored cancellation")
	}
	t.Setenv("PATH", t.TempDir())
	if _, err := pdfText(context.Background(), pdfFixture()); err == nil || !strings.Contains(err.Error(), "requires installed pdftotext") {
		t.Fatal(err)
	}
	if _, err := labelPDFPages("\f\f"); err == nil {
		t.Fatal("empty PDF accepted")
	}
	body, err := labelPDFPages("first\f\fthird\f")
	if err != nil || !strings.Contains(string(body), "[Page 2]\n[No extractable") || !strings.Contains(string(body), "[Page 3]\nthird") {
		t.Fatal(string(body), err)
	}
	if _, err := labelPDFPages(strings.Repeat("a", documentTextLimit)); err == nil {
		t.Fatal("labels bypassed limit")
	}
	capture := &documentCapture{limit: 4}
	if _, err := capture.Write([]byte("12345")); err == nil || !capture.exceeded || capture.Len() != 0 {
		t.Fatal("unbounded capture")
	}
}

func TestPDFReferenceRequiresConverterApproval(t *testing.T) {
	for _, mode := range []string{"denied", "readonly", "offline", "remote"} {
		t.Run(mode, func(t *testing.T) {
			s := approvalServer(t, config{}, appOptions{})
			c := config{}
			if mode == "readonly" {
				s.scope.readOnly = true
			}
			if mode == "offline" {
				c["network_enabled"] = false
			}
			if mode == "remote" {
				t.Setenv("ARIA_RUNTIME_SCOPE", "remote")
			}
			hooks := agentHooks{approve: func(_ context.Context, tool string, _ map[string]any, _ fileChange) (bool, string, error) {
				return tool == "read_file", "denied", nil
			}, emit: func(string, map[string]any) { t.Fatal("converter started before permission") }}
			if _, err := resolveReferences(context.Background(), s.scope, "@file:report.pdf", hooks, c); err == nil {
				t.Fatal("PDF converter bypassed policy")
			}
		})
	}
}
