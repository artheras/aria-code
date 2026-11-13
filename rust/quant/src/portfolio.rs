use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortfolioInput {
    pub assets: Vec<String>,
    pub returns: Vec<f64>,          // Expected annualized returns per asset
    pub cov_matrix: Vec<Vec<f64>>,  // N x N covariance matrix
    pub risk_free_rate: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PortfolioAllocation {
    pub method: String,
    pub weights: Vec<f64>,
    pub expected_return: f64,
    pub expected_volatility: f64,
    pub sharpe_ratio: f64,
}

// Portfolio variance = w^T * Sigma * w
pub fn portfolio_volatility(weights: &[f64], cov: &[Vec<f64>]) -> f64 {
    let n = weights.len();
    let mut var = 0.0;
    for i in 0..n {
        for j in 0..n {
            var += weights[i] * weights[j] * cov[i][j];
        }
    }
    var.max(1e-12).sqrt()
}

pub fn portfolio_return(weights: &[f64], returns: &[f64]) -> f64 {
    weights.iter().zip(returns.iter()).map(|(w, r)| w * r).sum()
}

// Exact Euclidean projection onto probability simplex: {w >= 0, sum(w) = 1}
pub fn project_simplex(v: &[f64]) -> Vec<f64> {
    let n = v.len();
    let mut u = v.to_vec();
    u.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));

    let mut cssv = 0.0;
    let mut rho = 0;
    for i in 0..n {
        cssv += u[i];
        let cond = u[i] - (cssv - 1.0) / ((i + 1) as f64);
        if cond > 0.0 {
            rho = i;
        }
    }
    let sum_rho: f64 = u[0..=rho].iter().sum();
    let theta = (sum_rho - 1.0) / ((rho + 1) as f64);
    v.iter().map(|&x| (x - theta).max(0.0)).collect()
}

// Inverse Variance / Equal Risk Contribution (Risk Parity)
pub fn optimize_risk_parity(cov: &[Vec<f64>]) -> Vec<f64> {
    let n = cov.len();
    if n == 0 {
        return vec![];
    }
    let mut w = vec![1.0 / (n as f64); n];
    let target_rc = 1.0 / (n as f64);
    let max_iter = 500;
    let lr = 0.05;

    for _ in 0..max_iter {
        let vol = portfolio_volatility(&w, cov);
        let mut sigma_w = vec![0.0; n];
        for i in 0..n {
            for j in 0..n {
                sigma_w[i] += cov[i][j] * w[j];
            }
        }
        let mut diff_sum = 0.0;
        for i in 0..n {
            let rc = w[i] * sigma_w[i] / (vol * vol);
            let diff = rc - target_rc;
            diff_sum += diff.abs();
            w[i] -= lr * diff * w[i];
            if w[i] < 1e-4 {
                w[i] = 1e-4;
            }
        }
        let sum_w: f64 = w.iter().sum();
        for x in w.iter_mut() {
            *x /= sum_w;
        }
        if diff_sum < 1e-5 {
            break;
        }
    }
    w
}

// Minimum Variance Portfolio via projected gradient descent on simplex
pub fn optimize_min_variance(cov: &[Vec<f64>]) -> Vec<f64> {
    let n = cov.len();
    if n == 0 {
        return vec![];
    }
    let mut w = vec![1.0 / (n as f64); n];
    let mut max_diag: f64 = 0.0;
    for i in 0..n {
        if cov[i][i] > max_diag {
            max_diag = cov[i][i];
        }
    }
    let step_size = 1.0 / (2.0 * max_diag.max(0.01) * (n as f64));

    for _ in 0..2000 {
        let mut grad = vec![0.0; n];
        for i in 0..n {
            for j in 0..n {
                grad[i] += 2.0 * cov[i][j] * w[j];
            }
        }
        let candidate: Vec<f64> = (0..n).map(|i| w[i] - step_size * grad[i]).collect();
        let new_w = project_simplex(&candidate);
        let diff: f64 = (0..n).map(|i| (new_w[i] - w[i]).abs()).sum();
        w = new_w;
        if diff < 1e-6 {
            break;
        }
    }
    w
}
