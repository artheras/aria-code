use serde::{Deserialize, Serialize};
use std::f64::consts::PI;

pub fn norm_pdf(x: f64) -> f64 {
    (-0.5 * x * x).exp() / (2.0 * PI).sqrt()
}

pub fn norm_cdf(x: f64) -> f64 {
    if x < 0.0 {
        return 1.0 - norm_cdf(-x);
    }
    let k = 1.0 / (1.0 + 0.2316419 * x);
    let a1 = 0.319381530;
    let a2 = -0.356563782;
    let a3 = 1.781477937;
    let a4 = -1.821255978;
    let a5 = 1.330274429;

    let poly = k * (a1 + k * (a2 + k * (a3 + k * (a4 + k * a5))));
    1.0 - norm_pdf(x) * poly
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OptionSpec {
    pub s: f64,        // Spot price
    pub k: f64,        // Strike price
    pub t: f64,        // Time to maturity (years)
    pub r: f64,        // Risk-free interest rate (annualized, e.g. 0.05)
    pub sigma: f64,    // Volatility (annualized, e.g. 0.20)
    pub is_call: bool, // true for Call, false for Put
    #[serde(default)]
    pub q: f64, // Dividend yield (annualized, default 0.0)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Greeks {
    pub price: f64,
    pub delta: f64,
    pub gamma: f64,
    pub vega: f64,
    pub theta: f64,
    pub rho: f64,
    pub vanna: f64,
    pub volga: f64,
}

pub fn calculate_greeks(spec: &OptionSpec) -> Greeks {
    let s = spec.s;
    let k = spec.k;
    let t = spec.t.max(1e-6);
    let r = spec.r;
    let q = spec.q;
    let sigma = spec.sigma.max(1e-6);

    let sqrt_t = t.sqrt();
    let d1 = ((s / k).ln() + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t);
    let d2 = d1 - sigma * sqrt_t;

    let eq_t = (-q * t).exp();
    let er_t = (-r * t).exp();
    let nd1_pdf = norm_pdf(d1);

    let (price, delta, rho, theta) = if spec.is_call {
        let p = s * eq_t * norm_cdf(d1) - k * er_t * norm_cdf(d2);
        let del = eq_t * norm_cdf(d1);
        let rh = k * t * er_t * norm_cdf(d2);
        let th = -(s * sigma * eq_t * nd1_pdf) / (2.0 * sqrt_t) + q * s * eq_t * norm_cdf(d1)
            - r * k * er_t * norm_cdf(d2);
        (p, del, rh, th)
    } else {
        let p = k * er_t * norm_cdf(-d2) - s * eq_t * norm_cdf(-d1);
        let del = -eq_t * norm_cdf(-d1);
        let rh = -k * t * er_t * norm_cdf(-d2);
        let th = -(s * sigma * eq_t * nd1_pdf) / (2.0 * sqrt_t) - q * s * eq_t * norm_cdf(-d1)
            + r * k * er_t * norm_cdf(-d2);
        (p, del, rh, th)
    };

    let gamma = (eq_t * nd1_pdf) / (s * sigma * sqrt_t);
    let vega = s * eq_t * sqrt_t * nd1_pdf; // per 1.0 vol (divide by 100 for 1% vol shift)
    let vanna = -eq_t * nd1_pdf * (d2 / sigma);
    let volga = vega * (d1 * d2 / sigma);

    Greeks {
        price,
        delta,
        gamma,
        vega,
        theta,
        rho,
        vanna,
        volga,
    }
}
