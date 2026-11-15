package quant

import (
	"math"
)

type SignalBarInput struct {
	Date   string  `json:"date"`
	Open   float64 `json:"open"`
	High   float64 `json:"high"`
	Low    float64 `json:"low"`
	Close  float64 `json:"close"`
	Volume float64 `json:"volume,omitempty"`
	Signal float64 `json:"signal"`
}

type SignalBacktestConfig struct {
	InitialCapital float64 `json:"initial_capital"`
	CommissionBps  float64 `json:"commission_bps"`
	SlippageBps    float64 `json:"slippage_bps"`
	AllowShort     bool    `json:"allow_short"`
	RiskFreeRate   float64 `json:"risk_free_rate"`
	PeriodsPerYear float64 `json:"periods_per_year"`
}

type SignalTradeRecord struct {
	Date         string  `json:"date"`
	Side         string  `json:"side"`
	Shares       float64 `json:"shares"`
	ExecPrice    float64 `json:"exec_price"`
	Commission   float64 `json:"commission"`
	SlippageCost float64 `json:"slippage_cost"`
	RealizedPnL  float64 `json:"realized_pnl"`
}

type SignalBacktestResult struct {
	InitialCapital          float64             `json:"initial_capital"`
	FinalEquity             float64             `json:"final_equity"`
	TotalReturnPct          float64             `json:"total_return_pct"`
	AnnualizedReturnPct     float64             `json:"annualized_return_pct"`
	AnnualizedVolatilityPct float64             `json:"annualized_volatility_pct"`
	SharpeRatio             float64             `json:"sharpe_ratio"`
	SortinoRatio            float64             `json:"sortino_ratio"`
	MaxDrawdownPct          float64             `json:"max_drawdown_pct"`
	CalmarRatio             float64             `json:"calmar_ratio"`
	WinRatePct              float64             `json:"win_rate_pct"`
	ProfitFactor            float64             `json:"profit_factor"`
	TotalTrades             int                 `json:"total_trades"`
	TotalCommission         float64             `json:"total_commission"`
	EquityCurve             []float64           `json:"equity_curve"`
	Trades                  []SignalTradeRecord `json:"trades"`
}

func RunSignalBacktest(bars []SignalBarInput, cfg SignalBacktestConfig) SignalBacktestResult {
	initialCapital := cfg.InitialCapital
	if initialCapital <= 0 {
		initialCapital = 100000.0
	}
	periods := cfg.PeriodsPerYear
	if periods <= 0 {
		periods = 252.0
	}
	if len(bars) == 0 {
		return SignalBacktestResult{
			InitialCapital: initialCapital,
			FinalEquity:    initialCapital,
			EquityCurve:    []float64{},
			Trades:         []SignalTradeRecord{},
		}
	}

	commRate := math.Max(0, cfg.CommissionBps) / 10000.0
	slipRate := math.Max(0, cfg.SlippageBps) / 10000.0

	cash := initialCapital
	shares := 0.0
	avgCost := 0.0
	totalCommission := 0.0
	equityCurve := make([]float64, 0, len(bars))
	trades := make([]SignalTradeRecord, 0)
	var closedPnls []float64

	for _, bar := range bars {
		price := math.Max(1e-8, bar.Close)
		markEquity := cash + shares*price
		minWeight := 0.0
		if cfg.AllowShort {
			minWeight = -1.0
		}
		targetWeight := bar.Signal
		if targetWeight < minWeight {
			targetWeight = minWeight
		} else if targetWeight > 1.0 {
			targetWeight = 1.0
		}
		targetShares := (markEquity * targetWeight) / price
		deltaShares := targetShares - shares

		if math.Abs(deltaShares)*price > 1.0 {
			isBuy := deltaShares > 0
			execPrice := price * (1.0 - slipRate)
			side := "SELL"
			if isBuy {
				execPrice = price * (1.0 + slipRate)
				side = "BUY"
			}
			tradeNotional := math.Abs(deltaShares) * execPrice
			commission := tradeNotional * commRate
			slippageCost := math.Abs(deltaShares) * math.Abs(execPrice-price)

			realizedPnL := 0.0
			if shares != 0 && ((shares > 0 && !isBuy) || (shares < 0 && isBuy)) {
				closedQty := math.Min(math.Abs(deltaShares), math.Abs(shares))
				sign := 1.0
				if shares < 0 {
					sign = -1.0
				}
				realizedPnL = sign*(execPrice-avgCost)*closedQty - commission
				closedPnls = append(closedPnls, realizedPnL)
			}

			newShares := shares + deltaShares
			if math.Abs(newShares) < 1e-8 {
				avgCost = 0.0
			} else if shares == 0 || (shares > 0 && isBuy) || (shares < 0 && !isBuy) {
				totalCost := math.Abs(shares)*avgCost + math.Abs(deltaShares)*execPrice
				avgCost = totalCost / math.Abs(newShares)
			} else if (shares > 0) != (newShares > 0) {
				avgCost = execPrice
			}

			cash -= deltaShares*execPrice + commission
			shares = newShares
			totalCommission += commission

			trades = append(trades, SignalTradeRecord{
				Date:         bar.Date,
				Side:         side,
				Shares:       math.Abs(deltaShares),
				ExecPrice:    execPrice,
				Commission:   commission,
				SlippageCost: slippageCost,
				RealizedPnL:  realizedPnL,
			})
		}

		equityCurve = append(equityCurve, cash+shares*price)
	}

	finalEquity := equityCurve[len(equityCurve)-1]
	totalReturn := (finalEquity - initialCapital) / initialCapital
	years := float64(len(equityCurve)) / periods
	annualizedReturn := totalReturn
	if years > 0 && finalEquity > 0 {
		annualizedReturn = math.Pow(finalEquity/initialCapital, 1.0/years) - 1.0
	}

	periodReturns := make([]float64, 0, len(equityCurve))
	prev := initialCapital
	peak := initialCapital
	maxDD := 0.0

	for _, eq := range equityCurve {
		if prev > 0 {
			periodReturns = append(periodReturns, (eq-prev)/prev)
		}
		prev = eq
		if eq > peak {
			peak = eq
		}
		if peak > 0 {
			dd := (peak - eq) / peak
			if dd > maxDD {
				maxDD = dd
			}
		}
	}

	meanRet := 0.0
	if len(periodReturns) > 0 {
		sum := 0.0
		for _, r := range periodReturns {
			sum += r
		}
		meanRet = sum / float64(len(periodReturns))
	}
	rfPerPeriod := cfg.RiskFreeRate / periods

	variance := 0.0
	if len(periodReturns) > 1 {
		sumSq := 0.0
		for _, r := range periodReturns {
			d := r - meanRet
			sumSq += d * d
		}
		variance = sumSq / float64(len(periodReturns)-1)
	}
	stdRet := math.Sqrt(variance)
	annVol := stdRet * math.Sqrt(periods)

	sharpe := 0.0
	if stdRet > 1e-12 {
		sharpe = ((meanRet - rfPerPeriod) / stdRet) * math.Sqrt(periods)
	}

	downsideSqSum := 0.0
	for _, r := range periodReturns {
		if r < rfPerPeriod {
			d := r - rfPerPeriod
			downsideSqSum += d * d
		}
	}
	downsideDev := 0.0
	if len(periodReturns) > 0 {
		downsideDev = math.Sqrt(downsideSqSum / float64(len(periodReturns)))
	}
	sortino := 0.0
	if downsideDev > 1e-12 {
		sortino = ((meanRet - rfPerPeriod) / downsideDev) * math.Sqrt(periods)
	}

	calmar := 0.0
	if maxDD > 1e-12 {
		calmar = annualizedReturn / maxDD
	}

	wins := 0
	grossProfit := 0.0
	grossLoss := 0.0
	for _, p := range closedPnls {
		if p > 0 {
			wins++
			grossProfit += p
		} else if p < 0 {
			grossLoss += -p
		}
	}
	winRatePct := 0.0
	if len(closedPnls) > 0 {
		winRatePct = (float64(wins) / float64(len(closedPnls))) * 100.0
	}
	profitFactor := 0.0
	if grossLoss > 1e-12 {
		profitFactor = grossProfit / grossLoss
	} else if grossProfit > 0 {
		profitFactor = 999.99
	}

	return SignalBacktestResult{
		InitialCapital:          initialCapital,
		FinalEquity:             finalEquity,
		TotalReturnPct:          totalReturn * 100.0,
		AnnualizedReturnPct:     annualizedReturn * 100.0,
		AnnualizedVolatilityPct: annVol * 100.0,
		SharpeRatio:             sharpe,
		SortinoRatio:            sortino,
		MaxDrawdownPct:          maxDD * 100.0,
		CalmarRatio:             calmar,
		WinRatePct:              winRatePct,
		ProfitFactor:            profitFactor,
		TotalTrades:             len(trades),
		TotalCommission:         totalCommission,
		EquityCurve:             equityCurve,
		Trades:                  trades,
	}
}
