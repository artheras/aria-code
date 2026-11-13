use rayon::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MonteCarloConfig {
    pub s0: f64,           // Initial asset price
    pub r: f64,            // Drift / risk-free rate
    pub q: f64,            // Dividend yield
    pub sigma: f64,        // Volatility
    pub t: f64,            // Total time in years
    pub steps: usize,      // Number of time steps per path
    pub num_paths: usize,  // Total number of simulated paths
    pub seed: Option<u64>, // Optional random seed
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MonteCarloResult {
    pub mean_terminal: f64,
    pub std_terminal: f64,
    pub var_95: f64,
    pub var_99: f64,
    pub cvar_95: f64,
    pub cvar_99: f64,
    pub p05: f64,
    pub p25: f64,
    pub median: f64,
    pub p75: f64,
    pub p95: f64,
    pub elapsed_ms: f64,
}

// Fast xorshift64star pseudo-random generator (high performance, thread-local)
struct FastRng {
    state: u64,
}

impl FastRng {
    fn new(seed: u64) -> Self {
        let s = if seed == 0 {
            0x5a17_9b32_14f8_2d09
        } else {
            seed
        };
        Self { state: s }
    }

    #[inline]
    fn next_u64(&mut self) -> u64 {
        let mut x = self.state;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.state = x;
        x.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    #[inline]
    fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / ((1u64 << 53) as f64))
    }

    // Box-Muller transform for standard normal random numbers
    #[inline]
    fn next_gaussian(&mut self) -> f64 {
        let u1 = self.next_f64().max(1e-15);
        let u2 = self.next_f64();
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
    }
}

pub fn simulate_gbm_parallel(cfg: &MonteCarloConfig) -> MonteCarloResult {
    let start = std::time::Instant::now();
    let n_paths = cfg.num_paths.max(100);
    let n_steps = cfg.steps.max(1);
    let dt = cfg.t / (n_steps as f64);
    let sqrt_dt = dt.sqrt();
    let drift = (cfg.r - cfg.q - 0.5 * cfg.sigma * cfg.sigma) * dt;
    let vol_sqrt = cfg.sigma * sqrt_dt;

    let base_seed = cfg.seed.unwrap_or(0x1337_c001_dead_beef);

    // Parallel simulation chunked across available CPU threads
    let terminal_prices: Vec<f64> = (0..n_paths)
        .into_par_iter()
        .map(|idx| {
            let mut rng =
                FastRng::new(base_seed.wrapping_add((idx as u64).wrapping_mul(0x9e3779b97f4a7c15)));
            let mut s = cfg.s0;
            for _ in 0..n_steps {
                let z = rng.next_gaussian();
                s *= (drift + vol_sqrt * z).exp();
            }
            s
        })
        .collect();

    let mut sorted = terminal_prices;
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

    let len = sorted.len();
    let sum: f64 = sorted.iter().sum();
    let mean = sum / (len as f64);

    let var: f64 = sorted.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / (len as f64);
    let std_dev = var.sqrt();

    let idx_p05 = ((len as f64) * 0.05).floor() as usize;
    let idx_p01 = ((len as f64) * 0.01).floor() as usize;
    let idx_p25 = ((len as f64) * 0.25).floor() as usize;
    let idx_p50 = ((len as f64) * 0.50).floor() as usize;
    let idx_p75 = ((len as f64) * 0.75).floor() as usize;
    let idx_p95 = ((len as f64) * 0.95).floor() as usize;

    let p01 = sorted[idx_p01.min(len - 1)];
    let p05 = sorted[idx_p05.min(len - 1)];
    let p25 = sorted[idx_p25.min(len - 1)];
    let median = sorted[idx_p50.min(len - 1)];
    let p75 = sorted[idx_p75.min(len - 1)];
    let p95 = sorted[idx_p95.min(len - 1)];

    // Value at Risk (VaR): loss relative to initial price s0
    let var_95 = (cfg.s0 - p05).max(0.0);
    let var_99 = (cfg.s0 - p01).max(0.0);

    // Conditional Value at Risk (Expected Shortfall)
    let tail_05_slice = &sorted[0..=idx_p05.min(len - 1)];
    let tail_05_mean = tail_05_slice.iter().sum::<f64>() / (tail_05_slice.len() as f64);
    let cvar_95 = (cfg.s0 - tail_05_mean).max(0.0);

    let tail_01_slice = &sorted[0..=idx_p01.min(len - 1)];
    let tail_01_mean = tail_01_slice.iter().sum::<f64>() / (tail_01_slice.len() as f64);
    let cvar_99 = (cfg.s0 - tail_01_mean).max(0.0);

    let elapsed = start.elapsed().as_secs_f64() * 1000.0;

    MonteCarloResult {
        mean_terminal: mean,
        std_terminal: std_dev,
        var_95,
        var_99,
        cvar_95,
        cvar_99,
        p05,
        p25,
        median,
        p75,
        p95,
        elapsed_ms: elapsed,
    }
}
