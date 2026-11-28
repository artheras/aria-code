package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
)

//xlsx_write.go: Deterministic, zero-dependency OOXML (.xlsx) workbook builder
//for financial modeling (DCF, statements, backtest ledgers) and 3PL logistics
//reconciliation. Writes live <f> formulas (with fullCalcOnLoad="1"), industry
//color conventions (blue hardcoded inputs, black same-sheet formulas, green
//cross-sheet links), accounting number formats, frozen headers, auto-sized
//columns, formula error gating, and an optional Audit_Provenance sheet.

type xlsxCellSpec struct {
	Value   any
	Formula string // stored without leading '=' inside <f>
	Format  string // "", "currency", "cny", "integer", "decimal", "percent", "multiple", "date", "text"
	Role    string // "", "input", "formula", "link", "total", "warning", "header"
	Bold    bool
}

type xlsxColumnSpec struct {
	Header string
	Width  float64
	Format string
}

type xlsxSheetSpec struct {
	Name         string
	Columns      []xlsxColumnSpec
	Rows         [][]xlsxCellSpec
	FreezeHeader bool
}

var (
	excelErrorLiterals = []string{"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NUM!", "#N/A", "#NULL!"}
	invalidTabChars    = regexp.MustCompile(`[:\\/?*\[\]\x00-\x1f]+`)
	currencyCleanRe    = regexp.MustCompile(`^([+-]?)\s*([$¥€£])\s*([0-9][0-9,]*(?:\.[0-9]+)?)$`)
	accountingNegRe    = regexp.MustCompile(`^\(\s*([$¥€£]?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*\)$`)
	percentCleanRe     = regexp.MustCompile(`^([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)%$`)
	multipleCleanRe    = regexp.MustCompile(`^([+-]?[0-9]+(?:\.[0-9]+)?)x$`)
	numberCleanRe      = regexp.MustCompile(`^[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?$|^[+-]?[0-9]+(?:\.[0-9]+)?$`)
	isoDateCleanRe     = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)
)

func xlsxColName(colIdx int) string {
	n := colIdx + 1
	var buf [8]byte
	pos := len(buf)
	for n > 0 {
		n--
		pos--
		buf[pos] = byte('A' + (n % 26))
		n /= 26
	}
	return string(buf[pos:])
}

func sanitizeSheetName(raw string, idx int, used map[string]bool) string {
	clean := strings.TrimSpace(invalidTabChars.ReplaceAllString(raw, " "))
	clean = strings.Trim(clean, "'")
	if clean == "" {
		clean = fmt.Sprintf("Sheet%d", idx+1)
	}
	runes := []rune(clean)
	if len(runes) > 31 {
		clean = string(runes[:31])
	}
	candidate := clean
	seq := 2
	for used[strings.ToLower(candidate)] {
		suffix := fmt.Sprintf("_%d", seq)
		seq++
		maxBase := 31 - len(suffix)
		baseRunes := []rune(clean)
		if len(baseRunes) > maxBase {
			baseRunes = baseRunes[:maxBase]
		}
		candidate = string(baseRunes) + suffix
	}
	used[strings.ToLower(candidate)] = true
	return candidate
}

func validateExcelFormula(formula string, allowErrors bool) (string, error) {
	trimmed := strings.TrimSpace(formula)
	trimmed = strings.TrimPrefix(trimmed, "=")
	if trimmed == "" {
		return "", errors.New("empty Excel formula")
	}
	if !allowErrors {
		upper := strings.ToUpper(trimmed)
		for _, tok := range excelErrorLiterals {
			if strings.Contains(upper, tok) {
				return "", fmt.Errorf("formula %q contains Excel error literal %s", formula, tok)
			}
		}
	}
	depth := 0
	inStr := false
	for i := 0; i < len(trimmed); i++ {
		ch := trimmed[i]
		if ch == '"' {
			if inStr && i+1 < len(trimmed) && trimmed[i+1] == '"' {
				i++
				continue
			}
			inStr = !inStr
			continue
		}
		if inStr {
			continue
		}
		switch ch {
		case '(':
			depth++
		case ')':
			depth--
			if depth < 0 {
				return "", fmt.Errorf("unbalanced parentheses in formula %q", formula)
			}
		}
	}
	if inStr {
		return "", fmt.Errorf("unclosed string literal in formula %q", formula)
	}
	if depth != 0 {
		return "", fmt.Errorf("unbalanced parentheses in formula %q", formula)
	}
	return trimmed, nil
}

func displayWidth(s string) float64 {
	w := 0.0
	for _, r := range s {
		if unicode.Is(unicode.Han, r) || r > 0x2E80 {
			w += 2.1
		} else {
			w += 1.05
		}
	}
	return w
}

func parseSmartScalar(raw string) (any, string) {
	s := strings.TrimSpace(raw)
	if s == "" || s == "-" || s == "—" {
		return "", ""
	}
	if m := accountingNegRe.FindStringSubmatch(s); m != nil {
		numStr := strings.ReplaceAll(m[2], ",", "")
		if v, err := strconv.ParseFloat(numStr, 64); err == nil {
			if m[1] == "¥" {
				return -v, "cny"
			}
			if m[1] != "" {
				return -v, "currency"
			}
			return -v, "decimal"
		}
	}
	if m := currencyCleanRe.FindStringSubmatch(s); m != nil {
		numStr := m[1] + strings.ReplaceAll(m[3], ",", "")
		if v, err := strconv.ParseFloat(numStr, 64); err == nil {
			if m[2] == "¥" {
				return v, "cny"
			}
			return v, "currency"
		}
	}
	if m := percentCleanRe.FindStringSubmatch(s); m != nil {
		numStr := strings.ReplaceAll(m[1], ",", "")
		if v, err := strconv.ParseFloat(numStr, 64); err == nil {
			return v / 100.0, "percent"
		}
	}
	if m := multipleCleanRe.FindStringSubmatch(s); m != nil {
		if v, err := strconv.ParseFloat(m[1], 64); err == nil {
			return v, "multiple"
		}
	}
	if isoDateCleanRe.MatchString(s) {
		return s, "date"
	}
	if numberCleanRe.MatchString(s) {
		// Preserve leading-zero identifiers like "0005" or "002594" as text
		plain := strings.TrimLeft(s, "+-")
		if len(plain) > 1 && plain[0] == '0' && plain[1] != '.' {
			return s, "text"
		}
		clean := strings.ReplaceAll(s, ",", "")
		if v, err := strconv.ParseFloat(clean, 64); err == nil {
			if strings.Contains(clean, ".") {
				return v, "decimal"
			}
			return v, "integer"
		}
	}
	return s, ""
}

func parseCellFromAny(raw any, colFormat string, highlightInputs, allowErrors bool) (xlsxCellSpec, error) {
	cell := xlsxCellSpec{Format: colFormat}
	switch v := raw.(type) {
	case nil:
		return cell, nil
	case xlsxCellSpec:
		if v.Formula != "" {
			cleanF, err := validateExcelFormula(v.Formula, allowErrors)
			if err != nil {
				return cell, err
			}
			v.Formula = cleanF
		}
		if v.Format == "" {
			v.Format = colFormat
		}
		return v, nil
	case float64:
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return cell, nil
		}
		cell.Value = v
		if cell.Format == "" {
			if math.Trunc(v) == v {
				cell.Format = "integer"
			} else {
				cell.Format = "decimal"
			}
		}
		if highlightInputs {
			cell.Role = "input"
		}
		return cell, nil
	case int:
		cell.Value = float64(v)
		if cell.Format == "" {
			cell.Format = "integer"
		}
		if highlightInputs {
			cell.Role = "input"
		}
		return cell, nil
	case bool:
		cell.Value = v
		return cell, nil
	case string:
		trimmed := strings.TrimSpace(v)
		if !allowErrors {
			upper := strings.ToUpper(trimmed)
			for _, tok := range excelErrorLiterals {
				if upper == tok {
					return cell, fmt.Errorf("cell contains Excel error literal %s", tok)
				}
			}
		}
		if strings.HasPrefix(trimmed, "=") && len(trimmed) > 1 {
			cleanF, err := validateExcelFormula(trimmed, allowErrors)
			if err != nil {
				return cell, err
			}
			cell.Formula = cleanF
			if strings.Contains(cleanF, "!") {
				cell.Role = "link"
			} else {
				cell.Role = "formula"
			}
			if cell.Format == "" {
				cell.Format = "decimal"
			}
			return cell, nil
		}
		parsed, inferredFmt := parseSmartScalar(v)
		cell.Value = parsed
		if cell.Format == "" && inferredFmt != "" {
			cell.Format = inferredFmt
		}
		if _, isNum := parsed.(float64); isNum && highlightInputs {
			cell.Role = "input"
		}
		return cell, nil
	case map[string]any:
		cell.Format = strings.ToLower(strings.TrimSpace(str(v, "format", colFormat)))
		cell.Role = strings.ToLower(strings.TrimSpace(str(v, "role", "")))
		if b, ok := v["bold"].(bool); ok {
			cell.Bold = b
		}
		if rawF := strings.TrimSpace(str(v, "formula", "")); rawF != "" {
			cleanF, err := validateExcelFormula(rawF, allowErrors)
			if err != nil {
				return cell, err
			}
			cell.Formula = cleanF
			if cell.Role == "" {
				if strings.Contains(cleanF, "!") {
					cell.Role = "link"
				} else {
					cell.Role = "formula"
				}
			}
		}
		if val, exists := v["value"]; exists && val != nil {
			sub, err := parseCellFromAny(val, cell.Format, false, allowErrors)
			if err != nil {
				return cell, err
			}
			cell.Value = sub.Value
			if cell.Format == "" {
				cell.Format = sub.Format
			}
		}
		if cell.Formula == "" && cell.Role == "" && highlightInputs {
			if _, ok := cell.Value.(float64); ok {
				cell.Role = "input"
			}
		}
		return cell, nil
	default:
		cell.Value = fmt.Sprint(v)
		return cell, nil
	}
}

func isSummaryRowLabel(firstCell any) bool {
	s, ok := firstCell.(string)
	if !ok {
		return false
	}
	lower := strings.ToLower(strings.TrimSpace(s))
	for _, kw := range []string{"total", "subtotal", "net ", "ebitda", "free cash flow", "合计", "总计", "小计", "净额"} {
		if strings.HasPrefix(lower, kw) || lower == strings.TrimSpace(kw) {
			return true
		}
	}
	return false
}

// Style registry builds minimal, deterministic cellXfs combinations so every
// number format + font color + row fill + total border renders cleanly in Excel.
type xlsxStyleKey struct {
	numFmt int
	font   int
	fill   int
	border int
	align  string // "", "left", "right", "center", "wrap"
}

type xlsxStyleTable struct {
	index map[xlsxStyleKey]int
	list  []xlsxStyleKey
}

func newXlsxStyleTable() *xlsxStyleTable {
	st := &xlsxStyleTable{index: map[xlsxStyleKey]int{}}
	st.id(xlsxStyleKey{0, 0, 0, 0, ""}) // index 0 default
	return st
}

func (st *xlsxStyleTable) id(k xlsxStyleKey) int {
	if idx, ok := st.index[k]; ok {
		return idx
	}
	idx := len(st.list)
	st.list = append(st.list, k)
	st.index[k] = idx
	return idx
}

func numFmtCodeID(format string) int {
	switch strings.ToLower(strings.TrimSpace(format)) {
	case "currency", "usd":
		return 164
	case "cny", "rmb":
		return 165
	case "integer", "int":
		return 166
	case "decimal", "float", "number":
		return 167
	case "percent", "pct":
		return 168
	case "multiple":
		return 169
	case "date":
		return 170
	default:
		return 0
	}
}

func (st *xlsxStyleTable) xml() string {
	var xfs strings.Builder
	for _, k := range st.list {
		applyNum := "0"
		if k.numFmt > 0 {
			applyNum = "1"
		}
		applyFont := "0"
		if k.font > 0 {
			applyFont = "1"
		}
		applyFill := "0"
		if k.fill > 0 {
			applyFill = "1"
		}
		applyBorder := "0"
		if k.border > 0 {
			applyBorder = "1"
		}
		if k.align != "" {
			horiz := k.align
			wrap := "0"
			if k.align == "wrap" {
				horiz = "left"
				wrap = "1"
			}
			fmt.Fprintf(&xfs, `<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0" applyNumberFormat="%s" applyFont="%s" applyFill="%s" applyBorder="%s" applyAlignment="1"><alignment horizontal="%s" vertical="center" wrapText="%s"/></xf>`,
				k.numFmt, k.font, k.fill, k.border, applyNum, applyFont, applyFill, applyBorder, horiz, wrap)
		} else {
			fmt.Fprintf(&xfs, `<xf numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0" applyNumberFormat="%s" applyFont="%s" applyFill="%s" applyBorder="%s"/>`,
				k.numFmt, k.font, k.fill, k.border, applyNum, applyFont, applyFill, applyBorder)
		}
	}

	return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
		`<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
		`<numFmts count="7">` +
		`<numFmt numFmtId="164" formatCode="$#,##0.00;($#,##0.00);&quot;-&quot;"/>` +
		`<numFmt numFmtId="165" formatCode="&quot;¥&quot;#,##0.00;(&quot;¥&quot;#,##0.00);&quot;-&quot;"/>` +
		`<numFmt numFmtId="166" formatCode="#,##0;(#,##0);&quot;-&quot;"/>` +
		`<numFmt numFmtId="167" formatCode="#,##0.00;(#,##0.00);&quot;-&quot;"/>` +
		`<numFmt numFmtId="168" formatCode="0.00%"/>` +
		`<numFmt numFmtId="169" formatCode="0.0&quot;x&quot;"/>` +
		`<numFmt numFmtId="170" formatCode="yyyy-mm-dd"/>` +
		`</numFmts>` +
		`<fonts count="6">` +
		`<font><sz val="10.5"/><color rgb="FF111827"/><name val="Calibri"/><family val="2"/></font>` + // 0: body / formula black
		`<font><b/><sz val="10.5"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>` + // 1: header white bold
		`<font><sz val="10.5"/><color rgb="FF0000FF"/><name val="Calibri"/><family val="2"/></font>` + // 2: hardcoded input blue
		`<font><sz val="10.5"/><color rgb="FF008000"/><name val="Calibri"/><family val="2"/></font>` + // 3: cross-sheet link green
		`<font><b/><sz val="10.5"/><color rgb="FF111827"/><name val="Calibri"/><family val="2"/></font>` + // 4: total/summary bold black
		`<font><b/><sz val="10.5"/><color rgb="FFB91C1C"/><name val="Calibri"/><family val="2"/></font>` + // 5: warning/anomaly red
		`</fonts>` +
		`<fills count="6">` +
		`<fill><patternFill patternType="none"/></fill>` + // 0: required none
		`<fill><patternFill patternType="gray125"/></fill>` + // 1: required gray125
		`<fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill>` + // 2: header slate
		`<fill><patternFill patternType="solid"><fgColor rgb="FFF8FAFC"/><bgColor indexed="64"/></patternFill></fill>` + // 3: zebra
		`<fill><patternFill patternType="solid"><fgColor rgb="FFEEF2FF"/><bgColor indexed="64"/></patternFill></fill>` + // 4: total tint
		`<fill><patternFill patternType="solid"><fgColor rgb="FFFEF2F2"/><bgColor indexed="64"/></patternFill></fill>` + // 5: warning tint
		`</fills>` +
		`<borders count="3">` +
		`<border><left/><right/><top/><bottom/><diagonal/></border>` +
		`<border><left style="thin"><color rgb="FFE2E8F0"/></left><right style="thin"><color rgb="FFE2E8F0"/></right><top style="thin"><color rgb="FFE2E8F0"/></top><bottom style="thin"><color rgb="FFE2E8F0"/></bottom><diagonal/></border>` +
		`<border><left style="thin"><color rgb="FFE2E8F0"/></left><right style="thin"><color rgb="FFE2E8F0"/></right><top style="thin"><color rgb="FF334155"/></top><bottom style="double"><color rgb="FF111827"/></bottom><diagonal/></border>` +
		`</borders>` +
		`<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>` +
		fmt.Sprintf(`<cellXfs count="%d">%s</cellXfs>`, len(st.list), xfs.String()) +
		`<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>` +
		`</styleSheet>`
}

func buildXlsxArchive(sheets []xlsxSheetSpec) ([]byte, int, error) {
	if len(sheets) == 0 {
		return nil, 0, errors.New("workbook requires at least one sheet")
	}
	if len(sheets) > 64 {
		return nil, 0, errors.New("workbook exceeds 64-sheet limit")
	}

	styles := newXlsxStyleTable()
	headerStyle := styles.id(xlsxStyleKey{numFmt: 0, font: 1, fill: 2, border: 1, align: "left"})

	sharedIndex := map[string]int{}
	var sharedList []string
	totalStrings := 0
	internString := func(s string) int {
		totalStrings++
		if idx, ok := sharedIndex[s]; ok {
			return idx
		}
		idx := len(sharedList)
		sharedList = append(sharedList, s)
		sharedIndex[s] = idx
		return idx
	}

	usedNames := map[string]bool{}
	files := map[string]string{
		"_rels/.rels": `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
	}
	order := []string{"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml", "xl/sharedStrings.xml"}

	var overrides, wbSheets, wbRels strings.Builder
	formulaTotal := 0

	for sIdx, spec := range sheets {
		sheetNum := sIdx + 1
		tabName := sanitizeSheetName(spec.Name, sIdx, usedNames)
		partPath := fmt.Sprintf("xl/worksheets/sheet%d.xml", sheetNum)
		order = append(order, partPath)

		fmt.Fprintf(&overrides, `<Override PartName="/%s" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`, partPath)
		fmt.Fprintf(&wbSheets, `<sheet name="%s" sheetId="%d" r:id="rId%d"/>`, xmlText(tabName), sheetNum, sheetNum)
		fmt.Fprintf(&wbRels, `<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>`, sheetNum, sheetNum)

		colCount := len(spec.Columns)
		for _, r := range spec.Rows {
			if len(r) > colCount {
				colCount = len(r)
			}
		}
		if colCount == 0 {
			colCount = 1
		}
		widths := make([]float64, colCount)
		for i := 0; i < colCount; i++ {
			widths[i] = 11.0
			if i < len(spec.Columns) {
				if spec.Columns[i].Width > 0 {
					widths[i] = spec.Columns[i].Width
				} else if w := displayWidth(spec.Columns[i].Header) + 4.0; w > widths[i] {
					widths[i] = w
				}
			}
		}

		var rowsXML strings.Builder
		rowNum := 0

		if len(spec.Columns) > 0 {
			rowNum++
			fmt.Fprintf(&rowsXML, `<row r="%d" ht="24" customHeight="1">`, rowNum)
			for cIdx := 0; cIdx < colCount; cIdx++ {
				hdr := ""
				if cIdx < len(spec.Columns) {
					hdr = spec.Columns[cIdx].Header
				}
				ref := fmt.Sprintf("%s%d", xlsxColName(cIdx), rowNum)
				sID := internString(hdr)
				fmt.Fprintf(&rowsXML, `<c r="%s" s="%d" t="s"><v>%d</v></c>`, ref, headerStyle, sID)
			}
			rowsXML.WriteString(`</row>`)
		}

		for rIdx, rowCells := range spec.Rows {
			rowNum++
			isSummary := len(rowCells) > 0 && isSummaryRowLabel(rowCells[0].Value)
			if isSummary {
				fmt.Fprintf(&rowsXML, `<row r="%d" ht="21" customHeight="1">`, rowNum)
			} else {
				fmt.Fprintf(&rowsXML, `<row r="%d">`, rowNum)
			}
			for cIdx := 0; cIdx < colCount; cIdx++ {
				var cell xlsxCellSpec
				if cIdx < len(rowCells) {
					cell = rowCells[cIdx]
				}
				if cell.Format == "" && cIdx < len(spec.Columns) {
					cell.Format = spec.Columns[cIdx].Format
				}

				fontID := 0
				fillID := 0
				borderID := 1
				if rIdx%2 == 1 {
					fillID = 3
				}
				if isSummary || cell.Role == "total" || cell.Bold {
					fontID = 4
					fillID = 4
					borderID = 2
				}
				switch cell.Role {
				case "input":
					if !isSummary && !cell.Bold {
						fontID = 2
					}
				case "link":
					if !isSummary && !cell.Bold {
						fontID = 3
					}
				case "warning":
					fontID = 5
					fillID = 5
				case "header":
					fontID = 1
					fillID = 2
				}

				numFmt := numFmtCodeID(cell.Format)
				align := ""
				if numFmt > 0 || cell.Formula != "" {
					align = "right"
				}
				stID := styles.id(xlsxStyleKey{numFmt: numFmt, font: fontID, fill: fillID, border: borderID, align: align})
				ref := fmt.Sprintf("%s%d", xlsxColName(cIdx), rowNum)

				if cell.Formula != "" {
					formulaTotal++
					cached := ""
					switch cv := cell.Value.(type) {
					case float64:
						cached = fmt.Sprintf("<v>%s</v>", strconv.FormatFloat(cv, 'f', -1, 64))
					case int:
						cached = fmt.Sprintf("<v>%d</v>", cv)
					}
					fmt.Fprintf(&rowsXML, `<c r="%s" s="%d"><f>%s</f>%s</c>`, ref, stID, xmlText(cell.Formula), cached)
					if w := 13.0; w > widths[cIdx] && (cIdx >= len(spec.Columns) || spec.Columns[cIdx].Width == 0) {
						widths[cIdx] = w
					}
					continue
				}

				switch val := cell.Value.(type) {
				case nil:
					fmt.Fprintf(&rowsXML, `<c r="%s" s="%d"/>`, ref, stID)
				case float64:
					txt := strconv.FormatFloat(val, 'f', -1, 64)
					fmt.Fprintf(&rowsXML, `<c r="%s" s="%d"><v>%s</v></c>`, ref, stID, txt)
					if w := float64(len(txt)) + 5.0; w > widths[cIdx] && (cIdx >= len(spec.Columns) || spec.Columns[cIdx].Width == 0) {
						widths[cIdx] = min(w, 28.0)
					}
				case bool:
					bVal := "0"
					if val {
						bVal = "1"
					}
					fmt.Fprintf(&rowsXML, `<c r="%s" s="%d" t="b"><v>%s</v></c>`, ref, stID, bVal)
				default:
					strVal := fmt.Sprint(val)
					if strVal == "" {
						fmt.Fprintf(&rowsXML, `<c r="%s" s="%d"/>`, ref, stID)
					} else {
						sID := internString(strVal)
						fmt.Fprintf(&rowsXML, `<c r="%s" s="%d" t="s"><v>%d</v></c>`, ref, stID, sID)
						if w := displayWidth(strVal) + 3.5; w > widths[cIdx] && (cIdx >= len(spec.Columns) || spec.Columns[cIdx].Width == 0) {
							widths[cIdx] = min(w, 52.0)
						}
					}
				}
			}
			rowsXML.WriteString(`</row>`)
		}

		var colsXML strings.Builder
		colsXML.WriteString(`<cols>`)
		for i, w := range widths {
			clamped := max(10.0, min(52.0, w))
			fmt.Fprintf(&colsXML, `<col min="%d" max="%d" width="%.2f" customWidth="1"/>`, i+1, i+1, clamped)
		}
		colsXML.WriteString(`</cols>`)

		tabSel := ""
		if sIdx == 0 {
			tabSel = ` tabSelected="1"`
		}
		paneXML := ""
		if spec.FreezeHeader && len(spec.Columns) > 0 {
			paneXML = `<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>`
		}
		filterXML := ""
		if len(spec.Columns) > 0 && rowNum > 1 {
			filterXML = fmt.Sprintf(`<autoFilter ref="A1:%s%d"/>`, xlsxColName(colCount-1), rowNum)
		}

		files[partPath] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
			`<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">` +
			fmt.Sprintf(`<sheetViews><sheetView%s workbookViewId="0">%s</sheetView></sheetViews>`, tabSel, paneXML) +
			`<sheetFormatPr defaultRowHeight="18"/>` +
			colsXML.String() +
			`<sheetData>` + rowsXML.String() + `</sheetData>` +
			filterXML +
			`</worksheet>`
	}

	styleRelID := len(sheets) + 1
	ssRelID := len(sheets) + 2
	fmt.Fprintf(&wbRels, `<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>`, styleRelID)
	fmt.Fprintf(&wbRels, `<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>`, ssRelID)

	files["[Content_Types].xml"] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">` +
		`<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
		`<Default Extension="xml" ContentType="application/xml"/>` +
		`<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>` +
		`<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>` +
		`<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>` +
		overrides.String() + `</Types>`

	files["xl/workbook.xml"] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
		`<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">` +
		`<sheets>` + wbSheets.String() + `</sheets>` +
		`<calcPr calcId="191029" fullCalcOnLoad="1"/>` +
		`</workbook>`

	files["xl/_rels/workbook.xml.rels"] = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>` +
		`<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">` +
		wbRels.String() + `</Relationships>`

	files["xl/styles.xml"] = styles.xml()

	var ssXML strings.Builder
	fmt.Fprintf(&ssXML, `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="%d" uniqueCount="%d">`, totalStrings, len(sharedList))
	for _, s := range sharedList {
		fmt.Fprintf(&ssXML, `<si><t xml:space="preserve">%s</t></si>`, xmlText(s))
	}
	ssXML.WriteString(`</sst>`)
	files["xl/sharedStrings.xml"] = ssXML.String()

	data, err := zipFiles(files, order)
	return data, formulaTotal, err
}

func markdownToXlsxSheets(md, defaultTitle string, highlightInputs, allowErrors bool) ([]xlsxSheetSpec, error) {
	blocks := markdownBlocks(md)
	var sheets []xlsxSheetSpec
	curHeading := defaultTitle
	tableIdx := 0

	for _, blk := range blocks {
		if blk.kind == "heading" {
			if t := runsText(blk.runs); t != "" {
				curHeading = t
			}
			continue
		}
		if blk.kind != "table" || len(blk.rows) == 0 {
			continue
		}
		tableIdx++
		name := curHeading
		if name == "" {
			name = fmt.Sprintf("Table_%d", tableIdx)
		}
		var cols []xlsxColumnSpec
		for _, hdrCell := range blk.rows[0] {
			cols = append(cols, xlsxColumnSpec{Header: runsText(hdrCell)})
		}
		var rows [][]xlsxCellSpec
		for _, r := range blk.rows[1:] {
			var parsedRow []xlsxCellSpec
			for _, c := range r {
				cell, err := parseCellFromAny(runsText(c), "", highlightInputs, allowErrors)
				if err != nil {
					return nil, err
				}
				parsedRow = append(parsedRow, cell)
			}
			rows = append(rows, parsedRow)
		}
		sheets = append(sheets, xlsxSheetSpec{
			Name:         name,
			Columns:      cols,
			Rows:         rows,
			FreezeHeader: true,
		})
		curHeading = ""
	}

	if len(sheets) == 0 {
		// Convert structured bullet/paragraph lines into a clean Summary sheet
		var rows [][]xlsxCellSpec
		section := defaultTitle
		for _, blk := range blocks {
			switch blk.kind {
			case "heading":
				section = runsText(blk.runs)
			case "para", "bullet", "code":
				txt := runsText(blk.runs)
				if txt != "" {
					rows = append(rows, []xlsxCellSpec{{Value: section}, {Value: txt}})
				}
			}
		}
		if len(rows) == 0 {
			return nil, errors.New("no tables or text blocks found in markdown")
		}
		sheets = append(sheets, xlsxSheetSpec{
			Name:         "Summary",
			Columns:      []xlsxColumnSpec{{Header: "Section", Width: 24}, {Header: "Content", Width: 52}},
			Rows:         rows,
			FreezeHeader: true,
		})
	}
	return sheets, nil
}

func compileXlsxFromParams(sc *scope, params map[string]any) ([]byte, int, int, error) {
	title := strings.TrimSpace(str(params, "title", "Workbook"))
	highlightInputs := true
	if hi, ok := params["highlight_inputs"].(bool); ok {
		highlightInputs = hi
	}
	allowErrors, _ := params["allow_error_literals"].(bool)

	var sheets []xlsxSheetSpec
	if rawSheets, ok := params["sheets"].([]any); ok && len(rawSheets) > 0 {
		for idx, item := range rawSheets {
			m, ok := item.(map[string]any)
			if !ok {
				return nil, 0, 0, fmt.Errorf("sheets[%d] must be an object", idx)
			}
			spec := xlsxSheetSpec{
				Name:         str(m, "name", fmt.Sprintf("Sheet%d", idx+1)),
				FreezeHeader: true,
			}
			if fh, ok := m["freeze_header"].(bool); ok {
				spec.FreezeHeader = fh
			}
			if rawCols, ok := m["columns"].([]any); ok {
				for _, rc := range rawCols {
					switch cv := rc.(type) {
					case string:
						spec.Columns = append(spec.Columns, xlsxColumnSpec{Header: cv})
					case map[string]any:
						w, _ := cv["width"].(float64)
						spec.Columns = append(spec.Columns, xlsxColumnSpec{
							Header: str(cv, "header", str(cv, "name", "")),
							Width:  w,
							Format: str(cv, "format", ""),
						})
					}
				}
			} else if rawHdrs, ok := m["headers"].([]any); ok {
				for _, rh := range rawHdrs {
					spec.Columns = append(spec.Columns, xlsxColumnSpec{Header: fmt.Sprint(rh)})
				}
			}
			if rawRows, ok := m["rows"].([]any); ok {
				for rIdx, rr := range rawRows {
					rSlice, ok := rr.([]any)
					if !ok {
						return nil, 0, 0, fmt.Errorf("sheets[%d].rows[%d] must be an array", idx, rIdx)
					}
					var parsedRow []xlsxCellSpec
					for cIdx, cellAny := range rSlice {
						colFmt := ""
						if cIdx < len(spec.Columns) {
							colFmt = spec.Columns[cIdx].Format
						}
						cell, err := parseCellFromAny(cellAny, colFmt, highlightInputs && cIdx > 0, allowErrors)
						if err != nil {
							return nil, 0, 0, fmt.Errorf("sheet %q row %d col %d: %w", spec.Name, rIdx+1, cIdx+1, err)
						}
						parsedRow = append(parsedRow, cell)
					}
					spec.Rows = append(spec.Rows, parsedRow)
				}
			}
			sheets = append(sheets, spec)
		}
	} else {
		md := strings.TrimSpace(str(params, "markdown", ""))
		if md == "" {
			if mdPath := strings.TrimSpace(str(params, "markdown_path", "")); mdPath != "" {
				if sc != nil {
					p, err := sc.open(mdPath, false)
					if err != nil {
						return nil, 0, 0, err
					}
					defer p.Close()
					data, err := readRegular(p, maxMarkdownBytes)
					if err != nil {
						return nil, 0, 0, err
					}
					md = string(data)
				} else {
					full, err := mcpPathIn(mdPath, true)
					if err != nil {
						return nil, 0, 0, err
					}
					data, err := os.ReadFile(full)
					if err != nil {
						return nil, 0, 0, err
					}
					md = string(data)
				}
			}
		}
		if md == "" {
			return nil, 0, 0, errors.New("provide 'sheets' array or 'markdown' / 'markdown_path'")
		}
		var err error
		sheets, err = markdownToXlsxSheets(md, title, highlightInputs, allowErrors)
		if err != nil {
			return nil, 0, 0, err
		}
	}

	// Append Audit_Provenance tab when provenance metadata or shipper scope is supplied
	prov, hasProv := params["provenance"].(map[string]any)
	ownerScope := strings.TrimSpace(os.Getenv("ARIA_OWNER_SCOPE"))
	if hasProv || ownerScope != "" {
		var auditRows [][]xlsxCellSpec
		addAudit := func(k, v string) {
			if strings.TrimSpace(v) != "" {
				auditRows = append(auditRows, []xlsxCellSpec{{Value: k, Bold: true}, {Value: v}})
			}
		}
		addAudit("Workbook Title", title)
		addAudit("Generated At", time.Now().UTC().Format("2006-01-02 15:04:05 UTC"))
		if ownerScope != "" {
			addAudit("Shipper / Tenant Scope", ownerScope)
		}
		if hasProv {
			addAudit("Data Source", str(prov, "source", ""))
			addAudit("As-Of / Period", str(prov, "as_of", str(prov, "period", "")))
			addAudit("Owner / Client", str(prov, "owner_id", ""))
			addAudit("Assumptions", str(prov, "assumptions", ""))
			addAudit("Limitations & Gaps", str(prov, "limitations", ""))
			addAudit("Color Convention", "Blue = hardcoded inputs | Black = same-sheet formulas | Green = cross-sheet links")
		}
		sheets = append(sheets, xlsxSheetSpec{
			Name:         "Audit_Provenance",
			Columns:      []xlsxColumnSpec{{Header: "Attribute", Width: 26}, {Header: "Detail", Width: 52}},
			Rows:         auditRows,
			FreezeHeader: true,
		})
	}

	xlsxBytes, formulaCount, err := buildXlsxArchive(sheets)
	if err != nil {
		return nil, 0, 0, err
	}
	return xlsxBytes, len(sheets), formulaCount, nil
}

func mcpReportXlsx(_ context.Context, args map[string]any) map[string]any {
	title := capRunes(strings.TrimSpace(str(args, "title", "Workbook")), 200)
	out, err := documentOutput(args, title, ".xlsx")
	if err != nil {
		return mcpFail(err.Error())
	}
	data, sheetCount, formulaCount, err := compileXlsxFromParams(nil, args)
	if err != nil {
		return mcpFail(err.Error())
	}
	if err := writeNew(out, data); err != nil {
		return mcpFail(err.Error())
	}
	return map[string]any{
		"success":       true,
		"path":          out,
		"bytes":         len(data),
		"sheet_count":   sheetCount,
		"formula_count": formulaCount,
	}
}

func init() {
	mcpServerTools = append(mcpServerTools,
		writeTool(
			"aria.report.xlsx",
			"Build an institutional-grade Excel (.xlsx) workbook with live recalculated formulas, Wall Street color conventions (blue inputs, black formulas, green cross-sheet links), number/currency/percentage formats, frozen headers, formula validation, and optional Audit_Provenance sheet.",
			map[string]any{
				"title":            map[string]any{"type": "string", "description": "Workbook title (used for filename and provenance)."},
				"output_path":      map[string]any{"type": "string", "description": "Optional new .xlsx file path (never overwritten); defaults to Aria's artifacts/report."},
				"sheets":           map[string]any{"type": "array", "description": "Structured worksheets: [{name, columns: [string|{header,width,format}], rows: [[value|'=FORMULA'|{value,formula,format,role,bold}]]}]."},
				"markdown":         map[string]any{"type": "string", "description": "Alternative: Markdown containing GFM tables to convert into typed worksheets."},
				"markdown_path":    map[string]any{"type": "string", "description": "Alternative: Path to a Markdown file containing GFM tables."},
				"highlight_inputs": map[string]any{"type": "boolean", "description": "Color hardcoded numeric inputs blue (#0000FF) per financial modeling convention (default true)."},
				"provenance":       map[string]any{"type": "object", "description": "Optional audit metadata {source, as_of, owner_id, assumptions, limitations} rendered as an Audit_Provenance tab."},
			},
		),
	)
	mcpServerHandlers["aria.report.xlsx"] = mcpReportXlsx
}
