use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PitCheckRequest {
    pub kind: String,
    pub success: bool,
    pub price: Option<f64>,
    pub available_at: Option<String>,
    pub as_of: Option<String>,
    #[serde(default)]
    pub stale: bool,
    #[serde(default)]
    pub missing_fields: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PitCheckResult {
    pub passed: bool,
    pub quality_status: String,
    pub reasons: Vec<String>,
}

/// Normalize ISO-8601 timestamp/date strings to comparable UTC YYYY-MM-DDTHH:MM:SS form.
fn normalize_iso_timestamp(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    if trimmed.len() == 10 && trimmed.as_bytes().get(4) == Some(&b'-') {
        return Some(format!("{}T00:00:00", trimmed));
    }
    let cleaned = trimmed.trim_end_matches('Z').replace(' ', "T");
    // Strip fractional seconds or timezone offset if present for simple UTC comparison.
    let base = cleaned
        .split('.')
        .next()
        .unwrap_or(&cleaned)
        .split('+')
        .next()
        .unwrap_or(&cleaned);
    if base.len() >= 19 {
        Some(base[..19].to_string())
    } else {
        Some(base.to_string())
    }
}

/// Point-in-Time & data quality gate for research/backtest consumers.
pub fn validate_point_in_time(req: &PitCheckRequest) -> PitCheckResult {
    let mut reasons = Vec::new();
    if !req.success {
        reasons.push("data_unavailable".to_string());
    }
    if req.kind == "quote" {
        match req.price {
            Some(p) if p > 0.0 && p.is_finite() => {}
            _ => reasons.push("invalid_price".to_string()),
        }
    }

    if let (Some(avail_raw), Some(as_of_raw)) = (&req.available_at, &req.as_of) {
        if let (Some(avail_norm), Some(as_of_norm)) = (
            normalize_iso_timestamp(avail_raw),
            normalize_iso_timestamp(as_of_raw),
        ) {
            if avail_norm > as_of_norm {
                reasons.push(format!(
                    "lookahead_leakage: available_at ({}) > as_of ({})",
                    avail_raw, as_of_raw
                ));
            }
        }
    }

    let quality_status = if !reasons.is_empty() {
        "rejected"
    } else if req.stale || !req.missing_fields.is_empty() {
        "degraded"
    } else {
        "verified"
    };

    PitCheckResult {
        passed: reasons.is_empty(),
        quality_status: quality_status.to_string(),
        reasons,
    }
}
