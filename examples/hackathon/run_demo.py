#!/usr/bin/env python3
"""Run a fresh Aria/Nemotron task; credentials stay in the parent proxy only."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import ssl

MODEL = "nvidia/nemotron-3-super-120b-a12b"
ROOT = Path(__file__).resolve().parents[2]
PROMPT = """Read inventory.csv. Using only Python standard library, implement inventory.py
with plan_inventory(rows). Return a list with sku, available=on_hand-reserved,
projected=available+inbound, reorder=max(0,reorder_point-projected).
Accept integer values and decimal integer strings. Reject negative or fractional
quantities, reserved greater than on_hand, and duplicate SKUs with ValueError.
CLI: python3 inventory.py inventory.csv writes result.json as that JSON list.
Write meaningful unittest cases in test_inventory.py. Actually execute
python3 -m unittest -v and python3 inventory.py inventory.csv.
Write REPORT.md with actual results and limitations: synthetic inventory simulation,
no physical robot connected. Read back the report and result.json before finishing.
Do not access network, credentials, other directories, or install dependencies.
Do not write completion marker files. Finish once files exist and checks pass."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path,
                        help="New directory; existing directories are never overwritten")
    parser.add_argument("--check", action="store_true", help="Check setup without inference or writes")
    args = parser.parse_args()
    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if not key:
        parser.error("Set NEBIUS_API_KEY in your shell; do not put it in arguments or files")
    cli = ROOT / "src/aria_code/aria_cli.py"
    if args.workspace.exists():
        parser.error("Choose a new workspace directory")
    if args.check:
        print(json.dumps({"model": MODEL, "credential_present": True,
                          "source_present": cli.is_file(), "inference_run": False}))
        return 0 if cli.is_file() else 1
    ws = args.workspace.resolve()
    ws.mkdir(parents=True)
    (ws / "inventory.csv").write_bytes(Path(__file__).with_name("inventory.csv").read_bytes())
    trace = []
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except ImportError:
        pass

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer demo-local-only":
                self.send_error(403)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            request = json.loads(body)
            entry = {"model": request.get("model"), "tools_offered": len(request.get("tools", []))}
            trace.append(entry)
            if request.get("model") != MODEL:
                self.send_error(400, "Unexpected model")
                return
            req = urllib.request.Request(
                "https://api.tokenfactory.nebius.com/v1/chat/completions", data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, context=context, timeout=120) as response:
                    entry["http_status"] = response.status
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except Exception as exc:
                entry["error_type"] = type(exc).__name__
                self.close_connection = True

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="aria-judge-home-") as home:
            config = {"local_provider": "custom", "backend_chat": False, "model": MODEL,
                      "custom_model": MODEL, "custom_endpoint": f"http://127.0.0.1:{server.server_port}/v1",
                      "acceptance_gate": True, "acceptance_commands": ["python3 -m unittest -v", "python3 inventory.py inventory.csv"],
                      "sandbox_mode": "workspace-write", "auto_update": False,
                      "check_for_update_on_startup": False}
            Path(home, "config.json").write_text(json.dumps(config))
            env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
            env.update(ARIA_HOME=home, LOCAL_LLM_API_KEY="demo-local-only",
                       PYTHONPATH=str(ROOT / "src"), PYTHONUNBUFFERED="1")
            command = [sys.executable, str(cli), "--no-banner", "-C", str(ws),
                                   "--allow-tools", "write_file,edit_file,run_command",
                                   "-p", PROMPT, "--format", "jsonl"]
            proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=420)
            # An independent failure is feedback, never a successful completion.
            # Keep the failed attempt and permit one bounded model repair.
            check = subprocess.run([sys.executable, str(Path(__file__).with_name("verify.py")), str(ws)],
                                   capture_output=True, text=True)
            (ws / "attempt-1.jsonl").write_text(proc.stdout.replace(key, "[REDACTED]").replace(str(ws), "<demo-workspace>"))
            (ws / "attempt-1-acceptance.txt").write_text(check.stdout + check.stderr)
            repaired = False
            if proc.returncode == 0 and check.returncode != 0:
                feedback = (check.stdout + check.stderr)[-10000:].replace(str(ws), "<demo-workspace>")
                command[command.index("-p") + 1] = (
                    "Repair the existing inventory.py and test_inventory.py. Independent acceptance failed:\n"
                    + feedback + "\nReject fractional numeric values in EVERY quantity field; int(1.5) truncation is invalid. "
                    "Read the code, fix the implementation, add regression tests, execute unittest and CLI, "
                    "update REPORT.md with actual results, read it back, then finish. Do not create extra summary files. "
                    "No network, installation or credentials access. This is a synthetic simulation with no robot.")
                proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=420)
                repaired = True
            raw = proc.stdout.replace(key, "[REDACTED]").replace(str(ws), "<demo-workspace>")
            (ws / "runtime.jsonl").write_text(raw)
            (ws / "runtime-stderr.txt").write_text(proc.stderr.replace(key, "[REDACTED]"))
            events = []
            for line in raw.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
            completed = [e for e in events if e.get("type") == "turn.completed"]
            success = bool(proc.returncode == 0 and completed and completed[-1].get("success")
                           and completed[-1].get("acceptance", {}).get("verified"))
            if success:
                check = subprocess.run([sys.executable, str(Path(__file__).with_name("verify.py")), str(ws)],
                                       capture_output=True, text=True)
                (ws / "independent-acceptance.txt").write_text(check.stdout + check.stderr)
                success = check.returncode == 0
            manifest = {"success": success, "repair_attempted": repaired, "model": MODEL, "duration_seconds": round(time.monotonic()-started, 2),
                        "requests": trace, "usage_measured": False,
                        "files": {name: hashlib.sha256((ws/name).read_bytes()).hexdigest()
                                  for name in ("inventory.py", "test_inventory.py", "result.json", "REPORT.md") if (ws/name).is_file()}}
            (ws / "evidence.json").write_text(json.dumps(manifest, indent=2))
            print(json.dumps(manifest, indent=2))
            return 0 if success else 1
    except subprocess.TimeoutExpired:
        (ws / "evidence.json").write_text(json.dumps({"success": False, "error": "timeout", "requests": trace}, indent=2))
        print("Task timed out; inspect evidence.json", file=sys.stderr)
        return 1
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
