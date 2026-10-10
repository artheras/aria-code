//! Rust terminal output with Python retaining inference, tools and approvals.
use crate::{render, state, worker::Worker};
use std::{
    env,
    ffi::{OsStr, OsString},
    io::{BufReader, Write},
    path::Path,
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc,
    },
    thread,
    time::{Duration, Instant},
};

pub fn validate_args(args: &[OsString]) -> Result<(), String> {
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        let arg = arg.to_str().ok_or("exec arguments must be UTF-8")?;
        let (name, inline) = arg
            .split_once('=')
            .map_or((arg, None), |(k, v)| (k, Some(v)));
        match name {
            "--thinking" | "--local" | "--dangerously-skip-permissions" if inline.is_none() => (),
            "--model" | "--url" | "--allow-tools" | "--add-dir" | "--read-dir" => {
                let value = inline
                    .or_else(|| args.next().and_then(|v| v.to_str()))
                    .ok_or_else(|| format!("{name} requires a value"))?;
                if value.is_empty() || value.starts_with('-') {
                    return Err(format!("{name} requires a non-empty value"));
                }
            }
            _ => {
                return Err(format!(
                    "Unsupported exec option: {name}. Use run for the full Python CLI."
                ))
            }
        }
    }
    Ok(())
}

pub fn exec(
    python: &OsStr,
    workspace: &Path,
    prompt: &str,
    args: &[OsString],
    timeout: Duration,
    jsonl: bool,
) -> Result<i32, String> {
    validate_args(args)?;
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
    let cancelled = Arc::new(AtomicBool::new(false));
    let signal = Arc::clone(&cancelled);
    ctrlc::set_handler(move || signal.store(true, Ordering::SeqCst))
        .map_err(|e| format!("Signal handler: {e}"))?;
    let mut command = Command::new(python);
    command.args(["-u", "-c",
        // Wait for host acknowledgement before importing the runtime. On
        // Windows this closes the race between execution and Job assignment.
        "import sys; sys.stdin.buffer.read(1) == b'\\n' or sys.exit(2); from aria_code.apps.cli.main import main; main()"])
        .args(["--format=jsonl", "--quiet", "--no-banner"])
        .arg(format!("--prompt={prompt}"))
        .args(args)
        .current_dir(&workspace)
        .env("ARIA_HOME", root)
        .env("ARIA_EVENTS_STREAM", "1")
        .env("ARIA_EVENTS_FULL", "0")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit());
    let mut worker = Worker::spawn(&mut command)?;
    let deadline = Instant::now() + timeout;
    let mut stdin = worker.child.stdin.take().ok_or("Missing worker stdin")?;
    stdin
        .write_all(b"\n")
        .map_err(|e| format!("Starting runtime: {e}"))?;
    drop(stdin); // Headless runtime sees EOF; it cannot wait for an input prompt.
    let stdout = worker.child.stdout.take().ok_or("Missing worker stdout")?;
    let (tx, rx) = mpsc::sync_channel(8); // Backpressure also bounds queued memory.
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let result = render::read_event(&mut reader);
            let done = !matches!(result, Ok(Some(_)));
            if tx.send(result).is_err() || done {
                break;
            }
        }
    });
    if !jsonl {
        eprintln!("[starting] Aria runtime (Ctrl+C to cancel)");
    }
    // Output may be a pipe whose consumer has stopped reading. Keep process
    // supervision on the main thread so blocked rendering cannot disable the
    // deadline or Ctrl+C cleanup.
    let (done_tx, done_rx) = mpsc::channel();
    thread::spawn(move || {
        let result = (|| {
            let mut renderer = render::Renderer::new(jsonl);
            let mut out = std::io::stdout().lock();
            let mut diagnostics = std::io::stderr();
            loop {
                match rx.recv() {
                    Ok(Ok(Some(bytes))) => renderer.consume(&bytes, &mut out, &mut diagnostics)?,
                    Ok(Ok(None)) => break,
                    Ok(Err(error)) => return Err(error),
                    Err(_) => return Err("Event reader disconnected".into()),
                }
            }
            renderer.finish(&mut out)
        })();
        let _ = done_tx.send(result);
    });
    let turn_code = loop {
        interrupted(&cancelled, deadline, timeout)?;
        match done_rx.recv_timeout(Duration::from_millis(10)) {
            Ok(result) => break result?,
            Err(mpsc::RecvTimeoutError::Timeout) => (),
            Err(_) => return Err("Event renderer disconnected".into()),
        }
    };
    loop {
        interrupted(&cancelled, deadline, timeout)?;
        if let Some(status) = worker.child.try_wait().map_err(|e| e.to_string())? {
            if !status.success() {
                eprintln!("aria-native: Python runtime exited with {status}");
                return Ok(status.code().filter(|code| *code != 0).unwrap_or(1));
            }
            return Ok(turn_code);
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn interrupted(cancelled: &AtomicBool, deadline: Instant, timeout: Duration) -> Result<(), String> {
    if cancelled.load(Ordering::SeqCst) {
        return Err("Cancelled".into());
    }
    if Instant::now() >= deadline {
        return Err(format!(
            "Python turn timed out after {} ms",
            timeout.as_millis()
        ));
    }
    Ok(())
}
