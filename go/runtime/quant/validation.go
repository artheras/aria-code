package quant

import (
	"fmt"
	"math"
)

const eulerGamma = 0.5772156649015329

func normCDF(x float64) float64 {
	return 0.5 * (1.0 + math.Erf(x/math.Sqrt2))
}

func normPPF(p float64) float64 {
	if p <= 0.0 || p >= 1.0 {
		panic("p must be in (0, 1)")
	}
	a := []float64{
		-3.969683028665376e+01, 2.209460984245205e+02,
		-2.759285104469687e+02, 1.383577518672690e+02,
		-3.066479806614716e+01, 2.506628277459239e+00,
	}
	b := []float64{
		-5.447609879822406e+01, 1.615858368580409e+02,
		-1.556989798598866e+02, 6.680131188771972e+01,
		-1.328068155288572e+01,
	}
	c := []float64{
		-7.784894002430293e-03, -3.223964580411365e-01,
		-2.400758277161838e+00, -2.549732539343734e+00,
		4.374664141464968e+00, 2.938163982698783e+00,
	}
	d := []float64{
		7.784695709041462e-03, 3.224671290700398e-01,
		2.445134137142996e+00, 3.754408661907416e+00,
	}

	pLow := 0.02425
	pHigh := 1.0 - pLow
	if p < pLow {
		q := math.Sqrt(-2.0 * math.Log(p))
		return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q + c[5]) /
			((((d[0]*q+d[1])*q+d[2])*q+d[3])*q + 1.0)
	}
	if p <= pHigh {
		q := p - 0.5
		r := q * q
		return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r + a[5]) * q /
			(((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r + 1.0)
	}
	q := math.Sqrt(-2.0 * math.Log(1.0-p))
	return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q + c[5]) /
		((((d[0]*q+d[1])*q+d[2])*q+d[3])*q + 1.0)
}

// ExpectedMaxSharpe returns the expected maximum Sharpe under nTrials random trials.
func ExpectedMaxSharpe(nTrials int, sharpeStd float64) float64 {
	if nTrials <= 1 {
		return 0.0
	}
	n := float64(nTrials)
	z1 := normPPF(1.0 - 1.0/n)
	z2 := normPPF(1.0 - 1.0/(n*math.E))
	return sharpeStd * ((1.0-eulerGamma)*z1 + eulerGamma*z2)
}

// DeflatedSharpeRatio computes the multiple-testing adjusted Sharpe probability in [0, 1].
func DeflatedSharpeRatio(sharpe float64, nObs, nTrials int, skew, kurtosis, sharpeStd float64, periodsPerYear int) float64 {
	if nObs <= 1 {
		return 0.0
	}
	if periodsPerYear < 1 {
		periodsPerYear = 252
	}
	if nTrials < 1 {
		nTrials = 1
	}
	scale := math.Sqrt(float64(periodsPerYear))
	srPeriod := sharpe / scale
	sr0Period := ExpectedMaxSharpe(nTrials, sharpeStd) / scale
	variance := (1.0 - skew*srPeriod + ((kurtosis-1.0)/4.0)*srPeriod*srPeriod) / float64(nObs-1)
	if variance < 1e-9 {
		variance = 1e-9
	}
	z := (srPeriod - sr0Period) / math.Sqrt(variance)
	return math.Round(normCDF(z)*1_000_000.0) / 1_000_000.0
}

// MinimumBacktestLengthYears returns the minimum years needed to avoid false discovery at 95% confidence.
func MinimumBacktestLengthYears(targetSharpe float64, nTrials, periodsPerYear int) float64 {
	if targetSharpe <= 0 {
		return math.Inf(1)
	}
	if periodsPerYear < 1 {
		periodsPerYear = 252
	}
	if nTrials < 1 {
		nTrials = 1
	}
	sr0 := ExpectedMaxSharpe(nTrials, 0.5)
	if targetSharpe <= sr0 {
		return math.Inf(1)
	}
	scale := math.Sqrt(float64(periodsPerYear))
	srDaily := targetSharpe / scale
	sr0Daily := sr0 / scale
	zAlpha := normPPF(0.95)
	diff := srDaily - sr0Daily
	if diff < 1e-9 {
		diff = 1e-9
	}
	minObs := 1.0 + math.Pow(zAlpha/diff, 2)
	return math.Round((minObs/float64(periodsPerYear))*100.0) / 100.0
}

// ResearchValidationReport summarizes the independent backtest audit.
type ResearchValidationReport struct {
	Passed       bool     `json:"passed"`
	Verdict      string   `json:"verdict"`
	DSR          float64  `json:"dsr"`
	MinBTLYrs    float64  `json:"min_btl_years"`
	OOSRetention float64  `json:"oos_retention"`
	Reasons      []string `json:"reasons"`
}

// ValidateResearchBacktest independently audits a backtest for PIT, sample length, OOS retention, and DSR.
func ValidateResearchBacktest(
	isSharpe, oosSharpe float64,
	nObs, nTrials int,
	pitPassed bool,
	dsrThreshold, minOOSRetention float64,
) ResearchValidationReport {
	var reasons []string
	if !pitPassed {
		reasons = append(reasons, "Data failed Point-in-Time / quality gate.")
	}
	if nObs < 60 {
		reasons = append(reasons, fmt.Sprintf("Insufficient sample length (%d < 60 observations).", nObs))
	}
	if oosSharpe <= 0 {
		reasons = append(reasons, fmt.Sprintf("Out-of-sample Sharpe must be positive (got %.3f).", oosSharpe))
	}
	oosRetention := 0.0
	if isSharpe > 0 {
		oosRetention = math.Round((oosSharpe/isSharpe)*10_000.0) / 10_000.0
		if oosRetention < minOOSRetention {
			reasons = append(reasons, fmt.Sprintf("Severe IS->OOS degradation (retention %.2f < %.2f).", oosRetention, minOOSRetention))
		}
	}
	dsr := DeflatedSharpeRatio(oosSharpe, nObs, nTrials, 0.0, 3.0, 0.5, 252)
	if dsr < dsrThreshold {
		reasons = append(reasons, fmt.Sprintf("Deflated Sharpe Ratio %.3f below threshold %.2f (n_trials=%d).", dsr, dsrThreshold, nTrials))
	}
	verdict := "approved"
	if len(reasons) > 0 {
		verdict = "rejected"
	}
	return ResearchValidationReport{
		Passed:       len(reasons) == 0,
		Verdict:      verdict,
		DSR:          dsr,
		MinBTLYrs:    MinimumBacktestLengthYears(oosSharpe, nTrials, 252),
		OOSRetention: oosRetention,
		Reasons:      reasons,
	}
}
