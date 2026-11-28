#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  double var_95_pct;
  double cvar_95_pct;
  double var_99_pct;
  double cvar_99_pct;
} aq_mc_var_result;

/**
 * Antithetic Monte Carlo VaR/CVaR over `paths` normal draws given periodic
 * return mean and standard deviation. Returns 0 on success, non-zero on invalid input.
 */
int aq_monte_carlo_var_cvar(
    double mean_return,
    double std_return,
    size_t paths,
    uint64_t seed,
    aq_mc_var_result* out_result);

/**
 * Antithetic Monte Carlo European option pricer.
 * Returns 0 on success, non-zero on invalid input.
 */
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
    double* out_stderr);

#ifdef __cplusplus
}
#endif
