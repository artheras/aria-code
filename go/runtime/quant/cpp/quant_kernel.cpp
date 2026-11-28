#include "quant_kernel.h"

#include <algorithm>
#include <cmath>
#include <random>
#include <vector>

namespace {

void tail_var_cvar(const std::vector<double>& sorted_rets, double alpha, double* out_var, double* out_cvar) {
  const size_t n = sorted_rets.size();
  size_t k = static_cast<size_t>(std::floor(alpha * static_cast<double>(n)));
  if (k < 1) {
    k = 1;
  }
  if (k > n) {
    k = n;
  }
  const double var = std::max(0.0, -sorted_rets[k - 1]);
  double sum = 0.0;
  for (size_t i = 0; i < k; ++i) {
    sum += sorted_rets[i];
  }
  const double cvar = std::max(0.0, -(sum / static_cast<double>(k)));
  *out_var = var * 100.0;
  *out_cvar = cvar * 100.0;
}

}  // namespace

extern "C" {

int aq_monte_carlo_var_cvar(
    double mean_return,
    double std_return,
    size_t paths,
    uint64_t seed,
    aq_mc_var_result* out_result) {
  if (!out_result || std_return < 0.0 || paths < 200 || !std::isfinite(mean_return) || !std::isfinite(std_return)) {
    return 1;
  }

  const size_t half = paths / 2;
  std::vector<double> sim;
  sim.reserve(half * 2);

  std::mt19937_64 rng(seed == 0 ? 42ULL : seed);
  std::normal_distribution<double> norm(0.0, 1.0);

  for (size_t i = 0; i < half; ++i) {
    const double z = norm(rng);
    sim.push_back(mean_return + std_return * z);
    sim.push_back(mean_return - std_return * z);
  }

  std::sort(sim.begin(), sim.end());
  tail_var_cvar(sim, 0.05, &out_result->var_95_pct, &out_result->cvar_95_pct);
  tail_var_cvar(sim, 0.01, &out_result->var_99_pct, &out_result->cvar_99_pct);
  return 0;
}

int aq_monte_carlo_european_option(
    double s,
    double k,
    double t,
    double r,
    double q,
    double sigma,
    int is_call,
    size_t paths,
    uint64_t seed,
    double* out_price,
    double* out_stderr) {
  if (!out_price || !out_stderr || s <= 0.0 || k <= 0.0 || t <= 0.0 || sigma <= 0.0 || paths < 200) {
    return 1;
  }

  const size_t half = paths / 2;
  const double drift = (r - q - 0.5 * sigma * sigma) * t;
  const double vol_sqrt_t = sigma * std::sqrt(t);
  const double disc = std::exp(-r * t);

  std::mt19937_64 rng(seed == 0 ? 42ULL : seed);
  std::normal_distribution<double> norm(0.0, 1.0);

  double sum_payoff = 0.0;
  double sum_sq = 0.0;
  const double count = static_cast<double>(half * 2);

  for (size_t i = 0; i < half; ++i) {
    const double z = norm(rng);
    const double st_pos = s * std::exp(drift + vol_sqrt_t * z);
    const double st_neg = s * std::exp(drift - vol_sqrt_t * z);

    const double p1 = is_call ? std::max(0.0, st_pos - k) : std::max(0.0, k - st_pos);
    const double p2 = is_call ? std::max(0.0, st_neg - k) : std::max(0.0, k - st_neg);
    const double d1 = disc * p1;
    const double d2 = disc * p2;

    sum_payoff += d1 + d2;
    sum_sq += d1 * d1 + d2 * d2;
  }

  const double mean = sum_payoff / count;
  const double var = std::max(0.0, (sum_sq / count) - mean * mean);
  *out_price = mean;
  *out_stderr = std::sqrt(var / count);
  return 0;
}

}  // extern "C"
