package main

// Finance figures in a closing answer have to be numbers financial-service
// returned on this turn. A price, change, position or metric with no such
// number is marked unverified. Ordinary amounts with no ticker and no finance
// wording are left alone.

import (
	"encoding/json"
	"math"
	"regexp"
	"strconv"
	"strings"
)

var financeValueKeys = map[string]bool{
	"price": true, "last": true, "close": true, "open": true, "high": true, "low": true,
	"prev_close": true, "previous_close": true,
	"change": true, "change_pct": true, "change_percent": true, "percent_change": true,
	"quantity": true, "qty": true, "shares": true, "position": true,
	"market_cap": true, "market_value": true, "nav": true, "value": true, "amount": true,
	"volume": true, "pe": true, "pb": true, "eps": true, "revenue": true,
	"beta": true, "volatility": true, "var": true, "drawdown": true, "weight": true,
	"assets": true, "liabilities": true, "equity": true, "cash": true,
}

var (
	currencyAmount = regexp.MustCompile(`(?:\$|USD|CNY|¥|￥|€|£)\s*-?\d[\d,]*(?:\.\d+)?`)
	percentAmount  = regexp.MustCompile(`-?\d[\d,]*(?:\.\d+)?\s*%`)
	labeledAmount  = regexp.MustCompile(`(?i)(?:报价|现价|股价|涨跌幅|涨跌|持仓|仓位|市值|净值|收盘价|开盘价|成交量|price|quote|last price|position|shares|market cap|market value)\s*[:：为是]?\s*-?\d[\d,]*(?:\.\d+)?%?`)
	tickerPrice    = regexp.MustCompile(`\b([A-Z]{2,5}|\d{6})\b\s*[:：]?\s*(-?\d[\d,]*(?:\.\d+)?)`)
	tickerWord     = regexp.MustCompile(`\b(?:[A-Z]{2,5}|\d{6})\b`)
	numberToken    = regexp.MustCompile(`-?\d[\d,]*(?:\.\d+)?`)
	financeKeyword = regexp.MustCompile(`报价|现价|股价|涨跌幅|涨跌|持仓|仓位|市值|净值|收盘价|开盘价|成交量|(?i)(?:\bprice\b|\bquote\b|\bposition\b|\bshares\b|market cap|market value)`)
)

func (e *turnEvidence) rememberFinance(tool string, result toolResult) {
	if e == nil || result.Data == nil || !isFinanceTool(tool, result.Data) {
		return
	}
	content, _ := result.Data["content"].(string)
	found := financeNumbers(content)
	if len(found) == 0 {
		return
	}
	if e.financeFigures == nil {
		e.financeFigures = map[string]bool{}
	}
	for key := range found {
		e.financeFigures[key] = true
	}
}

func isFinanceTool(tool string, data map[string]any) bool {
	if strings.Contains(tool, financeServer) {
		return true
	}
	server, _ := data["server"].(string)
	return server == financeServer
}

func financeNumbers(content string) map[string]bool {
	out := map[string]bool{}
	var payload any
	if json.Unmarshal([]byte(content), &payload) == nil {
		if body, ok := payload.(map[string]any); ok {
			if flag, exists := body["success"].(bool); exists && !flag {
				return out
			}
		}
		walkFinance(payload, "", out)
		return out
	}
	for _, match := range labeledAmount.FindAllString(content, -1) {
		if raw := numberToken.FindString(match); raw != "" {
			if canon, ok := canonicalFinance(raw); ok {
				out[canon] = true
			}
		}
	}
	return out
}

func walkFinance(v any, key string, out map[string]bool) {
	switch n := v.(type) {
	case map[string]any:
		for childKey, child := range n {
			walkFinance(child, childKey, out)
		}
	case []any:
		for _, child := range n {
			walkFinance(child, key, out)
		}
	default:
		if !financeValueKeys[strings.ToLower(key)] {
			return
		}
		f, ok := chartNumber(n)
		if !ok {
			return
		}
		if canon, ok := canonicalNumber(f); ok {
			out[canon] = true
		}
	}
}

func canonicalNumber(f float64) (string, bool) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", false
	}
	return strconv.FormatFloat(f, 'f', -1, 64), true
}

func canonicalFinance(raw string) (string, bool) {
	f, ok := parseFinanceNumber(raw)
	if !ok {
		return "", false
	}
	return canonicalNumber(f)
}

func parseFinanceNumber(raw string) (float64, bool) {
	text := strings.TrimSpace(raw)
	text = strings.TrimRight(text, "%")
	text = strings.TrimSpace(text)
	for _, prefix := range []string{"$", "USD", "CNY", "¥", "￥", "€", "£"} {
		if strings.HasPrefix(text, prefix) {
			text = strings.TrimSpace(strings.TrimPrefix(text, prefix))
		}
	}
	text = strings.ReplaceAll(text, ",", "")
	return chartNumber(text)
}

func financeFigureKnown(figures map[string]bool, raw string) bool {
	canon, ok := canonicalFinance(raw)
	return ok && figures[canon]
}

// financeClaimNumbers returns the market figures an answer asserts.
// have is true when this turn already received financial-service numbers,
// so a currency amount in that answer is also a claim.
func financeClaimNumbers(answer string, have bool) []string {
	var out []string
	seen := map[string]bool{}
	add := func(raw string) {
		canon, ok := canonicalFinance(raw)
		if !ok || seen[canon] {
			return
		}
		seen[canon] = true
		out = append(out, strings.TrimSpace(raw))
	}
	for _, line := range strings.FieldsFunc(answer, func(r rune) bool { return r == '\n' || r == '。' }) {
		ticker := lineHasTicker(line)
		for _, match := range labeledAmount.FindAllString(line, -1) {
			if raw := numberToken.FindString(match); raw != "" {
				add(raw)
			}
		}
		if ticker || have {
			for _, match := range currencyAmount.FindAllString(line, -1) {
				if raw := numberToken.FindString(match); raw != "" {
					add(raw)
				}
			}
		}
		if ticker || financeKeyword.MatchString(line) {
			for _, match := range percentAmount.FindAllString(line, -1) {
				add(match)
			}
		}
		for _, sub := range tickerPrice.FindAllStringSubmatch(line, -1) {
			if tickerAllowed(sub[1]) {
				add(sub[2])
			}
		}
	}
	return out
}

func lineHasTicker(line string) bool {
	for _, symbol := range tickerWord.FindAllString(line, -1) {
		if tickerAllowed(symbol) {
			return true
		}
	}
	return false
}

func tickerAllowed(symbol string) bool {
	switch strings.ToUpper(symbol) {
	case "USD", "CNY", "EUR", "GBP", "JPY", "THE", "AND", "FOR", "NOT", "ALL", "PCT":
		return false
	default:
		return symbol != ""
	}
}
