"""Exercise the actual Rust frontend and bundled worker before packaging.

No host configuration/credentials or model requests: temporary home, local
file read, application startup and orderly shutdown. Runs on all platforms.
"""
from __future__ import annotations

import json
import os
import queue
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


def main(directory: str) -> None:
    root = Path(directory).resolve()
    suffix = ".exe" if os.name == "nt" else ""
    frontend = root / ("aria-code-bin" + suffix)
    worker = root / ("aria-code-worker" + suffix)
    helper = root / "_internal" / ("aria-native" + suffix)
    with tempfile.TemporaryDirectory(prefix="aria-frozen-smoke-") as temp:
        state = Path(temp) / "state"
        state.mkdir()
        (state / "config.json").write_text(json.dumps({"model": "gemini-3.5-flash", "local_provider": "google",
            "check_for_update_on_startup": False, "auto_save_sessions": True, "task_isolation": "off"}), encoding="utf-8")
        env = dict(os.environ, ARIA_HOME=str(state), HOME=temp, USERPROFILE=temp, ARIA_OFFLINE="1", ARIA_THEME="dark")
        env.pop("ARIA_PYTHON", None)
        env.pop("ARIA_NATIVE_BINARY", None)
        env.pop("PYTHONPATH", None)
        def run(args, input=None):
            result = subprocess.run([str(a) for a in args], input=input, capture_output=True,
                                    cwd=temp, env=env, timeout=60)
            assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-4000:]
            return result.stdout.decode("utf-8")
        version = run([frontend, "--version"]).strip()
        assert version.startswith("aria-code "), version
        assert run([worker, "--aria-worker", "cli", "--version"]).strip() == version
        # Tests runtime::default_python from _internal too, plus containment,
        # worker handshake, real application imports and protocol shutdown.
        process = subprocess.Popen([str(helper), "chat", "--jsonl"], cwd=temp, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        incoming = queue.Queue()
        errors = []
        def read():
            try:
                for line in process.stdout:
                    incoming.put(json.loads(line))
            finally:
                incoming.put(None)
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=lambda: errors.append(process.stderr.read(65536)), daemon=True).start()
        def until(kind):
            while True:
                event = incoming.get(timeout=60)
                assert event is not None, errors
                if event["type"] == kind:
                    return event
        try:
            ready = until("session.ready")
            # Shutdown has a short grace period: request it AFTER startup, as
            # an actual frontend does, rather than interrupting a cold import.
            process.stdin.write(b'{"type":"shutdown","protocol":1}\x0a')
            process.stdin.flush()
            until("session.closed")
            assert process.wait(timeout=15) == 0, errors
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        assert version == "aria-code " + ready["version"]
        assert len(ready["robot"]) == 4
        (Path(temp) / "note.txt").write_text("bundled runtime file access", encoding="utf-8")
        result = json.loads(run([helper, "tool", "read_file", '{"path":"note.txt"}']))
        assert result["result"]["success"] is True, result
        print(f"{version}: Rust frontend, frozen worker, canonical robot, file tool and shutdown passed")


if __name__ == "__main__":
    main(sys.argv[1])
