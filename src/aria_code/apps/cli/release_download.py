"""Bounded, resumable release transfers. Activation still verifies the digest."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request


class DownloadTimeout(TimeoutError):
    pass


@contextmanager
def _lock(path: Path):
    # Native downloads run on macOS/Linux; flock releases on process exit,
    # including Ctrl+C. Do not let two updates append to the same partial file.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if not os.fstat(fd).st_size:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another update is downloading this release; wait for it to finish") from exc
        yield
    finally:
        os.close(fd)


def download(url: str, target: Path, *, deadline_seconds: float = 300,
             attempts: int = 3, progress=None) -> None:
    """Keep a partial across attempts/runs; resume only a validated byte range.

    A server ignoring Range starts a fresh file. Invalid Content-Range, changed
    total size or contradictory validators are never appended. Read in single
    socket-buffer chunks so a trickle cannot evade the total deadline.
    """
    target = Path(target)
    part = target.with_name(target.name + ".part")
    state = target.with_name(target.name + ".part.json")
    limit = time.monotonic() + deadline_seconds
    last_progress = 0.0

    def report(received, total, complete=False):
        nonlocal last_progress
        now = time.monotonic()
        if not complete and now - last_progress < 1:
            return
        last_progress = now
        if progress:
            progress(received, total)
        elif sys.stderr.isatty():
            size = f"{received / 1048576:.1f} MiB"
            if total:
                size += f" / {total / 1048576:.1f} MiB ({min(100, received * 100 // total)}%)"
            print(f"\rDownloading {target.name}: {size}", end="\n" if complete else "", file=sys.stderr, flush=True)

    with _lock(target.with_name(target.name + ".lock")):
        try:
            metadata = json.loads(state.read_text())
        except (OSError, ValueError):
            metadata = {}
        if metadata.get("url") != url:
            part.unlink(missing_ok=True)
            metadata = {"url": url}
        for attempt in range(attempts):
            left = limit - time.monotonic()
            if left <= 0:
                raise DownloadTimeout("Download exceeded its total deadline; partial retained for the next update")
            offset = part.stat().st_size if part.is_file() else 0
            headers = {"User-Agent": "aria-code-updater", "Accept": "*/*", "Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
                if metadata.get("validator"):
                    headers["If-Range"] = metadata["validator"]
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=min(15, left)) as response:
                    response_headers = getattr(response, "headers", {})
                    status = getattr(response, "status", 200)
                    etag = response_headers.get("ETag", "")
                    validator = (etag if etag and not etag.startswith("W/") else response_headers.get("Last-Modified"))
                    total = None
                    mode = "wb"
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response_headers.get("Content-Range", ""))
                        if not match or int(match[1]) != offset or int(match[2]) < offset:
                            raise ValueError("Release server returned an invalid resume range")
                        total = int(match[3])
                        if int(match[2]) >= total or (metadata.get("total") and metadata["total"] != total):
                            raise ValueError("Release size changed during a resumed download")
                        if metadata.get("validator") and validator and metadata["validator"] != validator:
                            raise ValueError("Release changed during a resumed download")
                        mode = "ab"
                    else:
                        offset = 0
                        length = response_headers.get("Content-Length")
                        total = int(length) if length else None
                    metadata = {"url": url, "validator": validator, "total": total}
                    state.write_text(json.dumps(metadata))
                    received = offset
                    with part.open(mode) as output:
                        reader = getattr(response, "read1", response.read)
                        while True:
                            if time.monotonic() >= limit:
                                raise DownloadTimeout("Download exceeded its total deadline; partial retained")
                            chunk = reader(65536)
                            if time.monotonic() >= limit:
                                raise DownloadTimeout("Download exceeded its total deadline; partial retained")
                            if not chunk:
                                break
                            output.write(chunk)
                            received += len(chunk)
                            report(received, total)
                    if total is not None and received != total:
                        raise OSError("Incomplete release transfer")
                    os.replace(part, target)
                    state.unlink(missing_ok=True)
                    report(received, total, True)
                    return
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    # A stale/complete partial is not trusted as a completed
                    # response. Re-fetch it; the outer installer checks its hash.
                    part.unlink(missing_ok=True)
                    state.unlink(missing_ok=True)
                    metadata = {"url": url}
                elif exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise
                if attempt + 1 == attempts:
                    raise
            except DownloadTimeout:
                raise
            except ValueError:
                part.unlink(missing_ok=True)
                state.unlink(missing_ok=True)
                raise
            except (OSError, urllib.error.URLError, http.client.HTTPException):
                if attempt + 1 == attempts:
                    raise
            delay = min(2 ** attempt, max(0, limit - time.monotonic()))
            time.sleep(delay)
        raise OSError("Release download did not complete")
