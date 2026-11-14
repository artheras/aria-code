use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BarInput {
    pub date: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    #[serde(default)]
    pub volume: f64,
    /// Target position weight in [-1.0, 1.0], e.g. 1.0 = 100% long, 0.0 = flat, -1.0 = 100% short
    pub signal: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BacktestConfig {
    #[serde(default = "default_initial_capital")]
    pub initial_capital: f64,
    #[serde(default = "default_bps")]
    pub commission_bps: f64,
    #[serde(default = "default_bps")]
    pub slippage_bps: f64,
    #[serde(default)]
    pub allow_short: bool,
    #[serde(default = "default_rf")]
    pub risk_free_rate: f64,
    #[serde(default = "default_periods")]
    pub periods_per_year: f64,
}

fn default_initial_capital() -> f64 {
    100_000.0
}
fn default_bps() -> f64 {
    5.0
}
fn default_rf() -> f64 {
    0.02
}
fn default_periods() -> f64 {
    252.0
}

impl Default for BacktestConfig {
    fn default() -> Self {
        Self {
            initial_capital: default_initial_capital(),
            commission_bps: default_bps(),
            slippage_bps: default_bps(),
            allow_short: false,
            risk_free_rate: default_rf(),
            periods_per_year: default_periods(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BacktestRequest {
    pub bars: Vec<BarInput>,
    #[serde(default)]
    pub config: BacktestConfig,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeRecord {
    pub date: String,
    pub side: String,
    pub shares: f64,
    pub exec_price: f64,
    pub commission: f64,
    pub slippage_cost: f64,
    pub realized_pnl: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BacktestResult {
    pub initial_capital: f64,
    pub final_equity: f64,
    pub total_return_pct: f64,
    pub annualized_return_pct: f64,
    pub annualized_volatility_pct: f64,
    pub sharpe_ratio: f64,
    pub sortino_ratio: f64,
    pub max_drawdown_pct: f64,
    pub calmar_ratio: f64,
    pub win_rate_pct: f64,
    pub profit_factor: f64,
    pub total_trades: usize,
    pub total_commission: f64,
    pub equity_curve: Vec<f64>,
    pub trades: Vec<TradeRecord>,
}

pub fn run_backtest(bars: &[BarInput], cfg: &BacktestConfig) -> BacktestResult {
    let initial_capital = cfg.initial_capital.max(1.0);
    if bars.is_empty() {
        return BacktestResult {
            initial_capital,
            final_equity: initial_capital,
            total_return_pct: 0.0,
            annualized_return_pct: 0.0,
            annualized_volatility_pct: 0.0,
            sharpe_ratio: 0.0,
            sortino_ratio: 0.0,
            max_drawdown_pct: 0.0,
            calmar_ratio: 0.0,
            win_rate_pct: 0.0,
            profit_factor: 0.0,
            total_trades: 0,
            total_commission: 0.0,
            equity_curve: vec![],
            trades: vec![],
        };
    }

    let comm_rate = cfg.commission_bps.max(0.0) / 10_000.0;
    let slip_rate = cfg.slippage_bps.max(0.0) / 10_000.0;

    let mut cash = initial_capital;
    let mut shares = 0.0_f64;
    let mut avg_cost = 0.0_f64;
    let mut total_commission = 0.0_f64;
    let mut equity_curve = Vec::with_capacity(bars.len());
    let mut trades = Vec::new();
    let mut closed_pnls = Vec::new();

    for bar in bars {
        let price = bar.close.max(1e-8);
        let mark_equity = cash + shares * price;
        let min_weight = if cfg.allow_short { -1.0 } else { 0.0 };
        let target_weight = bar.signal.clamp(min_weight, 1.0);
        let target_notional = mark_equity * target_weight;
        let target_shares = target_notional / price;
        let delta_shares = target_shares - shares;

        if delta_shares.abs() * price > 1.0 {
            let is_buy = delta_shares > 0.0;
            let exec_price = if is_buy {
                price * (1.0 + slip_rate)
            } else {
                price * (1.0 - slip_rate)
            };
            let trade_notional = delta_shares.abs() * exec_price;
            let commission = trade_notional * comm_rate;
            let slippage_cost = delta_shares.abs() * (exec_price - price).abs();

            let mut realized_pnl = 0.0;
            // Closing or reducing existing position
            if shares != 0.0 && ((shares > 0.0 && !is_buy) || (shares < 0.0 && is_buy)) {
                let closed_qty = delta_shares.abs().min(shares.abs());
                let sign = if shares > 0.0 { 1.0 } else { -1.0 };
                realized_pnl = sign * (exec_price - avg_cost) * closed_qty - commission;
                closed_pnls.push(realized_pnl);
            }

            // Update average cost basis
            let new_shares = shares + delta_shares;
            if new_shares.abs() < 1e-8 {
                avg_cost = 0.0;
            } else if shares == 0.0 || (shares > 0.0 && is_buy) || (shares < 0.0 && !is_buy) {
                let total_cost = shares.abs() * avg_cost + delta_shares.abs() * exec_price;
                avg_cost = total_cost / new_shares.abs();
            } else if shares.signum() != new_shares.signum() {
                // Flipped side
                avg_cost = exec_price;
            }

            cash -= delta_shares * exec_price + commission;
            shares = new_shares;
            total_commission += commission;

            trades.push(TradeRecord {
                date: bar.date.clone(),
                side: if is_buy {
                    "BUY".to_string()
                } else {
                    "SELL".to_string()
                },
                shares: delta_shares.abs(),
                exec_price,
                commission,
                slippage_cost,
                realized_pnl,
            });
        }

        equity_curve.push(cash + shares * price);
    }

    let final_equity = *equity_curve.last().unwrap_or(&initial_capital);
    let total_return = (final_equity - initial_capital) / initial_capital;

    let n = equity_curve.len() as f64;
    let periods = cfg.periods_per_year.max(1.0);
    let years = n / periods;
    let annualized_return = if years > 0.0 && final_equity > 0.0 {
        (final_equity / initial_capital).powf(1.0 / years) - 1.0
    } else {
        total_return
    };

    // Period returns & drawdown
    let mut period_returns = Vec::with_capacity(equity_curve.len());
    let mut prev = initial_capital;
    let mut peak = initial_capital;
    let mut max_dd = 0.0_f64;

    for &eq in &equity_curve {
        if prev > 0.0 {
            period_returns.push((eq - prev) / prev);
        }
        prev = eq;
        if eq > peak {
            peak = eq;
        }
        if peak > 0.0 {
            let dd = (peak - eq) / peak;
            if dd > max_dd {
                max_dd = dd;
            }
        }
    }

    let mean_ret = if !period_returns.is_empty() {
        period_returns.iter().sum::<f64>() / (period_returns.len() as f64)
    } else {
        0.0
    };
    let rf_per_period = cfg.risk_free_rate / periods;

    let variance = if period_returns.len() > 1 {
        period_returns
            .iter()
            .map(|r| (r - mean_ret).powi(2))
            .sum::<f64>()
            / ((period_returns.len() - 1) as f64)
    } else {
        0.0
    };
    let std_ret = variance.sqrt();
    let ann_vol = std_ret * periods.sqrt();

    let sharpe_ratio = if std_ret > 1e-12 {
        ((mean_ret - rf_per_period) / std_ret) * periods.sqrt()
    } else {
        0.0
    };

    let downside_sq_sum: f64 = period_returns
        .iter()
        .filter(|&&r| r < rf_per_period)
        .map(|&r| (r - rf_per_period).powi(2))
        .sum();
    let downside_dev = if !period_returns.is_empty() {
        (downside_sq_sum / (period_returns.len() as f64)).sqrt()
    } else {
        0.0
    };
    let sortino_ratio = if downside_dev > 1e-12 {
        ((mean_ret - rf_per_period) / downside_dev) * periods.sqrt()
    } else {
        0.0
    };

    let calmar_ratio = if max_dd > 1e-12 {
        annualized_return / max_dd
    } else {
        0.0
    };

    let wins = closed_pnls.iter().filter(|&&p| p > 0.0).count();
    let win_rate_pct = if !closed_pnls.is_empty() {
        (wins as f64) / (closed_pnls.len() as f64) * 100.0
    } else {
        0.0
    };
    let gross_profit: f64 = closed_pnls.iter().filter(|&&p| p > 0.0).sum();
    let gross_loss: f64 = closed_pnls.iter().filter(|&&p| p < 0.0).map(|p| -p).sum();
    let profit_factor = if gross_loss > 1e-12 {
        gross_profit / gross_loss
    } else if gross_profit > 0.0 {
        999.99
    } else {
        0.0
    };

    BacktestResult {
        initial_capital,
        final_equity,
        total_return_pct: total_return * 100.0,
        annualized_return_pct: annualized_return * 100.0,
        annualized_volatility_pct: ann_vol * 100.0,
        sharpe_ratio,
        sortino_ratio,
        max_drawdown_pct: max_dd * 100.0,
        calmar_ratio,
        win_rate_pct,
        profit_factor,
        total_trades: trades.len(),
        total_commission,
        equity_curve,
        trades,
    }
}
