package main

// Charts and the equity report for aria-runtime mcp: the Go port of
// report_generator.py's generate_price_chart, generate_indicator_chart,
// generate_comparison_chart, generate_allocation_chart and generate_report.
//
// Charts are self-contained SVG files (no plotting library, no network):
// they open in a browser, embed in the HTML/PDF report, and switch to dark
// colours with the viewer's theme. Price rows come from the caller (rows
// or data_path, in financial_market_history's {date, open, high, low,
// close, volume} shape) or, when only a symbol is given, from the Arthera
// financial-service registered with `/finance setup`.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"math"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	maxChartRows     = 5000
	maxChartDataSize = 20 << 20
	chartFont        = `-apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif`
)

// The reference categorical order (light, dark) and status colours for up/down.
var (
	chartPaletteLight = []string{"#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"}
	chartPaletteDark  = []string{"#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"}
	chartStyle        = func() string {
		var light, dark strings.Builder
		for i := range chartPaletteLight {
			fmt.Fprintf(&light, ".ac-l%d{stroke:%s;fill:none}.ac-f%d{fill:%s}", i+1, chartPaletteLight[i], i+1, chartPaletteLight[i])
			fmt.Fprintf(&dark, ".ac-l%d{stroke:%s}.ac-f%d{fill:%s}", i+1, chartPaletteDark[i], i+1, chartPaletteDark[i])
		}
		return `.ac-bg{fill:#fcfcfb}.ac-gap{stroke:#fcfcfb}.ac-ink{fill:#0b0b0b}.ac-muted{fill:#52514e}` +
			`.ac-grid{stroke:#e4e3df;stroke-width:1}.ac-axis{stroke:#a8a7a0;stroke-width:1}.ac-ref{stroke:#8a8983;stroke-width:1;stroke-dasharray:4 3}` +
			`.ac-ink-l{stroke:#0b0b0b;fill:none}.ac-up{fill:#0ca30c;stroke:#0ca30c}.ac-down{fill:#d03b3b;stroke:#d03b3b}` +
			light.String() +
			`text{font-family:` + chartFont + `;font-size:11px}.ac-title{font-size:15px;font-weight:600}.ac-small{font-size:10px}` +
			`@media (prefers-color-scheme: dark){.ac-bg{fill:#1a1a19}.ac-gap{stroke:#1a1a19}.ac-ink{fill:#f0efec}.ac-muted{fill:#c3c2b7}` +
			`.ac-grid{stroke:#2e2e2c}.ac-axis{stroke:#5d5c57}.ac-ref{stroke:#8f8e87}.ac-ink-l{stroke:#f0efec}` + dark.String() + `}`
	}()
	chartSymbol = regexp.MustCompile(`^[A-Za-z0-9.^=/_-]{1,20}$`)
	ashareCode  = regexp.MustCompile(`^\d{6}$`)
)

// ── Data ─────────────────────────────────────────────────────────────────────

type priceBar struct {
	Date                           string
	Open, High, Low, Close, Volume float64
	OHLC                           bool
}

func chartNumber(v any) (float64, bool) {
	var f float64
	switch x := v.(type) {
	case float64:
		f = x
	case int:
		f = float64(x)
	case int64:
		f = float64(x)
	case json.Number:
		var err error
		if f, err = x.Float64(); err != nil {
			return 0, false
		}
	case string:
		var err error
		if f, err = strconv.ParseFloat(strings.TrimSpace(x), 64); err != nil {
			return 0, false
		}
	default:
		return 0, false
	}
	return f, !math.IsNaN(f) && !math.IsInf(f, 0)
}

// rowField is a key of row, matched without regard to case.
func rowField(row map[string]any, names ...string) any {
	for _, name := range names {
		if v, ok := row[name]; ok {
			return v
		}
		for k, v := range row {
			if strings.EqualFold(k, name) {
				return v
			}
		}
	}
	return nil
}

// unwrapResult is a tool result's row list: {"data": [...]}, {"rows": [...]}.
func unwrapResult(value any, keys ...string) (any, error) {
	m, ok := value.(map[string]any)
	if !ok {
		return value, nil
	}
	if ok, present := m["success"].(bool); present && !ok {
		msg, _ := m["error"].(string)
		return nil, fmt.Errorf("the data is a failed tool result: %s", capRunes(msg, 200))
	}
	for _, key := range keys {
		if v, ok := m[key]; ok {
			return v, nil
		}
	}
	return value, nil
}

// parseBars reads daily rows, oldest first; rows without a usable close are dropped.
func parseBars(value any) ([]priceBar, error) {
	value, err := unwrapResult(value, "data", "rows", "bars", "history")
	if err != nil {
		return nil, err
	}
	items, ok := value.([]any)
	if !ok {
		return nil, errors.New("price rows must be a list of {date, open, high, low, close, volume}")
	}
	if len(items) > maxChartRows {
		items = items[len(items)-maxChartRows:]
	}
	bars := make([]priceBar, 0, len(items))
	for _, item := range items {
		row, ok := item.(map[string]any)
		if !ok {
			continue
		}
		c, ok := chartNumber(rowField(row, "close", "adj_close"))
		if !ok || c <= 0 {
			continue
		}
		b := priceBar{Close: c}
		switch d := rowField(row, "date", "datetime", "time", "timestamp").(type) {
		case string:
			b.Date = capRunes(strings.TrimSpace(d), 25)
		case float64:
			b.Date = strconv.FormatFloat(d, 'f', -1, 64)
		}
		o, ok1 := chartNumber(rowField(row, "open"))
		h, ok2 := chartNumber(rowField(row, "high"))
		l, ok3 := chartNumber(rowField(row, "low"))
		if ok1 && ok2 && ok3 && l > 0 && h >= math.Max(o, c) && l <= math.Min(o, c) {
			b.Open, b.High, b.Low, b.OHLC = o, h, l, true
		}
		if v, ok := chartNumber(rowField(row, "volume")); ok && v > 0 {
			b.Volume = v
		}
		bars = append(bars, b)
	}
	if len(bars) < 2 {
		return nil, errors.New("need at least two rows with a positive close")
	}
	if first, last := bars[0].Date, bars[len(bars)-1].Date; first != "" && last != "" && first > last {
		for i, j := 0, len(bars)-1; i < j; i, j = i+1, j-1 {
			bars[i], bars[j] = bars[j], bars[i]
		}
	}
	return bars, nil
}

// jsonArgument is args[key], or the JSON in the file args[pathKey].
func jsonArgument(args map[string]any, key, pathKey string) (any, bool, error) {
	value, has := args[key]
	has = has && value != nil
	path, _ := args[pathKey].(string)
	if strings.TrimSpace(path) == "" {
		return value, has, nil
	}
	if has {
		return nil, false, fmt.Errorf("give %s or %s, not both", key, pathKey)
	}
	full, err := mcpPathIn(path, true)
	if err != nil {
		return nil, false, err
	}
	if info, err := os.Stat(full); err != nil || info.Size() > maxChartDataSize {
		return nil, false, fmt.Errorf("%s must be a JSON file of at most 20 MiB", pathKey)
	}
	data, err := os.ReadFile(full)
	if err != nil {
		return nil, false, err
	}
	if err := json.Unmarshal(data, &value); err != nil {
		return nil, false, fmt.Errorf("%s is not JSON: %v", pathKey, err)
	}
	return value, true, nil
}

// fetchHistory reads daily bars from the registered financial-service.
// A variable so tests can stand in for the service.
var fetchHistory = func(ctx context.Context, symbol string) ([]priceBar, string, error) {
	state, err := stateRoot()
	if err != nil {
		return nil, "", err
	}
	configs, err := loadMCPConfig(state)
	if err != nil {
		return nil, "", err
	}
	var cfg *mcpServerConfig
	for i := range configs {
		if configs[i].Name == financeServer && (configs[i].Enabled == nil || *configs[i].Enabled) {
			cfg = &configs[i]
		}
	}
	if cfg == nil {
		return nil, "", errors.New("no price rows were given and the Arthera financial-service is not registered (aria: /finance setup <path>); " +
			"pass rows from financial_market_history, or data_path to a JSON file of them")
	}
	client, err := startMCP(ctx, *cfg)
	if err != nil {
		return nil, "", fmt.Errorf("financial-service: %w", err)
	}
	defer client.close()
	tool, arguments := "financial_market_history", map[string]any{"symbol": symbol, "days": 400}
	switch {
	case ashareCode.MatchString(symbol):
		tool, arguments = "financial_ashare_history", map[string]any{"symbol": symbol, "days": 400}
	case strings.Contains(symbol, "/"):
		tool, arguments = "financial_crypto_ohlcv", map[string]any{"symbol": symbol, "limit": 300}
	}
	callCtx, cancel := context.WithTimeout(ctx, max(client.timeout(), 60*time.Second))
	defer cancel()
	raw, err := client.request(callCtx, "tools/call", map[string]any{"name": tool, "arguments": arguments})
	if err != nil {
		return nil, "", fmt.Errorf("financial-service %s: %w", tool, client.explain(err))
	}
	var result struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		IsError bool `json:"isError"`
	}
	if err := json.Unmarshal(raw, &result); err != nil || len(result.Content) == 0 || result.Content[0].Type != "text" {
		return nil, "", fmt.Errorf("financial-service %s returned no rows", tool)
	}
	var payload any
	if err := json.Unmarshal([]byte(result.Content[0].Text), &payload); err != nil {
		return nil, "", fmt.Errorf("financial-service %s: %s", tool, capRunes(result.Content[0].Text, 300))
	}
	bars, err := parseBars(payload)
	if err != nil {
		return nil, "", fmt.Errorf("financial-service %s: %w", tool, err)
	}
	source := "financial-service " + tool
	if m, ok := payload.(map[string]any); ok {
		if s, ok := m["source"].(string); ok && s != "" {
			source += " (" + capRunes(s, 40) + ")"
		}
	}
	return bars, source, nil
}

// symbolBars is the symbol argument and its bars from rows, data_path or the service.
func symbolBars(ctx context.Context, args map[string]any) (string, []priceBar, string, error) {
	symbol, _ := args["symbol"].(string)
	symbol = strings.ToUpper(strings.TrimSpace(symbol))
	if !chartSymbol.MatchString(symbol) {
		return "", nil, "", errors.New("symbol is required (letters, digits and . ^ = / _ -, up to 20)")
	}
	value, has, err := jsonArgument(args, "rows", "data_path")
	if err != nil {
		return "", nil, "", err
	}
	if !has {
		bars, source, err := fetchHistory(ctx, symbol)
		return symbol, bars, source, err
	}
	bars, err := parseBars(value)
	return symbol, bars, "caller-supplied rows", err
}

// chartWindow is the days argument: trading days to show (default 126, about six months).
func chartWindow(args map[string]any) (int, error) {
	days := 126
	if v, ok := chartNumber(args["days"]); ok {
		if v != math.Trunc(v) || v < 20 || v > 1000 {
			return 0, errors.New("days must be a whole number from 20 to 1000")
		}
		days = int(v)
	} else if args["days"] != nil {
		return 0, errors.New("days must be a number")
	}
	return days, nil
}

// ── Indicators ───────────────────────────────────────────────────────────────

// sma is the n-period simple average; NaN until n values exist.
func sma(values []float64, n int) []float64 {
	out := make([]float64, len(values))
	sum := 0.0
	for i, v := range values {
		sum += v
		if i >= n {
			sum -= values[i-n]
		}
		out[i] = math.NaN()
		if i >= n-1 {
			out[i] = sum / float64(n)
		}
	}
	return out
}

// ema is pandas' ewm(span=n, adjust=False).
func ema(values []float64, n int) []float64 {
	out := make([]float64, len(values))
	alpha := 2 / float64(n+1)
	for i, v := range values {
		if i == 0 {
			out[i] = v
			continue
		}
		out[i] = alpha*v + (1-alpha)*out[i-1]
	}
	return out
}

// rsi is Wilder's RSI: ewm(alpha=1/n, adjust=False) of gains and losses, NaN for the first n values.
func rsi(values []float64, n int) []float64 {
	out := make([]float64, len(values))
	var gain, loss float64
	alpha := 1 / float64(n)
	for i := range values {
		out[i] = math.NaN()
		if i == 0 {
			continue
		}
		d := values[i] - values[i-1]
		g, l := math.Max(d, 0), math.Max(-d, 0)
		if i == 1 {
			gain, loss = g, l
		} else {
			gain, loss = alpha*g+(1-alpha)*gain, alpha*l+(1-alpha)*loss
		}
		if i < n {
			continue
		}
		switch {
		case loss == 0 && gain == 0:
			out[i] = 50
		case loss == 0:
			out[i] = 100
		default:
			out[i] = 100 - 100/(1+gain/loss)
		}
	}
	return out
}

// macd is EMA(12) - EMA(26), its EMA(9) signal and the histogram.
func macd(values []float64) (line, signal, hist []float64) {
	fast, slow := ema(values, 12), ema(values, 26)
	line = make([]float64, len(values))
	for i := range values {
		line[i] = fast[i] - slow[i]
	}
	signal = ema(line, 9)
	hist = make([]float64, len(values))
	for i := range values {
		hist[i] = line[i] - signal[i]
	}
	return
}

func closes(bars []priceBar) []float64 {
	out := make([]float64, len(bars))
	for i, b := range bars {
		out[i] = b.Close
	}
	return out
}

// ── SVG ──────────────────────────────────────────────────────────────────────

type svgDoc struct {
	b    strings.Builder
	w, h float64
}

func newSVG(w, h float64, label string) *svgDoc {
	s := &svgDoc{w: w, h: h}
	fmt.Fprintf(&s.b, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %g %g" width="%g" height="%g" role="img" aria-label="%s">`,
		w, h, w, h, html.EscapeString(label))
	fmt.Fprintf(&s.b, `<title>%s</title><style>%s</style><rect class="ac-bg" width="%g" height="%g"/>`, html.EscapeString(label), chartStyle, w, h)
	return s
}

func (s *svgDoc) add(format string, a ...any) { fmt.Fprintf(&s.b, format, a...) }

func (s *svgDoc) text(x, y float64, class, anchor, text string) {
	s.add(`<text x="%.1f" y="%.1f" class="%s" text-anchor="%s">%s</text>`, x, y, class, anchor, html.EscapeString(text))
}

func (s *svgDoc) done() string { return s.b.String() + "</svg>" }

type linearScale struct{ d0, d1, r0, r1 float64 }

func (l linearScale) at(v float64) float64 {
	if l.d1 == l.d0 {
		return (l.r0 + l.r1) / 2
	}
	return l.r0 + (v-l.d0)/(l.d1-l.d0)*(l.r1-l.r0)
}

// niceTicks are about n round values covering [lo, hi], and the step.
func niceTicks(lo, hi float64, n int) ([]float64, float64) {
	if hi <= lo {
		hi = lo + math.Max(math.Abs(lo)*0.01, 1)
	}
	raw := (hi - lo) / float64(max(n, 1))
	mag := math.Pow(10, math.Floor(math.Log10(raw)))
	step := mag
	for _, m := range []float64{1, 2, 2.5, 5, 10} {
		if raw <= m*mag {
			step = m * mag
			break
		}
	}
	var ticks []float64
	for v := math.Ceil(lo/step) * step; v <= hi+step*1e-9; v += step {
		ticks = append(ticks, v)
	}
	return ticks, step
}

// tickLabel formats a tick for its step: 1.2K, 3.4M, 120, 1.25.
func tickLabel(v, step float64) string {
	if math.Abs(v) < step*1e-6 {
		v = 0
	}
	abs := math.Abs(v)
	for _, u := range []struct {
		div float64
		sfx string
	}{{1e12, "T"}, {1e9, "B"}, {1e6, "M"}, {1e3, "K"}} {
		if abs >= u.div && step >= u.div/100 {
			return strconv.FormatFloat(v/u.div, 'f', decimalsFor(step/u.div), 64) + u.sfx
		}
	}
	return strconv.FormatFloat(v, 'f', decimalsFor(step), 64)
}

func decimalsFor(step float64) int {
	if step <= 0 || step >= 1 {
		return 0
	}
	return min(6, int(math.Ceil(-math.Log10(step)-1e-9)))
}

// compactLabel is a figure in its largest unit: 3.1T, 45.2M, 31.2, 0.0123.
func compactLabel(v float64) string {
	abs := math.Abs(v)
	for _, u := range []struct {
		div float64
		sfx string
	}{{1e12, "T"}, {1e9, "B"}, {1e6, "M"}, {1e3, "K"}} {
		if abs >= u.div {
			return strings.TrimSuffix(strings.TrimRight(strconv.FormatFloat(v/u.div, 'f', 2, 64), "0"), ".") + u.sfx
		}
	}
	if abs >= 1 || abs == 0 {
		return strconv.FormatFloat(v, 'f', 2, 64)
	}
	return strconv.FormatFloat(v, 'g', 3, 64)
}

// amountLabel is a value with thousands separators: 12,345 or 12.50.
func amountLabel(v float64) string {
	decimals := 0
	if math.Abs(v) < 100 {
		decimals = 2
	}
	text := strconv.FormatFloat(math.Abs(v), 'f', decimals, 64)
	whole, frac, _ := strings.Cut(text, ".")
	var b strings.Builder
	for i, r := range whole {
		if i > 0 && (len(whole)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	if frac != "" {
		b.WriteString("." + frac)
	}
	if v < 0 {
		return "-" + b.String()
	}
	return b.String()
}

func priceLabel(v float64) string {
	switch a := math.Abs(v); {
	case a >= 1000:
		return strconv.FormatFloat(v, 'f', 0, 64)
	case a >= 1:
		return strconv.FormatFloat(v, 'f', 2, 64)
	default:
		return strconv.FormatFloat(v, 'g', 4, 64)
	}
}

func extentOf(series ...[]float64) (float64, float64) {
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, values := range series {
		for _, v := range values {
			if !math.IsNaN(v) {
				lo, hi = math.Min(lo, v), math.Max(hi, v)
			}
		}
	}
	if math.IsInf(lo, 1) {
		return 0, 1
	}
	return lo, hi
}

// panel is one plot area sharing the chart's x positions.
type panel struct {
	top, bottom float64
	y           linearScale
	ticks       []float64
	step        float64
}

func newPanel(top, bottom, lo, hi float64, pad float64, ticks int) panel {
	span := hi - lo
	if span <= 0 {
		span = math.Max(math.Abs(hi)*0.02, 1)
	}
	lo, hi = lo-span*pad, hi+span*pad
	t, step := niceTicks(lo, hi, ticks)
	return panel{top: top, bottom: bottom, y: linearScale{lo, hi, bottom, top}, ticks: t, step: step}
}

// grid draws the panel's horizontal grid and right-hand labels.
func (s *svgDoc) grid(p panel, left, right float64, label func(float64) string) {
	for _, t := range p.ticks {
		y := p.y.at(t)
		if y < p.top-0.5 || y > p.bottom+0.5 {
			continue
		}
		s.add(`<line class="ac-grid" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>`, left, right, y, y)
		s.text(right+6, y+4, "ac-muted ac-small", "start", label(t))
	}
	s.add(`<line class="ac-axis" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>`, left, right, p.bottom, p.bottom)
}

// polyline is a 2px line through the non-NaN points.
func (s *svgDoc) polyline(class string, xs []float64, ys []float64, y linearScale) {
	var b strings.Builder
	started := false
	for i := range xs {
		if math.IsNaN(ys[i]) {
			started = false
			continue
		}
		cmd := "L"
		if !started {
			cmd = "M"
			started = true
		}
		fmt.Fprintf(&b, "%s%.1f %.1f", cmd, xs[i], y.at(ys[i]))
	}
	if b.Len() > 0 {
		s.add(`<path class="%s" d="%s" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" fill="none"/>`, class, b.String())
	}
}

// legend draws swatches and names right-aligned at y; each item is (class, name, line?).
func (s *svgDoc) legend(right, y float64, items [][3]string) {
	x := right
	for i := len(items) - 1; i >= 0; i-- {
		item := items[i]
		width := 30.0
		for _, r := range item[1] {
			if r > 0x2e80 {
				width += 11
			} else {
				width += 7
			}
		}
		x -= width
		if item[2] == "line" {
			s.add(`<line class="%s" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke-width="2"/>`, item[0], x, x+12, y-4, y-4)
		} else {
			s.add(`<rect class="%s" x="%.1f" y="%.1f" width="10" height="10" rx="2"/>`, item[0], x+1, y-9)
		}
		s.text(x+16, y, "ac-muted", "start", item[1])
	}
}

// dateAxis labels about six evenly spaced dates under the last panel.
func (s *svgDoc) dateAxis(dates []string, xs []float64, y float64) {
	n := len(dates)
	if n == 0 {
		return
	}
	count := min(6, n)
	last := ""
	for k := 0; k < count; k++ {
		i := 0
		if count > 1 {
			i = int(math.Round(float64(k) * float64(n-1) / float64(count-1)))
		}
		label := dates[i]
		if len(label) >= 10 && label[4] == '-' {
			label = label[:10]
		}
		if label == "" || label == last {
			continue
		}
		last = label
		anchor := "middle"
		if k == 0 {
			anchor = "start"
		} else if k == count-1 {
			anchor = "end"
		}
		s.text(xs[i], y, "ac-muted ac-small", anchor, label)
	}
}

func pctChange(from, to float64) float64 { return (to/from - 1) * 100 }

func signedPct(v float64) string {
	if math.IsNaN(v) {
		return "n/a"
	}
	return fmt.Sprintf("%+.1f%%", v)
}

// priceChartSVG is the candlestick (or close line) chart with MA20/MA50 and
// volume; withIndicators adds RSI(14) and MACD(12,26,9) panels. Averages and
// indicators are computed on the full history, then the last `days` shown.
func priceChartSVG(symbol string, all []priceBar, days int, withIndicators bool) string {
	allCloses := closes(all)
	ma20, ma50 := sma(allCloses, 20), sma(allCloses, 50)
	rsi14 := rsi(allCloses, 14)
	macdLine, macdSignal, macdHist := macd(allCloses)
	start := max(0, len(all)-days)
	bars := all[start:]
	cut := func(v []float64) []float64 { return v[start:] }
	ma20, ma50, rsi14, macdLine, macdSignal, macdHist = cut(ma20), cut(ma50), cut(rsi14), cut(macdLine), cut(macdSignal), cut(macdHist)

	candles, volume := true, false
	for _, b := range bars {
		candles = candles && b.OHLC
		volume = volume || b.Volume > 0
	}
	const width, left, right = 1100.0, 16.0, 1100.0 - 64
	top := 70.0
	height := top + 330
	if volume {
		height += 100
	}
	if withIndicators {
		height += 2 * 130
	}
	height += 30
	label := symbol + " price chart"
	if withIndicators {
		label = symbol + " price, volume, RSI and MACD"
	}
	s := newSVG(width, height, label)

	last, prev := bars[len(bars)-1], bars[len(bars)-2]
	title := fmt.Sprintf("%s · %d-day price", symbol, len(bars))
	if withIndicators {
		title = fmt.Sprintf("%s · price, volume, RSI 14, MACD 12·26·9", symbol)
	}
	s.text(left, 26, "ac-ink ac-title", "start", title)
	sub := fmt.Sprintf("Last %s · %s (1 day)", priceLabel(last.Close), signedPct(pctChange(prev.Close, last.Close)))
	if last.Date != "" {
		sub += " · as of " + last.Date
	}
	s.text(left, 46, "ac-muted", "start", sub)
	priceItem := [3]string{"ac-ink-l", "Close", "line"}
	if candles {
		priceItem = [3]string{"ac-up", "Up / down day", "box"}
	}
	s.legend(right, 26, [][3]string{priceItem, {"ac-l1", "MA20", "line"}, {"ac-l2", "MA50", "line"}})

	n := len(bars)
	step := (right - left) / float64(n)
	xs := make([]float64, n)
	dates := make([]string, n)
	for i := range bars {
		xs[i] = left + (float64(i)+0.5)*step
		dates[i] = bars[i].Date
	}
	lows, highs := make([]float64, n), make([]float64, n)
	for i, b := range bars {
		lows[i], highs[i] = b.Close, b.Close
		if candles {
			lows[i], highs[i] = b.Low, b.High
		}
	}
	lo, hi := extentOf(lows, highs, ma20, ma50)
	price := newPanel(top, top+330, lo, hi, 0.04, 6)
	s.grid(price, left, right, func(v float64) string { return tickLabel(v, price.step) })
	body := math.Max(1, step*0.6)
	up := func(i int) bool {
		if candles {
			return bars[i].Close >= bars[i].Open
		}
		return i == 0 || bars[i].Close >= bars[i-1].Close
	}
	if candles {
		for i, b := range bars {
			class := "ac-down"
			if up(i) {
				class = "ac-up"
			}
			y0, y1 := price.y.at(math.Max(b.Open, b.Close)), price.y.at(math.Min(b.Open, b.Close))
			s.add(`<g class="%s"><title>%s O %s H %s L %s C %s</title><line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke-width="1"/>`+
				`<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f"/></g>`,
				class, html.EscapeString(b.Date), priceLabel(b.Open), priceLabel(b.High), priceLabel(b.Low), priceLabel(b.Close),
				xs[i], xs[i], price.y.at(b.High), price.y.at(b.Low), xs[i]-body/2, y0, body, math.Max(1, y1-y0), math.Min(1, body/4))
		}
	} else {
		s.polyline("ac-ink-l", xs, closes(bars), price.y)
	}
	s.polyline("ac-l1", xs, ma20, price.y)
	s.polyline("ac-l2", xs, ma50, price.y)

	y := price.bottom
	if volume {
		vols := make([]float64, n)
		for i, b := range bars {
			vols[i] = b.Volume
		}
		_, vmax := extentOf(vols)
		p := panel{top: y + 12, bottom: y + 100, y: linearScale{0, vmax * 1.05, y + 100, y + 12}}
		p.ticks, p.step = niceTicks(0, vmax*1.05, 2)
		s.grid(p, left, right, func(v float64) string { return tickLabel(v, p.step) })
		s.text(left+4, p.top+10, "ac-muted ac-small", "start", "Volume")
		w := math.Max(1, step*0.7)
		for i, v := range vols {
			if v <= 0 {
				continue
			}
			class := "ac-down"
			if up(i) {
				class = "ac-up"
			}
			top := p.y.at(v)
			s.add(`<rect class="%s" opacity="0.5" x="%.1f" y="%.1f" width="%.1f" height="%.1f"><title>%s volume %s</title></rect>`,
				class, xs[i]-w/2, top, w, math.Max(0.5, p.bottom-top), html.EscapeString(bars[i].Date), tickLabel(v, 1))
		}
		y = p.bottom
	}
	if withIndicators {
		r := panel{top: y + 18, bottom: y + 130, y: linearScale{0, 100, y + 130, y + 18}, ticks: []float64{30, 50, 70}, step: 10}
		s.grid(r, left, right, func(v float64) string { return tickLabel(v, 10) })
		for _, level := range []float64{30, 70} {
			s.add(`<line class="ac-ref" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>`, left, right, r.y.at(level), r.y.at(level))
		}
		s.polyline("ac-l1", xs, rsi14, r.y)
		s.text(left+4, r.top-4, "ac-muted ac-small", "start", "RSI 14 (30 / 70 dashed)")
		y = r.bottom

		lo, hi := extentOf(macdLine, macdSignal, macdHist)
		lo, hi = math.Min(lo, 0), math.Max(hi, 0)
		m := newPanel(y+18, y+130, lo, hi, 0.08, 3)
		s.grid(m, left, right, func(v float64) string { return tickLabel(v, m.step) })
		zero := m.y.at(0)
		s.add(`<line class="ac-axis" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>`, left, right, zero, zero)
		w := math.Max(1, step*0.6)
		for i, v := range macdHist {
			class := "ac-up"
			if v < 0 {
				class = "ac-down"
			}
			yv := m.y.at(v)
			s.add(`<rect class="%s" opacity="0.55" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>`, class, xs[i]-w/2, math.Min(yv, zero), w, math.Max(0.5, math.Abs(zero-yv)))
		}
		s.polyline("ac-l1", xs, macdLine, m.y)
		s.polyline("ac-l2", xs, macdSignal, m.y)
		s.text(left+4, m.top-4, "ac-muted ac-small", "start", "MACD 12·26·9")
		s.legend(right, m.top-4, [][3]string{{"ac-l1", "MACD", "line"}, {"ac-l2", "Signal", "line"}, {"ac-up", "Histogram", "box"}})
		y = m.bottom
	}
	s.dateAxis(dates, xs, y+18)
	return s.done()
}

// ── Comparison ───────────────────────────────────────────────────────────────

type namedSeries struct {
	symbol string
	bars   []priceBar
}

// parseSeries reads {SYMBOL: rows | result} or [{symbol, data|rows}].
func parseSeries(value any) ([]namedSeries, error) {
	var out []namedSeries
	add := func(symbol string, rows any) error {
		symbol = strings.ToUpper(strings.TrimSpace(symbol))
		if !chartSymbol.MatchString(symbol) {
			return fmt.Errorf("invalid symbol %q", capRunes(symbol, 30))
		}
		bars, err := parseBars(rows)
		if err != nil {
			return fmt.Errorf("%s: %w", symbol, err)
		}
		out = append(out, namedSeries{symbol, bars})
		return nil
	}
	switch v := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(v))
		for k := range v {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			if err := add(k, v[k]); err != nil {
				return nil, err
			}
		}
	case []any:
		for _, item := range v {
			m, ok := item.(map[string]any)
			if !ok {
				return nil, errors.New("each series must be {symbol, data}")
			}
			symbol, _ := m["symbol"].(string)
			if err := add(symbol, m); err != nil {
				return nil, err
			}
		}
	default:
		return nil, errors.New("series must be {SYMBOL: rows} or a list of {symbol, data}")
	}
	return out, nil
}

// comparisonChartSVG indexes each series to 100 at the first date all of them share.
func comparisonChartSVG(series []namedSeries, days int, title string) (string, error) {
	dated := true
	for _, s := range series {
		for _, b := range s.bars {
			dated = dated && b.Date != ""
		}
	}
	type point struct {
		pos   int
		value float64
	}
	var axis []string
	lines := make([][]point, len(series))
	if dated {
		start := ""
		all := map[string]bool{}
		for _, s := range series {
			if first := s.bars[0].Date; first > start {
				start = first
			}
			for _, b := range s.bars {
				all[b.Date] = true
			}
		}
		for d := range all {
			if d >= start {
				axis = append(axis, d)
			}
		}
		sort.Strings(axis)
		if len(axis) > days+1 {
			axis = axis[len(axis)-days-1:]
		}
		if len(axis) < 2 {
			return "", errors.New("the series share fewer than two dates")
		}
		pos := map[string]int{}
		for i, d := range axis {
			pos[d] = i
		}
		for k, s := range series {
			base := math.NaN()
			for _, b := range s.bars {
				i, ok := pos[b.Date]
				if !ok {
					continue
				}
				if math.IsNaN(base) {
					base = b.Close
				}
				lines[k] = append(lines[k], point{i, b.Close / base * 100})
			}
			if len(lines[k]) < 2 {
				return "", fmt.Errorf("%s has fewer than two rows in the shared window", s.symbol)
			}
		}
	} else {
		n := days + 1
		for _, s := range series {
			n = min(n, len(s.bars))
		}
		axis = make([]string, n)
		for k, s := range series {
			tail := s.bars[len(s.bars)-n:]
			for i, b := range tail {
				lines[k] = append(lines[k], point{i, b.Close / tail[0].Close * 100})
			}
		}
	}

	const width, height, left, right, top = 1100.0, 560.0, 16.0, 1100.0 - 150, 70.0
	if title == "" {
		title = "Performance comparison (indexed to 100)"
	}
	s := newSVG(width, height, title)
	s.text(left, 26, "ac-ink ac-title", "start", title)
	sub := fmt.Sprintf("%d trading days", len(axis)-1)
	if dated {
		sub = fmt.Sprintf("From %s to %s · each series = 100 on the first shared date", axis[0], axis[len(axis)-1])
	}
	s.text(left, 46, "ac-muted", "start", sub)
	var items [][3]string
	var all [][]float64
	for k, line := range lines {
		items = append(items, [3]string{fmt.Sprintf("ac-l%d", k+1), series[k].symbol, "line"})
		values := make([]float64, len(line))
		for i, p := range line {
			values[i] = p.value
		}
		all = append(all, values)
	}
	s.legend(width-16, 26, items)
	lo, hi := extentOf(all...)
	lo, hi = math.Min(lo, 100), math.Max(hi, 100)
	p := newPanel(top, height-40, lo, hi, 0.05, 6)
	step := (right - left) / float64(max(len(axis)-1, 1))
	xAt := func(i int) float64 { return left + float64(i)*step }
	s.grid(p, left, right, func(v float64) string { return tickLabel(v, p.step) })
	s.add(`<line class="ac-ref" x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f"/>`, left, right, p.y.at(100), p.y.at(100))
	type endLabel struct {
		y     float64
		text  string
		class string
	}
	var ends []endLabel
	for k, line := range lines {
		xs, ys := make([]float64, len(line)), make([]float64, len(line))
		for i, pt := range line {
			xs[i], ys[i] = xAt(pt.pos), pt.value
		}
		s.polyline(fmt.Sprintf("ac-l%d", k+1), xs, ys, p.y)
		lastPt := line[len(line)-1]
		ends = append(ends, endLabel{p.y.at(lastPt.value), fmt.Sprintf("%s %s", series[k].symbol, signedPct(lastPt.value-100)), fmt.Sprintf("ac-f%d", k+1)})
	}
	if len(ends) <= 4 {
		sort.Slice(ends, func(i, j int) bool { return ends[i].y < ends[j].y })
		for i := 1; i < len(ends); i++ {
			ends[i].y = math.Max(ends[i].y, ends[i-1].y+14)
		}
		for _, e := range ends {
			s.add(`<circle class="%s" cx="%.1f" cy="%.1f" r="3"/>`, e.class, right+52, e.y)
			s.text(right+58, e.y+4, "ac-ink ac-small", "start", e.text)
		}
	}
	xs := make([]float64, len(axis))
	for i := range axis {
		xs[i] = xAt(i)
	}
	s.dateAxis(axis, xs, height-18)
	return s.done(), nil
}

// ── Allocation ───────────────────────────────────────────────────────────────

type allocSlice struct {
	label string
	value float64
}

// parsePositions reads broker positions: market_value, or quantity × price.
func parsePositions(value any) ([]allocSlice, error) {
	value, err := unwrapResult(value, "positions", "data")
	if err != nil {
		return nil, err
	}
	items, ok := value.([]any)
	if !ok {
		return nil, errors.New("positions must be a list of {symbol, market_value} or {symbol, quantity, last_price}")
	}
	var out []allocSlice
	for _, item := range items {
		row, ok := item.(map[string]any)
		if !ok {
			continue
		}
		label, _ := rowField(row, "symbol", "ticker", "name").(string)
		label = capRunes(strings.TrimSpace(label), 24)
		if label == "" {
			label = "?"
		}
		v, ok := chartNumber(rowField(row, "market_value", "value"))
		if !ok {
			q, okQ := chartNumber(rowField(row, "quantity", "qty"))
			p, okP := chartNumber(rowField(row, "last_price", "current_price", "price"))
			v, ok = q*p, okQ && okP
		}
		if ok && v > 0 {
			out = append(out, allocSlice{label, v})
		}
	}
	if len(out) == 0 {
		return nil, errors.New("no positions with a positive market value")
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].value > out[j].value })
	if len(out) > len(chartPaletteLight) {
		rest := 0.0
		for _, s := range out[len(chartPaletteLight)-1:] {
			rest += s.value
		}
		out = append(out[:len(chartPaletteLight)-1], allocSlice{fmt.Sprintf("Other (%d)", len(out)-len(chartPaletteLight)+1), rest})
	}
	return out, nil
}

func allocationChartSVG(slices []allocSlice, title string) string {
	const width, height, cx, cy, outer, inner = 900.0, 520.0, 250.0, 280.0, 190.0, 118.0
	if title == "" {
		title = "Portfolio allocation by market value"
	}
	s := newSVG(width, height, title)
	s.text(16, 26, "ac-ink ac-title", "start", title)
	total := 0.0
	for _, sl := range slices {
		total += sl.value
	}
	s.text(16, 46, "ac-muted", "start", fmt.Sprintf("%d holdings · total %s", len(slices), amountLabel(total)))
	angle := -math.Pi / 2
	for i, sl := range slices {
		frac := sl.value / total
		class := fmt.Sprintf("ac-f%d", i+1)
		tip := fmt.Sprintf("%s %.1f%% (%s)", sl.label, frac*100, amountLabel(sl.value))
		if len(slices) == 1 {
			s.add(`<path class="%s" fill-rule="evenodd" d="M%.1f %.1fa%g %g 0 1 0 0.01 0zM%.1f %.1fa%g %g 0 1 0 0.01 0z"><title>%s</title></path>`,
				class, cx, cy-outer, outer, outer, cx, cy-inner, inner, inner, html.EscapeString(tip))
			break
		}
		end := angle + frac*2*math.Pi
		large := 0
		if frac > 0.5 {
			large = 1
		}
		pt := func(r, a float64) (float64, float64) { return cx + r*math.Cos(a), cy + r*math.Sin(a) }
		x0, y0 := pt(outer, angle)
		x1, y1 := pt(outer, end)
		x2, y2 := pt(inner, end)
		x3, y3 := pt(inner, angle)
		s.add(`<path class="%s ac-gap" stroke-width="2" d="M%.2f %.2fA%g %g 0 %d 1 %.2f %.2fL%.2f %.2fA%g %g 0 %d 0 %.2f %.2fZ"><title>%s</title></path>`,
			class, x0, y0, outer, outer, large, x1, y1, x2, y2, inner, inner, large, x3, y3, html.EscapeString(tip))
		angle = end
	}
	s.text(cx, cy-4, "ac-ink ac-title", "middle", amountLabel(total))
	s.text(cx, cy+16, "ac-muted ac-small", "middle", "total value")
	x, y := 500.0, 110.0
	s.text(x+18, y-24, "ac-muted ac-small", "start", "Holding")
	s.text(x+300, y-24, "ac-muted ac-small", "end", "Weight")
	s.text(x+380, y-24, "ac-muted ac-small", "end", "Value")
	for i, sl := range slices {
		s.add(`<rect class="ac-f%d" x="%.1f" y="%.1f" width="10" height="10" rx="2"/>`, i+1, x, y-9)
		s.text(x+18, y, "ac-ink", "start", sl.label)
		s.text(x+300, y, "ac-ink", "end", fmt.Sprintf("%.1f%%", sl.value/total*100))
		s.text(x+380, y, "ac-muted", "end", amountLabel(sl.value))
		y += 28
	}
	return s.done()
}

// ── Tools ────────────────────────────────────────────────────────────────────

func writeChart(args map[string]any, name, svg string) map[string]any {
	out, err := artifactOutput(args, "chart", name, ".svg")
	if err != nil {
		return mcpFail(err.Error())
	}
	if err := writeNew(out, []byte(svg)); err != nil {
		return mcpFail(err.Error())
	}
	return map[string]any{"success": true, "path": out, "bytes": len(svg), "format": "svg"}
}

func symbolChartTool(suffix string, indicators bool) func(context.Context, map[string]any) map[string]any {
	return func(ctx context.Context, args map[string]any) map[string]any {
		days, err := chartWindow(args)
		if err != nil {
			return mcpFail(err.Error())
		}
		symbol, bars, source, err := symbolBars(ctx, args)
		if err != nil {
			return mcpFail(err.Error())
		}
		result := writeChart(args, symbol+suffix, priceChartSVG(symbol, bars, days, indicators))
		if result["success"] == true {
			result["rows"], result["source"], result["as_of"] = min(days, len(bars)), source, bars[len(bars)-1].Date
		}
		return result
	}
}

var (
	mcpReportChart          = symbolChartTool("_chart", false)
	mcpReportIndicatorChart = symbolChartTool("_indicators", true)
)

func mcpReportComparisonChart(ctx context.Context, args map[string]any) map[string]any {
	days, err := chartWindow(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	title, _ := args["title"].(string)
	title = capRunes(strings.TrimSpace(title), 120)
	value, has, err := jsonArgument(args, "series", "data_path")
	if err != nil {
		return mcpFail(err.Error())
	}
	var series []namedSeries
	if has {
		if series, err = parseSeries(value); err != nil {
			return mcpFail(err.Error())
		}
	} else {
		raw, _ := args["symbols"].([]any)
		seen := map[string]bool{}
		for _, item := range raw {
			symbol, _ := item.(string)
			symbol = strings.ToUpper(strings.TrimSpace(symbol))
			if seen[symbol] {
				continue
			}
			seen[symbol] = true
			if !chartSymbol.MatchString(symbol) {
				return mcpFail(fmt.Sprintf("invalid symbol %q", capRunes(symbol, 30)))
			}
			bars, _, err := fetchHistory(ctx, symbol)
			if err != nil {
				return mcpFail(fmt.Sprintf("%s: %v", symbol, err))
			}
			series = append(series, namedSeries{symbol, bars})
		}
	}
	if len(series) < 2 || len(series) > len(chartPaletteLight) {
		return mcpFail("compare 2 to 8 symbols: give series (or data_path), or symbols to fetch")
	}
	svg, err := comparisonChartSVG(series, days, title)
	if err != nil {
		return mcpFail(err.Error())
	}
	names := make([]string, len(series))
	for i, s := range series {
		names[i] = s.symbol
	}
	result := writeChart(args, strings.Join(names[:min(4, len(names))], "_")+"_comparison", svg)
	result["symbols"] = names
	return result
}

func mcpReportAllocationChart(_ context.Context, args map[string]any) map[string]any {
	value, has, err := jsonArgument(args, "positions", "data_path")
	if err != nil {
		return mcpFail(err.Error())
	}
	if !has {
		return mcpFail("positions are required (e.g. trading_account_state's positions or a broker's), or data_path to a JSON file of them")
	}
	slices, err := parsePositions(value)
	if err != nil {
		return mcpFail(err.Error())
	}
	title, _ := args["title"].(string)
	result := writeChart(args, "allocation", allocationChartSVG(slices, capRunes(strings.TrimSpace(title), 120)))
	result["holdings"] = len(slices)
	return result
}

// ── Equity report ────────────────────────────────────────────────────────────

const reportCSS = `.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:12px 0}
.kpi{border:1px solid #e2e8f0;border-radius:6px;padding:8px 10px;page-break-inside:avoid}
.kpi .k{font-size:8.5pt;color:#64748b}.kpi .v{font-size:13pt;font-weight:600;margin-top:2px}.kpi .s{font-size:8pt;color:#64748b}
.chart{margin:10px 0 4px;page-break-inside:avoid}.chart svg{width:100%;height:auto;display:block}
.source{font-size:8pt;color:#64748b;margin:2px 0 12px}`

type reportKPI struct{ label, value, sub string }

// equityKPIs are the headline figures computed from the bars alone.
func equityKPIs(bars []priceBar) []reportKPI {
	c := closes(bars)
	n := len(c)
	last := c[n-1]
	change := func(back int) string {
		if n <= back {
			return "n/a"
		}
		return signedPct(pctChange(c[n-1-back], last))
	}
	window := c[max(0, n-252):]
	lo, hi := extentOf(window)
	vol := "n/a"
	if n > 21 {
		var sum, sq float64
		k := 0
		for i := max(1, n-60); i < n; i++ {
			r := math.Log(c[i] / c[i-1])
			sum += r
			sq += r * r
			k++
		}
		mean := sum / float64(k)
		vol = fmt.Sprintf("%.1f%%", math.Sqrt(math.Max(sq/float64(k)-mean*mean, 0)*252)*100)
	}
	r := rsi(c, 14)[n-1]
	rsiText := "n/a"
	if !math.IsNaN(r) {
		rsiText = fmt.Sprintf("%.0f", r)
	}
	ma50 := sma(c, 50)[n-1]
	vsMA := "n/a"
	if !math.IsNaN(ma50) {
		vsMA = signedPct(pctChange(ma50, last))
	}
	return []reportKPI{
		{"Last close", priceLabel(last), bars[n-1].Date},
		{"1 day", change(1), ""},
		{"1 month", change(21), "21 trading days"},
		{"6 months", change(126), "126 trading days"},
		{"52-week range", priceLabel(lo) + " – " + priceLabel(hi), fmt.Sprintf("%d trading days", len(window))},
		{"Volatility", vol, "annualised, last 60 days"},
		{"RSI 14", rsiText, "above 70 overbought, below 30 oversold"},
		{"vs MA50", vsMA, "last close against the 50-day average"},
	}
}

// fundamentalsTable is the caller's scalar fundamentals as a two-column Markdown table.
func fundamentalsTable(value any) (string, error) {
	m, ok := value.(map[string]any)
	if !ok {
		return "", errors.New("fundamentals must be an object of names to values")
	}
	if inner, ok := m["data"].(map[string]any); ok {
		m = inner
	}
	keys := make([]string, 0, len(m))
	for k, v := range m {
		switch v.(type) {
		case string, float64, bool:
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	if len(keys) > 40 {
		keys = keys[:40]
	}
	if len(keys) == 0 {
		return "", nil
	}
	cell := strings.NewReplacer("|", `\|`, "\n", " ", "\r", " ")
	var b strings.Builder
	b.WriteString("| Metric | Value |\n|---|---|\n")
	for _, k := range keys {
		v := m[k]
		text := fmt.Sprint(v)
		if f, ok := v.(float64); ok {
			text = compactLabel(f)
		}
		fmt.Fprintf(&b, "| %s | %s |\n", cell.Replace(capRunes(k, 60)), cell.Replace(capRunes(text, 120)))
	}
	return b.String(), nil
}

func mcpReportGenerate(ctx context.Context, args map[string]any) map[string]any {
	format, _ := args["format"].(string)
	format = strings.ToLower(strings.TrimSpace(format))
	if format == "" {
		format = "html"
	}
	if format != "html" && format != "pdf" {
		return mcpFail("format must be html or pdf")
	}
	days, err := chartWindow(args)
	if err != nil {
		return mcpFail(err.Error())
	}
	analysis, _ := args["analysis"].(string)
	if path, _ := args["analysis_path"].(string); strings.TrimSpace(path) != "" {
		if analysis != "" {
			return mcpFail("give analysis or analysis_path, not both")
		}
		md, err := documentInput(map[string]any{"markdown_path": path})
		if err != nil {
			return mcpFail(err.Error())
		}
		analysis = md
	}
	if len(analysis) > maxMarkdownBytes {
		return mcpFail("analysis is larger than 1 MiB")
	}
	fundamentals := ""
	if value, has, err := jsonArgument(args, "fundamentals", "fundamentals_path"); err != nil {
		return mcpFail(err.Error())
	} else if has {
		if fundamentals, err = fundamentalsTable(value); err != nil {
			return mcpFail(err.Error())
		}
	}
	symbol, bars, source, err := symbolBars(ctx, args)
	if err != nil {
		return mcpFail(err.Error())
	}
	title, _ := args["title"].(string)
	title = capRunes(strings.TrimSpace(title), 200)
	if title == "" {
		title = symbol + " equity report"
	}
	asOf := bars[len(bars)-1].Date
	escape := strings.NewReplacer("\\", "\\\\", "*", "\\*", "_", "\\_", "#", "\\#", "<", "&lt;", "[", "\\[")
	var md strings.Builder
	fmt.Fprintf(&md, "# %s\n\n> Data to %s | Source: %s | %d trading days\n\nARIA-REPORT-CHARTS\n\n", escape.Replace(title), escape.Replace(asOf), escape.Replace(source), len(bars))
	if fundamentals != "" {
		md.WriteString("## Fundamentals\n\n" + fundamentals + "\n")
	}
	if strings.TrimSpace(analysis) != "" {
		md.WriteString("## Analysis\n\n" + analysis + "\n\n")
	}
	md.WriteString("## Recent sessions\n\n| Date | Open | High | Low | Close | Volume |\n|---|---|---|---|---|---|\n")
	for i := len(bars) - 1; i >= max(0, len(bars)-10); i-- {
		b := bars[i]
		cell := func(v float64, ok bool) string {
			if !ok {
				return "–"
			}
			return priceLabel(v)
		}
		vol := "–"
		if b.Volume > 0 {
			vol = tickLabel(b.Volume, 1)
		}
		fmt.Fprintf(&md, "| %s | %s | %s | %s | %s | %s |\n", escape.Replace(b.Date), cell(b.Open, b.OHLC), cell(b.High, b.OHLC), cell(b.Low, b.OHLC), priceLabel(b.Close), vol)
	}
	page, err := renderDocumentHTML(md.String(), title)
	if err != nil {
		return mcpFail(err.Error())
	}
	var charts strings.Builder
	charts.WriteString(`<div class="kpis">`)
	for _, k := range equityKPIs(bars) {
		fmt.Fprintf(&charts, `<div class="kpi"><div class="k">%s</div><div class="v">%s</div><div class="s">%s</div></div>`,
			html.EscapeString(k.label), html.EscapeString(k.value), html.EscapeString(k.sub))
	}
	charts.WriteString(`</div><h2>Price</h2><div class="chart">` + priceChartSVG(symbol, bars, days, false) + `</div>`)
	charts.WriteString(`<h2>Indicators</h2><div class="chart">` + priceChartSVG(symbol, bars, days, true) + `</div>`)
	charts.WriteString(`<p class="source">Moving averages, RSI and MACD are computed from the rows above; nothing here is a forecast.</p>`)
	page = strings.Replace(page, "<p>ARIA-REPORT-CHARTS</p>", charts.String(), 1)
	page = strings.Replace(page, "</style>", reportCSS+"</style>", 1)
	out, err := documentOutput(args, title, "."+format)
	if err != nil {
		return mcpFail(err.Error())
	}
	if format == "pdf" {
		err = printPDF(ctx, page, out)
	} else {
		err = writeNew(out, []byte(page))
	}
	if err != nil {
		os.Remove(out)
		return mcpFail(err.Error())
	}
	info, _ := os.Stat(out)
	return map[string]any{"success": true, "path": out, "bytes": info.Size(), "format": format, "source": source, "as_of": asOf, "rows": len(bars)}
}

func init() {
	rowsProps := func(extra map[string]any) map[string]any {
		props := map[string]any{
			"symbol":      map[string]any{"type": "string", "description": "Ticker, 6-digit A-share code or crypto pair (BTC/USDT)."},
			"rows":        map[string]any{"type": "array", "items": map[string]any{"type": "object"}, "description": "Daily rows {date, open, high, low, close, volume}, oldest first — e.g. financial_market_history's data. Omit to fetch them from the registered financial-service."},
			"data_path":   map[string]any{"type": "string", "description": "Or a JSON file of those rows (or of the whole tool result), under the working or home directory."},
			"days":        map[string]any{"type": "integer", "description": "Trading days to show, 20–1000 (default 126, about six months). Averages use the full history."},
			"output_path": map[string]any{"type": "string", "description": "Optional new file to write (never overwritten); default: a new file under Aria's artifacts."},
		}
		for k, v := range extra {
			props[k] = v
		}
		return props
	}
	fetching := func(t mcpServedTool) mcpServedTool {
		t.Annotations["openWorldHint"] = true
		return t
	}
	title := map[string]any{"type": "string", "description": "Optional chart title."}
	mcpServerTools = append(mcpServerTools,
		fetching(writeTool("aria.report.chart", "SVG price chart: candlesticks (or the close line), MA20 and MA50, and volume, into a new file.", rowsProps(nil), "symbol")),
		fetching(writeTool("aria.report.indicator_chart", "SVG chart of price, volume, RSI(14) and MACD(12,26,9), each in its own panel, into a new file.", rowsProps(nil), "symbol")),
		fetching(writeTool("aria.report.comparison_chart", "SVG chart comparing 2–8 symbols, each indexed to 100 on the first date they share, into a new file.",
			map[string]any{
				"series":      map[string]any{"type": "object", "description": "{SYMBOL: rows or a financial_market_history result}. Or give symbols to fetch them from the financial-service."},
				"symbols":     map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "2–8 symbols to fetch when series is not given."},
				"data_path":   map[string]any{"type": "string", "description": "Or a JSON file holding the series object."},
				"days":        map[string]any{"type": "integer", "description": "Trading days to show, 20–1000 (default 126)."},
				"title":       title,
				"output_path": map[string]any{"type": "string", "description": "Optional new .svg file (never overwritten)."},
			})),
		writeTool("aria.report.allocation_chart", "SVG donut chart of portfolio weights by market value, with a holdings table, into a new file.",
			map[string]any{
				"positions":   map[string]any{"type": "array", "items": map[string]any{"type": "object"}, "description": "Positions {symbol, market_value} or {symbol, quantity, last_price} — e.g. trading_account_state's positions."},
				"data_path":   map[string]any{"type": "string", "description": "Or a JSON file of the positions (or the whole account result)."},
				"title":       title,
				"output_path": map[string]any{"type": "string", "description": "Optional new .svg file (never overwritten)."},
			}, "positions"),
		fetching(writeTool("aria.report.generate", "Equity report for one symbol as HTML or PDF: headline figures, price and indicator charts, optional fundamentals and your analysis, recent sessions.",
			rowsProps(map[string]any{
				"fundamentals":      map[string]any{"type": "object", "description": "Optional figures {name: value}, e.g. financial_fundamentals' data; shown as given."},
				"fundamentals_path": map[string]any{"type": "string", "description": "Or a JSON file of them."},
				"analysis":          map[string]any{"type": "string", "description": "Optional Markdown analysis to include."},
				"analysis_path":     map[string]any{"type": "string", "description": "Or a Markdown file of it."},
				"title":             map[string]any{"type": "string", "description": "Report title (default: '<SYMBOL> equity report')."},
				"format":            map[string]any{"type": "string", "enum": []any{"html", "pdf"}, "description": "html (default) or pdf, which needs a local Chrome, Chromium or Edge."},
			}), "symbol")),
	)
	mcpServerHandlers["aria.report.chart"] = mcpReportChart
	mcpServerHandlers["aria.report.indicator_chart"] = mcpReportIndicatorChart
	mcpServerHandlers["aria.report.comparison_chart"] = mcpReportComparisonChart
	mcpServerHandlers["aria.report.allocation_chart"] = mcpReportAllocationChart
	mcpServerHandlers["aria.report.generate"] = mcpReportGenerate
}
