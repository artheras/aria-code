package quant

import (
	"errors"
	"math"
	"sort"
)

// PortfolioVolatility calculates annualized standard deviation w^T * Sigma * w.
func PortfolioVolatility(weights []float64, cov [][]float64) float64 {
	n := len(weights)
	var variance float64
	for i := 0; i < n; i++ {
		for j := 0; j < n; j++ {
			variance += weights[i] * weights[j] * cov[i][j]
		}
	}
	return math.Sqrt(math.Max(variance, 1e-12))
}

// PortfolioReturn calculates expected portfolio return w^T * R.
func PortfolioReturn(weights []float64, returns []float64) float64 {
	var total float64
	for i := range weights {
		total += weights[i] * returns[i]
	}
	return total
}

// projectSimplex projects vector v onto probability simplex: {w >= 0, sum(w) = 1}.
func projectSimplex(v []float64) []float64 {
	n := len(v)
	u := make([]float64, n)
	copy(u, v)
	sort.Slice(u, func(i, j int) bool { return u[i] > u[j] })

	cssv := 0.0
	rho := 0
	for i := 0; i < n; i++ {
		cssv += u[i]
		cond := u[i] - (cssv-1.0)/float64(i+1)
		if cond > 0 {
			rho = i
		}
	}
	sumRho := 0.0
	for i := 0; i <= rho; i++ {
		sumRho += u[i]
	}
	theta := (sumRho - 1.0) / float64(rho+1)
	w := make([]float64, n)
	for i := 0; i < n; i++ {
		w[i] = math.Max(0.0, v[i]-theta)
	}
	return w
}

// OptimizeRiskParity computes equal risk contribution allocation weights.
func OptimizeRiskParity(cov [][]float64) ([]float64, error) {
	n := len(cov)
	if n == 0 {
		return nil, errors.New("empty covariance matrix")
	}
	w := make([]float64, n)
	for i := range w {
		w[i] = 1.0 / float64(n)
	}
	targetRC := 1.0 / float64(n)
	lr := 0.05

	for iter := 0; iter < 500; iter++ {
		vol := PortfolioVolatility(w, cov)
		sigmaW := make([]float64, n)
		for i := 0; i < n; i++ {
			for j := 0; j < n; j++ {
				sigmaW[i] += cov[i][j] * w[j]
			}
		}

		diffSum := 0.0
		for i := 0; i < n; i++ {
			rc := (w[i] * sigmaW[i]) / (vol * vol)
			diff := rc - targetRC
			diffSum += math.Abs(diff)
			w[i] -= lr * diff * w[i]
			if w[i] < 1e-4 {
				w[i] = 1e-4
			}
		}

		sumW := 0.0
		for _, x := range w {
			sumW += x
		}
		for i := range w {
			w[i] /= sumW
		}

		if diffSum < 1e-5 {
			break
		}
	}
	return w, nil
}

// OptimizeMinVariance computes global minimum variance portfolio weights via projected gradient descent.
func OptimizeMinVariance(cov [][]float64) ([]float64, error) {
	n := len(cov)
	if n == 0 {
		return nil, errors.New("empty covariance matrix")
	}
	w := make([]float64, n)
	for i := range w {
		w[i] = 1.0 / float64(n)
	}
	// Lipschitz constant estimation: max diagonal * 2
	maxDiag := 0.0
	for i := 0; i < n; i++ {
		if cov[i][i] > maxDiag {
			maxDiag = cov[i][i]
		}
	}
	stepSize := 1.0 / (2.0 * math.Max(maxDiag, 0.01) * float64(n))

	for iter := 0; iter < 2000; iter++ {
		grad := make([]float64, n)
		for i := 0; i < n; i++ {
			for j := 0; j < n; j++ {
				grad[i] += 2.0 * cov[i][j] * w[j]
			}
		}
		candidate := make([]float64, n)
		for i := 0; i < n; i++ {
			candidate[i] = w[i] - stepSize*grad[i]
		}
		newW := projectSimplex(candidate)
		diff := 0.0
		for i := 0; i < n; i++ {
			diff += math.Abs(newW[i] - w[i])
		}
		w = newW
		if diff < 1e-6 {
			break
		}
	}
	return w, nil
}
