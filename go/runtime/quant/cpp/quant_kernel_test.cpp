#include "quant_kernel.h"

#include <cassert>
#include <cmath>
#include <cstdio>

int main() {
  aq_mc_var_result var_res{};
  int rc = aq_monte_carlo_var_cvar(0.0005, 0.015, 100000, 42, &var_res);
  assert(rc == 0);
  assert(var_res.var_95_pct > 2.0 && var_res.var_95_pct < 3.0);
  assert(var_res.cvar_95_pct >= var_res.var_95_pct);
  assert(var_res.cvar_99_pct >= var_res.var_99_pct);

  double call_px = 0.0;
  double call_se = 0.0;
  rc = aq_monte_carlo_european_option(100.0, 100.0, 1.0, 0.05, 0.0, 0.20, 1, 200000, 42, &call_px, &call_se);
  assert(rc == 0);
  // Black-Scholes ATM Call (S=100, K=100, T=1, r=5%, sigma=20%) ~= 10.4506
  assert(std::fabs(call_px - 10.4506) < 0.05);
  assert(call_se > 0.0 && call_se < 0.05);

  // Invalid inputs fail cleanly
  assert(aq_monte_carlo_var_cvar(0.0, -0.1, 1000, 1, &var_res) != 0);
  assert(aq_monte_carlo_european_option(-1.0, 100.0, 1.0, 0.05, 0.0, 0.2, 1, 1000, 1, &call_px, &call_se) != 0);

  std::printf("quant_kernel_test OK: VaR95=%.4f%% CVaR95=%.4f%% Call=%.4f (se=%.4f)\n",
              var_res.var_95_pct, var_res.cvar_95_pct, call_px, call_se);
  return 0;
}
