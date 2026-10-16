package main

import (
	"archive/zip"
	"bytes"
	"context"
	"strings"
	"testing"
)

func officeFixture(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var b bytes.Buffer
	w := zip.NewWriter(&b)
	for name, text := range files {
		f, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := f.Write([]byte(text)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

func TestOfficeTextMainBodyAndPresentationOrder(t *testing.T) {
	docx := officeFixture(t, map[string]string{"word/document.xml": `<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>中文 &amp; evidence</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>table 123</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>`})
	body, err := officeText(context.Background(), docx, ".docx")
	if err != nil || !strings.Contains(string(body), "中文 & evidence\ntable 123") {
		t.Fatal(string(body), err)
	}
	pptx := officeFixture(t, map[string]string{
		"ppt/presentation.xml":            `<p:presentation xmlns:p="p" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="second"/><p:sldId r:id="first"/></p:sldIdLst></p:presentation>`,
		"ppt/_rels/presentation.xml.rels": `<Relationships><Relationship Id="first" Target="slides/slide1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"/><Relationship Id="second" Target="slides/slide2.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"/></Relationships>`,
		"ppt/slides/slide1.xml":           `<sld><p><t>File one</t></p></sld>`,
		"ppt/slides/slide2.xml":           `<sld><p><t>File two</t></p></sld>`,
	})
	body, err = officeText(context.Background(), pptx, ".pptx")
	if err != nil || strings.Index(string(body), "File two") > strings.Index(string(body), "File one") || !strings.Contains(string(body), "[Slide 2]") {
		t.Fatal(string(body), err)
	}
}

func TestOfficeTextRejectsBrokenEmptyAndOversizedDocuments(t *testing.T) {
	for _, content := range []string{`<document><t>broken`, `<document/>`, `<document><t>` + strings.Repeat("x", documentTextLimit+1) + `</t></document>`, strings.Repeat("x", (8<<20)+1)} {
		if _, err := officeText(context.Background(), officeFixture(t, map[string]string{"word/document.xml": content}), ".docx"); err == nil {
			t.Fatal("invalid document accepted")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := officeText(ctx, officeFixture(t, map[string]string{"word/document.xml": `<t>hello</t>`}), ".docx"); err == nil {
		t.Fatal("cancel ignored")
	}
}
