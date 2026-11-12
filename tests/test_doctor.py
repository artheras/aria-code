import pathlib

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import shutil

from aria_code import doctor
from aria_code.doctor import analyze_python_drift, format_doctor_plain, integration_checks, npm_runtime_checks, provider_health_checks, provider_health_summary, run_doctor
from aria_code.packages.aria_services.provider_health import summarize_provider_health


def test_run_doctor_reports_core_checks(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    report = run_doctor({"data_sharing": False, "feedback_upload": False}, cwd=tmp_path)

    names = {check.name: check for check in report.checks}
    assert names["python"].status == "ok"
    assert names["artifact_root"].status == "ok"
    assert names["artifact_inventory"].status == "warn"
    assert "0 artifacts" in names["artifact_inventory"].detail
    assert names["privacy"].detail == "data_sharing=False, feedback_upload=False"
    # A check that did not run is not a warning about the thing it checks —
    # otherwise /doctor without --network always shows a warning that means
    # nothing, and a cloud-only install can never report green.
    assert names["ollama"].status == "skip"
    assert "not requested" in names["ollama"].detail


def test_format_doctor_plain_includes_summary(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    text = format_doctor_plain(run_doctor({}, cwd=tmp_path))

    assert text.startswith("Aria Code doctor")
    assert "artifact_root" in text
    assert "passed" in text


def test_npm_runtime_checks_report_custom_paths(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    venv_bin = runtime / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    config.mkdir()
    cache.mkdir()
    (runtime / "aria_cli.py").write_text("print('aria')\n", encoding="utf-8")
    (venv_bin / "python").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    (runtime / ".npm-install-info.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("ARIA_HOME", str(runtime))
    monkeypatch.setenv("ARIA_CONFIG_DIR", str(config))
    monkeypatch.setenv("ARIA_CACHE_DIR", str(cache))

    checks = {check.name: check for check in npm_runtime_checks()}

    assert checks["npm_runtime:install_dir"].status == "ok"
    assert str(runtime) in checks["npm_runtime:install_dir"].detail
    assert "source=env:ARIA_HOME" in checks["npm_runtime:install_dir"].detail
    assert checks["npm_runtime:aria_cli"].status == "ok"
    assert checks["npm_runtime:venv"].status == "ok"
    assert checks["npm_runtime:install_info"].status == "ok"
    assert checks["npm_runtime:config_dir"].status == "ok"
    assert checks["npm_runtime:cache_dir"].status == "ok"


def test_npm_runtime_checks_treat_source_checkout_as_valid(monkeypatch, tmp_path):
    runtime = tmp_path / "missing-runtime"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    config.mkdir()
    cache.mkdir()
    (tmp_path / "aria_cli.py").write_text("print('aria')\n", encoding="utf-8")
    (venv_bin / "python").write_text("#!/usr/bin/env python\n", encoding="utf-8")

    monkeypatch.setenv("ARIA_HOME", str(runtime))
    monkeypatch.setenv("ARIA_CONFIG_DIR", str(config))
    monkeypatch.setenv("ARIA_CACHE_DIR", str(cache))
    monkeypatch.delenv("ARIA_CODE_HOME", raising=False)
    monkeypatch.delenv("npm_config_aria_code_home", raising=False)
    monkeypatch.delenv("npm_config_aria_home", raising=False)
    monkeypatch.delenv("npm_config_ariacode_home", raising=False)

    checks = {check.name: check for check in npm_runtime_checks(cwd=tmp_path)}

    assert checks["npm_runtime:install_dir"].status == "warn"
    assert "source_checkout" in checks["npm_runtime:install_dir"].detail
    assert checks["npm_runtime:aria_cli"].status == "ok"
    assert checks["npm_runtime:venv"].status == "ok"


def test_provider_health_checks_report_cooldown_and_auth_failures():
    checks = provider_health_checks([
        {
            "provider": "yfinance",
            "status": "rate_limited",
            "failures": 2,
            "cooldown_active": True,
            "cooldown_remaining_seconds": 42,
            "last_error_category": "rate_limited",
            "last_error": "429 Too Many Requests",
        },
        {
            "provider": "finnhub",
            "status": "auth",
            "failures": 1,
            "cooldown_active": False,
            "last_error_category": "auth",
            "last_error": "invalid api key",
        },
        {
            "provider": "akshare",
            "status": "ok",
            "failures": 0,
            "cooldown_active": False,
        },
    ])

    by_name = {check.name: check for check in checks}
    assert by_name["data_provider:yfinance"].status == "warn"
    assert "cooldown=42s" in by_name["data_provider:yfinance"].detail
    assert "switch provider" in by_name["data_provider:yfinance"].suggestion
    assert by_name["data_provider:finnhub"].status == "err"
    assert "API key" in by_name["data_provider:finnhub"].suggestion
    assert by_name["data_provider:akshare"].status == "ok"


def test_provider_health_checks_warn_without_calls():
    checks = provider_health_checks([])

    assert checks[0].name == "data_provider_health"
    assert checks[0].status == "warn"
    assert "no provider calls" in checks[0].detail


def test_provider_health_summary_compacts_state():
    summary = provider_health_summary([
        {"provider": "yfinance", "status": "ok", "cooldown_active": False, "last_success_at": 100.0},
        {"provider": "finnhub", "status": "rate_limited", "cooldown_active": True, "cooldown_remaining_seconds": 42, "last_error_category": "rate_limited"},
        {"provider": "akshare", "status": "auth", "cooldown_active": False, "last_error_category": "auth"},
    ])

    assert summary.name == "provider_health_summary"
    assert summary.status == "err"
    assert "3 providers" in summary.detail
    assert "1 ok" in summary.detail
    assert "1 cooldown" in summary.detail
    assert "Fix API keys first" in summary.suggestion


def test_summarize_provider_health_builds_structured_snapshot():
    summary = summarize_provider_health([
        {"provider": "yfinance", "status": "ok", "cooldown_active": False},
        {"provider": "finnhub", "status": "rate_limited", "cooldown_active": True, "last_error_category": "rate_limited"},
    ])

    payload = summary.to_dict()
    assert payload["schema"] == "aria.provider_health_summary.v1"
    assert payload["total"] == 2
    assert payload["cooldown"] == 1
    assert payload["status"] == "warn"
    assert payload["providers"] == ["yfinance", "finnhub"]


def test_pyproject_packages_the_whole_aria_code_tree():
    """These modules must ship. They used to be root-level entries in py-modules;
    since the src-layout migration they live in src/aria_code and are covered by
    the packages.find include list instead."""
    with open("pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)

    include = set(data["tool"]["setuptools"]["packages"]["find"]["include"])
    assert "aria_code*" in include

    package_root = pathlib.Path("src") / "aria_code"
    for module in ("aria_cli", "doctor", "data_service", "artifacts", "report_generator"):
        assert (package_root / f"{module}.py").exists(), f"{module}.py missing from {package_root}"


def test_python_drift_ok_when_versions_match_and_home_exists():
    check = analyze_python_drift("3.14.6", "3.14.6", home_exists=True)
    assert check.name == "python_venv"
    assert check.status == "ok"


def test_python_drift_warns_on_minor_version_mismatch():
    check = analyze_python_drift("3.12.4", "3.14.6", home_exists=True)
    assert check.status == "warn"
    assert "3.12.4" in check.detail and "3.14.6" in check.detail
    assert "install.sh --rebuild" in check.suggestion


def test_python_drift_patch_difference_is_not_drift():
    # Patch upgrades (3.14.1 → 3.14.6) are routine; only minor drift warns.
    check = analyze_python_drift("3.14.1", "3.14.6", home_exists=True)
    assert check.status == "ok"


def test_python_drift_errs_when_base_interpreter_removed():
    # Homebrew upgraded/removed the keg the venv was built against.
    check = analyze_python_drift("3.13.2", "3.13.2", home_exists=False)
    assert check.status == "err"
    assert "base interpreter is gone" in check.detail
    assert "install.sh --rebuild" in check.suggestion


def test_run_doctor_includes_python_venv_check_inside_venv(monkeypatch, tmp_path):
    # This test suite itself runs inside the project venv, so the drift check
    # must be present and healthy.
    import sys
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        import pytest
        pytest.skip("not running inside a venv")
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    report = run_doctor({}, cwd=tmp_path)
    names = {c.name: c for c in report.checks}
    assert "python_venv" in names
    assert names["python_venv"].status == "ok"


def test_run_doctor_context_check_states(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    base = {"estimated_tokens": 100, "max_tokens": 1000, "fill_pct": 10,
            "message_count": 4, "threshold": 0.78}
    ok = run_doctor({}, cwd=tmp_path, context_stats={**base, "fill_ratio": 0.10})
    warn = run_doctor({}, cwd=tmp_path, context_stats={**base, "fill_ratio": 0.80, "fill_pct": 80})
    err = run_doctor({}, cwd=tmp_path, context_stats={**base, "fill_ratio": 0.96, "fill_pct": 96})
    def ctx(report):
        return {c.name: c for c in report.checks}["context"]
    assert ctx(ok).status == "ok" and ctx(ok).suggestion == ""
    assert ctx(warn).status == "warn" and "/compact" in ctx(warn).suggestion
    assert ctx(err).status == "err"
    assert "100/1000 tokens" in ctx(ok).detail


def test_run_doctor_omits_context_check_without_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    report = run_doctor({}, cwd=tmp_path)
    assert "context" not in {c.name for c in report.checks}


def test_context_health_snapshot_shape():
    from packages.aria_services.context import context_health_snapshot
    snap = context_health_snapshot(
        [{"role": "user", "content": "x" * 300}], max_tokens=2048, threshold=0.78,
    )
    assert snap["estimated_tokens"] == 100
    # ContextPolicy.normalized() floors max_tokens at 1024; 2048 passes through.
    assert snap["max_tokens"] == 2048
    assert snap["message_count"] == 1
    assert 0 < snap["fill_ratio"] < 1


def test_integration_checks_ffmpeg_ok_when_on_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:ffmpeg"].status == "ok"


def test_integration_checks_ffmpeg_warns_when_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:ffmpeg"].status == "warn"
    assert "ffmpeg" in checks["integration:ffmpeg"].suggestion.lower()


def test_integration_checks_video_analysis_modules(monkeypatch):
    def fake_has_module(name):
        return name in ("faster_whisper", "cv2")

    monkeypatch.setattr(doctor, "_has_module", fake_has_module)
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:faster_whisper"].status == "ok"
    assert checks["integration:opencv"].status == "ok"
    assert checks["integration:local_image_gen"].status == "warn"


def test_integration_checks_local_image_gen_needs_both_modules(monkeypatch):
    def fake_has_module(name):
        return name == "diffusers"  # torch missing

    monkeypatch.setattr(doctor, "_has_module", fake_has_module)
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:local_image_gen"].status == "warn"


def test_integration_checks_provider_key_present_reports_ok(monkeypatch):
    monkeypatch.setattr(doctor, "_provider_key_present", lambda module_name, key_fn: True)
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:openai_images"].status == "ok"
    assert checks["integration:kling"].status == "ok"
    assert checks["integration:runway"].status == "ok"
    assert checks["integration:figma"].status == "ok"


def test_integration_checks_provider_key_missing_reports_warn_with_setup_hint(monkeypatch):
    monkeypatch.setattr(doctor, "_provider_key_present", lambda module_name, key_fn: False)
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:kling"].status == "warn"
    assert "/apikey set kling" in checks["integration:kling"].suggestion


def test_provider_key_present_handles_tuple_keys(monkeypatch):
    import importlib
    import types

    fake_mod = types.SimpleNamespace(_keys=lambda: ("ak", "sk"))
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_mod)
    assert doctor._provider_key_present("kling_video_client", "_keys") is True


def test_provider_key_present_tuple_with_missing_half_is_false(monkeypatch):
    import importlib
    import types

    fake_mod = types.SimpleNamespace(_keys=lambda: ("ak", ""))
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_mod)
    assert doctor._provider_key_present("kling_video_client", "_keys") is False


def test_provider_key_present_swallows_import_errors():
    assert doctor._provider_key_present("nonexistent_module_xyz", "_api_key") is False


def test_integration_checks_canva_connected(monkeypatch):
    monkeypatch.setattr(doctor, "_provider_key_present", lambda module_name, key_fn: False)
    from aria_code.clients import canva_client
    monkeypatch.setattr(canva_client, "_load_canva_config", lambda: {"access_token": "tok"})
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:canva"].status == "ok"


def test_integration_checks_canva_not_connected(monkeypatch):
    monkeypatch.setattr(doctor, "_provider_key_present", lambda module_name, key_fn: False)
    from aria_code.clients import canva_client
    monkeypatch.setattr(canva_client, "_load_canva_config", lambda: {})
    checks = {c.name: c for c in integration_checks()}
    assert checks["integration:canva"].status == "warn"
    assert "/canva connect" in checks["integration:canva"].suggestion


def test_integration_checks_included_in_run_doctor(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    report = run_doctor({}, cwd=tmp_path)
    names = {c.name for c in report.checks}
    assert "integration:ffmpeg" in names
    assert "integration:openai_images" in names


def test_ollama_absence_is_only_a_warning_when_this_install_uses_it(monkeypatch, tmp_path):
    """Ollama is one route to a model, not the route.

    Reporting its absence as a warning meant a healthy cloud-only install could
    never show green, which is how a signal stops being read.
    """
    from aria_code.doctor import _check_ollama

    def _unreachable(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.build_opener", lambda *a, **k: type(
        "O", (), {"open": staticmethod(_unreachable)})())

    required = _check_ollama("http://localhost:11434", required=True)
    optional = _check_ollama("http://localhost:11434", required=False)

    assert required.status == "warn"
    assert optional.status == "skip"


def test_skipped_checks_do_not_colour_the_overall_status():
    from aria_code.doctor import DoctorCheck, DoctorReport

    report = DoctorReport(checks=[
        DoctorCheck(name="python", status="ok"),
        DoctorCheck(name="ollama", status="skip", detail="not this install's provider"),
    ])
    assert report.status == "ok"
    assert report.warnings == 0
