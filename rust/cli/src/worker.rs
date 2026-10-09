use serde_json::Value;
use std::{
    ffi::OsStr,
    io::{Read, Write},
    path::Path,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};

pub const MAX_MESSAGE: usize = 1_048_576;

struct Worker {
    child: Child,
    #[cfg(windows)]
    job: windows_sys::Win32::Foundation::HANDLE,
}

impl Worker {
    fn spawn(command: &mut Command) -> Result<Self, String> {
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let child = command.spawn().map_err(|e| {
            format!("Python worker: {e}. Select an installed Aria environment with --python.")
        })?;
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::{Foundation::CloseHandle, System::JobObjects::*};
            // Assignment precedes sending any tool request. Closing the job
            // terminates the worker and its children, including on cancellation.
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                let mut worker = Self { child, job };
                if job.is_null() {
                    return Err(format!(
                        "CreateJobObject: {}",
                        std::io::Error::last_os_error()
                    ));
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of_val(&info) as u32,
                ) == 0
                    || AssignProcessToJobObject(job, worker.child.as_raw_handle() as _) == 0
                {
                    let error = std::io::Error::last_os_error();
                    CloseHandle(job);
                    worker.job = std::ptr::null_mut();
                    return Err(format!("Assign worker job: {error}"));
                }
                Ok(worker)
            }
        }
        #[cfg(not(windows))]
        {
            Ok(Self { child })
        }
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        #[cfg(windows)]
        if !self.job.is_null() {
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(self.job);
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn validate_response(bytes: &[u8]) -> Result<Value, String> {
    if bytes.len() > MAX_MESSAGE {
        return Err("Python response exceeds 1 MiB".into());
    }
    let value: Value =
        serde_json::from_slice(bytes).map_err(|e| format!("Invalid Python response: {e}"))?;
    if !value.is_object()
        || value["jsonrpc"] != "2.0"
        || value["id"] != 1
        || (value.get("result").is_some() == value.get("error").is_some())
    {
        return Err(
            "Python response violates JSON-RPC version, id or result/error contract".into(),
        );
    }
    if let Some(error) = value.get("error") {
        if !error["code"].is_i64() || !error["message"].is_string() {
            return Err("Invalid JSON-RPC error".into());
        }
    } else if !value["result"]["success"].is_boolean() {
        return Err("Python tool response requires a boolean success field".into());
    }
    Ok(value)
}

pub fn call(
    python: &OsStr,
    workspace: &Path,
    timeout: Duration,
    approved: Option<&str>,
    request: &Value,
) -> Result<Value, String> {
    let workspace = workspace
        .canonicalize()
        .map_err(|e| format!("Workspace: {e}"))?;
    if !workspace.is_dir() {
        return Err("Workspace must be a directory".into());
    }
    let mut bytes = serde_json::to_vec(request).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    if bytes.len() > MAX_MESSAGE {
        return Err("Tool request exceeds 1 MiB".into());
    }
    let cancelled = Arc::new(AtomicBool::new(false));
    let signal = Arc::clone(&cancelled);
    ctrlc::set_handler(move || signal.store(true, Ordering::SeqCst))
        .map_err(|e| format!("Signal handler: {e}"))?;
    let mut command = Command::new(python);
    command
        .args([
            "-u",
            "-m",
            "aria_code.apps.cli.native_bridge",
            "--workspace",
        ])
        .arg(&workspace)
        .current_dir(&workspace)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    if let Some(name) = approved {
        command.arg("--approve-tool").arg(name);
    }
    let mut worker = Worker::spawn(&mut command)?;
    let deadline = Instant::now() + timeout;
    let stdout = worker.child.stdout.take().ok_or("Missing worker stdout")?;
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut output = Vec::new();
        let result = stdout
            .take((MAX_MESSAGE + 1) as u64)
            .read_to_end(&mut output)
            .map(|_| output);
        let _ = tx.send(result);
    });
    // Write in a separate thread too: a worker which never reads its stdin
    // must not block the timeout while a large request fills the pipe.
    let mut stdin = worker.child.stdin.take().ok_or("Missing worker stdin")?;
    thread::spawn(move || {
        let _ = stdin.write_all(&bytes);
    });
    let output = loop {
        if cancelled.load(Ordering::SeqCst) {
            return Err("Cancelled".into());
        }
        if Instant::now() >= deadline {
            return Err(format!(
                "Python tool timed out after {} ms",
                timeout.as_millis()
            ));
        }
        match rx.recv_timeout(Duration::from_millis(10)) {
            Ok(result) => break result.map_err(|e| format!("Reading Python response: {e}"))?,
            Err(mpsc::RecvTimeoutError::Timeout) => (),
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("Python reader disconnected".into())
            }
        }
    };
    let response = validate_response(&output)?;
    loop {
        if cancelled.load(Ordering::SeqCst) {
            return Err("Cancelled".into());
        }
        if let Some(status) = worker.child.try_wait().map_err(|e| e.to_string())? {
            if !status.success() {
                return Err(format!("Python worker exited with {status}"));
            }
            break;
        }
        if Instant::now() >= deadline {
            return Err("Python worker did not exit before timeout".into());
        }
        thread::sleep(Duration::from_millis(10));
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_mismatched_or_polluted_responses() {
        for response in [
            r#"{"jsonrpc":"2.0","id":2,"result":{"success":true}}"#,
            r#"{"jsonrpc":"2.0","id":1,"result":{},"error":{}}"#,
            r#"{"jsonrpc":"2.0","id":1,"result":{}}"#,
            r#"{"jsonrpc":"2.0","id":1,"error":{"code":"bad","message":"bad"}}"#,
            "welcome\n{}",
            "{}\n{}",
        ] {
            assert!(validate_response(response.as_bytes()).is_err());
        }
        assert!(validate_response(&vec![b' '; MAX_MESSAGE + 1]).is_err());
    }
    #[test]
    fn accepts_tool_result_and_rpc_error() {
        assert!(validate_response(
            br#"{"jsonrpc":"2.0","id":1,"result":{"success":false,"error":"denied"}}"#
        )
        .is_ok());
        assert!(validate_response(
            br#"{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"bad params"}}"#
        )
        .is_ok());
    }
}
