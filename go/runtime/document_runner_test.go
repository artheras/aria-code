package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"fmt"
	"image"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
)

func TestDocumentSandboxDeniesPrivateReadAndWrite(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("macOS Seatbelt")
	}
	private := filepath.Join(t.TempDir(), "private.txt")
	if err := os.WriteFile(private, []byte("PRIVATE_CANARY"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := runDocumentTool(context.Background(), "cat", []string{private}, nil, 1024, true); err == nil {
		t.Fatal("sandbox exposed private file")
	}
	if _, err := runDocumentTool(context.Background(), "touch", []string{private + ".written"}, nil, 1024, true); err == nil {
		t.Fatal("sandbox allowed write")
	}
	if _, err := os.Stat(private + ".written"); !os.IsNotExist(err) {
		t.Fatal("file created")
	}
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests.Add(1); w.WriteHeader(200) }))
	defer server.Close()
	if _, err := runDocumentTool(context.Background(), "curl", []string{"--max-time", "2", server.URL}, nil, 1024, true); err == nil {
		t.Fatal("sandbox allowed network")
	}
	if requests.Load() != 0 {
		t.Fatal("network request escaped sandbox")
	}
	if _, err := exec.LookPath("pdftotext"); err == nil {
		body, err := pdfText(context.Background(), pdfFixture(), documentOptions{Restricted: true})
		if err != nil || !strings.Contains(string(body), "PDF_NATIVE_EVIDENCE") {
			t.Fatal("restricted PDF cannot parse", err)
		}
	}
}

func TestChineseOCRDependenciesAndRealImage(t *testing.T) {
	if os.Getenv("ARIA_OCR_DATA") == "" {
		t.Skip("set ARIA_OCR_DATA for multilingual OCR integration")
	}
	for _, tool := range []string{"tesseract", "pdftoppm"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Fatal(err)
		}
	}
	langs, err := runDocumentTool(context.Background(), "tesseract", []string{"--list-langs"}, nil, 8192, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, lang := range []string{"eng", "chi_sim", "chi_tra"} {
		if !strings.Contains(string(langs), lang) {
			t.Fatalf("missing %s", lang)
		}
	}
	imageData, err := runDocumentTool(context.Background(), "pdftoppm", []string{"-singlefile", "-scale-to", "2200", "-png", "-"}, pdfFixture(), 16<<20, false)
	if err != nil {
		t.Fatal(err)
	}
	text, err := runDocumentTool(context.Background(), "tesseract", []string{"stdin", "stdout", "-l", "eng+chi_sim+chi_tra"}, imageData, documentTextLimit, false)
	if err != nil || !strings.Contains(string(text), "EVIDENCE") {
		t.Fatalf("OCR %q: %v", text, err)
	}
	if fixture := os.Getenv("ARIA_OCR_TEST_IMAGE"); fixture != "" {
		data, err := os.ReadFile(fixture)
		if err != nil {
			t.Fatal(err)
		}
		img, _, err := image.Decode(bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		var rgb bytes.Buffer
		writer := zlib.NewWriter(&rgb)
		bounds := img.Bounds()
		for y := bounds.Min.Y; y < bounds.Max.Y; y++ {
			for x := bounds.Min.X; x < bounds.Max.X; x++ {
				r, g, b, _ := img.At(x, y).RGBA()
				_, _ = writer.Write([]byte{byte(r >> 8), byte(g >> 8), byte(b >> 8)})
			}
		}
		_ = writer.Close()
		stream := "q 600 0 0 200 0 0 cm /Im0 Do Q"
		pdf := pdfFromObjects([]string{
			"<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
			"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 200] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
			fmt.Sprintf("<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream", bounds.Dx(), bounds.Dy(), rgb.Len(), rgb.String()),
			fmt.Sprintf("<< /Length %d >>\nstream\n%s\nendstream", len(stream), stream),
		})
		text, err := pdfText(context.Background(), pdf, documentOptions{OCR: true, Language: "eng+chi_sim+chi_tra", Restricted: documentSandboxAvailable()})
		if err != nil || !strings.Contains(string(text), "数据") || !strings.Contains(string(text), "報告") || !strings.Contains(string(text), "12345") || !strings.Contains(string(text), "[OCR:") {
			t.Fatalf("multilingual scanned PDF %q: %v", text, err)
		}
		t.Setenv("ARIA_OCR_DATA", t.TempDir())
		if _, err := pdfText(context.Background(), pdf, documentOptions{OCR: true, Language: "eng+chi_sim+chi_tra"}); err == nil || !strings.Contains(err.Error(), "language data missing") {
			t.Fatal("missing languages silently accepted", err)
		}
	}
}
