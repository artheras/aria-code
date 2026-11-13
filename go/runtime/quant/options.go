package quant

import (
	"errors"
	"math"
)

// OptionSpec holds contract parameters for option evaluation.
type OptionSpec struct {
	S      float64 `json:"s"`       // Underlying spot price
	K      float64 `json:"k"`       // Strike price
	T      float64 `json:"t"`       // Time to maturity in years
	R      float64 `json:"r"`       // Risk-free interest rate
	Sigma  float64 `json:"sigma"`   // Volatility (annualized)
	IsCall bool    `json:"is_call"` // true for Call, false for Put
	Q      float64 `json:"q"`       // Continuous dividend yield
}

// Greeks contains option price and analytical risk sensitivities.
type Greeks struct {
	Price float64 `json:"price"`
	Delta float64 `json:"delta"`
	Gamma float64 `json:"gamma"`
	Vega  float64 `json:"vega"`
	Theta float64 `json:"theta"`
	Rho   float64 `json:"rho"`
	Vanna float64 `json:"vanna"`
	Volga float64 `json:"volga"`
}

// NormPDF returns standard normal probability density function.
func NormPDF(x float64) float64 {
	return math.Exp(-0.5*x*x) / math.Sqrt(2.0*math.Pi)
}

// NormCDF returns standard normal cumulative distribution function (Abramowitz & Stegun 7.1.26).
func NormCDF(x float64) float64 {
	if x < 0.0 {
		return 1.0 - NormCDF(-x)
	}
	k := 1.0 / (1.0 + 0.2316419*x)
	poly := k * (0.319381530 + k*(-0.356563782+k*(1.781477937+k*(-1.821255978+k*1.330274429))))
	return 1.0 - NormPDF(x)*poly
}

// CalculateGreeks computes Black-Scholes price and first/second order Greeks.
func CalculateGreeks(spec OptionSpec) Greeks {
	s := spec.S
	k := spec.K
	t := math.Max(spec.T, 1e-6)
	r := spec.R
	q := spec.Q
	sigma := math.Max(spec.Sigma, 1e-6)

	sqrtT := math.Sqrt(t)
	d1 := (math.Log(s/k) + (r-q+0.5*sigma*sigma)*t) / (sigma * sqrtT)
	d2 := d1 - sigma*sqrtT

	eqT := math.Exp(-q * t)
	erT := math.Exp(-r * t)
	nd1PDF := NormPDF(d1)

	var price, delta, rho, theta float64
	if spec.IsCall {
		price = s*eqT*NormCDF(d1) - k*erT*NormCDF(d2)
		delta = eqT * NormCDF(d1)
		rho = k * t * erT * NormCDF(d2)
		theta = -(s*sigma*eqT*nd1PDF)/(2.0*sqrtT) + q*s*eqT*NormCDF(d1) - r*k*erT*NormCDF(d2)
	} else {
		price = k*erT*NormCDF(-d2) - s*eqT*NormCDF(-d1)
		delta = -eqT * NormCDF(-d1)
		rho = -k * t * erT * NormCDF(-d2)
		theta = -(s*sigma*eqT*nd1PDF)/(2.0*sqrtT) - q*s*eqT*NormCDF(-d1) + r*k*erT*NormCDF(-d2)
	}

	gamma := (eqT * nd1PDF) / (s * sigma * sqrtT)
	vega := s * eqT * sqrtT * nd1PDF
	vanna := -eqT * nd1PDF * (d2 / sigma)
	volga := vega * (d1 * d2 / sigma)

	return Greeks{
		Price: price,
		Delta: delta,
		Gamma: gamma,
		Vega:  vega,
		Theta: theta,
		Rho:   rho,
		Vanna: vanna,
		Volga: volga,
	}
}

// ImpliedVolatility solves for the annualized volatility matching marketPrice.
func ImpliedVolatility(marketPrice float64, spec OptionSpec) (float64, error) {
	if marketPrice <= 0.0 {
		return 0, errors.New("market price must be strictly positive")
	}
	s := spec.S
	k := spec.K
	t := spec.T
	r := spec.R
	q := spec.Q

	intrinsic := 0.0
	if spec.IsCall {
		intrinsic = math.Max(0.0, s*math.Exp(-q*t)-k*math.Exp(-r*t))
	} else {
		intrinsic = math.Max(0.0, k*math.Exp(-r*t)-s*math.Exp(-q*t))
	}
	if marketPrice < intrinsic {
		return 0, errors.New("market price violates lower arbitrage bound")
	}

	sigma := 0.30
	for iter := 0; iter < 40; iter++ {
		curSpec := spec
		curSpec.Sigma = sigma
		greeks := CalculateGreeks(curSpec)
		diff := greeks.Price - marketPrice
		if math.Abs(diff) < 1e-6 {
			return sigma, nil
		}
		if math.Abs(greeks.Vega) < 1e-8 {
			break
		}
		nextSigma := sigma - diff/greeks.Vega
		if nextSigma <= 0.001 || nextSigma > 10.0 {
			break
		}
		sigma = nextSigma
	}

	// Bisection fallback
	low, high := 0.001, 8.0
	for iter := 0; iter < 60; iter++ {
		mid := 0.5 * (low + high)
		curSpec := spec
		curSpec.Sigma = mid
		p := CalculateGreeks(curSpec).Price
		diff := p - marketPrice
		if math.Abs(diff) < 1e-6 {
			return mid, nil
		}
		if diff > 0 {
			high = mid
		} else {
			low = mid
		}
	}
	return 0.5 * (low + high), nil
}

// PriceAmericanCRR calculates American option price via Cox-Ross-Rubinstein lattice.
func PriceAmericanCRR(spec OptionSpec, steps int) float64 {
	if steps < 50 {
		steps = 50
	} else if steps > 2000 {
		steps = 2000
	}
	s := spec.S
	k := spec.K
	t := math.Max(spec.T, 1e-6)
	r := spec.R
	q := spec.Q
	sigma := math.Max(spec.Sigma, 1e-6)

	dt := t / float64(steps)
	u := math.Exp(sigma * math.Sqrt(dt))
	d := 1.0 / u
	df := math.Exp(-r * dt)
	p := (math.Exp((r-q)*dt) - d) / (u - d)
	oneMinusP := 1.0 - p

	values := make([]float64, steps+1)
	for j := 0; j <= steps; j++ {
		st := s * math.Pow(u, float64(2*j-steps))
		if spec.IsCall {
			values[j] = math.Max(0.0, st-k)
		} else {
			values[j] = math.Max(0.0, k-st)
		}
	}

	for i := steps - 1; i >= 0; i-- {
		for j := 0; j <= i; j++ {
			st := s * math.Pow(u, float64(2*j-i))
			hold := df * (p*values[j+1] + oneMinusP*values[j])
			exercise := 0.0
			if spec.IsCall {
				exercise = math.Max(0.0, st-k)
			} else {
				exercise = math.Max(0.0, k-st)
			}
			values[j] = math.Max(hold, exercise)
		}
	}
	return values[0]
}
