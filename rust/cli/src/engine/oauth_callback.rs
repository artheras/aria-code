//! Native Rust OAuth 2.0 PKCE & one-shot loopback callback listener (`127.0.0.1`).
//!
//! Replaces Python `http.server` loopback callback handlers in `google_login.py` / `web_login.py`
//! with zero external HTTP server dependencies.

#![allow(dead_code)]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use sha2::{Digest, Sha256};

const DONE_HTML: &str = "<!doctype html><html><head><meta charset=\"utf-8\"><title>Aria Sign-In</title></head><body style=\"font-family:sans-serif;padding:40px;text-align:center\"><h2>Sign-in complete</h2><p>You can close this tab and return to Aria Code.</p></body></html>";

#[derive(Debug, Clone)]
pub struct PkcePair {
    pub verifier: String,
    pub challenge: String,
    pub state: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OAuthCallbackResult {
    pub code: String,
    pub state: String,
}

fn pseudo_random_bytes(salt: &str) -> [u8; 32] {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut hasher = Sha256::new();
    hasher.update(nanos.to_le_bytes());
    hasher.update(std::process::id().to_le_bytes());
    hasher.update(salt.as_bytes());
    // Mix in stack address entropy for distinct calls within the same nanosecond
    let stack_addr = (&nanos as *const u128) as usize;
    hasher.update(stack_addr.to_le_bytes());
    let digest = hasher.finalize();
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest);
    out
}

/// Generate a fresh PKCE S256 verifier, challenge, and CSRF state token.
pub fn generate_pkce_pair() -> PkcePair {
    let v_bytes = pseudo_random_bytes("aria-pkce-verifier");
    let s_bytes = pseudo_random_bytes("aria-oauth-state");
    let verifier = URL_SAFE_NO_PAD.encode(v_bytes);
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let state = URL_SAFE_NO_PAD.encode(&s_bytes[..16]);
    PkcePair {
        verifier,
        challenge,
        state,
    }
}

fn parse_query_params(raw_target: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Some((_, query)) = raw_target.split_once('?') else {
        return map;
    };
    for pair in query.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            map.insert(percent_decode(k), percent_decode(v));
        }
    }
    map
}

fn percent_decode(input: &str) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(hex) = std::str::from_utf8(&bytes[i + 1..i + 3]) {
                if let Ok(val) = u8::from_str_radix(hex, 16) {
                    out.push(val);
                    i += 3;
                    continue;
                }
            }
        } else if bytes[i] == b'+' {
            out.push(b' ');
            i += 1;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub struct LoopbackOAuthListener {
    listener: TcpListener,
    pub port: u16,
    pub redirect_uri: String,
}

impl LoopbackOAuthListener {
    pub fn bind() -> Result<Self, String> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .map_err(|e| format!("failed to bind loopback OAuth callback: {e}"))?;
        listener
            .set_nonblocking(true)
            .map_err(|e| format!("failed to configure nonblocking listener: {e}"))?;
        let port = listener
            .local_addr()
            .map_err(|e| format!("local_addr failed: {e}"))?
            .port();
        Ok(Self {
            listener,
            port,
            redirect_uri: format!("http://127.0.0.1:{port}/callback"),
        })
    }

    /// Wait up to `timeout` for a single valid OAuth callback matching `expected_state`.
    pub fn wait_for_callback(
        self,
        expected_state: &str,
        timeout: Duration,
    ) -> Result<OAuthCallbackResult, String> {
        let deadline = Instant::now() + timeout;
        loop {
            match self.listener.accept() {
                Ok((mut stream, _)) => {
                    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                    let mut reader = BufReader::new(match stream.try_clone() {
                        Ok(s) => s,
                        Err(_) => continue,
                    });
                    let mut line = String::new();
                    if reader.read_line(&mut line).is_err() {
                        continue;
                    }
                    let parts: Vec<&str> = line.split_whitespace().collect();
                    if parts.len() < 2 || parts[0] != "GET" {
                        continue;
                    }
                    let target = parts[1];
                    if !target.starts_with("/callback") {
                        let resp = "HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\nConnection: close\r\n\r\nNot Found";
                        let _ = stream.write_all(resp.as_bytes());
                        continue;
                    }
                    let params = parse_query_params(target);
                    if let Some(err) = params.get("error") {
                        let resp = "HTTP/1.1 400 Bad Request\r\nContent-Length: 13\r\nConnection: close\r\n\r\nSign-in error";
                        let _ = stream.write_all(resp.as_bytes());
                        return Err(format!("OAuth provider returned error: {err}"));
                    }
                    let state = params.get("state").cloned().unwrap_or_default();
                    let code = params
                        .get("handoff_code")
                        .or_else(|| params.get("code"))
                        .cloned()
                        .unwrap_or_default();
                    if state != expected_state || code.is_empty() {
                        let resp = "HTTP/1.1 400 Bad Request\r\nContent-Length: 13\r\nConnection: close\r\n\r\nInvalid state";
                        let _ = stream.write_all(resp.as_bytes());
                        return Err("OAuth callback state mismatch or missing code".to_owned());
                    }
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        DONE_HTML.len(),
                        DONE_HTML
                    );
                    let _ = stream.write_all(resp.as_bytes());
                    let _ = stream.flush();
                    return Ok(OAuthCallbackResult { code, state });
                }
                Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        return Err(
                            "OAuth sign-in timed out waiting for browser callback".to_owned()
                        );
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
                Err(err) => return Err(format!("OAuth callback accept error: {err}")),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::TcpStream;

    #[test]
    fn pkce_challenge_matches_sha256_of_verifier() {
        let pkce = generate_pkce_pair();
        assert!(pkce.verifier.len() >= 43);
        let expected = URL_SAFE_NO_PAD.encode(Sha256::digest(pkce.verifier.as_bytes()));
        assert_eq!(pkce.challenge, expected);
        assert!(!pkce.state.is_empty());
    }

    #[test]
    fn loopback_listener_accepts_valid_callback_and_rejects_bad_state() {
        let listener = LoopbackOAuthListener::bind().expect("bind");
        let port = listener.port;
        let handle = std::thread::spawn(move || {
            let mut stream = TcpStream::connect(("127.0.0.1", port)).expect("connect");
            let req = "GET /callback?code=abc%2D123&state=goodstate HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
            stream.write_all(req.as_bytes()).expect("write");
            let mut buf = String::new();
            stream.read_to_string(&mut buf).expect("read");
            buf
        });

        let result = listener
            .wait_for_callback("goodstate", Duration::from_secs(3))
            .expect("callback");
        assert_eq!(result.code, "abc-123");
        assert_eq!(result.state, "goodstate");
        let http_resp = handle.join().expect("join");
        assert!(http_resp.starts_with("HTTP/1.1 200 OK"));
        assert!(http_resp.contains("X-Content-Type-Options: nosniff"));

        // Mismatched state is rejected
        let listener2 = LoopbackOAuthListener::bind().expect("bind2");
        let port2 = listener2.port;
        let _h2 = std::thread::spawn(move || {
            if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port2)) {
                let _ = stream.write_all(
                    b"GET /callback?code=xyz&state=wrong HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
                );
            }
        });
        let err = listener2
            .wait_for_callback("expected", Duration::from_secs(3))
            .expect_err("must reject bad state");
        assert!(err.contains("state mismatch"));
    }
}
