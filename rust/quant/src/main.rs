use aria_quant::{
    calculate_greeks, implied_volatility, optimize_min_variance, optimize_risk_parity,
    portfolio_return, portfolio_volatility, price_american_crr, run_backtest,
    simulate_gbm_parallel, BacktestRequest, MonteCarloConfig, OptionSpec, PortfolioAllocation,
    PortfolioInput,
};
use std::env;
use std::io::{self, Read};

fn print_usage() {
    eprintln!(
        "Usage: aria-quant <command> [json_payload]\n\
         Commands:\n\
           price         Calculate Black-Scholes price and all Greeks\n\
           implied-vol   Solve for Black-Scholes implied volatility\n\
           american      Calculate American option price via CRR binomial tree\n\
           monte-carlo   Run parallel multi-threaded Monte Carlo path simulation\n\
           portfolio     Compute Risk Parity & Minimum Variance portfolio weights\n\
           backtest      Run high-speed event-driven strategy backtest\n\
         If json_payload is omitted, payload is read from stdin."
    );
}

fn read_input(args: &[String]) -> Result<String, String> {
    if args.len() > 2 {
        Ok(args[2].clone())
    } else {
        let mut buffer = String::new();
        io::stdin()
            .read_to_string(&mut buffer)
            .map_err(|e| format!("Failed to read stdin: {}", e))?;
        Ok(buffer)
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        print_usage();
        std::process::exit(1);
    }

    let command = args[1].as_str();
    let raw_json = match read_input(&args) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Error: {}", e);
            std::process::exit(1);
        }
    };

    match command {
        "price" => {
            let spec: OptionSpec = match serde_json::from_str(&raw_json) {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("Invalid OptionSpec JSON: {}", e);
                    std::process::exit(1);
                }
            };
            let greeks = calculate_greeks(&spec);
            println!("{}", serde_json::to_string_pretty(&greeks).unwrap());
        }
        "implied-vol" => {
            #[derive(serde::Deserialize)]
            struct IvRequest {
                market_price: f64,
                s: f64,
                k: f64,
                t: f64,
                r: f64,
                #[serde(default)]
                q: f64,
                is_call: bool,
            }
            let req: IvRequest = match serde_json::from_str(&raw_json) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("Invalid IvRequest JSON: {}", e);
                    std::process::exit(1);
                }
            };
            match implied_volatility(
                req.market_price,
                req.s,
                req.k,
                req.t,
                req.r,
                req.q,
                req.is_call,
            ) {
                Ok(iv) => {
                    println!(
                        "{}",
                        serde_json::json!({ "implied_volatility": iv, "success": true })
                    );
                }
                Err(e) => {
                    println!("{}", serde_json::json!({ "error": e, "success": false }));
                    std::process::exit(1);
                }
            }
        }
        "american" => {
            #[derive(serde::Deserialize)]
            struct AmericanRequest {
                #[serde(flatten)]
                spec: OptionSpec,
                #[serde(default = "default_steps")]
                steps: usize,
            }
            fn default_steps() -> usize {
                500
            }

            let req: AmericanRequest = match serde_json::from_str(&raw_json) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("Invalid AmericanRequest JSON: {}", e);
                    std::process::exit(1);
                }
            };
            let price = price_american_crr(&req.spec, req.steps);
            println!(
                "{}",
                serde_json::json!({ "american_price": price, "steps": req.steps })
            );
        }
        "monte-carlo" => {
            let cfg: MonteCarloConfig = match serde_json::from_str(&raw_json) {
                Ok(c) => c,
                Err(e) => {
                    eprintln!("Invalid MonteCarloConfig JSON: {}", e);
                    std::process::exit(1);
                }
            };
            let res = simulate_gbm_parallel(&cfg);
            println!("{}", serde_json::to_string_pretty(&res).unwrap());
        }
        "portfolio" => {
            let inp: PortfolioInput = match serde_json::from_str(&raw_json) {
                Ok(i) => i,
                Err(e) => {
                    eprintln!("Invalid PortfolioInput JSON: {}", e);
                    std::process::exit(1);
                }
            };
            let rf = inp.risk_free_rate.unwrap_or(0.02);
            let rp_weights = optimize_risk_parity(&inp.cov_matrix);
            let rp_ret = portfolio_return(&rp_weights, &inp.returns);
            let rp_vol = portfolio_volatility(&rp_weights, &inp.cov_matrix);
            let rp_sharpe = if rp_vol > 1e-8 {
                (rp_ret - rf) / rp_vol
            } else {
                0.0
            };

            let mv_weights = optimize_min_variance(&inp.cov_matrix);
            let mv_ret = portfolio_return(&mv_weights, &inp.returns);
            let mv_vol = portfolio_volatility(&mv_weights, &inp.cov_matrix);
            let mv_sharpe = if mv_vol > 1e-8 {
                (mv_ret - rf) / mv_vol
            } else {
                0.0
            };

            let result = serde_json::json!({
                "assets": inp.assets,
                "risk_parity": PortfolioAllocation {
                    method: "risk_parity".to_string(),
                    weights: rp_weights,
                    expected_return: rp_ret,
                    expected_volatility: rp_vol,
                    sharpe_ratio: rp_sharpe,
                },
                "minimum_variance": PortfolioAllocation {
                    method: "minimum_variance".to_string(),
                    weights: mv_weights,
                    expected_return: mv_ret,
                    expected_volatility: mv_vol,
                    sharpe_ratio: mv_sharpe,
                }
            });
            println!("{}", serde_json::to_string_pretty(&result).unwrap());
        }
        "backtest" => {
            let req: BacktestRequest = match serde_json::from_str(&raw_json) {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("Invalid BacktestRequest JSON: {}", e);
                    std::process::exit(1);
                }
            };
            let res = run_backtest(&req.bars, &req.config);
            println!("{}", serde_json::to_string_pretty(&res).unwrap());
        }
        _ => {
            eprintln!("Unknown command: {}", command);
            print_usage();
            std::process::exit(1);
        }
    }
}
