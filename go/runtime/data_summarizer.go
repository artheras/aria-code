package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

type colStats struct {
	Name         string
	InferredType string // "integer", "float", "boolean", "datetime", "string"
	TotalCount   int
	NullCount    int
	// Numeric stats
	IsNumeric bool
	MinVal    float64
	MaxVal    float64
	SumVal    float64
	// Categorical stats
	FreqMap map[string]int
}

// inspectDataTool profiles and summarizes structured tabular datasets (CSV, TSV, JSON, JSONL).
// It returns schema, data health, numeric distributions and head samples,
// keeping token usage under ~400 tokens regardless of raw file size.
func inspectDataTool(ctx context.Context, sc *scope, params map[string]any) toolResult {
	rawPath := strings.TrimSpace(str(params, "path", ""))
	if rawPath == "" {
		return failed(errors.New("inspect_data requires 'path' parameter"))
	}
	scoped, err := sc.open(rawPath, false)
	if err != nil {
		return failed(err)
	}
	defer scoped.Close()
	targetPath := scoped.absolute

	maxRows := 10000
	if mr, ok := params["max_rows"].(float64); ok && mr > 0 {
		maxRows = int(mr)
	}
	headLimit := 5
	if hl, ok := params["head"].(float64); ok && hl >= 0 {
		headLimit = int(hl)
	}

	file, err := os.Open(targetPath)
	if err != nil {
		return failed(err)
	}
	defer file.Close()

	ext := strings.ToLower(filepath.Ext(targetPath))
	var headers []string
	var rows [][]string

	switch ext {
	case ".csv", ".tsv", ".txt":
		sep := rune(',')
		if ext == ".tsv" {
			sep = '\t'
		} else {
			// Auto-detect comma vs tab vs semicolon from first line
			leadBytes := make([]byte, 2048)
			n, _ := file.Read(leadBytes)
			lead := string(leadBytes[:n])
			if strings.Count(lead, "\t") > strings.Count(lead, ",") {
				sep = '\t'
			} else if strings.Count(lead, ";") > strings.Count(lead, ",") {
				sep = ';'
			}
			_, _ = file.Seek(0, io.SeekStart)
		}

		reader := csv.NewReader(bufio.NewReader(file))
		reader.Comma = sep
		reader.LazyQuotes = true
		reader.FieldsPerRecord = -1 // Allow variable row widths gracefully

		first, err := reader.Read()
		if err != nil {
			return failed(fmt.Errorf("failed to read table header: %w", err))
		}
		headers = first
		count := 0
		for {
			record, err := reader.Read()
			if err == io.EOF || (maxRows > 0 && count >= maxRows) {
				break
			}
			if err != nil {
				continue // Skip malformed lines
			}
			rows = append(rows, record)
			count++
		}

	case ".jsonl":
		scanner := bufio.NewScanner(file)
		scanner.Buffer(make([]byte, 64*1024), 2*1024*1024)
		headerSet := map[string]bool{}
		var headerOrder []string
		var parsedRows []map[string]any

		count := 0
		for scanner.Scan() {
			if maxRows > 0 && count >= maxRows {
				break
			}
			line := strings.TrimSpace(scanner.Text())
			if line == "" {
				continue
			}
			var obj map[string]any
			if err := json.Unmarshal([]byte(line), &obj); err == nil {
				parsedRows = append(parsedRows, obj)
				for k := range obj {
					if !headerSet[k] {
						headerSet[k] = true
						headerOrder = append(headerOrder, k)
					}
				}
				count++
			}
		}
		headers = headerOrder
		for _, obj := range parsedRows {
			row := make([]string, len(headers))
			for i, h := range headers {
				if v, ok := obj[h]; ok && v != nil {
					row[i] = fmt.Sprint(v)
				}
			}
			rows = append(rows, row)
		}

	case ".json":
		data, err := io.ReadAll(io.LimitReader(file, 32*1024*1024))
		if err != nil {
			return failed(err)
		}
		var list []map[string]any
		if err := json.Unmarshal(data, &list); err != nil {
			return failed(fmt.Errorf("json is not an array of objects: %w", err))
		}
		headerSet := map[string]bool{}
		var headerOrder []string
		for _, obj := range list {
			for k := range obj {
				if !headerSet[k] {
					headerSet[k] = true
					headerOrder = append(headerOrder, k)
				}
			}
		}
		headers = headerOrder
		for idx, obj := range list {
			if maxRows > 0 && idx >= maxRows {
				break
			}
			row := make([]string, len(headers))
			for i, h := range headers {
				if v, ok := obj[h]; ok && v != nil {
					row[i] = fmt.Sprint(v)
				}
			}
			rows = append(rows, row)
		}

	default:
		return failed(fmt.Errorf("unsupported structured data format '%s'; supported: .csv, .tsv, .json, .jsonl", ext))
	}

	if len(headers) == 0 {
		return failed(errors.New("dataset has no columns or headers"))
	}

	// Compute Column Profiles
	statsList := make([]*colStats, len(headers))
	for i, h := range headers {
		statsList[i] = &colStats{
			Name:      h,
			FreqMap:   map[string]int{},
			MinVal:    math.MaxFloat64,
			MaxVal:    -math.MaxFloat64,
			IsNumeric: true,
		}
	}

	for _, row := range rows {
		for i := 0; i < len(headers); i++ {
			st := statsList[i]
			st.TotalCount++
			val := ""
			if i < len(row) {
				val = strings.TrimSpace(row[i])
			}
			if val == "" || strings.EqualFold(val, "null") || strings.EqualFold(val, "none") || strings.EqualFold(val, "nan") {
				st.NullCount++
				continue
			}

			if st.IsNumeric {
				if num, err := strconv.ParseFloat(val, 64); err == nil {
					st.SumVal += num
					if num < st.MinVal {
						st.MinVal = num
					}
					if num > st.MaxVal {
						st.MaxVal = num
					}
				} else {
					st.IsNumeric = false
				}
			}

			if len(st.FreqMap) < 200 {
				st.FreqMap[val]++
			}
		}
	}

	// Classify inferred types
	for _, st := range statsList {
		validCount := st.TotalCount - st.NullCount
		if validCount == 0 {
			st.InferredType = "empty"
			st.IsNumeric = false
			continue
		}
		if st.IsNumeric {
			if math.Trunc(st.MinVal) == st.MinVal && math.Trunc(st.MaxVal) == st.MaxVal {
				st.InferredType = "integer"
			} else {
				st.InferredType = "float"
			}
		} else {
			// Check datetime
			sampleVal := ""
			for k := range st.FreqMap {
				sampleVal = k
				break
			}
			if isDateTime(sampleVal) {
				st.InferredType = "datetime"
			} else {
				st.InferredType = "string"
			}
		}
	}

	// Build compact summary report
	var b bytes.Buffer
	b.WriteString(fmt.Sprintf("### Dataset Profile: `%s`\n", filepath.Base(targetPath)))
	b.WriteString(fmt.Sprintf("- **Rows Sampled**: %d | **Columns**: %d\n\n", len(rows), len(headers)))
	b.WriteString("| Column | Type | Null% | Min / Stats | Max / Top Values |\n")
	b.WriteString("| :--- | :--- | :--- | :--- | :--- |\n")

	for _, st := range statsList {
		nullPct := 0.0
		if st.TotalCount > 0 {
			nullPct = float64(st.NullCount) * 100.0 / float64(st.TotalCount)
		}
		validCount := st.TotalCount - st.NullCount

		minStr, maxStr := "-", "-"
		if st.IsNumeric && validCount > 0 {
			mean := st.SumVal / float64(validCount)
			minStr = fmt.Sprintf("min: %.2f (avg: %.2f)", st.MinVal, mean)
			maxStr = fmt.Sprintf("max: %.2f", st.MaxVal)
		} else if len(st.FreqMap) > 0 {
			topVals := topCategories(st.FreqMap, 2)
			minStr = fmt.Sprintf("%d distinct", len(st.FreqMap))
			maxStr = strings.Join(topVals, ", ")
		}

		b.WriteString(fmt.Sprintf("| **%s** | `%s` | %.1f%% | %s | %s |\n",
			st.Name, st.InferredType, nullPct, minStr, maxStr))
	}

	// Add Head Sample rows
	if headLimit > 0 && len(rows) > 0 {
		b.WriteString(fmt.Sprintf("\n#### Sample Rows (Top %d):\n", minInt(headLimit, len(rows))))
		b.WriteString("| " + strings.Join(headers, " | ") + " |\n")
		b.WriteString("|" + strings.Repeat(" --- |", len(headers)) + "\n")
		for i := 0; i < minInt(headLimit, len(rows)); i++ {
			rowVals := make([]string, len(headers))
			for colIdx := 0; colIdx < len(headers); colIdx++ {
				val := ""
				if colIdx < len(rows[i]) {
					val = rows[i][colIdx]
					if len(val) > 30 {
						val = val[:27] + "..."
					}
					val = strings.ReplaceAll(val, "|", "\\|")
				}
				rowVals[colIdx] = val
			}
			b.WriteString("| " + strings.Join(rowVals, " | ") + " |\n")
		}
	}

	return toolResult{
		Success: true,
		Data: map[string]any{
			"summary":   b.String(),
			"row_count": len(rows),
			"col_count": len(headers),
			"columns":   headers,
			"path":      targetPath,
		},
	}
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func isDateTime(val string) bool {
	layouts := []string{
		time.RFC3339,
		"2006-01-02",
		"2006-01-02 15:04:05",
		"01/02/2006",
	}
	for _, l := range layouts {
		if _, err := time.Parse(l, val); err == nil {
			return true
		}
	}
	return false
}

func topCategories(freq map[string]int, limit int) []string {
	type pair struct {
		k string
		c int
	}
	var pairs []pair
	for k, c := range freq {
		pairs = append(pairs, pair{k, c})
	}
	sort.Slice(pairs, func(i, j int) bool {
		return pairs[i].c > pairs[j].c
	})
	var out []string
	for i := 0; i < len(pairs) && i < limit; i++ {
		out = append(out, fmt.Sprintf("%s (%d)", pairs[i].k, pairs[i].c))
	}
	return out
}

type aggItem struct {
	Count int
	Sum   float64
	Min   float64
	Max   float64
}

type groupResult struct {
	Group string  `json:"group"`
	Count int     `json:"count"`
	Value float64 `json:"value"`
}

// aggregateDataTool performs deterministic streaming group-by and aggregation
// over structured tabular datasets (CSV, TSV, JSONL).
// Supported functions: "sum", "avg", "count", "min", "max".
// Keeps context token usage under ~200 tokens.
func aggregateDataTool(ctx context.Context, sc *scope, params map[string]any) toolResult {
	rawPath := strings.TrimSpace(str(params, "path", ""))
	if rawPath == "" {
		return failed(errors.New("aggregate_data requires 'path' parameter"))
	}
	groupByCol := strings.TrimSpace(str(params, "group_by", ""))
	if groupByCol == "" {
		return failed(errors.New("aggregate_data requires 'group_by' parameter"))
	}
	metricCol := strings.TrimSpace(str(params, "metric", ""))
	fn := strings.ToLower(strings.TrimSpace(str(params, "function", "count")))
	if fn == "" {
		fn = "count"
	}
	if fn != "count" && metricCol == "" {
		return failed(fmt.Errorf("aggregate function '%s' requires 'metric' parameter", fn))
	}
	topN := 20
	if n, ok := params["top_n"].(float64); ok && n > 0 {
		topN = int(n)
	}

	scoped, err := sc.open(rawPath, false)
	if err != nil {
		return failed(err)
	}
	defer scoped.Close()
	targetPath := scoped.absolute

	file, err := os.Open(targetPath)
	if err != nil {
		return failed(err)
	}
	defer file.Close()

	ext := strings.ToLower(filepath.Ext(targetPath))
	var headers []string
	var rows [][]string

	switch ext {
	case ".csv", ".tsv", ".txt":
		sep := rune(',')
		if ext == ".tsv" {
			sep = '\t'
		} else {
			leadBytes := make([]byte, 2048)
			n, _ := file.Read(leadBytes)
			lead := string(leadBytes[:n])
			if strings.Count(lead, "\t") > strings.Count(lead, ",") {
				sep = '\t'
			} else if strings.Count(lead, ";") > strings.Count(lead, ",") {
				sep = ';'
			}
			_, _ = file.Seek(0, io.SeekStart)
		}
		reader := csv.NewReader(bufio.NewReader(file))
		reader.Comma = sep
		reader.LazyQuotes = true
		reader.FieldsPerRecord = -1
		first, err := reader.Read()
		if err != nil {
			return failed(fmt.Errorf("failed to read table header: %w", err))
		}
		headers = first
		for {
			rec, err := reader.Read()
			if err == io.EOF {
				break
			}
			if err != nil {
				continue
			}
			rows = append(rows, rec)
		}

	case ".jsonl":
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			line := bytes.TrimSpace(scanner.Bytes())
			if len(line) == 0 {
				continue
			}
			var m map[string]any
			if err := json.Unmarshal(line, &m); err == nil {
				if len(headers) == 0 {
					for k := range m {
						headers = append(headers, k)
					}
					sort.Strings(headers)
				}
				row := make([]string, len(headers))
				for i, h := range headers {
					if v, ok := m[h]; ok && v != nil {
						row[i] = fmt.Sprintf("%v", v)
					}
				}
				rows = append(rows, row)
			}
		}

	default:
		return failed(fmt.Errorf("unsupported tabular format '%s' for aggregate_data; supported: .csv, .tsv, .jsonl", ext))
	}

	groupIdx := -1
	metricIdx := -1
	for i, h := range headers {
		if strings.EqualFold(h, groupByCol) {
			groupIdx = i
		}
		if metricCol != "" && strings.EqualFold(h, metricCol) {
			metricIdx = i
		}
	}
	if groupIdx == -1 {
		return failed(fmt.Errorf("group_by column '%s' not found in headers %v", groupByCol, headers))
	}
	if fn != "count" && metricIdx == -1 {
		return failed(fmt.Errorf("metric column '%s' not found in headers %v", metricCol, headers))
	}

	groups := make(map[string]*aggItem)
	totalRows := len(rows)

	for _, row := range rows {
		if groupIdx >= len(row) {
			continue
		}
		gKey := strings.TrimSpace(row[groupIdx])
		if gKey == "" {
			gKey = "(null)"
		}
		item, exists := groups[gKey]
		if !exists {
			item = &aggItem{Min: math.MaxFloat64, Max: -math.MaxFloat64}
			groups[gKey] = item
		}
		item.Count++

		if metricIdx >= 0 && metricIdx < len(row) {
			vStr := strings.TrimSpace(row[metricIdx])
			if vStr != "" {
				if val, err := strconv.ParseFloat(vStr, 64); err == nil {
					item.Sum += val
					if val < item.Min {
						item.Min = val
					}
					if val > item.Max {
						item.Max = val
					}
				}
			}
		}
	}

	var results []groupResult
	for gKey, item := range groups {
		var val float64
		switch fn {
		case "count":
			val = float64(item.Count)
		case "sum":
			val = item.Sum
		case "avg":
			if item.Count > 0 {
				val = item.Sum / float64(item.Count)
			}
		case "min":
			if item.Min != math.MaxFloat64 {
				val = item.Min
			}
		case "max":
			if item.Max != -math.MaxFloat64 {
				val = item.Max
			}
		}
		results = append(results, groupResult{
			Group: gKey,
			Count: item.Count,
			Value: math.Round(val*100) / 100,
		})
	}

	// Sort descending by Value
	sort.Slice(results, func(i, j int) bool {
		return results[i].Value > results[j].Value
	})

	if len(results) > topN {
		results = results[:topN]
	}

	// Build markdown table
	var b strings.Builder
	b.WriteString(fmt.Sprintf("### Aggregation: %s by %s\n\n", strings.ToUpper(fn), groupByCol))
	b.WriteString(fmt.Sprintf("| %s | Count | %s(%s) |\n", groupByCol, strings.ToUpper(fn), metricCol))
	b.WriteString("|---|---|---|\n")
	for _, r := range results {
		b.WriteString(fmt.Sprintf("| %s | %d | %.2f |\n", r.Group, r.Count, r.Value))
	}

	return toolResult{
		Success: true,
		Data: map[string]any{
			"total_rows":     totalRows,
			"groups_count":   len(groups),
			"group_by":       groupByCol,
			"metric":         metricCol,
			"function":       fn,
			"top_results":    results,
			"markdown_table": b.String(),
		},
	}
}
