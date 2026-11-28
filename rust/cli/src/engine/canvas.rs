//! Native Rust `/canvas` live artifact preview server.
//!
//! Opt-in local HTTP server bound strictly to `127.0.0.1` (ports 8765..=8774).
//! Replaces legacy Python `preview_server.py` with zero extra crate dependencies.
//!
//! Security invariants:
//! - Loopback-only (`127.0.0.1`), never `0.0.0.0`.
//! - Framed artifacts use `sandbox="allow-scripts"` (never `allow-same-origin`).
//! - Artifact responses carry strict CSP (`default-src 'none'`, `connect-src 'none'`,
//!   `form-action 'none'`, `base-uri 'none'`), `X-Content-Type-Options: nosniff`,
//!   and `Cache-Control: no-store`.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::json;

use crate::engine::artifacts::{list_artifacts, ArtifactItem};

pub const DEFAULT_HOST: &str = "127.0.0.1";
const PORT_START: u16 = 8765;
const PORT_END: u16 = 8774;

pub const ARTIFACT_CSP: &str = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com data:; img-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'";

const CANVAS_SHELL_HTML: &str = r##"<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Aria Canvas</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; height: 100vh; background: #0f1117; color: #e5e7eb; }
    aside { width: 280px; border-right: 1px solid #262a36; padding: 14px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
    h1 { font-size: 14px; letter-spacing: .04em; text-transform: uppercase; color: #9ca3af; margin: 0 0 8px; }
    .item { padding: 8px 10px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; background: #161923; }
    .item:hover { border-color: #374151; }
    .item.active { border-color: #3b82f6; background: #1d2538; }
    .name { font-size: 13px; font-weight: 600; word-break: break-all; }
    .meta { font-size: 11px; color: #9ca3af; margin-top: 2px; }
    main { flex: 1; display: flex; flex-direction: column; position: relative; }
    iframe { flex: 1; width: 100%; border: none; background: #ffffff; }
    .office-card { display: none; flex: 1; align-items: center; justify-content: center; flex-direction: column; gap: 12px; background: #0f1117; color: #e5e7eb; padding: 32px; text-align: center; }
    .office-card.visible { display: flex; }
    .office-btn { padding: 10px 18px; border-radius: 8px; background: #2563eb; color: #fff; text-decoration: none; font-size: 13px; font-weight: 600; }
    .empty { padding: 24px; color: #9ca3af; font-size: 13px; }
  </style>
</head>
<body>
  <aside>
    <h1>Aria Canvas</h1>
    <div id="list"><div class="empty">Waiting for artifacts…</div></div>
  </aside>
  <main>
    <div id="officeCard" class="office-card">
      <div id="officeName" style="font-size:18px;font-weight:600;"></div>
      <div id="officeMeta" style="font-size:13px;color:#9ca3af;"></div>
      <a id="officeDownload" class="office-btn" href="#" download>Download Deliverable</a>
    </div>
    <iframe id="viewer" sandbox="allow-scripts" title="Artifact Preview"></iframe>
  </main>
  <script>
    let currentSig = "";
    let selectedIdx = 0;
    const OFFICE_KINDS = new Set(["xlsx", "xlsm", "docx", "pptx"]);
    function showArtifact(item, idx) {
      const viewer = document.getElementById("viewer");
      const card = document.getElementById("officeCard");
      const url = "/artifact/" + idx + "?t=" + Date.now();
      if (item && OFFICE_KINDS.has((item.kind || "").toLowerCase())) {
        viewer.style.display = "none";
        viewer.removeAttribute("src");
        document.getElementById("officeName").textContent = item.name;
        document.getElementById("officeMeta").textContent = item.kind.toUpperCase() + " deliverable · " + item.size_bytes + " B";
        const dl = document.getElementById("officeDownload");
        dl.href = url;
        dl.setAttribute("download", item.name);
        card.classList.add("visible");
      } else {
        card.classList.remove("visible");
        viewer.style.display = "block";
        viewer.src = url;
      }
    }
    async function refresh() {
      try {
        const res = await fetch("/api/artifacts", { cache: "no-store" });
        const data = await res.json();
        const items = data.artifacts || [];
        const sig = JSON.stringify(items.map(i => [i.name, i.modified_secs, i.size_bytes]));
        if (sig === currentSig) return;
        currentSig = sig;
        const list = document.getElementById("list");
        const viewer = document.getElementById("viewer");
        if (!items.length) {
          list.innerHTML = '<div class="empty">No artifacts yet.</div>';
          document.getElementById("officeCard").classList.remove("visible");
          viewer.style.display = "block";
          viewer.removeAttribute("src");
          return;
        }
        if (selectedIdx >= items.length) selectedIdx = 0;
        list.innerHTML = "";
        items.forEach((item, idx) => {
          const div = document.createElement("div");
          div.className = "item" + (idx === selectedIdx ? " active" : "");
          div.innerHTML = '<div class="name"></div><div class="meta"></div>';
          div.querySelector(".name").textContent = item.name;
          div.querySelector(".meta").textContent = item.kind + " · " + item.size_bytes + " B";
          div.onclick = () => {
            selectedIdx = idx;
            showArtifact(item, idx);
            currentSig = "";
            refresh();
          };
          list.appendChild(div);
        });
        showArtifact(items[selectedIdx], selectedIdx);
      } catch (_) {}
    }
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"##;

pub struct CanvasSession {
    pub url: String,
    pub port: u16,
    workspace: PathBuf,
    discovery_file: Option<PathBuf>,
    stop_flag: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl CanvasSession {
    pub fn stop(&mut self) {
        self.stop_flag.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect((DEFAULT_HOST, self.port));
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
        if let Some(path) = &self.discovery_file {
            let _ = fs::remove_file(path);
        }
    }
}

impl Drop for CanvasSession {
    fn drop(&mut self) {
        self.stop();
    }
}

static ACTIVE_SESSION: OnceLock<Mutex<Option<CanvasSession>>> = OnceLock::new();

fn session_slot() -> &'static Mutex<Option<CanvasSession>> {
    ACTIVE_SESSION.get_or_init(|| Mutex::new(None))
}

fn default_discovery_path() -> Option<PathBuf> {
    if cfg!(test) {
        return None;
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    Some(PathBuf::from(home).join(".arthera").join("canvas.json"))
}

fn content_type_for(kind: &str) -> &'static str {
    match kind {
        "html" | "htm" => "text/html; charset=utf-8",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "pdf" => "application/pdf",
        "xlsx" | "xlsm" => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx" => "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "json" => "application/json; charset=utf-8",
        "csv" => "text/csv; charset=utf-8",
        "md" | "markdown" | "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn write_http_response(
    stream: &mut TcpStream,
    status: &str,
    content_type: &str,
    extra_headers: &[(&str, &str)],
    body: &[u8],
) {
    let mut head = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    );
    for (k, v) in extra_headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    head.push_str("\r\n");
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(body);
    let _ = stream.flush();
}

fn serialize_artifacts(items: &[ArtifactItem]) -> String {
    let arr: Vec<_> = items
        .iter()
        .enumerate()
        .map(|(idx, item)| {
            let name = item
                .path
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| format!("artifact-{idx}"));
            json!({
                "index": idx,
                "name": name,
                "kind": item.kind,
                "size_bytes": item.size_bytes,
                "modified_secs": item.modified_secs,
                "root_label": item.root_label,
            })
        })
        .collect();
    json!({ "artifacts": arr }).to_string()
}

fn handle_connection(mut stream: TcpStream, workspace: &Path) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let mut reader = BufReader::new(match stream.try_clone() {
        Ok(s) => s,
        Err(_) => return,
    });
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        return;
    }
    let parts: Vec<&str> = request_line.split_whitespace().collect();
    if parts.len() < 2 || parts[0] != "GET" {
        write_http_response(
            &mut stream,
            "405 Method Not Allowed",
            "text/plain",
            &[],
            b"Method Not Allowed",
        );
        return;
    }
    let raw_target = parts[1].split('?').next().unwrap_or("/");

    if raw_target == "/" || raw_target == "/index.html" {
        write_http_response(
            &mut stream,
            "200 OK",
            "text/html; charset=utf-8",
            &[
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"),
            ],
            CANVAS_SHELL_HTML.as_bytes(),
        );
        return;
    }

    if raw_target == "/api/artifacts" {
        let items = list_artifacts(workspace);
        let payload = serialize_artifacts(&items);
        write_http_response(
            &mut stream,
            "200 OK",
            "application/json; charset=utf-8",
            &[
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "no-store"),
            ],
            payload.as_bytes(),
        );
        return;
    }

    if let Some(idx_str) = raw_target.strip_prefix("/artifact/") {
        if let Ok(idx) = idx_str.parse::<usize>() {
            let items = list_artifacts(workspace);
            if let Some(item) = items.get(idx) {
                if let Ok(bytes) = fs::read(&item.path) {
                    write_http_response(
                        &mut stream,
                        "200 OK",
                        content_type_for(&item.kind),
                        &[
                            ("Content-Security-Policy", ARTIFACT_CSP),
                            ("X-Content-Type-Options", "nosniff"),
                            ("Cache-Control", "no-store"),
                        ],
                        &bytes,
                    );
                    return;
                }
            }
        }
    }

    write_http_response(
        &mut stream,
        "404 Not Found",
        "text/plain",
        &[],
        b"Not Found",
    );
}

fn bind_loopback_listener(preferred_port: Option<u16>) -> Result<TcpListener, String> {
    if let Some(p) = preferred_port {
        return TcpListener::bind((DEFAULT_HOST, p))
            .map_err(|e| format!("failed to bind {DEFAULT_HOST}:{p}: {e}"));
    }
    for port in PORT_START..=PORT_END {
        if let Ok(listener) = TcpListener::bind((DEFAULT_HOST, port)) {
            return Ok(listener);
        }
    }
    TcpListener::bind((DEFAULT_HOST, 0)).map_err(|e| format!("failed to bind {DEFAULT_HOST}: {e}"))
}

pub fn start_canvas_session(
    workspace: &Path,
    preferred_port: Option<u16>,
    discovery_override: Option<PathBuf>,
) -> Result<CanvasSession, String> {
    let listener = bind_loopback_listener(preferred_port)?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("local_addr error: {e}"))?
        .port();
    let url = format!("http://{DEFAULT_HOST}:{port}");
    let discovery_file = discovery_override.or_else(default_discovery_path);

    if let Some(path) = &discovery_file {
        if let Some(parent) = path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let body = json!({
            "url": url,
            "port": port,
            "pid": std::process::id(),
            "engine": "rust-native-canvas",
        })
        .to_string();
        let _ = fs::write(path, body);
    }

    let stop_flag = Arc::new(AtomicBool::new(false));
    let stop_clone = Arc::clone(&stop_flag);
    let ws = workspace.to_path_buf();

    let handle = thread::spawn(move || {
        for incoming in listener.incoming() {
            if stop_clone.load(Ordering::SeqCst) {
                break;
            }
            if let Ok(stream) = incoming {
                handle_connection(stream, &ws);
            }
        }
    });

    Ok(CanvasSession {
        url,
        port,
        workspace: workspace.to_path_buf(),
        discovery_file,
        stop_flag,
        handle: Some(handle),
    })
}

fn open_in_browser(url: &str) {
    if cfg!(test) {
        return;
    }
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(url).spawn();
    #[cfg(target_os = "linux")]
    let _ = std::process::Command::new("xdg-open").arg(url).spawn();
    #[cfg(target_os = "windows")]
    let _ = std::process::Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn();
}

pub fn handle_canvas_command(workspace: &Path, arg: &str, zh: bool) -> (String, &'static str) {
    let sub = arg.trim().to_ascii_lowercase();
    let mut guard = match session_slot().lock() {
        Ok(g) => g,
        Err(_) => return ("Canvas lock poisoned".to_owned(), "failed"),
    };

    match sub.as_str() {
        "stop" => {
            if let Some(mut session) = guard.take() {
                session.stop();
                let msg = if zh {
                    "✓ 已停止原生 Rust Canvas 预览服务器。".to_owned()
                } else {
                    "✓ Stopped native Rust Canvas preview server.".to_owned()
                };
                (msg, "completed")
            } else {
                let msg = if zh {
                    "Canvas 预览服务器未在运行。".to_owned()
                } else {
                    "Canvas preview server is not running.".to_owned()
                };
                (msg, "completed")
            }
        }
        "status" => {
            if let Some(session) = guard.as_ref() {
                let count = list_artifacts(&session.workspace).len();
                let msg = if zh {
                    format!(
                        "Canvas 预览服务器运行中: {}（当前发现 {} 个产物）",
                        session.url, count
                    )
                } else {
                    format!(
                        "Canvas preview server running at {} ({} artifacts discovered)",
                        session.url, count
                    )
                };
                (msg, "completed")
            } else {
                let msg = if zh {
                    "Canvas 预览服务器未在运行。输入 `/canvas` 启动。".to_owned()
                } else {
                    "Canvas preview server is stopped. Run `/canvas` to start.".to_owned()
                };
                (msg, "completed")
            }
        }
        "" | "start" | "open" => {
            if let Some(session) = guard.as_ref() {
                open_in_browser(&session.url);
                let msg = if zh {
                    format!("Canvas 预览服务器已在运行: {}", session.url)
                } else {
                    format!("Canvas preview server already running at {}", session.url)
                };
                return (msg, "completed");
            }
            match start_canvas_session(workspace, None, None) {
                Ok(session) => {
                    let url = session.url.clone();
                    open_in_browser(&url);
                    *guard = Some(session);
                    let msg = if zh {
                        format!(
                            "✓ 原生 Rust Canvas 预览服务器已启动: {url}\n产物生成后会自动刷新展示；输入 `/canvas stop` 可随时关闭。"
                        )
                    } else {
                        format!(
                            "✓ Started native Rust Canvas preview server at {url}\nNew artifacts refresh automatically; run `/canvas stop` to shut down."
                        )
                    };
                    (msg, "completed")
                }
                Err(err) => {
                    let msg = if zh {
                        format!("Canvas 预览服务器启动失败: {err}")
                    } else {
                        format!("Failed to start Canvas preview server: {err}")
                    };
                    (msg, "failed")
                }
            }
        }
        _ => {
            let msg = if zh {
                "用法: `/canvas [start|status|stop]`".to_owned()
            } else {
                "Usage: `/canvas [start|status|stop]`".to_owned()
            };
            (msg, "completed")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    fn http_get(port: u16, path: &str) -> String {
        let mut stream = TcpStream::connect((DEFAULT_HOST, port)).expect("connect");
        let req = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
        stream.write_all(req.as_bytes()).expect("write");
        let mut resp = String::new();
        stream.read_to_string(&mut resp).expect("read");
        resp
    }

    #[test]
    fn canvas_serves_sandboxed_shell_artifacts_and_strict_csp() {
        let dir = tempfile::tempdir().expect("tempdir");
        let exports = dir.path().join(".aria").join("exports");
        fs::create_dir_all(&exports).expect("mkdir");
        fs::write(exports.join("report.html"), "<h1>Hello Canvas</h1>").expect("write");

        let disc = dir.path().join("canvas.json");
        let mut session =
            start_canvas_session(dir.path(), Some(0), Some(disc.clone())).expect("start");
        assert!(session.url.starts_with("http://127.0.0.1:"));
        assert!(disc.is_file());

        let shell = http_get(session.port, "/");
        assert!(shell.starts_with("HTTP/1.1 200 OK"));
        assert!(shell.contains(r#"sandbox="allow-scripts""#));
        assert!(!shell.contains("allow-same-origin"));

        let api = http_get(session.port, "/api/artifacts");
        assert!(api.contains("report.html"));

        let art = http_get(session.port, "/artifact/0");
        assert!(art.contains("<h1>Hello Canvas</h1>"));
        assert!(art.contains("default-src 'none'"));
        assert!(art.contains("connect-src 'none'"));
        assert!(art.contains("X-Content-Type-Options: nosniff"));
        assert!(art.contains("Cache-Control: no-store"));

        let missing = http_get(session.port, "/artifact/99");
        assert!(missing.starts_with("HTTP/1.1 404 Not Found"));

        session.stop();
        assert!(!disc.exists());
    }
}
