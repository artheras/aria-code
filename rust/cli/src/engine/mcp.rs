//! Native Rust MCP (Model Context Protocol) JSON-RPC 2.0 stdio client.

use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
};

pub struct McpStdioClient {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: AtomicU64,
}

impl McpStdioClient {
    pub fn spawn(program: &str, args: &[&str]) -> Result<Self, String> {
        let mut child = Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("Failed to spawn MCP server `{program}`: {e}"))?;
        let stdin = child.stdin.take().ok_or("Missing MCP stdin")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("Missing MCP stdout")?);
        let mut client = Self {
            child,
            stdin,
            stdout,
            next_id: AtomicU64::new(1),
        };
        client.initialize()?;
        Ok(client)
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let msg = format_jsonrpc_request(id, method, params);
        let line = serde_json::to_string(&msg).map_err(|e| e.to_string())?;
        self.stdin
            .write_all(line.as_bytes())
            .and_then(|_| self.stdin.write_all(b"\n"))
            .and_then(|_| self.stdin.flush())
            .map_err(|e| format!("MCP write failed: {e}"))?;

        let mut buf = String::new();
        loop {
            buf.clear();
            let n = self
                .stdout
                .read_line(&mut buf)
                .map_err(|e| format!("MCP read failed: {e}"))?;
            if n == 0 {
                return Err("MCP server closed stdout".into());
            }
            let Ok(resp) = serde_json::from_str::<Value>(&buf) else {
                continue;
            };
            if resp.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(err) = resp.get("error") {
                    return Err(format!("MCP error: {err}"));
                }
                return Ok(resp.get("result").cloned().unwrap_or(Value::Null));
            }
        }
    }

    fn initialize(&mut self) -> Result<(), String> {
        let _ = self.request(
            "initialize",
            json!({
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": { "name": "aria-native", "version": env!("CARGO_PKG_VERSION") }
            }),
        )?;
        let notif = json!({ "jsonrpc": "2.0", "method": "notifications/initialized" });
        let line = serde_json::to_string(&notif).map_err(|e| e.to_string())?;
        let _ = self.stdin.write_all(line.as_bytes());
        let _ = self.stdin.write_all(b"\n");
        let _ = self.stdin.flush();
        Ok(())
    }

    pub fn list_tools(&mut self) -> Result<Vec<Value>, String> {
        let res = self.request("tools/list", json!({}))?;
        Ok(res
            .get("tools")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    #[allow(dead_code)]
    pub fn call_tool(&mut self, name: &str, arguments: Value) -> Result<Value, String> {
        self.request(
            "tools/call",
            json!({ "name": name, "arguments": arguments }),
        )
    }
}

impl Drop for McpStdioClient {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Format a JSON-RPC 2.0 request line for MCP callers.
pub fn format_jsonrpc_request(id: u64, method: &str, params: Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": method,
        "params": params,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_valid_jsonrpc_mcp_requests() {
        let req = format_jsonrpc_request(7, "tools/call", json!({"name": "ping"}));
        assert_eq!(req["jsonrpc"], "2.0");
        assert_eq!(req["id"], 7);
        assert_eq!(req["method"], "tools/call");
        assert_eq!(req["params"]["name"], "ping");
    }
}
