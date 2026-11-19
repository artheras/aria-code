from pathlib import Path
import asyncio
import json
from datetime import datetime

import pandas as pd

from aria_code.artifacts import (
    artifact_dir,
    artifact_root,
    artifact_summary_all,
    artifact_summary,
    create_artifact,
    create_user_artifact,
    recent_artifacts,
    recent_artifacts_all,
    prune_artifacts,
    prune_artifacts_all,
    register_existing_artifact,
    slugify_topic,
    user_generated_dir,
    user_output_root,
    user_projects_dir,
    write_artifact_metadata,
)
from data_cleaner import CleanResult


def test_artifact_dir_uses_per_user_aria_code_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "aria-artifacts"))

    out = artifact_dir("reports/stock-charts", "AAPL 市场分析")

    assert artifact_root() == tmp_path / "aria-artifacts"
    assert out == tmp_path / "aria-artifacts" / "reports" / "stock-charts" / "AAPL-市场分析"
    assert out.exists()


def test_user_output_dirs_ignore_project_ariarc(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".ariarc").write_text('{"artifact_root": "aria-output"}', encoding="utf-8")
    user_root = tmp_path / "user-local"
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(user_root))
    monkeypatch.chdir(project)

    assert user_output_root() == user_root
    assert user_projects_dir() == user_root / "projects"
    assert user_generated_dir() == user_root / "generated"
    assert user_projects_dir().exists()
    assert user_generated_dir().exists()


def test_artifact_root_defaults_to_project_aria_output(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".ariarc").write_text('{"project": "demo"}', encoding="utf-8")
    monkeypatch.delenv("ARIA_ARTIFACT_ROOT", raising=False)
    monkeypatch.chdir(project)

    assert artifact_root() == project / "aria-output"


def test_slugify_topic_keeps_readable_project_names():
    assert slugify_topic("分析 AAPL / 回测策略") == "分析-AAPL-回测策略"
    assert slugify_topic("") == "general"


def test_create_artifact_uses_dated_bundle_and_metadata(monkeypatch, tmp_path: Path):
    from datetime import datetime

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "aria-artifacts"))

    record = create_artifact(
        "reports/market",
        "600362",
        "600362_market_report",
        ".html",
        timestamp=datetime(2026, 6, 12, 9, 30, 5),
    )
    write_artifact_metadata(record, {"kind": "market_report", "status": "partial"})

    assert record.path == tmp_path / "aria-artifacts" / "reports" / "market" / "600362" / "2026-06-12" / "093005_600362_market_report.html"
    assert record.metadata_path.exists()
    payload = json.loads(record.metadata_path.read_text(encoding="utf-8"))
    assert payload["status"] == "partial"
    assert payload["artifact"]["path"] == str(record.path)


def test_create_user_artifact_ignores_project_artifact_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "project-artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))

    record = create_user_artifact(
        "stock-charts",
        "603351",
        "603351_chart",
        ".html",
        timestamp=datetime(2026, 6, 19, 0, 49, 23),
    )

    assert record.path == tmp_path / "user-output" / "generated" / "stock-charts" / "603351" / "2026-06-19" / "004923_603351_chart.html"
    assert str(tmp_path / "project-artifacts") not in str(record.path)


def test_recent_artifacts_reads_metadata(monkeypatch, tmp_path: Path):
    from datetime import datetime

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "aria-artifacts"))
    record = create_artifact(
        "reports/market",
        "AAPL",
        "AAPL_market_report",
        ".html",
        timestamp=datetime(2026, 6, 12, 9, 30, 5),
    )
    record.path.write_text("<html></html>", encoding="utf-8")
    write_artifact_metadata(record, {"kind": "market_report", "status": "complete", "symbol": "AAPL"})

    items = recent_artifacts(limit=5)

    assert len(items) == 1
    assert items[0]["kind"] == "market_report"
    assert items[0]["status"] == "complete"
    assert items[0]["topic"] == "AAPL"


def test_recent_artifacts_all_includes_user_generated(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "project-artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))

    project_record = create_artifact(
        "reports/market",
        "AAPL",
        "AAPL_market_report",
        ".html",
        timestamp=datetime(2026, 6, 12, 9, 30, 5),
    )
    project_record.path.write_text("<html>AAPL</html>", encoding="utf-8")
    write_artifact_metadata(project_record, {"kind": "market_report", "status": "complete"})

    user_record = create_user_artifact(
        "stock-charts",
        "603351",
        "603351_chart",
        ".html",
        timestamp=datetime(2026, 6, 19, 0, 49, 23),
    )
    user_record.path.write_text("<html>603351</html>", encoding="utf-8")
    write_artifact_metadata(user_record, {"kind": "stock_chart", "status": "complete"})

    items = recent_artifacts_all(limit=10)

    assert {item["kind"] for item in items} >= {"market_report", "stock_chart"}
    assert any(str(tmp_path / "user-output" / "generated") in item["root"] for item in items)


def test_register_existing_artifact_indexes_generated_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "project-artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))
    output = user_generated_dir() / "四环生物综合报告.md"
    output.write_text("# 四环生物\n\n完整报告内容。", encoding="utf-8")

    record = register_existing_artifact(output, topic="000518")

    assert record is not None
    assert record.metadata_path.exists()
    items = recent_artifacts_all(limit=10)
    assert items[0]["kind"] == "markdown_document"
    assert items[0]["topic"] == "000518"
    assert items[0]["path"] == str(output.resolve())


def test_register_existing_artifact_ignores_source_files(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))
    source = tmp_path / "repo" / "app.py"
    source.parent.mkdir()
    source.write_text("print('source')", encoding="utf-8")

    assert register_existing_artifact(source) is None


def test_artifact_summary_and_prune(monkeypatch, tmp_path: Path):
    from datetime import datetime

    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "aria-artifacts"))

    records = []
    for day, symbol in enumerate(["AAPL", "MSFT", "NVDA"], start=1):
        record = create_artifact(
            "reports/market",
            symbol,
            f"{symbol}_market_report",
            ".html",
            timestamp=datetime(2026, 6, day, 9, 30, 5),
        )
        record.path.write_text(f"<html>{symbol}</html>", encoding="utf-8")
        write_artifact_metadata(record, {"kind": "market_report", "status": "complete", "symbol": symbol})
        record.raw_data_path.write_text(json.dumps({"symbol": symbol}), encoding="utf-8")
        records.append(record)

    summary = artifact_summary()
    assert summary["total"] == 3
    assert summary["by_kind"]["market_report"] == 3

    dry_run = prune_artifacts(keep=1, dry_run=True)
    assert dry_run["removed"] == 2
    assert records[-1].path.exists()
    assert records[0].path.exists()

    result = prune_artifacts(keep=1)
    assert result["removed"] == 2
    assert records[-1].path.exists()
    assert not records[0].path.exists()
    assert not records[0].metadata_path.exists()
    assert not records[0].raw_data_path.exists()
    assert len(recent_artifacts(limit=10)) == 1


def test_artifact_summary_and_prune_all_include_user_generated(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "project-artifacts"))
    monkeypatch.setenv("ARIA_USER_OUTPUT_ROOT", str(tmp_path / "user-output"))

    project_records = []
    user_records = []
    for idx, symbol in enumerate(["AAPL", "MSFT"], start=1):
        record = create_artifact(
            "reports/market",
            symbol,
            f"{symbol}_market_report",
            ".html",
            timestamp=datetime(2026, 6, idx, 9, 30, 5),
        )
        record.path.write_text(f"<html>{symbol}</html>", encoding="utf-8")
        write_artifact_metadata(record, {"kind": "market_report", "status": "complete"})
        project_records.append(record)
    for idx, symbol in enumerate(["603351", "300750"], start=1):
        record = create_user_artifact(
            "stock-charts",
            symbol,
            f"{symbol}_chart",
            ".html",
            timestamp=datetime(2026, 6, idx, 10, 0, 0),
        )
        record.path.write_text(f"<html>{symbol}</html>", encoding="utf-8")
        write_artifact_metadata(record, {"kind": "stock_chart", "status": "complete"})
        user_records.append(record)

    summary = artifact_summary_all()

    assert summary["total"] == 4
    assert summary["by_kind"]["market_report"] == 2
    assert summary["by_kind"]["stock_chart"] == 2

    dry_run = prune_artifacts_all(keep=1, dry_run=True)
    assert dry_run["removed"] == 2
    assert project_records[0].path.exists()
    assert user_records[0].path.exists()

    result = prune_artifacts_all(keep=1)
    assert result["removed"] == 2
    assert project_records[-1].path.exists()
    assert user_records[-1].path.exists()
    assert not project_records[0].path.exists()
    assert not user_records[0].path.exists()
