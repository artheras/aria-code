//! Native background process manager for long-running dev servers, builds, and watchers.
//!
//! Replaces Python `runtime/processes.py` with a lightweight thread-safe Rust registry.

#![allow(dead_code)]

use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{Read, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
};

struct ManagedProcess {
    command: String,
    child: Child,
    stdin: Option<ChildStdin>,
    output: Arc<Mutex<String>>,
}

#[derive(Default)]
pub struct ProcessManager {
    next_id: u64,
    procs: HashMap<String, ManagedProcess>,
}

impl ProcessManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn start(&mut self, workspace: &Path, command: &str) -> Result<String, String> {
        #[cfg(windows)]
        let mut cmd = {
            let mut c = Command::new("cmd");
            c.args(["/C", command]);
            c
        };
        #[cfg(not(windows))]
        let mut cmd = {
            let mut c = Command::new("sh");
            c.args(["-c", command]);
            c
        };

        let mut child = cmd
            .current_dir(workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("Failed to spawn process: {e}"))?;

        let stdin = child.stdin.take();
        let output = Arc::new(Mutex::new(String::new()));

        if let Some(mut stdout) = child.stdout.take() {
            let buf = Arc::clone(&output);
            thread::spawn(move || {
                let mut chunk = [0u8; 4096];
                while let Ok(n) = stdout.read(&mut chunk) {
                    if n == 0 {
                        break;
                    }
                    let text = String::from_utf8_lossy(&chunk[..n]);
                    if let Ok(mut guard) = buf.lock() {
                        guard.push_str(&text);
                        if guard.len() > 65_536 {
                            let keep = guard.len() - 65_536;
                            guard.drain(..keep);
                        }
                    }
                }
            });
        }
        if let Some(mut stderr) = child.stderr.take() {
            let buf = Arc::clone(&output);
            thread::spawn(move || {
                let mut chunk = [0u8; 4096];
                while let Ok(n) = stderr.read(&mut chunk) {
                    if n == 0 {
                        break;
                    }
                    let text = String::from_utf8_lossy(&chunk[..n]);
                    if let Ok(mut guard) = buf.lock() {
                        guard.push_str(&text);
                        if guard.len() > 65_536 {
                            let keep = guard.len() - 65_536;
                            guard.drain(..keep);
                        }
                    }
                }
            });
        }

        self.next_id += 1;
        let id = format!("proc-{}", self.next_id);
        self.procs.insert(
            id.clone(),
            ManagedProcess {
                command: command.to_owned(),
                child,
                stdin,
                output,
            },
        );
        Ok(id)
    }

    pub fn read_output(&mut self, id: &str) -> Result<Value, String> {
        let proc = self
            .procs
            .get_mut(id)
            .ok_or_else(|| format!("Unknown process: {id}"))?;
        let status = proc
            .child
            .try_wait()
            .map_err(|e| format!("Failed to poll process: {e}"))?;
        let text = proc.output.lock().map(|g| g.clone()).unwrap_or_default();
        Ok(json!({
            "id": id,
            "command": proc.command,
            "running": status.is_none(),
            "exit_code": status.and_then(|s| s.code()),
            "output": text,
        }))
    }

    pub fn write_input(&mut self, id: &str, data: &str) -> Result<(), String> {
        let proc = self
            .procs
            .get_mut(id)
            .ok_or_else(|| format!("Unknown process: {id}"))?;
        let stdin = proc
            .stdin
            .as_mut()
            .ok_or_else(|| format!("Process {id} stdin is closed"))?;
        stdin
            .write_all(data.as_bytes())
            .and_then(|_| stdin.flush())
            .map_err(|e| format!("Failed to write to process stdin: {e}"))
    }

    pub fn stop(&mut self, id: &str) -> Result<Value, String> {
        let mut proc = self
            .procs
            .remove(id)
            .ok_or_else(|| format!("Unknown process: {id}"))?;
        let _ = proc.child.kill();
        let status = proc.child.wait().ok();
        let text = proc.output.lock().map(|g| g.clone()).unwrap_or_default();
        Ok(json!({
            "id": id,
            "stopped": true,
            "exit_code": status.and_then(|s| s.code()),
            "output": text,
        }))
    }

    pub fn list(&mut self) -> Vec<Value> {
        let mut out = Vec::new();
        for (id, proc) in &mut self.procs {
            let status = proc.child.try_wait().ok().flatten();
            out.push(json!({
                "id": id,
                "command": proc.command,
                "running": status.is_none(),
                "exit_code": status.and_then(|s| s.code()),
            }));
        }
        out
    }
}

impl Drop for ProcessManager {
    fn drop(&mut self) {
        for proc in self.procs.values_mut() {
            let _ = proc.child.kill();
            let _ = proc.child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn spawns_reads_and_stops_background_process() {
        let tmp = tempfile::tempdir().unwrap();
        let mut mgr = ProcessManager::new();
        let id = mgr.start(tmp.path(), "echo hello_bg").unwrap();
        let mut info = Value::Null;
        for _ in 0..40 {
            thread::sleep(Duration::from_millis(50));
            info = mgr.read_output(&id).unwrap();
            if info["output"].as_str().unwrap_or("").contains("hello_bg") {
                break;
            }
        }
        assert!(info["output"].as_str().unwrap_or("").contains("hello_bg"));
        let stopped = mgr.stop(&id).unwrap();
        assert_eq!(stopped["stopped"], true);
        assert!(mgr.list().is_empty());
    }
}
