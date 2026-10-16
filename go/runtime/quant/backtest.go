// Package quant supplies the native event-driven portfolio backtest core.
// Orders decided on a bar's close fill at the following date's open.
package quant

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"time"
)

type Bar struct {
	Date   string  `json:"date"`
	Open   float64 `json:"open"`
	High   float64 `json:"high"`
	Low    float64 `json:"low"`
	Close  float64 `json:"close"`
	Volume float64 `json:"volume"`
}
type Series struct {
	Symbol string
	Bars   []Bar
}
type Order struct {
	Symbol       string   `json:"symbol"`
	Action       string   `json:"action"`
	Quantity     float64  `json:"quantity"`
	TargetWeight *float64 `json:"target_weight,omitempty"`
}
type Portfolio struct {
	Cash      float64
	Positions map[string]float64
}

func (p Portfolio) MarketValue(prices map[string]float64) float64 {
	v := 0.0
	for s, q := range p.Positions {
		v += q * prices[s]
	}
	return v
}
func (p Portfolio) Equity(prices map[string]float64) float64 { return p.Cash + p.MarketValue(prices) }

type Context struct {
	Date      string
	Symbols   []string
	Bars      map[string]Bar
	History   map[string][]Bar
	Portfolio Portfolio
}

func (c Context) Price(symbol string) float64    { return c.Bars[symbol].Close }
func (c Context) Position(symbol string) float64 { return c.Portfolio.Positions[symbol] }
func (c Context) Closes(symbol string, n int) []float64 {
	bars := c.History[symbol]
	if n > 0 && len(bars) > n {
		bars = bars[len(bars)-n:]
	}
	out := make([]float64, len(bars))
	for i, b := range bars {
		out[i] = b.Close
	}
	return out
}
func (c Context) Equity() float64 {
	prices := map[string]float64{}
	for s, b := range c.Bars {
		prices[s] = b.Close
	}
	return c.Portfolio.Equity(prices)
}

type Strategy interface {
	Name() string
	OnBar(Context) ([]Order, error)
}
type Point struct {
	Date   string  `json:"date"`
	Equity float64 `json:"equity"`
}
type Trade struct {
	Date        string   `json:"date"`
	Symbol      string   `json:"symbol"`
	Side        string   `json:"side"`
	Quantity    float64  `json:"qty"`
	Price       float64  `json:"price"`
	Fee         float64  `json:"fee"`
	RealizedPnL *float64 `json:"realized_pnl,omitempty"`
}
type Metrics struct {
	TotalReturn  float64 `json:"total_return"`
	AnnualReturn float64 `json:"annual_return"`
	Sharpe       float64 `json:"sharpe"`
	MaxDrawdown  float64 `json:"max_drawdown"`
	Trades       int     `json:"n_trades"`
	WinRate      float64 `json:"win_rate"`
}
type Result struct {
	Strategy     string             `json:"strategy"`
	Symbols      []string           `json:"symbols"`
	StartingCash float64            `json:"starting_cash"`
	Equity       []Point            `json:"equity_curve"`
	Trades       []Trade            `json:"trades"`
	Metrics      Metrics            `json:"metrics"`
	Benchmark    map[string]float64 `json:"benchmark"`
}
type Engine struct {
	StartingCash, Commission, Slippage float64
	PeriodsPerYear                     int
}

// DefaultEngine leaves PeriodsPerYear at 0: periods per year come from the data's dates.
func DefaultEngine() Engine { return Engine{100000, 0.0005, 0.0005, 0} }
func finite(n float64) bool { return !math.IsNaN(n) && !math.IsInf(n, 0) }

// Decimal formatting rounds the original binary value, matching Python round.
// Scaling first would incorrectly turn round(2.675, 2) into 2.68.
func round(n float64, places int) float64 {
	v, _ := strconv.ParseFloat(strconv.FormatFloat(n, 'f', places, 64), 64)
	return v
}
func copyPositions(in map[string]float64) map[string]float64 {
	out := map[string]float64{}
	for s, n := range in {
		out[s] = n
	}
	return out
}
func normalize(o Order, equity float64, prices, positions map[string]float64) Order {
	if o.Action != "target" || o.TargetWeight == nil {
		return o
	}
	price := prices[o.Symbol]
	if price <= 0 {
		return Order{Symbol: o.Symbol, Action: "buy"}
	}
	delta := *o.TargetWeight*equity/price - positions[o.Symbol]
	side := "buy"
	if delta < 0 {
		side = "sell"
	}
	return Order{Symbol: o.Symbol, Action: side, Quantity: round(math.Abs(delta), 6)}
}

func (e Engine) Run(data []Series, strategy Strategy) (Result, error) {
	if strategy == nil {
		return Result{}, errors.New("strategy is required")
	}
	if !finite(e.StartingCash) || e.StartingCash <= 0 || !finite(e.Commission) || e.Commission < 0 || e.Commission >= 1 || !finite(e.Slippage) || e.Slippage < 0 || e.Slippage >= 1 || e.PeriodsPerYear < 0 {
		return Result{}, errors.New("invalid backtest capital, costs or periods")
	}
	r := Result{Strategy: strategy.Name(), StartingCash: e.StartingCash, Symbols: []string{}, Equity: []Point{}, Trades: []Trade{}}
	byDate := map[string]map[string]Bar{}
	datesSet := map[string]bool{}
	for _, series := range data {
		if series.Symbol == "" {
			return r, errors.New("empty symbol")
		}
		if _, exists := byDate[series.Symbol]; exists {
			return r, errors.New("duplicate symbol")
		}
		r.Symbols = append(r.Symbols, series.Symbol)
		byDate[series.Symbol] = map[string]Bar{}
		for _, bar := range series.Bars {
			if bar.Date == "" || !finite(bar.Open) || !finite(bar.Close) || bar.Open <= 0 || bar.Close <= 0 {
				return r, fmt.Errorf("invalid date or open/close price for %s", series.Symbol)
			}
			byDate[series.Symbol][bar.Date] = bar
			datesSet[bar.Date] = true
		}
	}
	dates := make([]string, 0, len(datesSet))
	for d := range datesSet {
		dates = append(dates, d)
	}
	sort.Strings(dates)
	cash := e.StartingCash
	positions := map[string]float64{}
	cost := map[string]float64{}
	history := map[string][]Bar{}
	last := map[string]float64{}
	pending := []Order{}
	for _, date := range dates {
		current := map[string]Bar{}
		currentSymbols := []string{}
		for _, symbol := range r.Symbols {
			if bar, ok := byDate[symbol][date]; ok {
				current[symbol] = bar
				currentSymbols = append(currentSymbols, symbol)
				history[symbol] = append(history[symbol], bar)
				last[symbol] = bar.Close
			}
		}
		for _, order := range pending {
			bar, ok := current[order.Symbol]
			if !ok || order.Quantity <= 0 {
				continue
			}
			fill := bar.Open * (1 + e.Slippage)
			if order.Action == "sell" {
				fill = bar.Open * (1 - e.Slippage)
			}
			qty := order.Quantity
			if order.Action == "buy" {
				gross := fill * qty
				fee := gross * e.Commission
				if gross+fee > cash {
					qty = math.Max(0, cash/(fill*(1+e.Commission)))
					gross = fill * qty
					fee = gross * e.Commission
				}
				if qty <= 0 {
					continue
				}
				oldQty := positions[order.Symbol]
				newQty := oldQty + qty
				cost[order.Symbol] = (cost[order.Symbol]*oldQty + fill*qty) / newQty
				positions[order.Symbol] = newQty
				cash -= gross + fee
				r.Trades = append(r.Trades, Trade{Date: date, Symbol: order.Symbol, Side: "buy", Quantity: round(qty, 6), Price: round(fill, 6), Fee: round(fee, 6)})
			} else if order.Action == "sell" {
				qty = math.Min(qty, positions[order.Symbol])
				if qty <= 0 {
					continue
				}
				gross := fill * qty
				fee := gross * e.Commission
				realized := round((fill-cost[order.Symbol])*qty-fee, 6)
				positions[order.Symbol] -= qty
				cash += gross - fee
				r.Trades = append(r.Trades, Trade{Date: date, Symbol: order.Symbol, Side: "sell", Quantity: round(qty, 6), Price: round(fill, 6), Fee: round(fee, 6), RealizedPnL: &realized})
			}
		}
		pending = nil
		market := 0.0
		for _, symbol := range r.Symbols {
			market += positions[symbol] * last[symbol]
		}
		equity := cash + market
		r.Equity = append(r.Equity, Point{date, round(equity, 4)})
		// A strategy receives snapshots, never the engine's mutable positions.
		hist := map[string][]Bar{}
		for symbol, bars := range history {
			hist[symbol] = append([]Bar{}, bars...)
		}
		orders, err := strategy.OnBar(Context{date, currentSymbols, current, hist, Portfolio{cash, copyPositions(positions)}})
		if err != nil {
			continue
		} // Preserve existing strategy-error/no-order behavior.
		for _, o := range orders {
			if _, ok := byDate[o.Symbol]; !ok {
				return r, errors.New("strategy returned an unknown symbol")
			}
			if !finite(o.Quantity) || (o.TargetWeight != nil && !finite(*o.TargetWeight)) {
				return r, errors.New("strategy returned a non-finite order")
			}
			if o.Action != "buy" && o.Action != "sell" && o.Action != "target" {
				return r, errors.New("strategy returned an unknown action")
			}
			pending = append(pending, normalize(o, equity, last, positions))
		}
	}
	r.Metrics = calculateMetrics(r.Equity, r.Trades, e.PeriodsPerYear)
	benchmark, n := 0.0, 0
	if len(dates) >= 2 {
		for _, s := range data {
			if len(s.Bars) > 0 && s.Bars[0].Close > 0 {
				benchmark += s.Bars[len(s.Bars)-1].Close/s.Bars[0].Close - 1
				n++
			}
		}
	}
	if n > 0 {
		benchmark /= float64(n)
	}
	r.Benchmark = map[string]float64{"total_return": round(benchmark, 6)}
	if !finite(r.Metrics.AnnualReturn) || !finite(r.Metrics.Sharpe) {
		return r, errors.New("backtest metrics exceeded finite numeric range")
	}
	return r, nil
}

const (
	fallbackPeriodsPerYear = 252
	daysPerYear            = 365.25
)

// spanYears is the calendar span of two ISO dates in years, clamped to one
// trading day from below. ok is false when either date is unreadable or the
// span does not run forward.
func spanYears(first, last string) (float64, bool) {
	a, okA := parseDay(first)
	b, okB := parseDay(last)
	if !okA || !okB || !b.After(a) {
		return 0, false
	}
	days := math.Round(b.Sub(a).Hours() / 24)
	return math.Max(days/daysPerYear, 1.0/fallbackPeriodsPerYear), true
}

func parseDay(text string) (time.Time, bool) {
	if len(text) < 10 {
		return time.Time{}, false
	}
	t, err := time.Parse("2006-01-02", text[:10])
	return t, err == nil
}

func calculateMetrics(curve []Point, trades []Trade, periods int) Metrics {
	m := Metrics{Trades: len(trades)}
	if len(curve) < 2 || curve[0].Equity <= 0 {
		return m
	}
	returns := []float64{}
	peak := curve[0].Equity
	drawdown := 0.0
	for i, p := range curve {
		peak = math.Max(peak, p.Equity)
		if peak > 0 {
			drawdown = math.Min(drawdown, p.Equity/peak-1)
		}
		if i > 0 && curve[i-1].Equity > 0 {
			returns = append(returns, p.Equity/curve[i-1].Equity-1)
		}
	}
	total := curve[len(curve)-1].Equity/curve[0].Equity - 1
	m.TotalReturn = round(total, 6)
	// periods > 0 is the caller's own periods-per-year and keeps the original
	// formula. Otherwise the span comes from the curve's dates (see
	// performance_stats.py, which this mirrors); unreadable dates fall back to 252.
	sharpePeriods := float64(fallbackPeriodsPerYear)
	annual := -1.0
	switch years, ok := spanYears(curve[0].Date, curve[len(curve)-1].Date); {
	case periods > 0:
		sharpePeriods = float64(periods)
		if total > -1 {
			annual = math.Pow(1+total, sharpePeriods/float64(len(curve))) - 1
		}
	case ok:
		sharpePeriods = float64(len(returns)) / years
		if total > -1 {
			annual = math.Pow(1+total, 1/years) - 1
		}
	default:
		if total > -1 {
			annual = math.Pow(1+total, sharpePeriods/float64(len(curve))) - 1
		}
	}
	m.AnnualReturn = round(annual, 6)
	m.MaxDrawdown = round(drawdown, 6)
	if len(returns) >= 2 {
		mean := 0.0
		for _, r := range returns {
			mean += r
		}
		mean /= float64(len(returns))
		variance := 0.0
		for _, r := range returns {
			variance += (r - mean) * (r - mean)
		}
		std := math.Sqrt(variance / float64(len(returns)-1))
		if std > 0 {
			m.Sharpe = round(mean/std*math.Sqrt(sharpePeriods), 4)
		}
	}
	sells, wins := 0, 0
	for _, t := range trades {
		if t.Side == "sell" {
			sells++
			if t.RealizedPnL != nil && *t.RealizedPnL > 0 {
				wins++
			}
		}
	}
	if sells > 0 {
		m.WinRate = round(float64(wins)/float64(sells), 4)
	}
	return m
}

type BuyHold struct {
	Weight  float64
	entered map[string]bool
}

func (s *BuyHold) Name() string { return "buy_hold" }
func (s *BuyHold) OnBar(c Context) ([]Order, error) {
	if s.entered == nil {
		s.entered = map[string]bool{}
	}
	orders := []Order{}
	per := s.Weight / math.Max(1, float64(len(c.Bars)))
	for _, symbol := range c.Symbols {
		if !s.entered[symbol] {
			s.entered[symbol] = true
			orders = append(orders, Order{Symbol: symbol, Action: "target", TargetWeight: &per})
		}
	}
	return orders, nil
}

type SMACross struct {
	Fast, Slow int
	Weight     float64
}

func (s *SMACross) Name() string { return "sma_cross" }
func (s *SMACross) OnBar(c Context) ([]Order, error) {
	orders := []Order{}
	per := s.Weight / math.Max(1, float64(len(c.Bars)))
	for _, symbol := range c.Symbols {
		closes := c.Closes(symbol, 0)
		if len(closes) < s.Slow {
			continue
		}
		start := len(closes) - s.Fast
		if start < 0 {
			start = 0
		}
		fast, slow := 0.0, 0.0
		for _, v := range closes[start:] {
			fast += v
		}
		for _, v := range closes[len(closes)-s.Slow:] {
			slow += v
		}
		fast /= float64(s.Fast)
		slow /= float64(s.Slow)
		held := c.Position(symbol) > 0
		if fast > slow && !held {
			orders = append(orders, Order{Symbol: symbol, Action: "target", TargetWeight: &per})
		} else if fast <= slow && held {
			zero := 0.0
			orders = append(orders, Order{Symbol: symbol, Action: "target", TargetWeight: &zero})
		}
	}
	return orders, nil
}
func NewStrategy(name string, fast, slow int, weight *float64) (Strategy, error) {
	switch name {
	case "buy_hold":
		w := 1.0
		if weight != nil {
			w = *weight
		}
		if !finite(w) || w < 0 {
			return nil, errors.New("invalid strategy weight")
		}
		return &BuyHold{Weight: w}, nil
	case "sma_cross":
		if fast == 0 {
			fast = 20
		}
		if slow == 0 {
			slow = 60
		}
		if fast < 1 || slow < 1 {
			return nil, errors.New("moving-average windows must be positive")
		}
		w := 0.95
		if weight != nil {
			w = *weight
		}
		if !finite(w) || w < 0 {
			return nil, errors.New("invalid strategy weight")
		}
		return &SMACross{fast, slow, w}, nil
	default:
		return nil, fmt.Errorf("unknown strategy: %s (have: buy_hold, sma_cross)", name)
	}
}

// The same order representation can enter a native broker preview/risk flow.
type OrderIntent struct {
	Symbol       string   `json:"symbol"`
	Side         string   `json:"side"`
	Quantity     *float64 `json:"quantity,omitempty"`
	TargetWeight *float64 `json:"target_weight,omitempty"`
	Price        *float64 `json:"price,omitempty"`
	Source       string   `json:"source"`
}

func ToIntent(o Order, price *float64, source string) OrderIntent {
	if o.Action == "target" {
		return OrderIntent{Symbol: o.Symbol, Side: "buy", TargetWeight: o.TargetWeight, Source: source}
	}
	qty := o.Quantity
	return OrderIntent{Symbol: o.Symbol, Side: o.Action, Quantity: &qty, Price: price, Source: source}
}
