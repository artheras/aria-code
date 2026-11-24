//! Native Rust `/daemon` status inspection and constant-time TradingView webhook server.
//!
//! Complements Go `aria-runtime daemon` (which handles price alerts and schedules) and
//! replaces the webhook/status surface of legacy Python `aria_daemon.py`.

#![allow(dead_code)]

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::Value;

pub fn daemon_dir() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("ARIA_DAEMON_DIR") {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(home).join(".aria"))
}

fn read_pid(path: &Path) -> Option<u32> {
    fs::read_to_string(path).ok()?.trim().parse::<u32>().ok()
}

fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(unix)]
    {
        // SAFETY: kill(pid, 0) only probes process existence without sending a signal.
        unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        false
    }
}

/// Constant-time byte comparison to prevent timing side-channels on webhook tokens.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

pub fn resolve_webhook_token() -> Option<String> {
    for key in [
        "TRADINGVIEW_WEBHOOK_SECRET",
        "ARIA_WEBHOOK_SECRET",
        "WEBHOOK_TOKEN",
    ] {
        if let Ok(val) = std::env::var(key) {
            let trimmed = val.trim();
            if !trimmed.is_empty() {
                return Some(trimmed.to_owned());
            }
        }
    }
    None
}

pub struct WebhookServer {
    pub port: u16,
    pub url: String,
    stop_flag: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl WebhookServer {
    pub fn stop(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(("127.0.0.1", self.port));
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

impl Drop for WebhookServer {
    fn drop(&mut self) {
        self.stop();
    }
}

fn write_http(stream: &mut TcpStream, status: &str, body: &str) {
    let resp = format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(resp.as_bytes());
    let _ = stream.flush();
}

fn handle_webhook_conn<F>(mut stream: TcpStream, expected_token: Option<&str>, on_payload: &F)
where
    F: Fn(&Value),
{
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let mut reader = BufReader::new(&mut stream);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        return;
    }
    let parts: Vec<&str> = request_line.split_whitespace().collect();
    if parts.len() < 2 {
        return;
    }
    let method = parts[0];
    let target = parts[1];

    let mut content_length: usize = 0;
    let mut aria_token: Option<String> = None;
    let mut auth_bearer: Option<String> = None;
    loop {
        let mut hline = String::new();
        if reader.read_line(&mut hline).is_err() || hline.trim().is_empty() {
            break;
        }
        if let Some((k, v)) = hline.split_once(':') {
            let key = k.trim().to_ascii_lowercase();
            let val = v.trim();
            if key == "content-length" {
                content_length = val.parse::<usize>().unwrap_or(0).min(256 * 1024);
            } else if key == "x-aria-token" {
                aria_token = Some(val.to_owned());
            } else if key == "authorization" {
                if let Some(tok) = val
                    .strip_prefix("Bearer ")
                    .or_else(|| val.strip_prefix("bearer "))
                {
                    auth_bearer = Some(tok.trim().to_owned());
                }
            }
        }
    }

    if method == "GET" && target == "/" {
        write_http(
            &mut stream,
            "200 OK",
            r#"{"status":"ok","engine":"rust-daemon"}"#,
        );
        return;
    }

    if method != "POST" || !target.starts_with("/api/v1/webhook/tradingview") {
        write_http(&mut stream, "404 Not Found", r#"{"error":"not_found"}"#);
        return;
    }

    // Reject fail-open when no webhook secret is configured
    let Some(secret) = expected_token else {
        write_http(
            &mut stream,
            "503 Service Unavailable",
            r#"{"error":"webhook_secret_not_configured"}"#,
        );
        return;
    };

    // Never accept query-string ?token= or ?secret= (leaks into access logs)
    let supplied = aria_token.or(auth_bearer);
    let authorized = supplied
        .as_deref()
        .is_some_and(|s| constant_time_eq(s.as_bytes(), secret.as_bytes()));
    if !authorized {
        write_http(
            &mut stream,
            "401 Unauthorized",
            r#"{"error":"unauthorized"}"#,
        );
        return;
    }

    let mut body = vec![0u8; content_length];
    if content_length > 0 && reader.read_exact(&mut body).is_err() {
        write_http(
            &mut stream,
            "400 Bad Request",
            r#"{"error":"incomplete_body"}"#,
        );
        return;
    }
    let Ok(payload) = serde_json::from_slice::<Value>(&body) else {
        write_http(
            &mut stream,
            "400 Bad Request",
            r#"{"error":"invalid_json"}"#,
        );
        return;
    };
    on_payload(&payload);
    write_http(&mut stream, "202 Accepted", r#"{"accepted":true}"#);
}

pub fn start_webhook_server<F>(
    host: &str,
    port: u16,
    expected_token: Option<String>,
    on_payload: F,
) -> Result<WebhookServer, String>
where
    F: Fn(&Value) + Send + Sync + 'static,
{
    let listener = TcpListener::bind((host, port))
        .map_err(|e| format!("failed to bind webhook server on {host}:{port}: {e}"))?;
    let bound_port = listener
        .local_addr()
        .map_err(|e| format!("local_addr failed: {e}"))?
        .port();
    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_clone = Arc::clone(&stop_flag);
    let cb = Arc::new(on_payload);

    let handle = thread::spawn(move || {
        for incoming in listener.incoming() {
            if stop_clone.load(Ordering::SeqCst) {
                break;
            }
            if let Ok(stream) = incoming {
                handle_webhook_conn(stream, expected_token.as_deref(), &*cb);
            }
        }
    });

    Ok(WebhookServer {
        port: bound_port,
        url: format!("http://{host}:{bound_port}/api/v1/webhook/tradingview"),
        stop_flag,
        handle: Some(handle),
    })
}

pub fn daemon_status_report(dir: Option<&Path>, zh: bool) -> String {
    let Some(base) = dir else {
        return if zh {
            "无法定位 `~/.aria` 守护进程目录。".to_owned()
        } else {
            "Could not resolve `~/.aria` daemon directory.".to_owned()
        };
    };
    let go_pid = read_pid(&base.join("daemon-go.pid"));
    let py_pid = read_pid(&base.join("daemon.pid"));
    let go_alive = go_pid.is_some_and(pid_alive);
    let py_alive = py_pid.is_some_and(pid_alive);
    let db_path = base.join("daemon.db");
    let db_info = fs::metadata(&db_path)
        .map(|m| format!("{} ({} B)", db_path.display(), m.len()))
        .unwrap_or_else(|_| "not initialized".to_owned());
    let webhook_secret_set = resolve_webhook_token().is_some();

    if zh {
        format!(
            "### 原生 Daemon 状态 (`/daemon`)\n- Go `aria-runtime daemon`: {}\n- Legacy Python daemon: {}\n- 数据库: `{}`\n- Webhook 鉴权密钥: {}",
            if go_alive { format!("运行中 (PID {})", go_pid.unwrap_or(0)) } else { "未运行".to_owned() },
            if py_alive { format!("运行中 (PID {})", py_pid.unwrap_or(0)) } else { "未运行".to_owned() },
            db_info,
            if webhook_secret_set { "已配置" } else { "未配置 (fail-closed 503)" }
        )
    } else {
        format!(
            "### Native Daemon Status (`/daemon`)\n- Go `aria-runtime daemon`: {}\n- Legacy Python daemon: {}\n- Database: `{}`\n- Webhook secret: {}",
            if go_alive { format!("running (PID {})", go_pid.unwrap_or(0)) } else { "stopped".to_owned() },
            if py_alive { format!("running (PID {})", py_pid.unwrap_or(0)) } else { "stopped".to_owned() },
            db_info,
            if webhook_secret_set { "configured" } else { "unset (fail-closed 503)" }
        )
    }
}

pub fn handle_daemon_command(arg: &str, zh: bool) -> (String, &'static str) {
    let sub = arg.trim().to_ascii_lowercase();
    match sub.as_str() {
        "" | "status" => {
            let dir = if cfg!(test) { None } else { daemon_dir() };
            (daemon_status_report(dir.as_deref(), zh), "completed")
        }
        _ => (
            if zh {
                "用法: `/daemon [status]`（后台告警与定时任务由 `aria-runtime daemon` 托管）"
                    .to_owned()
            } else {
                "Usage: `/daemon [status]` (background alerts/schedules are hosted by `aria-runtime daemon`)".to_owned()
            },
            "completed",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    fn post_webhook(port: u16, path: &str, header: Option<(&str, &str)>, body: &str) -> u16 {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).expect("connect");
        let h = header
            .map(|(k, v)| format!("{k}: {v}\r\n"))
            .unwrap_or_default();
        let req = format!(
            "POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{h}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(req.as_bytes()).expect("write");
        let mut resp = String::new();
        stream.read_to_string(&mut resp).expect("read");
        resp.lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|c| c.parse::<u16>().ok())
            .unwrap_or(0)
    }

    #[test]
    fn webhook_server_enforces_header_auth_and_rejects_query_tokens() {
        let received: Arc<Mutex<Vec<Value>>> = Arc::new(Mutex::new(Vec::new()));
        let rec_clone = Arc::clone(&received);
        let mut srv = start_webhook_server("127.0.0.1", 0, Some("s3cret".into()), move |val| {
            rec_clone.lock().expect("lock").push(val.clone());
        })
        .expect("start");

        assert_eq!(
            post_webhook(
                srv.port,
                "/api/v1/webhook/tradingview",
                Some(("X-Aria-Token", "s3cret")),
                r#"{"symbol":"AAPL"}"#,
            ),
            202
        );
        assert_eq!(
            post_webhook(
                srv.port,
                "/api/v1/webhook/tradingview",
                Some(("Authorization", "Bearer s3cret")),
                r#"{"symbol":"NVDA"}"#,
            ),
            202
        );
        assert_eq!(
            post_webhook(
                srv.port,
                "/api/v1/webhook/tradingview?token=s3cret",
                None,
                r#"{"symbol":"TSLA"}"#,
            ),
            401
        );
        assert_eq!(
            post_webhook(
                srv.port,
                "/api/v1/webhook/tradingview",
                Some(("X-Aria-Token", "wrong")),
                r#"{"symbol":"TSLA"}"#,
            ),
            401
        );
        assert_eq!(received.lock().expect("lock").len(), 2);
        srv.stop();

        // Fail-closed 503 when secret is unset
        let mut unconfigured = start_webhook_server("127.0.0.1", 0, None, |_| {}).expect("start2");
        assert_eq!(
            post_webhook(
                unconfigured.port,
                "/api/v1/webhook/tradingview",
                None,
                r#"{"symbol":"AAPL"}"#,
            ),
            503
        );
        unconfigured.stop();
    }
}
