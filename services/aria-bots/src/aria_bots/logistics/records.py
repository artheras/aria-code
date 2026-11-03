"""Input loading and field validation shared by the logistics analyses.

Every analysis here reports figures a 3PL may bill or negotiate on, so the
rules are strict: a value that cannot be read is an error naming the row,
never a silent zero, and an empty input is an error, never a sample dataset.
"""

from __future__ import annotations

import csv
import json
import math
import pathlib
from typing import Any


def load_records(params: dict[str, Any], key: str) -> tuple[list[dict[str, Any]], str]:
    """Records from params[key] or a CSV/JSON file, and a label for where they came from."""
    inline = params.get(key)
    file_path = params.get("file_path")
    if inline is not None and file_path:
        raise ValueError(f"Provide either file_path or {key}, not both")
    if file_path:
        path = pathlib.Path(str(file_path)).expanduser().resolve()
        if not path.is_file():
            # The OSError read "[Errno 2] No such file or directory:
            # '/private/var/…/shipments.csv'". Say which files are there.
            here = sorted(p.name for p in path.parent.glob("*") if p.suffix.lower() in (".csv", ".json"))[:6]
            hint = f" CSV/JSON files in {path.parent.name or path.parent}: {', '.join(here)}" if here else ""
            raise ValueError(f"No such file: {file_path}.{hint}")
        suffix = path.suffix.lower()
        if suffix == ".json":
            content = json.loads(path.read_text(encoding="utf-8"))
            records = content.get(key) if isinstance(content, dict) else content
        elif suffix == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as handle:
                records = list(csv.DictReader(handle))
        else:
            raise ValueError("Only CSV and JSON files are supported")
        source = f"file:{path.name}"
    else:
        records = inline
        source = "caller-supplied records"
    if not isinstance(records, list) or not records:
        raise ValueError(f"No {key} records supplied")
    for row, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"Row {row}: each record must be an object")
    return records, source


def number(record: dict[str, Any], field: str, row: int, *, required: bool = False,
           positive: bool = False) -> float | None:
    """A finite, non-negative number, or None when absent and optional."""
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"Row {row}: {field} is required")
        return None
    if isinstance(value, bool):
        raise ValueError(f"Row {row}: {field} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Row {row}: {field} must be a number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Row {row}: {field} must be finite and non-negative")
    if positive and result == 0:
        raise ValueError(f"Row {row}: {field} must be greater than zero")
    return result


def flag(record: dict[str, Any], field: str, row: int) -> bool | None:
    """A yes/no field, accepting the spellings spreadsheets produce."""
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1", "是"}:
            return True
        if normalized in {"false", "no", "n", "0", "否"}:
            return False
    raise ValueError(f"Row {row}: {field} must be true/false")


def series(record: dict[str, Any], field: str, row: int) -> list[float] | None:
    """A list of non-negative numbers, from a JSON array or a CSV cell like "3;5;2"."""
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        parts = [p for p in value.replace(",", ";").replace(" ", ";").split(";") if p]
    elif isinstance(value, list):
        parts = value
    else:
        raise ValueError(f"Row {row}: {field} must be a list of numbers")
    out: list[float] = []
    for index, part in enumerate(parts, 1):
        try:
            item = float(part)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Row {row}: {field} item {index} must be a number") from exc
        if not math.isfinite(item) or item < 0:
            raise ValueError(f"Row {row}: {field} item {index} must be finite and non-negative")
        out.append(item)
    return out


def text(record: dict[str, Any], field: str, row: int, *, required: bool = False) -> str:
    value = record.get(field)
    result = "" if value is None else str(value).strip()
    if required and not result:
        raise ValueError(f"Row {row}: {field} is required")
    return result
