package quant

import (
	"errors"
	"fmt"
	"math"
	"math/rand"
	"sort"
)

type DCFProjection struct {
	Year           int     `json:"year"`
	GrowthRate     float64 `json:"growth_rate"`
	ProjectedFCF   float64 `json:"projected_fcf"`
	DiscountFactor float64 `json:"discount_factor"`
	PVFCF          float64 `json:"pv_fcf"`
}

type DCFSensitivityRow struct {
	WACC            float64            `json:"wacc"`
	ImpliedByGrowth map[string]float64 `json:"implied_share_prices_by_tg"`
}

type DCFResult struct {
	Projections          []DCFProjection     `json:"projections"`
	PVExplicitFCF        float64             `json:"pv_explicit_fcf"`
	TerminalValue        float64             `json:"terminal_value"`
	PVTerminalValue      float64             `json:"pv_terminal_value"`
	TerminalValueShareEV float64             `json:"terminal_value_share_of_ev"`
	EnterpriseValue      float64             `json:"enterprise_value"`
	EquityValue          float64             `json:"equity_value"`
	ImpliedSharePrice    float64             `json:"implied_share_price"`
	UpsideVsCurrentPrice *float64            `json:"upside_vs_current_price,omitempty"`
	SensitivityMatrix    []DCFSensitivityRow `json:"sensitivity_matrix"`
}

func roundTo(v float64, decimals int) float64 {
	pow := math.Pow(10, float64(decimals))
	return math.Round(v*pow) / pow
}

func DCFValuation(
	baseFCF float64,
	growthRates []float64,
	wacc float64,
	terminalGrowth float64,
	netDebt float64,
	dilutedShares float64,
	currentPrice *float64,
) (DCFResult, error) {
	if math.IsNaN(baseFCF) || math.IsInf(baseFCF, 0) || baseFCF <= 0 {
		return DCFResult{}, errors.New("base_fcf must be positive")
	}
	if len(growthRates) == 0 || len(growthRates) > 10 {
		return DCFResult{}, errors.New("growth_rates requires 1..10 annual growth rates")
	}
	if wacc <= 0.01 || wacc > 0.50 {
		return DCFResult{}, errors.New("wacc must be in (0.01, 0.50]")
	}
	if terminalGrowth < -0.05 || terminalGrowth > 0.15 {
		return DCFResult{}, errors.New("terminal_growth must be in [-0.05, 0.15]")
	}
	if wacc <= terminalGrowth {
		return DCFResult{}, errors.New("wacc must be strictly greater than terminal_growth")
	}
	if dilutedShares <= 0 {
		return DCFResult{}, errors.New("diluted_shares must be positive")
	}

	runOne := func(rWacc, rTg float64) (float64, float64, float64, float64, float64, float64, []DCFProjection, bool) {
		if rWacc <= rTg {
			return 0, 0, 0, 0, 0, 0, nil, false
		}
		fcf := baseFCF
		pvSum := 0.0
		projs := make([]DCFProjection, 0, len(growthRates))
		for i, g := range growthRates {
			fcf *= (1.0 + g)
			df := math.Pow(1.0+rWacc, float64(i+1))
			pv := fcf / df
			pvSum += pv
			projs = append(projs, DCFProjection{
				Year:           i + 1,
				GrowthRate:     roundTo(g, 4),
				ProjectedFCF:   roundTo(fcf, 2),
				DiscountFactor: roundTo(df, 4),
				PVFCF:          roundTo(pv, 2),
			})
		}
		tv := (fcf * (1.0 + rTg)) / (rWacc - rTg)
		pvTV := tv / math.Pow(1.0+rWacc, float64(len(growthRates)))
		ev := pvSum + pvTV
		eq := ev - netDebt
		px := eq / dilutedShares
		return pvSum, tv, pvTV, ev, eq, px, projs, true
	}

	pvSum, tv, pvTV, ev, eq, px, projs, ok := runOne(wacc, terminalGrowth)
	if !ok {
		return DCFResult{}, errors.New("invalid DCF discount parameters")
	}

	wOffsets := []float64{-0.01, -0.005, 0.0, 0.005, 0.01}
	tgOffsets := []float64{-0.005, -0.0025, 0.0, 0.0025, 0.005}
	matrix := make([]DCFSensitivityRow, 0, len(wOffsets))
	for _, dw := range wOffsets {
		wVal := roundTo(wacc+dw, 4)
		rowMap := map[string]float64{}
		for _, dtg := range tgOffsets {
			tgVal := roundTo(terminalGrowth+dtg, 4)
			if _, _, _, _, _, sPx, _, valid := runOne(wVal, tgVal); valid {
				rowMap[fmt.Sprintf("%.4f", tgVal)] = roundTo(sPx, 2)
			}
		}
		matrix = append(matrix, DCFSensitivityRow{WACC: wVal, ImpliedByGrowth: rowMap})
	}

	tvShare := 0.0
	if ev > 0 {
		tvShare = roundTo(pvTV/ev, 4)
	}
	res := DCFResult{
		Projections:          projs,
		PVExplicitFCF:        roundTo(pvSum, 2),
		TerminalValue:        roundTo(tv, 2),
		PVTerminalValue:      roundTo(pvTV, 2),
		TerminalValueShareEV: tvShare,
		EnterpriseValue:      roundTo(ev, 2),
		EquityValue:          roundTo(eq, 2),
		ImpliedSharePrice:    roundTo(px, 2),
		SensitivityMatrix:    matrix,
	}
	if currentPrice != nil && *currentPrice > 0 {
		up := roundTo((px-*currentPrice)/(*currentPrice), 4)
		res.UpsideVsCurrentPrice = &up
	}
	return res, nil
}

type RiskMetricsResult struct {
	Observations        int     `json:"observations"`
	TotalReturnPct      float64 `json:"total_return_pct"`
	AnnualizedReturnPct float64 `json:"annualized_return_pct"`
	AnnualizedVolPct    float64 `json:"annualized_volatility_pct"`
	SharpeRatio         float64 `json:"sharpe_ratio"`
	SortinoRatio        float64 `json:"sortino_ratio"`
	MaxDrawdownPct      float64 `json:"max_drawdown_pct"`
	CalmarRatio         float64 `json:"calmar_ratio"`
	HistoricalVaR95Pct  float64 `json:"historical_var_95_pct"`
	HistoricalCVaR95Pct float64 `json:"historical_cvar_95_pct"`
	HistoricalVaR99Pct  float64 `json:"historical_var_99_pct"`
	HistoricalCVaR99Pct float64 `json:"historical_cvar_99_pct"`
	ParametricVaR95Pct  float64 `json:"parametric_var_95_pct"`
	ParametricCVaR95Pct float64 `json:"parametric_cvar_95_pct"`
	MonteCarloVaR95Pct  float64 `json:"monte_carlo_var_95_pct"`
	MonteCarloCVaR95Pct float64 `json:"monte_carlo_cvar_95_pct"`
}

func tailVaRCVaR(sortedReturns []float64, alpha float64) (float64, float64) {
	n := len(sortedReturns)
	if n == 0 {
		return 0, 0
	}
	idx := int(math.Floor(alpha * float64(n)))
	if idx < 0 {
		idx = 0
	}
	if idx >= n {
		idx = n - 1
	}
	varLoss := -sortedReturns[idx]
	tailCount := max(1, idx+1)
	sum := 0.0
	for i := 0; i < tailCount; i++ {
		sum += sortedReturns[i]
	}
	cvarLoss := -(sum / float64(tailCount))
	return max(0.0, varLoss), max(0.0, cvarLoss)
}

// CalculateRiskMetrics computes return, drawdown, Sharpe/Sortino/Calmar, and
// Historical, Parametric Gaussian, and deterministic seeded Monte Carlo VaR/CVaR
// from an ordered equity curve or periodic simple return series.
func CalculateRiskMetrics(equityOrReturns []float64, isReturns bool, periodsPerYear, riskFreeRate float64) (RiskMetricsResult, error) {
	if periodsPerYear <= 0 {
		periodsPerYear = 252.0
	}
	var rets []float64
	if isReturns {
		if len(equityOrReturns) < 2 {
			return RiskMetricsResult{}, errors.New("at least 2 return observations are required")
		}
		rets = append(rets, equityOrReturns...)
	} else {
		if len(equityOrReturns) < 3 {
			return RiskMetricsResult{}, errors.New("at least 3 equity points are required")
		}
		for i, v := range equityOrReturns {
			if math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 {
				return RiskMetricsResult{}, fmt.Errorf("equity point %d must be positive", i)
			}
			if i > 0 {
				rets = append(rets, (v/equityOrReturns[i-1])-1.0)
			}
		}
	}

	n := len(rets)
	mean := 0.0
	eq := 1.0
	peak := 1.0
	maxDD := 0.0
	downsideSq := 0.0
	downsideCount := 0
	rfPeriod := riskFreeRate / periodsPerYear

	for _, r := range rets {
		mean += r
		eq *= (1.0 + r)
		if eq > peak {
			peak = eq
		}
		dd := (peak - eq) / peak
		if dd > maxDD {
			maxDD = dd
		}
		if r < rfPeriod {
			diff := r - rfPeriod
			downsideSq += diff * diff
			downsideCount++
		}
	}
	mean /= float64(n)

	varSum := 0.0
	for _, r := range rets {
		d := r - mean
		varSum += d * d
	}
	std := 0.0
	if n > 1 {
		std = math.Sqrt(varSum / float64(n-1))
	}
	downsideDev := 0.0
	if downsideCount > 0 {
		downsideDev = math.Sqrt(downsideSq / float64(n))
	}

	totalRet := eq - 1.0
	annRet := math.Pow(max(eq, 1e-12), periodsPerYear/float64(n)) - 1.0
	annVol := std * math.Sqrt(periodsPerYear)
	sharpe := 0.0
	if annVol > 1e-12 {
		sharpe = (annRet - riskFreeRate) / annVol
	}
	sortino := 0.0
	if downsideDev > 1e-12 {
		sortino = (annRet - riskFreeRate) / (downsideDev * math.Sqrt(periodsPerYear))
	}
	calmar := 0.0
	if maxDD > 1e-12 {
		calmar = annRet / maxDD
	}

	sorted := append([]float64(nil), rets...)
	sort.Float64s(sorted)
	hVaR95, hCVaR95 := tailVaRCVaR(sorted, 0.05)
	hVaR99, hCVaR99 := tailVaRCVaR(sorted, 0.01)

	// Parametric Gaussian VaR/CVaR at 95% (z = 1.6448536, phi(z)/0.05 = 2.0627128)
	pVaR95 := max(0.0, -(mean - 1.6448536*std))
	pCVaR95 := max(0.0, -(mean - 2.0627128*std))

	// Deterministic seeded Monte Carlo (10,000 draws) for reproducible VaR/CVaR
	rng := rand.New(rand.NewSource(42))
	const mcN = 10000
	mcRets := make([]float64, mcN)
	for i := 0; i < mcN; i++ {
		mcRets[i] = mean + std*rng.NormFloat64()
	}
	sort.Float64s(mcRets)
	mcVaR95, mcCVaR95 := tailVaRCVaR(mcRets, 0.05)

	return RiskMetricsResult{
		Observations:        n,
		TotalReturnPct:      roundTo(totalRet*100, 2),
		AnnualizedReturnPct: roundTo(annRet*100, 2),
		AnnualizedVolPct:    roundTo(annVol*100, 2),
		SharpeRatio:         roundTo(sharpe, 3),
		SortinoRatio:        roundTo(sortino, 3),
		MaxDrawdownPct:      roundTo(maxDD*100, 2),
		CalmarRatio:         roundTo(calmar, 3),
		HistoricalVaR95Pct:  roundTo(hVaR95*100, 3),
		HistoricalCVaR95Pct: roundTo(hCVaR95*100, 3),
		HistoricalVaR99Pct:  roundTo(hVaR99*100, 3),
		HistoricalCVaR99Pct: roundTo(hCVaR99*100, 3),
		ParametricVaR95Pct:  roundTo(pVaR95*100, 3),
		ParametricCVaR95Pct: roundTo(pCVaR95*100, 3),
		MonteCarloVaR95Pct:  roundTo(mcVaR95*100, 3),
		MonteCarloCVaR95Pct: roundTo(mcCVaR95*100, 3),
	}, nil
}

type WACCResult struct {
	CostOfEquity float64 `json:"cost_of_equity"`
	AfterTaxDebt float64 `json:"after_tax_cost_of_debt"`
	EquityWeight float64 `json:"equity_weight"`
	DebtWeight   float64 `json:"debt_weight"`
	WACC         float64 `json:"wacc"`
}

// ComputeWACC computes CAPM cost of equity, after-tax cost of debt, and weighted average cost of capital.
func ComputeWACC(
	riskFreeRate, beta, equityRiskPremium, costOfDebt, taxRate, equityValue, debtValue float64,
) (WACCResult, error) {
	if equityValue < 0 || debtValue < 0 || (equityValue+debtValue) <= 0 {
		return WACCResult{}, errors.New("total capital (equity_value + debt_value) must be positive")
	}
	if taxRate < 0 || taxRate > 1 {
		return WACCResult{}, errors.New("tax_rate must be in [0, 1]")
	}
	ke := riskFreeRate + beta*equityRiskPremium
	kdAfterTax := costOfDebt * (1.0 - taxRate)
	totalCap := equityValue + debtValue
	we := equityValue / totalCap
	wd := debtValue / totalCap
	wacc := we*ke + wd*kdAfterTax
	return WACCResult{
		CostOfEquity: roundTo(ke, 4),
		AfterTaxDebt: roundTo(kdAfterTax, 4),
		EquityWeight: roundTo(we, 4),
		DebtWeight:   roundTo(wd, 4),
		WACC:         roundTo(wacc, 4),
	}, nil
}

type PeerMultipleInput struct {
	Symbol    string   `json:"symbol"`
	EVEBITDA  *float64 `json:"ev_ebitda,omitempty"`
	PE        *float64 `json:"pe,omitempty"`
	EVRevenue *float64 `json:"ev_revenue,omitempty"`
}

type MultipleSummary struct {
	Median                float64  `json:"median"`
	P25                   float64  `json:"p25"`
	P75                   float64  `json:"p75"`
	ImpliedSharePriceMid  *float64 `json:"implied_share_price_mid,omitempty"`
	ImpliedSharePriceLow  *float64 `json:"implied_share_price_low,omitempty"`
	ImpliedSharePriceHigh *float64 `json:"implied_share_price_high,omitempty"`
}

type PeerCompsResult struct {
	PeerCount int              `json:"peer_count"`
	EVEBITDA  *MultipleSummary `json:"ev_ebitda,omitempty"`
	PE        *MultipleSummary `json:"pe,omitempty"`
	EVRevenue *MultipleSummary `json:"ev_revenue,omitempty"`
}

func sortedPercentile(sorted []float64, p float64) float64 {
	if len(sorted) == 1 {
		return sorted[0]
	}
	pos := p * float64(len(sorted)-1)
	lo := int(math.Floor(pos))
	hi := int(math.Ceil(pos))
	if lo == hi {
		return sorted[lo]
	}
	frac := pos - float64(lo)
	return sorted[lo]*(1.0-frac) + sorted[hi]*frac
}

func summarizeMultiple(vals []float64, metric, netDebt, shares float64, isEV bool) *MultipleSummary {
	if len(vals) == 0 {
		return nil
	}
	s := append([]float64(nil), vals...)
	sort.Float64s(s)
	p25 := sortedPercentile(s, 0.25)
	med := sortedPercentile(s, 0.50)
	p75 := sortedPercentile(s, 0.75)
	out := &MultipleSummary{
		Median: roundTo(med, 2),
		P25:    roundTo(p25, 2),
		P75:    roundTo(p75, 2),
	}
	if metric > 0 && shares > 0 {
		toPrice := func(mult float64) float64 {
			if isEV {
				return roundTo((mult*metric-netDebt)/shares, 2)
			}
			return roundTo((mult*metric)/shares, 2)
		}
		lo := toPrice(p25)
		mid := toPrice(med)
		hi := toPrice(p75)
		out.ImpliedSharePriceLow = &lo
		out.ImpliedSharePriceMid = &mid
		out.ImpliedSharePriceHigh = &hi
	}
	return out
}

// PeerCompsValuation computes peer multiple percentiles (P25/Median/P75) and implied share price ranges.
func PeerCompsValuation(
	peers []PeerMultipleInput,
	targetEBITDA, targetNetIncome, targetRevenue, netDebt, dilutedShares float64,
) (PeerCompsResult, error) {
	if len(peers) == 0 {
		return PeerCompsResult{}, errors.New("at least one peer is required")
	}
	var evEbitdas, pes, evRevs []float64
	for _, p := range peers {
		if p.EVEBITDA != nil && *p.EVEBITDA > 0 {
			evEbitdas = append(evEbitdas, *p.EVEBITDA)
		}
		if p.PE != nil && *p.PE > 0 {
			pes = append(pes, *p.PE)
		}
		if p.EVRevenue != nil && *p.EVRevenue > 0 {
			evRevs = append(evRevs, *p.EVRevenue)
		}
	}
	return PeerCompsResult{
		PeerCount: len(peers),
		EVEBITDA:  summarizeMultiple(evEbitdas, targetEBITDA, netDebt, dilutedShares, true),
		PE:        summarizeMultiple(pes, targetNetIncome, netDebt, dilutedShares, false),
		EVRevenue: summarizeMultiple(evRevs, targetRevenue, netDebt, dilutedShares, true),
	}, nil
}
