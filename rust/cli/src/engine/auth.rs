//! Native Rust `/login`, `/logout`, and `/whoami` account authentication.
//!
//! Drives the browser PKCE OAuth flow using `oauth_callback::LoopbackOAuthListener`,
//! exchanges the handoff code at `POST {api_url}/api/v2/auth/cli/exchange` via `ureq`,
//! and persists credentials to `~/.arthera/config.json` with restrictive permissions.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};

use crate::engine::cloud::CloudProvider;
use crate::engine::oauth_callback::{generate_pkce_pair, LoopbackOAuthListener};

const LOGIN_TIMEOUT: Duration = Duration::from_secs(180);

pub fn auth_origin() -> String {
    std::env::var("ARIA_AUTH_URL")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(|v| v.trim().trim_end_matches('/').to_owned())
        .unwrap_or_else(|| "https://auth.arthera.xyz".to_owned())
}

pub fn config_path() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("ARIA_HOME") {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed).join("config.json"));
        }
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(home).join(".arthera").join("config.json"))
}

pub fn read_config_file(path: &Path) -> Value {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .filter(|v| v.is_object())
        .unwrap_or_else(|| json!({}))
}

pub fn write_secret_config(path: &Path, value: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed to create config dir: {e}"))?;
    }
    let text = serde_json::to_string_pretty(value).map_err(|e| format!("serialize error: {e}"))?;
    fs::write(path, format!("{text}\n")).map_err(|e| format!("failed to write config: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

pub fn build_login_url(port: u16, state: &str, challenge: &str) -> String {
    format!(
        "{}/cli-login?port={}&state={}&code_challenge={}&code_challenge_method=S256",
        auth_origin(),
        port,
        state,
        challenge
    )
}

fn open_browser(url: &str) -> bool {
    if cfg!(test) {
        return false;
    }
    #[cfg(target_os = "macos")]
    return std::process::Command::new("open").arg(url).spawn().is_ok();
    #[cfg(target_os = "linux")]
    return std::process::Command::new("xdg-open")
        .arg(url)
        .spawn()
        .is_ok();
    #[cfg(target_os = "windows")]
    return std::process::Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn()
        .is_ok();
    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    false
}

fn exchange_handoff_code(
    api_url: &str,
    code: &str,
    verifier: &str,
    state: &str,
) -> Result<Value, String> {
    let endpoint = format!("{}/api/v2/auth/cli/exchange", api_url.trim_end_matches('/'));
    let payload = json!({
        "code": code,
        "code_verifier": verifier,
        "state": state,
    });
    let agent = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(20)))
        .build()
        .new_agent();
    let mut resp = agent
        .post(&endpoint)
        .header("Accept", "application/json")
        .header("Content-Type", "application/json")
        .send(payload.to_string())
        .map_err(|e| format!("auth exchange request failed: {e}"))?;
    let raw = resp
        .body_mut()
        .read_to_string()
        .map_err(|e| format!("failed to read auth exchange response: {e}"))?;
    let body: Value =
        serde_json::from_str(&raw).map_err(|e| format!("invalid auth exchange response: {e}"))?;
    let data = body.get("data").cloned().unwrap_or(Value::Null);
    if data
        .get("auth_token")
        .and_then(|v| v.as_str())
        .is_none_or(|s| s.is_empty())
    {
        return Err("Sign-in response did not include an auth_token".to_owned());
    }
    Ok(data)
}

fn apply_session_to_config(path: &Path, data: &Value) -> Result<String, String> {
    let mut cfg = read_config_file(path);
    let obj = cfg
        .as_object_mut()
        .ok_or_else(|| "config.json is not a JSON object".to_owned())?;
    for key in [
        "auth_token",
        "refresh_token",
        "user_id",
        "email",
        "display_name",
    ] {
        if let Some(val) = data.get(key).filter(|v| !v.is_null()) {
            obj.insert(key.to_owned(), val.clone());
        }
    }
    if let Some(exp) = data.get("expires_in").filter(|v| !v.is_null()) {
        let exp_str = exp
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| exp.to_string());
        obj.insert("expires_in".to_owned(), Value::String(exp_str));
    }
    obj.insert("provider".to_owned(), Value::String("firebase".to_owned()));
    let who = obj
        .get("email")
        .and_then(|v| v.as_str())
        .or_else(|| obj.get("user_id").and_then(|v| v.as_str()))
        .unwrap_or("authenticated user")
        .to_owned();
    write_secret_config(path, &cfg)?;
    Ok(who)
}

pub fn clear_session_from_config(path: &Path) -> Result<bool, String> {
    if !path.is_file() {
        return Ok(false);
    }
    let mut cfg = read_config_file(path);
    let Some(obj) = cfg.as_object_mut() else {
        return Ok(false);
    };
    let mut removed = false;
    for key in [
        "auth_token",
        "refresh_token",
        "expires_in",
        "user_id",
        "email",
        "display_name",
    ] {
        if obj.remove(key).is_some() {
            removed = true;
        }
    }
    if removed {
        write_secret_config(path, &cfg)?;
    }
    Ok(removed)
}

pub fn handle_login_command(zh: bool) -> (String, &'static str) {
    let Some(cfg_path) = config_path() else {
        return (
            "Could not resolve ~/.arthera/config.json".to_owned(),
            "failed",
        );
    };
    let listener = match LoopbackOAuthListener::bind() {
        Ok(l) => l,
        Err(e) => return (e, "failed"),
    };
    let pkce = generate_pkce_pair();
    let url = build_login_url(listener.port, &pkce.state, &pkce.challenge);
    if !open_browser(&url) {
        let msg = if zh {
            format!("无法自动打开浏览器，请手动访问以下链接完成登录:\n{url}")
        } else {
            format!("Could not open a browser. Visit this URL manually:\n{url}")
        };
        return (msg, "failed");
    }
    let cb = match listener.wait_for_callback(&pkce.state, LOGIN_TIMEOUT) {
        Ok(res) => res,
        Err(err) => return (err, "failed"),
    };
    let api_url = CloudProvider::arthera_base_url();
    let data = match exchange_handoff_code(&api_url, &cb.code, &pkce.verifier, &cb.state) {
        Ok(d) => d,
        Err(err) => return (err, "failed"),
    };
    match apply_session_to_config(&cfg_path, &data) {
        Ok(who) => {
            let msg = if zh {
                format!("✓ 已登录 Arthera 账号: `{who}`")
            } else {
                format!("✓ Signed in to Arthera as `{who}`")
            };
            (msg, "completed")
        }
        Err(err) => (err, "failed"),
    }
}

pub fn handle_logout_command(zh: bool) -> (String, &'static str) {
    let Some(cfg_path) = config_path() else {
        return (
            "Could not resolve ~/.arthera/config.json".to_owned(),
            "failed",
        );
    };
    match clear_session_from_config(&cfg_path) {
        Ok(true) => (
            if zh {
                "✓ 已退出登录并清除本地账号凭据。".to_owned()
            } else {
                "✓ Signed out and cleared local session credentials.".to_owned()
            },
            "completed",
        ),
        Ok(false) => (
            if zh {
                "当前未登录任何账号。".to_owned()
            } else {
                "No active account session found.".to_owned()
            },
            "completed",
        ),
        Err(err) => (err, "failed"),
    }
}

pub fn handle_whoami_command(zh: bool) -> (String, &'static str) {
    let Some(cfg_path) = config_path() else {
        return (
            "Could not resolve ~/.arthera/config.json".to_owned(),
            "failed",
        );
    };
    let cfg = read_config_file(&cfg_path);
    let has_token = cfg
        .get("auth_token")
        .and_then(|v| v.as_str())
        .is_some_and(|s| !s.is_empty());
    if !has_token {
        return (
            if zh {
                "当前未登录。运行 `/login` 可在浏览器中登录。".to_owned()
            } else {
                "Not signed in. Run `/login` to sign in via browser.".to_owned()
            },
            "completed",
        );
    }
    let email = cfg.get("email").and_then(|v| v.as_str()).unwrap_or("-");
    let uid = cfg.get("user_id").and_then(|v| v.as_str()).unwrap_or("-");
    let name = cfg
        .get("display_name")
        .and_then(|v| v.as_str())
        .unwrap_or("-");
    let msg = if zh {
        format!("当前账号: `{email}`（姓名: {name} · UID: `{uid}`）")
    } else {
        format!("Signed in as `{email}` (name: {name} · uid: `{uid}`)")
    };
    (msg, "completed")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn login_url_and_session_config_roundtrip() {
        let url = build_login_url(43210, "st123", "ch456");
        assert!(url.contains(
            "/cli-login?port=43210&state=st123&code_challenge=ch456&code_challenge_method=S256"
        ));

        let dir = tempfile::tempdir().expect("tempdir");
        let cfg_path = dir.path().join("config.json");
        let who = apply_session_to_config(
            &cfg_path,
            &json!({
                "auth_token": "tok_abc",
                "refresh_token": "ref_xyz",
                "expires_in": 3600,
                "user_id": "uid_1",
                "email": "alice@arthera.xyz",
                "display_name": "Alice"
            }),
        )
        .expect("apply");
        assert_eq!(who, "alice@arthera.xyz");

        let saved = read_config_file(&cfg_path);
        assert_eq!(saved["auth_token"], "tok_abc");
        assert_eq!(saved["provider"], "firebase");
        assert_eq!(saved["expires_in"], "3600");

        assert!(clear_session_from_config(&cfg_path).expect("clear"));
        let after = read_config_file(&cfg_path);
        assert!(after.get("auth_token").is_none());
        assert_eq!(after["provider"], "firebase");
    }
}
