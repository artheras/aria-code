//go:build cquant

package quant

/*
#cgo CXXFLAGS: -std=c++20 -O3
#cgo darwin LDFLAGS: -lc++
#cgo linux LDFLAGS: -lstdc++
#include "cpp/quant_kernel.h"
*/
import "C"
import (
	"errors"
)

// NativeMonteCarloVaR runs the C++20 antithetic Monte Carlo VaR/CVaR kernel.
func NativeMonteCarloVaR(meanReturn, stdReturn float64, paths int, seed uint64) (var95, cvar95, var99, cvar99 float64, err error) {
	if paths < 200 {
		return 0, 0, 0, 0, errors.New("paths must be >= 200")
	}
	var res C.aq_mc_var_result
	rc := C.aq_monte_carlo_var_cvar(C.double(meanReturn), C.double(stdReturn), C.size_t(paths), C.uint64_t(seed), &res)
	if rc != 0 {
		return 0, 0, 0, 0, errors.New("native C++ Monte Carlo VaR kernel rejected inputs")
	}
	return float64(res.var_95_pct), float64(res.cvar_95_pct), float64(res.var_99_pct), float64(res.cvar_99_pct), nil
}
