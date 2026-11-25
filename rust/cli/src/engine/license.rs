//! Enterprise offline/signed license issuance and verification (`aria1.<payload>.<hmac>`).
//!
//! Uses HMAC-SHA256 over Base64URL-encoded JSON claims so packaged agents
//! exported via `aria-native pack export-docker` or run in enterprise
//! environments can enforce seat, package, and expiration boundaries offline.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const TOKEN_PREFIX: &str = "aria1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LicenseClaims {
    pub customer: String,
    pub package: String,
    pub tier: String,
    pub seats: u32,
    pub expires_at: u64,
}

impl LicenseClaims {
    pub fn to_json(&self) -> Value {
        json!({
            "customer": self.customer,
            "package": self.package,
            "tier": self.tier,
            "seats": self.seats,
            "expires_at": self.expires_at,
        })
    }

    pub fn from_json(v: &Value) -> Result<Self, String> {
        let customer = v
            .get("customer")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or("License missing `customer`")?
            .to_owned();
        let package = v
            .get("package")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or("License missing `package`")?
            .to_owned();
        let tier = v
            .get("tier")
            .and_then(Value::as_str)
            .unwrap_or("enterprise")
            .to_owned();
        let seats = v.get("seats").and_then(Value::as_u64).unwrap_or(1).max(1) as u32;
        let expires_at = v.get("expires_at").and_then(Value::as_u64).unwrap_or(0);
        Ok(Self {
            customer,
            package,
            tier,
            seats,
            expires_at,
        })
    }
}

fn hmac_sha256(secret: &[u8], data: &[u8]) -> [u8; 32] {
    let mut key_block = [0u8; 64];
    if secret.len() > 64 {
        let hashed = Sha256::digest(secret);
        key_block[..32].copy_from_slice(&hashed);
    } else {
        key_block[..secret.len()].copy_from_slice(secret);
    }

    let mut ipad = [0x36u8; 64];
    let mut opad = [0x5cu8; 64];
    for i in 0..64 {
        ipad[i] ^= key_block[i];
        opad[i] ^= key_block[i];
    }

    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(data);
    let inner_hash = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_hash);
    outer.finalize().into()
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (&x, &y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn issue_license(claims: &LicenseClaims, secret: &str) -> Result<String, String> {
    if secret.trim().is_empty() {
        return Err("License signing secret must not be empty".into());
    }
    let json_bytes = serde_json::to_vec(&claims.to_json())
        .map_err(|e| format!("Failed to serialize claims: {e}"))?;
    let payload = URL_SAFE_NO_PAD.encode(json_bytes);
    let signing_input = format!("{TOKEN_PREFIX}.{payload}");
    let sig = hmac_sha256(secret.as_bytes(), signing_input.as_bytes());
    Ok(format!("{signing_input}.{}", hex_encode(&sig)))
}

pub fn verify_license(
    token: &str,
    secret: &str,
    expected_package: Option<&str>,
    now_secs: u64,
) -> Result<LicenseClaims, String> {
    if secret.trim().is_empty() {
        return Err("License verification secret must not be empty".into());
    }
    let parts: Vec<&str> = token.trim().split('.').collect();
    if parts.len() != 3 || parts[0] != TOKEN_PREFIX {
        return Err("Invalid license token format (expected aria1.<payload>.<sig>)".into());
    }
    let signing_input = format!("{}.{}", parts[0], parts[1]);
    let expected_sig = hex_encode(&hmac_sha256(secret.as_bytes(), signing_input.as_bytes()));
    if !constant_time_eq(parts[2].as_bytes(), expected_sig.as_bytes()) {
        return Err("Invalid license signature".into());
    }
    let raw = URL_SAFE_NO_PAD
        .decode(parts[1])
        .map_err(|e| format!("Invalid license payload encoding: {e}"))?;
    let val: Value =
        serde_json::from_slice(&raw).map_err(|e| format!("Invalid license JSON: {e}"))?;
    let claims = LicenseClaims::from_json(&val)?;
    if claims.expires_at > 0 && now_secs > claims.expires_at {
        return Err(format!(
            "License expired at unix timestamp {} (now {})",
            claims.expires_at, now_secs
        ));
    }
    if let Some(pkg) = expected_package {
        if claims.package != "*" && claims.package != pkg {
            return Err(format!(
                "License scoped to `{}` does not cover `{pkg}`",
                claims.package
            ));
        }
    }
    Ok(claims)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issue_and_verify_valid_license() {
        let claims = LicenseClaims {
            customer: "Acme Logistics".into(),
            package: "freight-auditor".into(),
            tier: "enterprise".into(),
            seats: 25,
            expires_at: 2_000_000_000,
        };
        let token = issue_license(&claims, "top-secret-key").expect("issue");
        let verified = verify_license(
            &token,
            "top-secret-key",
            Some("freight-auditor"),
            1_700_000_000,
        )
        .expect("verify");
        assert_eq!(verified, claims);
    }

    #[test]
    fn verify_rejects_tampered_expired_and_wrong_package() {
        let claims = LicenseClaims {
            customer: "Acme".into(),
            package: "freight-auditor".into(),
            tier: "enterprise".into(),
            seats: 5,
            expires_at: 1_500_000_000,
        };
        let token = issue_license(&claims, "secret").expect("issue");
        assert!(verify_license(&token, "wrong-secret", None, 1_400_000_000).is_err());
        assert!(verify_license(&token, "secret", None, 1_600_000_000).is_err());
        assert!(verify_license(&token, "secret", Some("other-pack"), 1_400_000_000).is_err());
    }
}
