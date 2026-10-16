package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

type documentCapture struct {
	bytes.Buffer
	limit    int
	exceeded bool
}

func (b *documentCapture) Write(p []byte) (int, error) {
	if len(p) > b.limit-b.Len() {
		b.exceeded = true
		return 0, errors.New("document output limit exceeded")
	}
	return b.Buffer.Write(p)
}

// No shell, user-controlled arguments, inherited credentials or project cwd.
// This is resource-bounded, not an OS sandbox; callers must enforce policy.
func pdfText(ctx context.Context, input []byte, options ...documentOptions) ([]byte, error) {
	if len(input) > 32<<20 {
		return nil, errors.New("PDF exceeds 32 MiB input limit")
	}
	if !bytes.HasPrefix(input, []byte("%PDF-")) {
		return nil, errors.New("invalid PDF header")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	o := documentOptions{}
	if len(options) > 0 {
		o = options[0]
	}
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	out, err := runDocumentTool(ctx, "pdftotext", []string{"-layout", "-enc", "UTF-8", "-", "-"}, input, documentTextLimit, o.Restricted)
	if err != nil {
		return nil, err
	}
	if !utf8.Valid(out) {
		return nil, errors.New("PDF returned invalid UTF-8")
	}
	if o.Images != nil {
		pages := strings.Split(strings.TrimSuffix(string(out), "\f"), "\f")
		if len(pages) > 6 {
			return nil, errors.New("PDF visual analysis supports at most 6 pages; split the document or disable document_vision")
		}
		for i := range pages {
			data, err := runDocumentTool(ctx, "pdftoppm", []string{"-f", strconv.Itoa(i + 1), "-l", strconv.Itoa(i + 1), "-singlefile", "-scale-to", "1600", "-png", "-"}, input, 6<<20, o.Restricted)
			if err != nil {
				return nil, err
			}
			if err := appendDocumentImage(o.Images, data, fmt.Sprintf("%s PDF page %d", o.Source, i+1)); err != nil {
				return nil, err
			}
		}
	}
	if o.OCR {
		if o.Language == "" {
			o.Language = "eng+chi_sim+chi_tra"
		}
		if !regexp.MustCompile(`^[a-z_]+(?:\+[a-z_]+)*$`).MatchString(o.Language) || len(o.Language) > 80 {
			return nil, errors.New("invalid OCR language")
		}
		pages := strings.Split(strings.TrimSuffix(string(out), "\f"), "\f")
		count := 0
		for i, page := range pages {
			if strings.TrimSpace(page) != "" {
				continue
			}
			count++
			if count == 1 {
				available, err := runDocumentTool(ctx, "tesseract", []string{"--list-langs"}, nil, 8192, o.Restricted)
				if err != nil {
					return nil, err
				}
				installed := map[string]bool{}
				for _, line := range strings.Split(string(available), "\n") {
					installed[strings.TrimSpace(line)] = true
				}
				for _, language := range strings.Split(o.Language, "+") {
					if !installed[language] {
						return nil, fmt.Errorf("OCR language data missing: %s; install it or explicitly choose another ocr_language", language)
					}
				}
			}
			if count > 20 {
				return nil, errors.New("PDF exceeds 20 OCR-page limit; split the document")
			}
			image, err := runDocumentTool(ctx, "pdftoppm", []string{"-f", strconv.Itoa(i + 1), "-l", strconv.Itoa(i + 1), "-singlefile", "-scale-to", "2200", "-png", "-"}, input, 16<<20, o.Restricted)
			if err != nil {
				return nil, err
			}
			text, err := runDocumentTool(ctx, "tesseract", []string{"stdin", "stdout", "-l", o.Language}, image, documentTextLimit, o.Restricted)
			if err != nil {
				return nil, err
			}
			if !utf8.Valid(text) {
				return nil, errors.New("OCR returned invalid UTF-8")
			}
			if strings.TrimSpace(string(text)) != "" {
				pages[i] = "[OCR: " + o.Language + "; may contain recognition errors]\n" + string(text)
			}
		}
		out = []byte(strings.Join(pages, "\f"))
	}
	if o.Images != nil && strings.TrimSpace(string(out)) == "" {
		return []byte("PDF pages attached as visual evidence; no text layer or OCR text available."), nil
	}
	return labelPDFPages(string(out))
}

func labelPDFPages(text string) ([]byte, error) {
	pages := strings.Split(strings.TrimSuffix(text, "\f"), "\f")
	if len(pages) > 2000 {
		return nil, errors.New("PDF exceeds 2000 page limit")
	}
	var result strings.Builder
	hasText := false
	for i, page := range pages {
		page = strings.TrimSpace(page)
		if page == "" {
			page = "[No extractable text on this page; it may be blank or require OCR.]"
		} else {
			hasText = true
		}
		fmt.Fprintf(&result, "[Page %d]\n%s\n\n", i+1, page)
		if result.Len() > documentTextLimit {
			return nil, errors.New("PDF extracted text exceeds 256 KiB")
		}
	}
	if !hasText {
		return nil, errors.New("PDF has no extractable text; enable document_ocr or document_vision, or provide a clearer scan")
	}
	return []byte(result.String()), nil
}
