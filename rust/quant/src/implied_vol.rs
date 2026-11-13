use crate::black_scholes::{calculate_greeks, OptionSpec};

pub fn implied_volatility(
    market_price: f64,
    s: f64,
    k: f64,
    t: f64,
    r: f64,
    q: f64,
    is_call: bool,
) -> Result<f64, String> {
    if market_price <= 0.0 {
        return Err("Market price must be positive".to_string());
    }
    let intrinsic = if is_call {
        (s * (-q * t).exp() - k * (-r * t).exp()).max(0.0)
    } else {
        (k * (-r * t).exp() - s * (-q * t).exp()).max(0.0)
    };
    if market_price < intrinsic {
        return Err("Market price violates intrinsic value bound".to_string());
    }

    let mut sigma = 0.30; // initial guess 30%
    let max_iter = 50;
    let tol = 1e-6;

    // 1. Newton-Raphson iterations
    for _ in 0..max_iter {
        let spec = OptionSpec {
            s,
            k,
            t,
            r,
            sigma,
            is_call,
            q,
        };
        let greeks = calculate_greeks(&spec);
        let diff = greeks.price - market_price;
        if diff.abs() < tol {
            return Ok(sigma);
        }
        let vega = greeks.vega;
        if vega.abs() < 1e-8 {
            break; // vega too flat, fallback to bisection
        }
        let next_sigma = sigma - diff / vega;
        if next_sigma <= 0.001 || next_sigma > 10.0 {
            break; // out of reasonable bounds, switch to bisection
        }
        sigma = next_sigma;
    }

    // 2. Robust Bisection fallback
    let mut low = 0.001;
    let mut high = 8.0;
    for _ in 0..100 {
        let mid = 0.5 * (low + high);
        let spec = OptionSpec {
            s,
            k,
            t,
            r,
            sigma: mid,
            is_call,
            q,
        };
        let price = calculate_greeks(&spec).price;
        let diff = price - market_price;
        if diff.abs() < tol {
            return Ok(mid);
        }
        if diff > 0.0 {
            high = mid;
        } else {
            low = mid;
        }
    }

    Ok(0.5 * (low + high))
}
