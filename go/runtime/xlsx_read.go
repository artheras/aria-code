package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"math"
	"path"
	"strconv"
	"strings"
	"time"
)

type xlsxSheet struct {
	Name         string
	Rows         [][]string
	FormulaCount int
	FormulaNotes []string
}

func xlsxColIndex(cellRef string) int {
	col := 0
	found := false
	for i := 0; i < len(cellRef); i++ {
		ch := cellRef[i]
		if ch == '$' {
			continue
		}
		if ch >= 'A' && ch <= 'Z' {
			col = col*26 + int(ch-'A'+1)
			found = true
		} else if ch >= 'a' && ch <= 'z' {
			col = col*26 + int(ch-'a'+1)
			found = true
		} else {
			break
		}
	}
	if !found || col <= 0 {
		return -1
	}
	return col - 1
}

func isExcelDateNumFmt(id int, code string) bool {
	if (id >= 14 && id <= 22) || (id >= 45 && id <= 47) {
		return true
	}
	if code == "" {
		return false
	}
	clean := strings.ToLower(code)
	// Strip quoted literals and color brackets like [Red]
	for {
		start := strings.IndexByte(clean, '[')
		end := strings.IndexByte(clean, ']')
		if start >= 0 && end > start {
			clean = clean[:start] + clean[end+1:]
		} else {
			break
		}
	}
	if strings.ContainsAny(clean, "#0?") {
		return false
	}
	return strings.Contains(clean, "yy") || (strings.Contains(clean, "m") && strings.Contains(clean, "d"))
}

func formatExcelSerialDate(serial float64) (string, bool) {
	if math.IsNaN(serial) || math.IsInf(serial, 0) || serial < 1 || serial > 2958465 {
		return "", false
	}
	// Excel 1900 leap-year bug: serial 60 is the non-existent 1900-02-29.
	days := int(math.Floor(serial))
	frac := serial - float64(days)
	if days > 60 {
		days--
	}
	base := time.Date(1899, 12, 31, 0, 0, 0, 0, time.UTC)
	ts := base.AddDate(0, 0, days).Add(time.Duration(math.Round(frac*86400)) * time.Second)
	if frac < 1e-6 {
		return ts.Format("2006-01-02"), true
	}
	return ts.Format("2006-01-02 15:04:05"), true
}

func parseXlsxDateStyles(raw []byte) map[int]bool {
	var styles struct {
		NumFmts []struct {
			ID   int    `xml:"numFmtId,attr"`
			Code string `xml:"formatCode,attr"`
		} `xml:"numFmts>numFmt"`
		CellXfs []struct {
			NumFmtID int `xml:"numFmtId,attr"`
		} `xml:"cellXfs>xf"`
	}
	if xml.Unmarshal(raw, &styles) != nil {
		return nil
	}
	codes := map[int]string{}
	for _, nf := range styles.NumFmts {
		codes[nf.ID] = nf.Code
	}
	dateStyles := map[int]bool{}
	for idx, xf := range styles.CellXfs {
		if isExcelDateNumFmt(xf.NumFmtID, codes[xf.NumFmtID]) {
			dateStyles[idx] = true
		}
	}
	return dateStyles
}

func parseXlsxSharedStrings(ctx context.Context, raw []byte) ([]string, error) {
	dec := xml.NewDecoder(bytes.NewReader(raw))
	var out []string
	var cur strings.Builder
	inSI := false
	inT := false
	inPhonetic := false
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("invalid XLSX sharedStrings XML: %w", err)
		}
		switch t := tok.(type) {
		case xml.StartElement:
			switch t.Name.Local {
			case "si":
				inSI = true
				cur.Reset()
			case "rPh":
				inPhonetic = true
			case "t":
				if inSI && !inPhonetic {
					inT = true
				}
			}
		case xml.EndElement:
			switch t.Name.Local {
			case "t":
				inT = false
			case "rPh":
				inPhonetic = false
			case "si":
				inSI = false
				out = append(out, cur.String())
				if len(out) > 250000 {
					return nil, errors.New("XLSX sharedStrings exceeds item limit")
				}
			}
		case xml.CharData:
			if inSI && inT && !inPhonetic {
				cur.Write(t)
			}
		}
	}
	return out, nil
}

func xlsxReadWorkbook(ctx context.Context, body []byte, maxRowsPerSheet int) ([]xlsxSheet, error) {
	archive, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		return nil, errors.New("invalid or encrypted Excel workbook")
	}
	if len(archive.File) > 20000 {
		return nil, errors.New("Excel archive contains too many entries")
	}
	files := map[string]*zip.File{}
	for _, f := range archive.File {
		if files[f.Name] != nil {
			return nil, errors.New("duplicate Excel archive entry")
		}
		files[f.Name] = f
	}
	budget := 32 << 20
	read := func(name string) ([]byte, error) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		f := files[name]
		if f == nil {
			return nil, fmt.Errorf("missing Excel component: %s", name)
		}
		if f.UncompressedSize64 > 16<<20 || f.UncompressedSize64 > uint64(budget) {
			return nil, errors.New("Excel XML exceeds decompression budget")
		}
		r, err := f.Open()
		if err != nil {
			return nil, err
		}
		defer r.Close()
		data, err := io.ReadAll(io.LimitReader(r, int64(min(16<<20, budget))+1))
		if err != nil {
			return nil, err
		}
		budget -= len(data)
		if budget < 0 || len(data) > 16<<20 {
			return nil, errors.New("Excel XML exceeds decompression budget")
		}
		return data, nil
	}

	wbRaw, err := read("xl/workbook.xml")
	if err != nil {
		return nil, err
	}
	relsRaw, err := read("xl/_rels/workbook.xml.rels")
	if err != nil {
		return nil, err
	}

	var wb struct {
		Sheets []struct {
			Name  string `xml:"name,attr"`
			State string `xml:"state,attr"`
			RelID string `xml:"http://schemas.openxmlformats.org/officeDocument/2006/relationships id,attr"`
		} `xml:"sheets>sheet"`
	}
	var rels struct {
		Entries []struct {
			ID     string `xml:"Id,attr"`
			Target string `xml:"Target,attr"`
			Mode   string `xml:"TargetMode,attr"`
			Type   string `xml:"Type,attr"`
		} `xml:"Relationship"`
	}
	if err := xml.Unmarshal(wbRaw, &wb); err != nil {
		return nil, fmt.Errorf("invalid workbook.xml: %w", err)
	}
	if err := xml.Unmarshal(relsRaw, &rels); err != nil {
		return nil, fmt.Errorf("invalid workbook.xml.rels: %w", err)
	}

	targets := map[string]string{}
	for _, r := range rels.Entries {
		if r.Mode == "External" || !strings.HasSuffix(r.Type, "/worksheet") {
			continue
		}
		target := path.Clean(path.Join("xl", r.Target))
		if strings.HasPrefix(r.Target, "/") {
			target = strings.TrimPrefix(path.Clean(r.Target), "/")
		}
		if !strings.HasPrefix(target, "xl/worksheets/") {
			return nil, errors.New("invalid worksheet relationship target")
		}
		targets[r.ID] = target
	}

	var sharedStrings []string
	if files["xl/sharedStrings.xml"] != nil {
		ssRaw, err := read("xl/sharedStrings.xml")
		if err != nil {
			return nil, err
		}
		sharedStrings, err = parseXlsxSharedStrings(ctx, ssRaw)
		if err != nil {
			return nil, err
		}
	}

	var dateStyles map[int]bool
	if files["xl/styles.xml"] != nil {
		if stRaw, err := read("xl/styles.xml"); err == nil {
			dateStyles = parseXlsxDateStyles(stRaw)
		}
	}

	var sheets []xlsxSheet
	for idx, sh := range wb.Sheets {
		if len(sheets) >= 64 {
			break
		}
		target := targets[sh.RelID]
		if target == "" {
			continue
		}
		rawSheet, err := read(target)
		if err != nil {
			return nil, err
		}
		name := strings.TrimSpace(sh.Name)
		if name == "" {
			name = fmt.Sprintf("Sheet%d", idx+1)
		}
		parsed, err := parseXlsxWorksheet(ctx, name, rawSheet, sharedStrings, dateStyles, maxRowsPerSheet)
		if err != nil {
			return nil, err
		}
		sheets = append(sheets, parsed)
	}
	if len(sheets) == 0 {
		return nil, errors.New("Excel workbook contains no readable worksheets")
	}
	return sheets, nil
}

func parseXlsxWorksheet(
	ctx context.Context,
	name string,
	raw []byte,
	shared []string,
	dateStyles map[int]bool,
	maxRows int,
) (xlsxSheet, error) {
	type xmlInlineRun struct {
		Text string `xml:"t"`
	}
	type xmlInlineStr struct {
		Text string         `xml:"t"`
		Runs []xmlInlineRun `xml:"r"`
	}
	type xmlCell struct {
		Ref     string       `xml:"r,attr"`
		Type    string       `xml:"t,attr"`
		Style   int          `xml:"s,attr"`
		Formula string       `xml:"f"`
		Value   string       `xml:"v"`
		Inline  xmlInlineStr `xml:"is"`
	}
	type xmlRow struct {
		Cells []xmlCell `xml:"c"`
	}

	dec := xml.NewDecoder(bytes.NewReader(raw))
	out := xlsxSheet{Name: name}
	maxCols := 0

	for {
		if err := ctx.Err(); err != nil {
			return out, err
		}
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return out, fmt.Errorf("invalid worksheet XML (%s): %w", name, err)
		}
		start, ok := tok.(xml.StartElement)
		if !ok || start.Name.Local != "row" {
			continue
		}
		if maxRows > 0 && len(out.Rows) >= maxRows {
			break
		}
		var r xmlRow
		if err := dec.DecodeElement(&r, &start); err != nil {
			return out, fmt.Errorf("invalid row XML (%s): %w", name, err)
		}
		var row []string
		nextCol := 0
		nonEmpty := false
		for _, c := range r.Cells {
			col := xlsxColIndex(c.Ref)
			if col < 0 {
				col = nextCol
			}
			if col > 512 {
				continue
			}
			nextCol = col + 1
			for len(row) <= col {
				row = append(row, "")
			}

			if f := strings.TrimSpace(c.Formula); f != "" {
				out.FormulaCount++
				if len(out.FormulaNotes) < 12 {
					ref := c.Ref
					if ref == "" {
						ref = fmt.Sprintf("col%d", col+1)
					}
					out.FormulaNotes = append(out.FormulaNotes, fmt.Sprintf("%s=+%s", ref, strings.TrimPrefix(f, "=")))
				}
			}

			val := ""
			switch c.Type {
			case "s":
				if idx, err := strconv.Atoi(strings.TrimSpace(c.Value)); err == nil && idx >= 0 && idx < len(shared) {
					val = shared[idx]
				}
			case "inlineStr":
				if c.Inline.Text != "" {
					val = c.Inline.Text
				} else if len(c.Inline.Runs) > 0 {
					var sb strings.Builder
					for _, run := range c.Inline.Runs {
						sb.WriteString(run.Text)
					}
					val = sb.String()
				}
			case "b":
				if strings.TrimSpace(c.Value) == "1" || strings.EqualFold(strings.TrimSpace(c.Value), "true") {
					val = "TRUE"
				} else if strings.TrimSpace(c.Value) != "" {
					val = "FALSE"
				}
			default:
				val = strings.TrimSpace(c.Value)
				if val != "" && (c.Type == "" || c.Type == "n") && dateStyles[c.Style] {
					if num, err := strconv.ParseFloat(val, 64); err == nil {
						if formatted, ok := formatExcelSerialDate(num); ok {
							val = formatted
						}
					}
				}
			}
			if val == "" && strings.TrimSpace(c.Formula) != "" {
				val = "=" + strings.TrimPrefix(strings.TrimSpace(c.Formula), "=")
			}
			if strings.TrimSpace(val) != "" {
				nonEmpty = true
			}
			row[col] = val
		}
		if nonEmpty {
			if len(row) > maxCols {
				maxCols = len(row)
			}
			out.Rows = append(out.Rows, row)
		}
	}

	for i := range out.Rows {
		for len(out.Rows[i]) < maxCols {
			out.Rows[i] = append(out.Rows[i], "")
		}
	}
	return out, nil
}

func selectXlsxSheet(sheets []xlsxSheet, requested string) (xlsxSheet, []string, error) {
	names := make([]string, 0, len(sheets))
	for _, s := range sheets {
		names = append(names, s.Name)
	}
	req := strings.TrimSpace(requested)
	if req != "" {
		for _, s := range sheets {
			if strings.EqualFold(s.Name, req) {
				return s, names, nil
			}
		}
		return xlsxSheet{}, names, fmt.Errorf("sheet %q not found; available sheets: %s", req, strings.Join(names, ", "))
	}
	for _, s := range sheets {
		if len(s.Rows) > 0 {
			return s, names, nil
		}
	}
	return sheets[0], names, nil
}

func xlsxDocumentText(ctx context.Context, body []byte) ([]byte, error) {
	sheets, err := xlsxReadWorkbook(ctx, body, 2000)
	if err != nil {
		return nil, err
	}
	var out strings.Builder
	hasContent := false
	for idx, sh := range sheets {
		if len(sh.Rows) == 0 && sh.FormulaCount == 0 {
			continue
		}
		hasContent = true
		if idx > 0 && out.Len() > 0 {
			out.WriteByte('\n')
		}
		fmt.Fprintf(&out, "[Sheet %d: %s (%d rows)]\n", idx+1, sh.Name, len(sh.Rows))
		if sh.FormulaCount > 0 {
			for i, note := range sh.FormulaNotes {
				sh.FormulaNotes[i] = strings.Replace(note, "=+", "=", 1)
			}
			fmt.Fprintf(&out, "[Formulas: %d cells; sample: %s]\n", sh.FormulaCount, strings.Join(sh.FormulaNotes, "; "))
		}
		for _, row := range sh.Rows {
			cleaned := make([]string, len(row))
			for i, cell := range row {
				cleaned[i] = strings.ReplaceAll(strings.ReplaceAll(cell, "\r\n", " "), "\n", " ")
			}
			out.WriteString(strings.Join(cleaned, "\t"))
			out.WriteByte('\n')
			if out.Len() > documentTextLimit {
				return nil, errors.New("extracted Excel workbook text exceeds 256 KiB; inspect individual sheets with inspect_data")
			}
		}
	}
	if !hasContent {
		return nil, errors.New("Excel workbook has no extractable cell values")
	}
	return []byte(out.String()), nil
}
