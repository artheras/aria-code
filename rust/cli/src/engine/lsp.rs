//! Native Language Server Protocol (LSP) stdio client (`hover`, `definition`, `references`).
//!
//! Replaces Python `runtime/lsp.py` with direct `Content-Length` framed JSON-RPC over stdio.

#![allow(dead_code)]

use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LspServerSpec {
    pub language_id: &'static str,
    pub command: &'static str,
    pub args: &'static [&'static str],
}

/// Detect the appropriate language server binary for a file extension.
pub fn server_for_path(path: &Path) -> Option<LspServerSpec> {
    let ext = path.extension().and_then(|e| e.to_str())?;
    match ext {
        "rs" => Some(LspServerSpec {
            language_id: "rust",
            command: "rust-analyzer",
            args: &[],
        }),
        "py" => Some(LspServerSpec {
            language_id: "python",
            command: "pyright-langserver",
            args: &["--stdio"],
        }),
        "ts" | "tsx" | "js" | "jsx" => Some(LspServerSpec {
            language_id: "typescript",
            command: "typescript-language-server",
            args: &["--stdio"],
        }),
        "go" => Some(LspServerSpec {
            language_id: "go",
            command: "gopls",
            args: &[],
        }),
        _ => None,
    }
}

/// Format a JSON-RPC message with LSP `Content-Length` framing.
pub fn frame_lsp_message(payload: &Value) -> Vec<u8> {
    let body = payload.to_string();
    format!("Content-Length: {}\r\n\r\n{}", body.len(), body).into_bytes()
}

/// Maximum accepted LSP JSON-RPC payload size (16 MiB) to guard against OOM on corrupt headers.
const MAX_LSP_CONTENT_LENGTH: usize = 16 * 1024 * 1024;

/// Read one `Content-Length` framed JSON-RPC message from `reader`.
pub fn read_lsp_message<R: BufRead>(reader: &mut R) -> Result<Value, String> {
    let mut content_length: Option<usize> = None;
    loop {
        let mut line = String::new();
        let n = reader
            .read_line(&mut line)
            .map_err(|e| format!("LSP header read failed: {e}"))?;
        if n == 0 {
            return Err("LSP stream closed".into());
        }
        let trimmed = line.trim();
        if trimmed.is_empty() {
            break;
        }
        if let Some((key, val)) = trimmed.split_once(':') {
            if key.trim().eq_ignore_ascii_case("content-length") {
                content_length = val.trim().parse::<usize>().ok();
            }
        }
    }
    let len = content_length.ok_or("Missing Content-Length header")?;
    if len > MAX_LSP_CONTENT_LENGTH {
        return Err(format!(
            "LSP Content-Length {len} exceeds maximum allowed {MAX_LSP_CONTENT_LENGTH} bytes"
        ));
    }
    let mut buf = vec![0u8; len];
    reader
        .read_exact(&mut buf)
        .map_err(|e| format!("LSP body read failed: {e}"))?;
    serde_json::from_slice(&buf).map_err(|e| format!("Invalid LSP JSON: {e}"))
}

fn format_file_uri_from_str(raw: &str) -> String {
    let stripped = raw.strip_prefix(r"\\?\").unwrap_or(raw);
    let normalized = stripped.replace('\\', "/").replace(' ', "%20");
    if normalized.starts_with('/') {
        format!("file://{normalized}")
    } else {
        format!("file:///{normalized}")
    }
}

pub fn file_uri(path: &Path) -> String {
    let canon = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    format_file_uri_from_str(&canon.to_string_lossy())
}

pub struct LspClient {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl LspClient {
    pub fn start(spec: &LspServerSpec, workspace: &Path) -> Result<Self, String> {
        let mut child = Command::new(spec.command)
            .args(spec.args)
            .current_dir(workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Failed to start LSP `{}`: {e}", spec.command))?;
        let stdin = child.stdin.take().ok_or("LSP stdin unavailable")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("LSP stdout unavailable")?);
        let mut client = Self {
            child,
            stdin,
            stdout,
            next_id: 0,
        };
        let root_uri = file_uri(workspace);
        let _ = client.request(
            "initialize",
            json!({
                "processId": std::process::id(),
                "rootUri": root_uri,
                "capabilities": {},
            }),
        )?;
        client.notify("initialized", json!({}))?;
        Ok(client)
    }

    pub fn notify(&mut self, method: &str, params: Value) -> Result<(), String> {
        let msg = json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        });
        let framed = frame_lsp_message(&msg);
        self.stdin
            .write_all(&framed)
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("LSP notify write failed: {e}"))
    }

    pub fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        self.next_id += 1;
        let id = self.next_id;
        let msg = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });
        let framed = frame_lsp_message(&msg);
        self.stdin
            .write_all(&framed)
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("LSP request write failed: {e}"))?;
        loop {
            let reply = read_lsp_message(&mut self.stdout)?;
            if reply.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(err) = reply.get("error") {
                    return Err(format!("LSP error: {err}"));
                }
                return Ok(reply.get("result").cloned().unwrap_or(Value::Null));
            }
        }
    }

    pub fn did_open(&mut self, path: &Path, language_id: &str, text: &str) -> Result<(), String> {
        self.notify(
            "textDocument/didOpen",
            json!({
                "textDocument": {
                    "uri": file_uri(path),
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                }
            }),
        )
    }

    pub fn hover(&mut self, path: &Path, line: u32, character: u32) -> Result<Value, String> {
        self.request(
            "textDocument/hover",
            json!({
                "textDocument": { "uri": file_uri(path) },
                "position": { "line": line, "character": character },
            }),
        )
    }

    pub fn definition(&mut self, path: &Path, line: u32, character: u32) -> Result<Value, String> {
        self.request(
            "textDocument/definition",
            json!({
                "textDocument": { "uri": file_uri(path) },
                "position": { "line": line, "character": character },
            }),
        )
    }

    pub fn references(&mut self, path: &Path, line: u32, character: u32) -> Result<Value, String> {
        self.request(
            "textDocument/references",
            json!({
                "textDocument": { "uri": file_uri(path) },
                "position": { "line": line, "character": character },
                "context": { "includeDeclaration": true },
            }),
        )
    }
}

impl Drop for LspClient {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn frames_and_parses_lsp_messages() {
        let msg = json!({"jsonrpc": "2.0", "id": 1, "method": "textDocument/hover"});
        let framed = frame_lsp_message(&msg);
        let mut cursor = Cursor::new(framed);
        let decoded = read_lsp_message(&mut cursor).unwrap();
        assert_eq!(decoded, msg);
    }

    #[test]
    fn maps_extensions_to_language_servers() {
        assert_eq!(
            server_for_path(Path::new("src/main.rs")).unwrap().command,
            "rust-analyzer"
        );
        assert_eq!(
            server_for_path(Path::new("app.py")).unwrap().command,
            "pyright-langserver"
        );
        assert_eq!(
            server_for_path(Path::new("index.ts")).unwrap().command,
            "typescript-language-server"
        );
        assert_eq!(
            server_for_path(Path::new("main.go")).unwrap().command,
            "gopls"
        );
        assert!(server_for_path(Path::new("notes.txt")).is_none());
    }

    #[test]
    fn handles_lowercase_content_length_rejects_oversized_and_normalizes_file_uris() {
        let raw = b"content-length: 17\r\n\r\n{\"jsonrpc\":\"2.0\"}";
        let mut cur = Cursor::new(raw.as_slice());
        assert_eq!(read_lsp_message(&mut cur).unwrap()["jsonrpc"], "2.0");

        let huge = b"Content-Length: 999999999\r\n\r\n{}";
        let mut huge_cur = Cursor::new(huge.as_slice());
        assert!(read_lsp_message(&mut huge_cur)
            .unwrap_err()
            .contains("exceeds maximum"));

        assert_eq!(
            format_file_uri_from_str("/Users/mac/My Repo/src/main.rs"),
            "file:///Users/mac/My%20Repo/src/main.rs"
        );
        assert_eq!(
            format_file_uri_from_str(r"\\?\C:\Users\dev\My Repo\main.rs"),
            "file:///C:/Users/dev/My%20Repo/main.rs"
        );
    }
}
