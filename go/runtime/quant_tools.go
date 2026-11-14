package main

import (
	"context"
	"errors"
	"fmt"

	"github.com/artheras/aria-code/go/runtime/quant"
)

func priceOptionTool(ctx context.Context, params map[string]any) toolResult {
	s, okS := num(params, "s")
	k, okK := num(params, "k")
	t, okT := num(params, "t")
	if !okS || !okK || !okT || s <= 0 || k <= 0 || t <= 0 {
		return failed(errors.New("parameters s (spot), k (strike), and t (time to maturity > 0) are required positive numbers"))
	}
	r, _ := num(params, "r")
	q, _ := num(params, "q")
	sigma, _ := num(params, "sigma")
	if sigma <= 0 {
		sigma = 0.20 // default 20% vol
	}
	isCall, _ := params["is_call"].(bool)
	if _, hasCall := params["is_call"]; !hasCall {
		isCall = true // default Call
	}

	spec := quant.OptionSpec{
		S:      s,
		K:      k,
		T:      t,
		R:      r,
		Sigma:  sigma,
		IsCall: isCall,
		Q:      q,
	}

	data := map[string]any{
		"spec": map[string]any{
			"s":       s,
			"k":       k,
			"t":       t,
			"r":       r,
			"q":       q,
			"sigma":   sigma,
			"is_call": isCall,
		},
	}

	// 1. Black-Scholes Greeks
	greeks := quant.CalculateGreeks(spec)
	data["greeks"] = map[string]any{
		"price": greeks.Price,
		"delta": greeks.Delta,
		"gamma": greeks.Gamma,
		"vega":  greeks.Vega,
		"theta": greeks.Theta,
		"rho":   greeks.Rho,
		"vanna": greeks.Vanna,
		"volga": greeks.Volga,
	}

	// 2. American option price if requested
	if amer, _ := params["american"].(bool); amer {
		data["american_price"] = quant.PriceAmericanCRR(spec, 500)
	}

	// 3. Implied volatility if market_price is supplied
	if mktPrice, okMkt := num(params, "market_price"); okMkt && mktPrice > 0 {
		if iv, err := quant.ImpliedVolatility(mktPrice, spec); err == nil {
			data["implied_volatility"] = iv
		} else {
			data["implied_volatility_error"] = err.Error()
		}
	}

	return toolResult{
		Success: true,
		Data:    data,
	}
}

func optimizePortfolioTool(ctx context.Context, params map[string]any) toolResult {
	assetsRaw, _ := params["assets"].([]any)
	returnsRaw, _ := params["returns"].([]any)
	covRaw, _ := params["cov_matrix"].([]any)

	n := len(assetsRaw)
	if n == 0 || len(returnsRaw) != n || len(covRaw) != n {
		return failed(errors.New("assets, returns, and cov_matrix must be provided with matching dimensions"))
	}

	assets := make([]string, n)
	returns := make([]float64, n)
	cov := make([][]float64, n)

	for i := 0; i < n; i++ {
		assets[i] = fmt.Sprint(assetsRaw[i])
		retVal, _ := numVal(returnsRaw[i])
		returns[i] = retVal

		rowRaw, okRow := covRaw[i].([]any)
		if !okRow || len(rowRaw) != n {
			return failed(fmt.Errorf("cov_matrix row %d has length %d, expected %d", i, len(rowRaw), n))
		}
		cov[i] = make([]float64, n)
		for j := 0; j < n; j++ {
			cVal, _ := numVal(rowRaw[j])
			cov[i][j] = cVal
		}
	}

	rpWeights, errRP := quant.OptimizeRiskParity(cov)
	if errRP != nil {
		return failed(errRP)
	}
	rpVol := quant.PortfolioVolatility(rpWeights, cov)
	rpRet := quant.PortfolioReturn(rpWeights, returns)

	mvWeights, errMV := quant.OptimizeMinVariance(cov)
	if errMV != nil {
		return failed(errMV)
	}
	mvVol := quant.PortfolioVolatility(mvWeights, cov)
	mvRet := quant.PortfolioReturn(mvWeights, returns)

	allocations := map[string]any{
		"assets": assets,
		"risk_parity": map[string]any{
			"weights":             rpWeights,
			"expected_return":     rpRet,
			"expected_volatility": rpVol,
			"sharpe_ratio":        rpRet / mathMax(rpVol, 1e-8),
		},
		"minimum_variance": map[string]any{
			"weights":             mvWeights,
			"expected_return":     mvRet,
			"expected_volatility": mvVol,
			"sharpe_ratio":        mvRet / mathMax(mvVol, 1e-8),
		},
	}

	return toolResult{
		Success: true,
		Data:    allocations,
	}
}

func runBacktestTool(ctx context.Context, params map[string]any) toolResult {
	barsRaw, ok := params["bars"].([]any)
	if !ok || len(barsRaw) == 0 {
		return failed(errors.New("parameter 'bars' must be a non-empty array of bar objects {date, close, signal, ...}"))
	}
	bars := make([]quant.SignalBarInput, 0, len(barsRaw))
	for i, item := range barsRaw {
		m, ok := item.(map[string]any)
		if !ok {
			return failed(fmt.Errorf("bar at index %d must be an object", i))
		}
		closePrice, okClose := num(m, "close")
		if !okClose || closePrice <= 0 {
			return failed(fmt.Errorf("bar at index %d missing positive 'close' price", i))
		}
		dateStr, _ := m["date"].(string)
		if dateStr == "" {
			dateStr = fmt.Sprintf("bar-%d", i)
		}
		openPrice, _ := num(m, "open")
		highPrice, _ := num(m, "high")
		lowPrice, _ := num(m, "low")
		vol, _ := num(m, "volume")
		signal, _ := num(m, "signal")
		bars = append(bars, quant.SignalBarInput{
			Date:   dateStr,
			Open:   openPrice,
			High:   highPrice,
			Low:    lowPrice,
			Close:  closePrice,
			Volume: vol,
			Signal: signal,
		})
	}

	cfg := quant.SignalBacktestConfig{
		InitialCapital: 100000.0,
		CommissionBps:  5.0,
		SlippageBps:    5.0,
		RiskFreeRate:   0.02,
		PeriodsPerYear: 252.0,
	}
	if capVal, ok := num(params, "initial_capital"); ok && capVal > 0 {
		cfg.InitialCapital = capVal
	}
	if comm, ok := num(params, "commission_bps"); ok && comm >= 0 {
		cfg.CommissionBps = comm
	}
	if slip, ok := num(params, "slippage_bps"); ok && slip >= 0 {
		cfg.SlippageBps = slip
	}
	if allowShort, ok := params["allow_short"].(bool); ok {
		cfg.AllowShort = allowShort
	}
	if rf, ok := num(params, "risk_free_rate"); ok {
		cfg.RiskFreeRate = rf
	}
	if ppy, ok := num(params, "periods_per_year"); ok && ppy > 0 {
		cfg.PeriodsPerYear = ppy
	}

	res := quant.RunSignalBacktest(bars, cfg)
	// Return concise summary + truncated curve/trades if huge to keep LLM context clean
	maxTrades := 50
	tradesOut := res.Trades
	if len(tradesOut) > maxTrades {
		tradesOut = tradesOut[len(tradesOut)-maxTrades:]
	}
	return toolResult{
		Success: true,
		Data: map[string]any{
			"initial_capital":           res.InitialCapital,
			"final_equity":              res.FinalEquity,
			"total_return_pct":          res.TotalReturnPct,
			"annualized_return_pct":     res.AnnualizedReturnPct,
			"annualized_volatility_pct": res.AnnualizedVolatilityPct,
			"sharpe_ratio":              res.SharpeRatio,
			"sortino_ratio":             res.SortinoRatio,
			"max_drawdown_pct":          res.MaxDrawdownPct,
			"calmar_ratio":              res.CalmarRatio,
			"win_rate_pct":              res.WinRatePct,
			"profit_factor":             res.ProfitFactor,
			"total_trades":              res.TotalTrades,
			"total_commission":          res.TotalCommission,
			"recent_trades":             tradesOut,
		},
	}
}

func num(params map[string]any, key string) (float64, bool) {
	val, exists := params[key]
	if !exists {
		return 0, false
	}
	return numVal(val)
}

func numVal(val any) (float64, bool) {
	switch v := val.(type) {
	case float64:
		return v, true
	case float32:
		return float64(v), true
	case int:
		return float64(v), true
	case int64:
		return float64(v), true
	default:
		return 0, false
	}
}

func mathMax(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
