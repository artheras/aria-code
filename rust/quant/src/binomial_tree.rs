use crate::black_scholes::OptionSpec;

pub fn price_american_crr(spec: &OptionSpec, steps: usize) -> f64 {
    let n = steps.clamp(50, 5000);
    let s = spec.s;
    let k = spec.k;
    let t = spec.t.max(1e-6);
    let r = spec.r;
    let q = spec.q;
    let sigma = spec.sigma.max(1e-6);

    let dt = t / (n as f64);
    let u = (sigma * dt.sqrt()).exp();
    let d = 1.0 / u;
    let df = (-r * dt).exp();
    let p = (((r - q) * dt).exp() - d) / (u - d);
    let one_minus_p = 1.0 - p;

    // Terminal payoffs at step N
    let mut values: Vec<f64> = (0..=n)
        .map(|j| {
            let s_t = s * u.powi((2 * (j as i32)) - (n as i32));
            if spec.is_call {
                (s_t - k).max(0.0)
            } else {
                (k - s_t).max(0.0)
            }
        })
        .collect();

    // Step backwards through the tree
    for i in (0..n).rev() {
        for j in 0..=i {
            let s_t = s * u.powi((2 * (j as i32)) - (i as i32));
            let hold = df * (p * values[j + 1] + one_minus_p * values[j]);
            let exercise = if spec.is_call {
                (s_t - k).max(0.0)
            } else {
                (k - s_t).max(0.0)
            };
            values[j] = hold.max(exercise);
        }
    }

    values[0]
}
