pub mod backtest;
pub mod binomial_tree;
pub mod black_scholes;
pub mod implied_vol;
pub mod monte_carlo;
pub mod pit;
pub mod portfolio;
pub mod risk_policy;
pub mod validation;

pub use backtest::{
    run_backtest, BacktestConfig, BacktestRequest, BacktestResult, BarInput, TradeRecord,
};
pub use binomial_tree::price_american_crr;
pub use black_scholes::{calculate_greeks, norm_cdf, norm_pdf, Greeks, OptionSpec};
pub use implied_vol::implied_volatility;
pub use monte_carlo::{simulate_gbm_parallel, MonteCarloConfig, MonteCarloResult};
pub use pit::{validate_point_in_time, PitCheckRequest, PitCheckResult};
pub use portfolio::{
    optimize_min_variance, optimize_risk_parity, portfolio_return, portfolio_volatility,
    PortfolioAllocation, PortfolioInput,
};
pub use risk_policy::{check_order_compliance, resolve_risk_policy, RiskCheckOutcome, RiskPolicy};
pub use validation::{
    deflated_sharpe_ratio, expected_max_sharpe, minimum_backtest_length_years,
    validate_research_backtest, ResearchValidationReport, ResearchValidationRequest,
};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_black_scholes_pricing_and_greeks() {
        let spec = OptionSpec {
            s: 100.0,
            k: 100.0,
            t: 1.0,
            r: 0.05,
            sigma: 0.20,
            is_call: true,
            q: 0.0,
        };
        let g = calculate_greeks(&spec);
        assert!((g.price - 10.4506).abs() < 0.01, "price was {}", g.price);
        assert!(g.delta > 0.5 && g.delta < 0.7);
        assert!(g.gamma > 0.0 && g.vega > 0.0);

        // Put-Call Parity: C - P = S - K * exp(-rT)
        let put_spec = OptionSpec {
            is_call: false,
            ..spec
        };
        let put_g = calculate_greeks(&put_spec);
        let lhs = g.price - put_g.price;
        let rhs = spec.s - spec.k * (-spec.r * spec.t).exp();
        assert!((lhs - rhs).abs() < 1e-4);
    }

    #[test]
    fn test_implied_volatility_recovery() {
        let spec = OptionSpec {
            s: 150.0,
            k: 155.0,
            t: 0.5,
            r: 0.04,
            sigma: 0.28,
            is_call: true,
            q: 0.01,
        };
        let g = calculate_greeks(&spec);
        let iv = implied_volatility(
            g.price,
            spec.s,
            spec.k,
            spec.t,
            spec.r,
            spec.q,
            spec.is_call,
        )
        .unwrap();
        assert!((iv - 0.28).abs() < 0.001, "recovered iv was {}", iv);
    }

    #[test]
    fn test_american_crr_early_exercise() {
        let spec = OptionSpec {
            s: 100.0,
            k: 110.0,
            t: 1.0,
            r: 0.08,
            sigma: 0.30,
            is_call: false,
            q: 0.0,
        };
        let euro = calculate_greeks(&spec).price;
        let amer = price_american_crr(&spec, 200);
        assert!(amer >= euro - 1e-6);
    }

    #[test]
    fn test_parallel_monte_carlo() {
        let cfg = MonteCarloConfig {
            s0: 100.0,
            r: 0.05,
            q: 0.0,
            sigma: 0.20,
            t: 1.0,
            steps: 50,
            num_paths: 20000,
            seed: Some(42),
        };
        let res = simulate_gbm_parallel(&cfg);
        assert!(res.mean_terminal > 100.0 && res.mean_terminal < 110.0);
        assert!(res.var_95 > 0.0);
        assert!(res.cvar_95 >= res.var_95);
    }

    #[test]
    fn test_portfolio_optimization() {
        let cov = vec![
            vec![0.04, 0.015, 0.001],
            vec![0.015, 0.035, 0.002],
            vec![0.001, 0.002, 0.01],
        ];
        let rp = optimize_risk_parity(&cov);
        let sum_rp: f64 = rp.iter().sum();
        assert!((sum_rp - 1.0).abs() < 1e-4);

        let mv = optimize_min_variance(&cov);
        let sum_mv: f64 = mv.iter().sum();
        assert!((sum_mv - 1.0).abs() < 1e-4);

        let vol_mv = portfolio_volatility(&mv, &cov);
        let vol_rp = portfolio_volatility(&rp, &cov);
        assert!(vol_mv <= vol_rp + 1e-6);
    }

    #[test]
    fn test_backtest_engine() {
        let bars = vec![
            BarInput {
                date: "2026-01-01".into(),
                open: 100.0,
                high: 102.0,
                low: 99.0,
                close: 100.0,
                volume: 1000.0,
                signal: 1.0,
            },
            BarInput {
                date: "2026-01-02".into(),
                open: 100.0,
                high: 112.0,
                low: 100.0,
                close: 110.0,
                volume: 1200.0,
                signal: 1.0,
            },
            BarInput {
                date: "2026-01-03".into(),
                open: 110.0,
                high: 110.0,
                low: 104.0,
                close: 105.0,
                volume: 900.0,
                signal: 0.0,
            },
        ];
        let cfg = BacktestConfig {
            initial_capital: 100_000.0,
            commission_bps: 5.0,
            slippage_bps: 5.0,
            allow_short: false,
            risk_free_rate: 0.02,
            periods_per_year: 252.0,
        };
        let res = run_backtest(&bars, &cfg);
        assert_eq!(res.equity_curve.len(), 3);
        assert!(res.total_trades >= 2);
        assert!(res.total_return_pct > 0.0);
        assert!(res.max_drawdown_pct > 0.0);
        assert!(res.win_rate_pct > 0.0);
    }

    #[test]
    fn test_risk_policy_strictest_wins_and_compliance() {
        let account = RiskPolicy {
            name: "account".into(),
            max_position_weight: 0.40,
            max_leverage: 1.5,
            max_drawdown_pct: 0.25,
            approval_required_above_weight: 0.20,
            min_cash_reserve_pct: 0.05,
            allow_short: true,
        };
        let execution = RiskPolicy {
            name: "execution".into(),
            max_position_weight: 0.15,
            max_leverage: 1.0,
            max_drawdown_pct: 0.15,
            approval_required_above_weight: 0.10,
            min_cash_reserve_pct: 0.10,
            allow_short: false,
        };
        let eff = resolve_risk_policy(&[account, execution], "effective");
        assert!((eff.max_position_weight - 0.15).abs() < 1e-9);
        assert!((eff.min_cash_reserve_pct - 0.10).abs() < 1e-9);
        assert!(!eff.allow_short);

        let blocked = check_order_compliance(&eff, 0.20, 1.2, 0.16, 0.05, true);
        assert!(!blocked.allowed);
        assert_eq!(blocked.violations.len(), 5);
    }

    #[test]
    fn test_validation_dsr_and_gate() {
        let dsr_1 = deflated_sharpe_ratio(1.8, 500, 1, 0.0, 3.0, 0.5, 252);
        let dsr_100 = deflated_sharpe_ratio(1.8, 500, 100, 0.0, 3.0, 0.5, 252);
        assert!(dsr_1 > 0.99);
        assert!(dsr_100 < dsr_1);

        let btl_1 = minimum_backtest_length_years(1.5, 1, 252);
        let btl_50 = minimum_backtest_length_years(1.5, 50, 252);
        assert!(btl_50 > btl_1 && btl_1 > 0.0);

        let report = validate_research_backtest(&ResearchValidationRequest {
            is_sharpe: 2.0,
            oos_sharpe: 1.6,
            n_obs: 750,
            n_trials: 5,
            max_drawdown_pct: 0.10,
            skew: 0.0,
            kurtosis: 3.0,
            pit_passed: true,
            risk_policy: Some(RiskPolicy::default()),
            target_weight: 0.10,
            dsr_threshold: 0.95,
            min_oos_retention: 0.40,
        });
        assert!(report.passed);
        assert_eq!(report.verdict, "approved");
    }

    #[test]
    fn test_pit_gate_blocks_lookahead_and_bad_price() {
        let ok = validate_point_in_time(&PitCheckRequest {
            kind: "quote".into(),
            success: true,
            price: Some(210.5),
            available_at: Some("2026-11-10T16:00:00Z".into()),
            as_of: Some("2026-11-11T00:00:00Z".into()),
            stale: false,
            missing_fields: vec![],
        });
        assert!(ok.passed);
        assert_eq!(ok.quality_status, "verified");

        let leaked = validate_point_in_time(&PitCheckRequest {
            kind: "quote".into(),
            success: true,
            price: Some(0.0),
            available_at: Some("2026-11-10T16:00:00Z".into()),
            as_of: Some("2026-11-09T00:00:00Z".into()),
            stale: false,
            missing_fields: vec![],
        });
        assert!(!leaked.passed);
        assert_eq!(leaked.quality_status, "rejected");
        assert_eq!(leaked.reasons.len(), 2);
    }
}
