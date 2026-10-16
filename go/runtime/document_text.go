package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"path"
	"path/filepath"
	"strings"
	"unicode/utf8"
)

const documentTextLimit = 256 << 10

func readReferenceDocument(ctx context.Context, file *scopedPath, filename string, options ...documentOptions) ([]byte, error) {
	ext := strings.ToLower(filepath.Ext(filename))
	if ext == ".pdf" {
		body, err := readRegular(file, 32<<20)
		if err != nil {
			return nil, err
		}
		return pdfText(ctx, body, options...)
	}
	if ext != ".docx" && ext != ".pptx" {
		body, err := readRegular(file, documentTextLimit)
		if err == nil && (!utf8.Valid(body) || bytes.IndexByte(body, 0) >= 0) {
			return nil, errors.New("unsupported binary document; supported: UTF-8 text, DOCX, PPTX and PDF text/OCR")
		}
		return body, err
	}
	body, err := readRegular(file, 32<<20)
	if err != nil {
		return nil, err
	}
	return officeText(ctx, body, ext)
}

func officeText(ctx context.Context, body []byte, ext string) ([]byte, error) {
	archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		return nil, errors.New("invalid or encrypted Office document")
	}
	if len(archive.File) > 20000 {
		return nil, errors.New("Office archive contains too many entries")
	}
	files := map[string]*zip.File{}
	for _, f := range archive.File {
		if files[f.Name] != nil {
			return nil, errors.New("duplicate Office archive entry")
		}
		files[f.Name] = f
	}
	budget := 16 << 20
	read := func(name string) ([]byte, error) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		f := files[name]
		if f == nil {
			return nil, fmt.Errorf("missing Office component: %s", name)
		}
		if f.UncompressedSize64 > 8<<20 || f.UncompressedSize64 > uint64(budget) {
			return nil, errors.New("Office XML exceeds decompression budget")
		}
		r, err := f.Open()
		if err != nil {
			return nil, err
		}
		defer r.Close()
		data, err := io.ReadAll(io.LimitReader(r, int64(min(8<<20, budget))+1))
		if err != nil {
			return nil, err
		}
		budget -= len(data)
		if budget < 0 || len(data) > 8<<20 {
			return nil, errors.New("Office XML exceeds decompression budget")
		}
		return data, nil
	}
	names := []string{"word/document.xml"}
	if ext == ".pptx" {
		manifest, err := read("ppt/presentation.xml")
		if err != nil {
			return nil, err
		}
		rels, err := read("ppt/_rels/presentation.xml.rels")
		if err != nil {
			return nil, err
		}
		var presentation struct {
			Slides []struct {
				ID string `xml:"http://schemas.openxmlformats.org/officeDocument/2006/relationships id,attr"`
			} `xml:"sldIdLst>sldId"`
		}
		var relationships struct {
			Entries []struct {
				ID     string `xml:"Id,attr"`
				Target string `xml:"Target,attr"`
				Mode   string `xml:"TargetMode,attr"`
				Type   string `xml:"Type,attr"`
			} `xml:"Relationship"`
		}
		if err := xml.Unmarshal(manifest, &presentation); err != nil {
			return nil, err
		}
		if err := xml.Unmarshal(rels, &relationships); err != nil {
			return nil, err
		}
		targets := map[string]string{}
		for _, r := range relationships.Entries {
			if r.Mode == "External" || !strings.HasSuffix(r.Type, "/slide") {
				continue
			}
			target := path.Clean(path.Join("ppt", r.Target))
			if strings.HasPrefix(r.Target, "/") {
				target = strings.TrimPrefix(path.Clean(r.Target), "/")
			}
			if !strings.HasPrefix(target, "ppt/slides/") {
				return nil, errors.New("invalid slide relationship target")
			}
			if targets[r.ID] != "" {
				return nil, errors.New("duplicate slide relationship")
			}
			targets[r.ID] = target
		}
		names = nil
		for _, slide := range presentation.Slides {
			if targets[slide.ID] == "" {
				return nil, errors.New("missing or external slide relationship")
			}
			names = append(names, targets[slide.ID])
		}
		if len(names) > 2000 {
			return nil, errors.New("too many slides")
		}
	}
	var output strings.Builder
	hasText := false
	for i, name := range names {
		data, err := read(name)
		if err != nil {
			return nil, err
		}
		if ext == ".pptx" {
			fmt.Fprintf(&output, "\n[Slide %d]\n", i+1)
		}
		decoder := xml.NewDecoder(bytes.NewReader(data))
		var chartIDs []string
		inText := false
		for {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			token, err := decoder.Token()
			if err == io.EOF {
				break
			}
			if err != nil {
				return nil, fmt.Errorf("invalid Office XML: %w", err)
			}
			switch t := token.(type) {
			case xml.StartElement:
				if t.Name.Local == "chart" && strings.HasSuffix(t.Name.Space, "/chart") {
					for _, attr := range t.Attr {
						if attr.Name.Local == "id" && strings.HasSuffix(attr.Name.Space, "/relationships") {
							chartIDs = append(chartIDs, attr.Value)
						}
					}
				}
				if t.Name.Local == "t" {
					inText = true
				}
				if t.Name.Local == "tab" {
					output.WriteByte('\t')
				}
				if t.Name.Local == "br" {
					output.WriteByte('\n')
				}
			case xml.EndElement:
				if t.Name.Local == "t" {
					inText = false
				}
				if t.Name.Local == "p" {
					output.WriteByte('\n')
				}
			case xml.CharData:
				if inText {
					output.Write(t)
					hasText = hasText || strings.TrimSpace(string(t)) != ""
				}
			}
			if output.Len() > documentTextLimit {
				return nil, errors.New("extracted document text exceeds 256 KiB; split the document")
			}
		}
		charts, err := officeCharts(ctx, name, chartIDs, read)
		if err != nil {
			return nil, err
		}
		output.WriteString(charts)
		hasText = hasText || charts != ""
		if output.Len() > documentTextLimit {
			return nil, errors.New("extracted document text exceeds 256 KiB; split the document")
		}
	}
	if !hasText {
		return nil, errors.New("document has no extractable text; image-only content requires OCR")
	}
	return []byte(output.String()), nil
}
