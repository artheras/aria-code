"""Bounded task acceptance from workspace bytes and host-observed tool receipts.

Never executes a command or interprets the delegate's prose as evidence.
Verification covers declared predicates, not the completeness of a user's goal.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import time

MAX_BYTES = 1_048_576
MAX_CHECKS = 16
MAX_RECEIPTS = 256



class PredicateFailure(ValueError):
    """A safe, static diagnostic that may be returned without file contents."""


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PredicateFailure("Duplicate JSON object keys are not accepted")
        result[key] = value
    return result


def _json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise PredicateFailure("Non-finite JSON numbers are not accepted")
    return parsed


def _json_constant(value):
    raise PredicateFailure("Non-finite JSON numbers are not accepted")

def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("A bounded workspace-relative path is required")
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or "\\" in value
            or ".." in path.parts or not path.parts or len(path.parts) > 32):
        raise ValueError("Paths must stay inside the task workspace; use forward slashes")
    return str(path)


def validate_checks(checks) -> list[dict]:
    if not isinstance(checks, list) or len(checks) > MAX_CHECKS:
        raise ValueError(f"checks must be a list of at most {MAX_CHECKS} predicates")
    normalized = []
    for item in checks:
        if not isinstance(item, dict):
            raise ValueError("Each check must be an object")
        check = dict(item)
        kind = check.get("kind")
        allowed = {
            "artifact": {"kind", "path", "format", "min_bytes", "required_keys", "min_rows", "contains", "sha256", "changed"},
            "tool": {"kind", "tool", "path"},
            "command": {"kind", "command", "cwd"},
        }
        if not isinstance(kind, str) or kind not in allowed or check.keys() - allowed[kind]:
            raise ValueError("Unknown check kind or fields")
        if kind == "artifact":
            check["path"] = relative_path(check.get("path"))
            fmt = check.setdefault("format", "binary")
            if not isinstance(fmt, str) or fmt not in {"binary", "text", "json", "csv"}:
                raise ValueError("artifact format must be binary, text, json or csv")
            for key, limit in (("min_bytes", MAX_BYTES), ("min_rows", 10000)):
                if key in check and (type(check[key]) is not int or not 0 <= check[key] <= limit):
                    raise ValueError(f"Invalid {key}")
            if "min_rows" in check and fmt != "csv":
                raise ValueError("min_rows requires csv format")
            if "required_keys" in check:
                keys = check["required_keys"]
                if (fmt not in {"json", "csv"} or not isinstance(keys, list) or len(keys) > 32
                        or any(not isinstance(k, str) or not k or len(k) > 128 for k in keys)):
                    raise ValueError("required_keys must be bounded JSON keys or CSV column names")
            if "contains" in check and (fmt != "text" or not isinstance(check["contains"], str)
                                         or not 1 <= len(check["contains"]) <= 256):
                raise ValueError("contains requires a bounded literal text condition")
            if "sha256" in check and not re.fullmatch(r"[a-f0-9]{64}", str(check["sha256"])):
                raise ValueError("sha256 must be a lowercase SHA-256 digest")
            if "changed" in check and type(check["changed"]) is not bool:
                raise ValueError("changed must be boolean")
        elif kind == "tool":
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", str(check.get("tool", ""))):
                raise ValueError("tool name is required")
            if check["tool"] == "run_command":
                raise ValueError("Use a command check to require an observed exit code")
            if "path" in check:
                check["path"] = relative_path(check["path"])
        else:
            if not isinstance(check.get("command"), str) or not 1 <= len(check["command"].strip()) <= 4096:
                raise ValueError("command requires the exact expected command string")
            check["cwd"] = relative_path(check.get("cwd", ".")) if check.get("cwd", ".") != "." else "."
        normalized.append(check)
    return normalized


def _read_file(workspace: str, name: str) -> bytes:
    """Read a bounded regular file, rejecting symlink components.

    POSIX uses descriptor-relative O_NOFOLLOW opens. Other platforms additionally
    compare file identity before/after reading; this is not an OS sandbox.
    """
    parts = PurePosixPath(relative_path(name)).parts
    root = Path(workspace).resolve(strict=True)
    descriptors = []
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(parent)
            for part in parts[:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(parent)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        else:
            target = root
            for part in parts:
                target /= part
                if target.is_symlink():
                    raise PredicateFailure("Symlink artifacts are not accepted")
            before_path = target.stat()
            if not stat.S_ISREG(before_path.st_mode):
                raise PredicateFailure("Artifact must be a regular file")
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            opened = os.fstat(fd)
            if (before_path.st_dev, before_path.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(fd)
                raise PredicateFailure("Artifact changed while opening")
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise PredicateFailure("Artifact must be a regular file no larger than 1 MiB")
        data = bytearray()
        while len(data) <= MAX_BYTES:
            block = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        after = os.fstat(fd)
        if (len(data) > MAX_BYTES or len(data) != before.st_size
                or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise PredicateFailure("Artifact changed while reading or exceeds the size limit")
        return bytes(data)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def capture_baseline(workspace: str, checks: list[dict]) -> dict:
    result = {}
    for check in checks:
        if check["kind"] == "artifact" and check.get("changed"):
            try:
                result[check["path"]] = hashlib.sha256(_read_file(workspace, check["path"])).hexdigest()
            except FileNotFoundError:
                result[check["path"]] = None
    return result


def _relative_observation(workspace: str, value: str) -> str | None:
    try:
        root = Path(workspace).resolve()
        path = Path(value)
        return str((path if path.is_absolute() else root / path).resolve().relative_to(root)).replace(os.sep, "/")
    except (ValueError, OSError, TypeError):
        return None


def receipt_from_record(record, workspace: str) -> dict:
    """Project a trusted executor record without copying contents or command text."""
    result = record.result if isinstance(record.result, dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    receipt = {"tool": record.tool, "success": result.get("success") is True and not bool(result.get("error")),
               "observed_at": record.ended_at, "source": "runtime_tool_executor"}
    if record.params.get("path"):
        receipt["path"] = _relative_observation(workspace, record.params["path"])
    if record.tool == "run_command":
        receipt["command_sha256"] = hashlib.sha256(str(record.params.get("command", "")).encode()).hexdigest()
        receipt["cwd"] = _relative_observation(workspace, record.params.get("cwd") or workspace)
        code = data.get("exit_code", data.get("returncode"))
        receipt["exit_code"] = code if type(code) is int else None
    return receipt


def verify_checks(workspace: str, checks: list[dict], receipts: list[dict], baseline: dict | None = None, *, receipts_truncated: bool = False) -> dict:
    if not checks:
        return {"status": "not_requested", "verified": None, "checks": [], "checked_at": time.time()}
    checks = validate_checks(checks)
    reports = []
    for index, check in enumerate(checks):
        report = {"index": index, "kind": check["kind"], "passed": False}
        try:
            if check["kind"] == "artifact":
                report["path"] = check["path"]
                data = _read_file(workspace, check["path"])
                digest = hashlib.sha256(data).hexdigest()
                report.update(sha256=digest, bytes=len(data))
                if len(data) < check.get("min_bytes", 0):
                    raise PredicateFailure("Artifact is smaller than required")
                if "sha256" in check and digest != check["sha256"]:
                    raise PredicateFailure("Artifact digest differs from required digest")
                if check.get("changed") and (baseline is None or check["path"] not in baseline
                                               or baseline[check["path"]] == digest):
                    raise PredicateFailure("No verified change from the pre-execution baseline")
                fmt = check["format"]
                if fmt == "json":
                    parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_json_object, parse_float=_json_float, parse_constant=_json_constant)
                    if check.get("required_keys") and (not isinstance(parsed, dict) or not set(check["required_keys"]) <= parsed.keys()):
                        raise PredicateFailure("Required top-level JSON keys are missing")
                elif fmt == "csv":
                    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), strict=True)
                    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                        raise PredicateFailure("CSV requires unique column names")
                    if not set(check.get("required_keys", [])) <= set(reader.fieldnames):
                        raise PredicateFailure("Required CSV columns are missing")
                    count = 0
                    for row in reader:
                        if None in row or any(value is None for value in row.values()):
                            raise PredicateFailure("CSV rows do not match the header")
                        count += 1
                    report["rows"] = count
                    if count < check.get("min_rows", 0):
                        raise PredicateFailure("CSV has fewer data rows than required")
                elif fmt == "text":
                    text = data.decode("utf-8")
                    if "contains" in check and check["contains"] not in text:
                        raise PredicateFailure("Required literal text is missing")
                report["passed"] = True
            else:
                if receipts_truncated:
                    raise PredicateFailure("Incomplete execution receipt history")
                if check["kind"] == "command":
                    digest = hashlib.sha256(check["command"].encode()).hexdigest()
                    matches = [r for r in receipts if r.get("tool") == "run_command" and r.get("command_sha256") == digest and r.get("cwd") == check["cwd"]]
                else:
                    matches = [r for r in receipts if r.get("tool") == check["tool"] and ("path" not in check or r.get("path") == check["path"])]
                # Only a host receipt is evidence. A later failing invocation
                # supersedes an earlier pass of the same declared operation.
                matches = [r for r in matches if r.get("source") == "runtime_tool_executor"]
                if not matches:
                    raise PredicateFailure("No matching host-observed execution")
                last = matches[-1]
                report["receipt"] = last
                report["passed"] = last["success"] is True and (check["kind"] != "command" or last.get("exit_code") == 0)
                if not report["passed"]:
                    raise PredicateFailure("The last matching execution failed or has no zero exit code")
        except (OSError, ValueError, UnicodeError, csv.Error, RecursionError) as exc:
            # Do not return parser excerpts, file contents or unrelated paths.
            report["error"] = str(exc) if isinstance(exc, PredicateFailure) else "Missing, unsafe or invalid artifact data"
        reports.append(report)
    passed = all(r["passed"] for r in reports)
    return {"status": "verified" if passed else "failed", "verified": passed, "checks": reports,
            "checked_at": time.time(), "coverage": "declared_predicates_only"}
