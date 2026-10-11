//! Bounded persistent worker transport; the host never blocks on a pipe write.
use crate::{render, state, worker::Worker};
use serde_json::{json, Value};
use std::{
    env,
    ffi::{OsStr, OsString},
    io::{BufReader, Read, Write},
    path::Path,
    process::{Command, Stdio},
    sync::mpsc::{self, Receiver, SyncSender, TrySendError},
    thread,
};

pub struct Session {
    pub worker: Worker,
    pub events: Receiver<Result<Option<Value>, String>>,
    requests: SyncSender<Value>,
}
impl Session {
    pub fn spawn(python: &OsStr, workspace: &Path, args: &[OsString]) -> Result<Self, String> {
        let workspace = workspace
            .canonicalize()
            .map_err(|e| format!("Workspace: {e}"))?;
        if !workspace.is_dir() {
            return Err("Workspace must be a directory".into());
        }
        let root = state::root()?;
        let root = if root.is_absolute() {
            root
        } else {
            env::current_dir().map_err(|e| e.to_string())?.join(root)
        };
        let mut command = crate::runtime::command(python, "app");
        command
            .args(args)
            .current_dir(workspace)
            .env("ARIA_HOME", root)
            .env("PYTHONIOENCODING", "utf-8")
            .env("ARIA_EVENTS_FULL", "0")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut worker = Worker::spawn(&mut command)?;
        // Worker/descendant containment is assigned before Python imports Aria.
        let mut input = worker.child.stdin.take().ok_or("Missing worker stdin")?;
        input.write_all(b"\n").map_err(|e| e.to_string())?;
        let stdout = worker.child.stdout.take().ok_or("Missing worker stdout")?;
        let stderr = worker.child.stderr.take().ok_or("Missing worker stderr")?;
        let (tx, events) = mpsc::sync_channel(32);
        let errors = tx.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let result = render::read_event(&mut reader).and_then(|line| {
                    line.map(|bytes| {
                        serde_json::from_slice(&bytes).map_err(|e| format!("Runtime JSON: {e}"))
                    })
                    .transpose()
                });
                let done = !matches!(result, Ok(Some(_)));
                if tx.send(result).is_err() || done {
                    break;
                }
            }
        });
        thread::spawn(move || {
            let mut stderr = stderr;
            let mut bytes = [0; 4096];
            loop {
                match stderr.read(&mut bytes) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let text = String::from_utf8_lossy(&bytes[..n]);
                        if errors
                            .send(Ok(Some(
                                json!({"type":"output.delta","protocol":1,"text":text}),
                            )))
                            .is_err()
                        {
                            break;
                        }
                    }
                }
            }
        });
        let (requests, rx) = mpsc::sync_channel::<Value>(32);
        thread::spawn(move || {
            while let Ok(value) = rx.recv() {
                let mut bytes = match serde_json::to_vec(&value) {
                    Ok(v) => v,
                    Err(_) => break,
                };
                bytes.push(b'\n');
                if input.write_all(&bytes).is_err() || input.flush().is_err() {
                    break;
                }
            }
        });
        Ok(Self {
            worker,
            events,
            requests,
        })
    }
    pub fn send(&self, value: Value) -> Result<(), String> {
        if serde_json::to_vec(&value).map_err(|e| e.to_string())?.len()
            >= crate::worker::MAX_MESSAGE
        {
            return Err("Application request exceeds 1 MiB".into());
        }
        self.requests.try_send(value).map_err(|e| match e {
            TrySendError::Full(_) => "Runtime input queue is full".into(),
            TrySendError::Disconnected(_) => "Runtime input disconnected".into(),
        })
    }
}

// Aria's background/command tools may own separate process groups. On hard
// shutdown, stop descendants while their parentage is still available. Normal
// shutdown also closes the Python process registry before session.closed.
#[cfg(unix)]
impl Drop for Session {
    fn drop(&mut self) {
        if !matches!(self.worker.child.try_wait(), Ok(None)) {
            return;
        }
        let root = self.worker.child.id();
        unsafe {
            libc::kill(root as i32, libc::SIGSTOP);
        }
        if let Ok(mut scan) = Command::new("/bin/ps")
            .args(["-eo", "pid=,ppid="])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
        {
            let mut text = String::new();
            if let Some(out) = scan.stdout.take() {
                let _ = out.take(1_048_576).read_to_string(&mut text);
            }
            let _ = scan.kill();
            let _ = scan.wait();
            let pairs: Vec<(u32, u32)> = text
                .lines()
                .filter_map(|line| {
                    let mut fields = line.split_whitespace();
                    Some((fields.next()?.parse().ok()?, fields.next()?.parse().ok()?))
                })
                .collect();
            let mut descendants = std::collections::HashSet::from([root]);
            loop {
                let before = descendants.len();
                for &(pid, parent) in &pairs {
                    if descendants.contains(&parent) {
                        descendants.insert(pid);
                    }
                }
                if descendants.len() == before {
                    break;
                }
            }
            for pid in descendants {
                if pid != root {
                    unsafe {
                        libc::kill(pid as i32, libc::SIGKILL);
                    }
                }
            }
        }
        // Worker::drop then kills/reaps the group leader and Windows Job.
    }
}
