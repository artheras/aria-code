#!/usr/bin/env python3
"""Record a real Aria Code session running the logistics commands.

Nothing here is drawn or scripted output: this starts the actual `aria-code`
CLI in a pseudo-terminal, types four commands at human speed, and writes every
byte the CLI prints to an asciinema v2 .cast file, with real timestamps. The
data is the eval fixtures — a sample 3PL's SKUs and waybills — copied into a
throwaway working directory, plus a two-shipper export built from them to show
the shipper-isolation refusal.

    python scripts/record_logistics_demo.py --cast /tmp/logistics.cast
    python scripts/render_logistics_demo.py --cast /tmp/logistics.cast

The CLI's own commands do the work (`/inventory`, `/carriers`); no model is
called, so the numbers on screen are the tools' and nobody else's.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "fixtures"
COLS, ROWS = 104, 40

COMMANDS = (
    "/inventory skus.csv",
    "/inventory all_skus.csv",
    "/inventory all_skus.csv --owner BETA",
    "/carriers waybills.csv",
)
# Printed by the CLI when each command has finished; the recorder waits for it.
DONE_MARKERS = ("Basis:", "different shippers", "Basis:", "Basis:")


def prepare_workspace(home: Path) -> Path:
    work = home / "acme-3pl"
    work.mkdir(parents=True)
    shutil.copy(FIXTURES / "inventory_reorder" / "skus.csv", work / "skus.csv")
    shutil.copy(FIXTURES / "freight_audit" / "waybills.csv", work / "waybills.csv")
    rows = list(csv.DictReader((work / "skus.csv").open()))
    beta = [{**r, "owner_id": "BETA", "sku": "B" + r["sku"][1:]} for r in rows]
    with (work / "all_skus.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows + beta)
    return work


def record(cast: Path, cli: str) -> None:
    # Resolved, so the CLI sees its cwd under HOME and shows "~/acme-3pl"
    # rather than a /private/var/folders/... temp path.
    home = Path(tempfile.mkdtemp(prefix="aria-demo-home-")).resolve()
    try:
        work = prepare_workspace(home)
        # A light terminal, pinned: left alone, the CLI follows the recording
        # machine's appearance and a re-record could come out in another palette.
        env = {**os.environ, "HOME": str(home), "TERM": "xterm-256color", "COLORTERM": "truecolor",
               "COLORFGBG": "0;15", "ARIA_THEME": "light",
               "COLUMNS": str(COLS), "LINES": str(ROWS), "ARIA_OFFLINE": "1"}
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(work)
            os.execve(cli, [cli], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

        start = time.time()
        events: list[list] = []
        seen = ""
        # Incremental: a read can end inside a multi-byte character (a box
        # corner, "→"), and decoding each read alone turns it into "���".
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        def pump(until: float, marker: str = "") -> bool:
            nonlocal seen
            while time.time() < until:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                text = decoder.decode(chunk)
                if not text:
                    continue
                events.append([round(time.time() - start, 4), "o", text])
                seen += text
                if marker and marker in seen:
                    return True
            return not marker

        pump(time.time() + 20, "Ask Aria")
        pump(time.time() + 1.5)
        for command, marker in zip(COMMANDS, DONE_MARKERS):
            seen = ""
            # asciicast "m" (marker) events: start, enter and end of each command,
            # so a renderer can cut one command's real output without guessing.
            events.append([round(time.time() - start, 4), "m", f"start {command}"])
            for ch in command:
                os.write(fd, ch.encode())
                pump(time.time() + 0.06)
            pump(time.time() + 0.4)
            events.append([round(time.time() - start, 4), "m", f"enter {command}"])
            os.write(fd, b"\r")
            if not pump(time.time() + 30, marker):
                raise RuntimeError(f"{command!r} did not finish (no {marker!r} in its output)")
            pump(time.time() + 1.0)
            events.append([round(time.time() - start, 4), "m", f"end {command}"])
            pump(time.time() + 1.5)
        os.kill(pid, 9)

        with cast.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": 2, "width": COLS, "height": ROWS,
                                     "timestamp": int(start), "env": {"TERM": "xterm-256color"}}) + "\n")
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cast", type=Path, required=True)
    parser.add_argument("--cli", default=shutil.which("aria-code") or str(Path(sys.prefix) / "bin" / "aria-code"))
    args = parser.parse_args()
    record(args.cast, args.cli)
    print(args.cast)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
