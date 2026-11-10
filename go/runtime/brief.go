package main

import (
	"encoding/json"
	"math"
	"strings"
	"unicode/utf8"
)

// serviceBrief is the one line the terminal shows for a service result.
// It copies numbers the tool actually returned. A missing field is left out.
func serviceBrief(tool string, data map[string]any) map[string]any {
	if len(data) == 0 {
		return nil
	}
	name := tool
	if i := strings.LastIndex(tool, "__"); i >= 0 {
		name = tool[i+2:]
	}
	bodies := briefBodies(data)
	switch name {
	case "get_market_data", "get_crypto_data", "get_forex_data", "financial_quote":
		return quoteBrief(bodies)
	case "backtest_strategy", "cloud_backtest", "run_portfolio_backtest", "financial_backtest":
		return backtestBrief(bodies)
	}
	for _, body := range bodies {
		if diff, ok := body["diff"].(string); ok && diff != "" {
			added, removed, _ := diffStats(diff)
			return map[string]any{"kind": "edit", "added": added, "removed": removed}
		}
	}
	return nil
}

func briefBodies(data map[string]any) []map[string]any {
	out := []map[string]any{data}
	if nested, ok := data["data"].(map[string]any); ok {
		out = append(out, nested)
	}
	if metrics, ok := data["metrics"].(map[string]any); ok {
		out = append(out, metrics)
	}
	text, _ := data["content"].(string)
	text = strings.TrimSpace(text)
	if strings.HasPrefix(text, "{") {
		var inner map[string]any
		if json.Unmarshal([]byte(text), &inner) == nil {
			out = append(out, inner)
			if nested, ok := inner["data"].(map[string]any); ok {
				out = append(out, nested)
			}
			if metrics, ok := inner["metrics"].(map[string]any); ok {
				out = append(out, metrics)
			}
		}
	}
	return out
}

func quoteBrief(bodies []map[string]any) map[string]any {
	for _, body := range bodies {
		price, ok := briefNum(body, "price", "latest_close", "current_price")
		if !ok {
			continue
		}
		brief := map[string]any{"kind": "quote", "price": price}
		if name := briefText(body, "name", "symbol"); name != "" {
			brief["name"] = name
		}
		if change, ok := briefNum(body, "change_pct", "change_pct_24h", "changePercent"); ok {
			brief["change_pct"] = change
		}
		if fresh, ok := body["stale"].(bool); ok {
			brief["stale"] = fresh
		}
		return brief
	}
	return nil
}

func backtestBrief(bodies []map[string]any) map[string]any {
	for _, body := range bodies {
		ret, ok := briefPercent(body, "total_return", "total_return_pct")
		if !ok {
			continue
		}
		brief := map[string]any{"kind": "backtest", "return_pct": ret}
		if dd, ok := briefPercent(body, "max_drawdown", "max_drawdown_pct"); ok {
			brief["drawdown_pct"] = math.Abs(dd)
		}
		return brief
	}
	return nil
}

// briefPercent reads a percent field as-is, or a fraction field as percent.
func briefPercent(body map[string]any, fractionKey, pctKey string) (float64, bool) {
	if value, ok := briefNum(body, pctKey); ok {
		return math.Round(value*100) / 100, true
	}
	if value, ok := briefNum(body, fractionKey); ok {
		return math.Round(value*10000) / 100, true
	}
	return 0, false
}

func briefNum(body map[string]any, keys ...string) (float64, bool) {
	for _, key := range keys {
		switch value := body[key].(type) {
		case float64:
			if math.IsNaN(value) || math.IsInf(value, 0) {
				continue
			}
			return value, true
		case int:
			return float64(value), true
		case int64:
			return float64(value), true
		case json.Number:
			parsed, err := value.Float64()
			if err == nil && !math.IsNaN(parsed) && !math.IsInf(parsed, 0) {
				return parsed, true
			}
		}
	}
	return 0, false
}

func briefText(body map[string]any, keys ...string) string {
	for _, key := range keys {
		text, ok := body[key].(string)
		if !ok {
			continue
		}
		text = strings.Join(strings.Fields(text), " ")
		if text == "" {
			continue
		}
		if utf8.RuneCountInString(text) > 24 {
			text = string([]rune(text)[:24])
		}
		return text
	}
	return ""
}
