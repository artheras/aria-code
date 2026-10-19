"""Tests for packages/aria_mcp/server.py — the safety-critical properties:
the _WRITE_SAFE allowlist boundary, the video-generation cost-confirm gate
(submission is the moment real money is spent, so it must hard-refuse
without confirmed=true), and the two-gate chat-confirmed trade execution
path (aria.broker.confirm_order): it must hard-refuse unless the broker was
separately opted in via /trade allow-chat-confirm at the terminal AND the
call itself passes confirmed=true.
"""
from __future__ import annotations

import importlib.util
import pytest

from aria_code.packages.aria_mcp.server import (
    TOOLS,
    _call_allocation_chart,
    _call_broker_confirm_order,
    _call_comparison_chart,
    _call_edit_image,
    _call_generate_image,
    _call_indicator_chart,
    _call_skill_get,
    _call_skill_list,
    _call_video_generate_submit,
)


# 图表渲染需要 mplfinance/matplotlib（charts extra）。CI 的 test workflow 只装
# .[cn,dev]，缺依赖时 report_generator 的绘图分支返回 success=False，用例断言
# `result["success"] is True` 便 FAILED——而不是按该 workflow 声明的契约 SKIPPED。
# 只标记真正绘图的用例；同文件其余用例不受影响。
requires_charts = pytest.mark.skipif(
    importlib.util.find_spec("mplfinance") is None,
    reason="需要 charts extra（pip install 'aria-code[charts]'）",
)


def test_no_skill_backed_exposures_reach_tools_list():
    # skill: targets aren't clean callables in this server — must never
    # silently appear as a callable tool.
    from packages.aria_mcp.bridge import default_exposures

    tool_names = {t["name"] for t in TOOLS}
    for exposure in default_exposures():
        if exposure.target.startswith("skill:"):
            assert exposure.name not in tool_names


def test_execute_order_preview_is_not_bound_at_module_level():
    # execute_order_preview is real and reachable now (via
    # _call_broker_confirm_order's gated path), but only as a function-local
    # import inside that one handler — never a module-level attribute a
    # careless import elsewhere in this file could pick up unguarded.
    # Must be the module the _call_* handlers at the top of this file come
    # from — patching the bare `packages.aria_mcp.server` copy left _get_broker
    # real, so confirm_order failed at broker lookup before reaching the gate
    # these tests are about.
    import aria_code.packages.aria_mcp.server as server_mod

    assert not hasattr(server_mod, "execute_order_preview")
    assert "_call_broker_confirm_order" in dir(server_mod)


@pytest.mark.asyncio
async def test_confirm_order_refuses_without_confirmed():
    result = await _call_broker_confirm_order({"preview_id": "tp_x", "broker_id": "some_broker"})
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_confirm_order_refuses_with_confirmed_false():
    result = await _call_broker_confirm_order(
        {"preview_id": "tp_x", "broker_id": "some_broker", "confirmed": False}
    )
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_confirm_order_refuses_missing_preview_id():
    result = await _call_broker_confirm_order({"confirmed": True})
    assert result["success"] is False
    assert "preview_id" in result["error"]


@pytest.mark.asyncio
async def test_confirm_order_refuses_when_chat_confirm_not_enabled(monkeypatch):
    import aria_code.packages.aria_mcp.server as server_mod

    class _FakeBroker:
        broker_id = "some_broker"

    monkeypatch.setattr(server_mod, "_get_broker", lambda broker_id="": _FakeBroker())
    monkeypatch.setattr("brokers.config.is_chat_confirm_enabled", lambda broker_id: False)

    result = await _call_broker_confirm_order(
        {"preview_id": "tp_x", "broker_id": "some_broker", "confirmed": True}
    )
    assert result["success"] is False
    assert "allow-chat-confirm" in result["error"]


@pytest.mark.asyncio
async def test_confirm_order_executes_only_when_both_gates_pass(monkeypatch):
    import aria_code.packages.aria_mcp.server as server_mod

    class _FakeBroker:
        broker_id = "some_broker"

    monkeypatch.setattr(server_mod, "_get_broker", lambda broker_id="": _FakeBroker())
    monkeypatch.setattr("brokers.config.is_chat_confirm_enabled", lambda broker_id: True)

    called = {}

    def fake_execute(broker, preview_id, *, confirmed, source):
        called["preview_id"] = preview_id
        called["confirmed"] = confirmed
        called["source"] = source
        return {"success": True, "order_id": "o1"}

    monkeypatch.setattr("brokers.trading.execute_order_preview", fake_execute)

    result = await _call_broker_confirm_order(
        {"preview_id": "tp_x", "broker_id": "some_broker", "confirmed": True}
    )
    assert result["success"] is True
    assert called["preview_id"] == "tp_x"
    assert called["confirmed"] is True
    assert called["source"] == "chat_mcp"


@pytest.mark.asyncio
async def test_generate_submit_refuses_without_confirmed():
    result = await _call_video_generate_submit({"provider": "kling", "prompt": "a sunset"})
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_generate_submit_refuses_with_confirmed_false():
    result = await _call_video_generate_submit(
        {"provider": "kling", "prompt": "a sunset", "confirmed": False}
    )
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_generate_submit_rejects_unknown_provider_even_when_confirmed():
    result = await _call_video_generate_submit(
        {"provider": "not-a-real-provider", "prompt": "a sunset", "confirmed": True}
    )
    assert result["success"] is False
    assert "provider" in result["error"]


@pytest.mark.asyncio
async def test_generate_submit_calls_provider_only_when_confirmed(monkeypatch):
    import kling_video_client

    called = {}

    def fake_submit_video(prompt, **kwargs):
        called["prompt"] = prompt
        called["kwargs"] = kwargs
        return {"success": True, "task_id": "t1", "provider": "kling"}

    monkeypatch.setattr(kling_video_client, "submit_video", fake_submit_video)
    result = await _call_video_generate_submit(
        {"provider": "kling", "prompt": "a sunset", "confirmed": True, "duration": 5}
    )
    assert result["success"] is True
    assert called["prompt"] == "a sunset"


@pytest.mark.asyncio
async def test_generate_image_refuses_without_confirmed():
    result = await _call_generate_image({"prompt": "a poster"})
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_generate_image_refuses_with_confirmed_false():
    result = await _call_generate_image({"prompt": "a poster", "confirmed": False})
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_generate_image_calls_client_only_when_confirmed(monkeypatch):
    import openai_image_client

    called = {}

    def fake_generate_image(prompt, **kwargs):
        called["prompt"] = prompt
        called["kwargs"] = kwargs
        return {"success": True, "path": "/tmp/out.png"}

    monkeypatch.setattr(openai_image_client, "generate_image", fake_generate_image)
    result = await _call_generate_image({"prompt": "a poster", "confirmed": True})
    assert result["success"] is True
    assert called["prompt"] == "a poster"
    assert called["kwargs"]["confirmed"] is True


@pytest.mark.asyncio
async def test_edit_image_refuses_without_confirmed():
    result = await _call_edit_image({"image_path": "/tmp/x.jpg", "prompt": "make it duotone"})
    assert result["success"] is False
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_edit_image_calls_client_only_when_confirmed(monkeypatch):
    import openai_image_client

    called = {}

    def fake_edit_image(image_path, prompt, **kwargs):
        called["image_path"] = image_path
        called["kwargs"] = kwargs
        return {"success": True, "path": "/tmp/out.png"}

    monkeypatch.setattr(openai_image_client, "edit_image", fake_edit_image)
    result = await _call_edit_image({"image_path": "/tmp/x.jpg", "prompt": "make it duotone", "confirmed": True})
    assert result["success"] is True
    assert called["image_path"] == "/tmp/x.jpg"
    assert called["kwargs"]["confirmed"] is True


@pytest.mark.asyncio
async def test_edit_image_omits_mask_path_by_default(monkeypatch):
    import openai_image_client

    called = {}

    def fake_edit_image(image_path, prompt, **kwargs):
        called["kwargs"] = kwargs
        return {"success": True, "path": "/tmp/out.png"}

    monkeypatch.setattr(openai_image_client, "edit_image", fake_edit_image)
    await _call_edit_image({"image_path": "/tmp/x.jpg", "prompt": "make it duotone", "confirmed": True})
    assert called["kwargs"]["mask_path"] is None


@pytest.mark.asyncio
async def test_edit_image_passes_through_mask_path_for_inpainting(monkeypatch):
    import openai_image_client

    called = {}

    def fake_edit_image(image_path, prompt, **kwargs):
        called["kwargs"] = kwargs
        return {"success": True, "path": "/tmp/out.png"}

    monkeypatch.setattr(openai_image_client, "edit_image", fake_edit_image)
    await _call_edit_image({
        "image_path": "/tmp/x.jpg", "prompt": "fill in the sky", "confirmed": True,
        "mask_path": "/tmp/mask.png",
    })
    assert called["kwargs"]["mask_path"] == "/tmp/mask.png"


# ── New chart tools: indicator/comparison/allocation ────────────────────────

def _fake_ohlcv_df():
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2025-01-01", periods=60, freq="D")
    close = 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 60))
    return pd.DataFrame(
        {"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1000},
        index=idx,
    )


@pytest.mark.asyncio
async def test_indicator_chart_requires_symbol():
    result = await _call_indicator_chart({})
    assert result["success"] is False
    assert "symbol" in result["error"]


@pytest.mark.asyncio
@requires_charts
async def test_indicator_chart_writes_artifact_on_success(monkeypatch, tmp_path):
    df = _fake_ohlcv_df()
    import report_generator
    monkeypatch.setattr(report_generator, "_fetch_report_data_sync", lambda symbol: (df, None, {}))

    fake_path = tmp_path / "out.png"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("artifacts.create_user_artifact", lambda *a, **kw: type("A", (), {"path": fake_path})())
        result = await _call_indicator_chart({"symbol": "aapl"})
    assert result["success"] is True
    assert result["path"] == str(fake_path)
    assert fake_path.exists()


@pytest.mark.asyncio
async def test_indicator_chart_no_data_reports_error(monkeypatch):
    import pandas as pd
    import report_generator
    monkeypatch.setattr(report_generator, "_fetch_report_data_sync", lambda symbol: (pd.DataFrame(), None, {}))

    result = await _call_indicator_chart({"symbol": "AAPL"})
    assert result["success"] is False


@pytest.mark.asyncio
async def test_comparison_chart_requires_at_least_two_symbols():
    result = await _call_comparison_chart({"symbols": ["AAPL"]})
    assert result["success"] is False
    assert "symbols" in result["error"]


@pytest.mark.asyncio
@requires_charts
async def test_comparison_chart_writes_artifact_on_success(monkeypatch, tmp_path):
    df = _fake_ohlcv_df()
    import report_generator
    monkeypatch.setattr(report_generator, "_fetch_report_data_sync", lambda symbol: (df, None, {}))

    fake_path = tmp_path / "out.png"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("artifacts.create_user_artifact", lambda *a, **kw: type("A", (), {"path": fake_path})())
        result = await _call_comparison_chart({"symbols": ["AAPL", "MSFT"]})
    assert result["success"] is True
    assert fake_path.exists()


@pytest.mark.asyncio
async def test_comparison_chart_no_usable_data_reports_error(monkeypatch):
    import pandas as pd
    import report_generator
    monkeypatch.setattr(report_generator, "_fetch_report_data_sync", lambda symbol: (pd.DataFrame(), None, {}))

    result = await _call_comparison_chart({"symbols": ["AAPL", "MSFT"]})
    assert result["success"] is False


@pytest.mark.asyncio
@requires_charts
async def test_allocation_chart_writes_artifact_on_success(monkeypatch, tmp_path):
    import aria_code.packages.aria_mcp.server as server_mod
    from brokers.base import Position

    class _FakeBroker:
        def positions(self):
            return [Position(symbol="AAPL", market_value=15000)]

    monkeypatch.setattr(server_mod, "_get_broker", lambda broker_id="": _FakeBroker())

    fake_path = tmp_path / "out.png"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("artifacts.create_user_artifact", lambda *a, **kw: type("A", (), {"path": fake_path})())
        result = await _call_allocation_chart({})
    assert result["success"] is True
    assert fake_path.exists()


@pytest.mark.asyncio
async def test_allocation_chart_no_positions_reports_error(monkeypatch):
    import aria_code.packages.aria_mcp.server as server_mod

    class _FakeBroker:
        def positions(self):
            return []

    monkeypatch.setattr(server_mod, "_get_broker", lambda broker_id="": _FakeBroker())
    result = await _call_allocation_chart({})
    assert result["success"] is False


# ── Skill exposure (aria.skill.list / aria.skill.get) ───────────────────────

def _fake_skill(name, qualified, description="", instructions="body", integrity="verified"):
    return type("S", (), {
        "name": name,
        "qualified_name": qualified,
        "description": description,
        "plugin_name": qualified.split(":")[0],
        "integrity": integrity,
        "instructions": instructions,
    })()


@pytest.mark.asyncio
async def test_skill_list_returns_all_when_no_query(monkeypatch):
    from packages.aria_skills import loader

    skills = [_fake_skill("a", "cat:a"), _fake_skill("b", "cat:b")]
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: skills)

    result = await _call_skill_list({})
    assert result["success"] is True
    assert result["matched_by_relevance"] is False
    assert {s["qualified_name"] for s in result["skills"]} == {"cat:a", "cat:b"}


@pytest.mark.asyncio
async def test_skill_list_ranks_by_query_when_matches_exist(monkeypatch):
    from packages.aria_skills import loader

    skills = [_fake_skill("a", "cat:a"), _fake_skill("b", "cat:b")]
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: skills)
    monkeypatch.setattr(loader, "select_external_skills", lambda q, s=None: [skills[1]])

    result = await _call_skill_list({"query": "something"})
    assert result["matched_by_relevance"] is True
    assert [s["qualified_name"] for s in result["skills"]] == ["cat:b"]


@pytest.mark.asyncio
async def test_skill_list_falls_back_to_full_list_when_query_matches_nothing(monkeypatch):
    """An off-topic query must not read as "no skills are installed"."""
    from packages.aria_skills import loader

    skills = [_fake_skill("a", "cat:a"), _fake_skill("b", "cat:b")]
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: skills)
    monkeypatch.setattr(loader, "select_external_skills", lambda q, s=None: [])

    result = await _call_skill_list({"query": "totally unrelated"})
    assert result["matched_by_relevance"] is False
    assert len(result["skills"]) == 2


@pytest.mark.asyncio
async def test_skill_list_omits_instructions_to_keep_listing_small(monkeypatch):
    from packages.aria_skills import loader

    monkeypatch.setattr(loader, "discover_external_skills",
                        lambda *a, **kw: [_fake_skill("a", "cat:a", instructions="x" * 5000)])
    result = await _call_skill_list({})
    assert "instructions" not in result["skills"][0]


@pytest.mark.asyncio
async def test_skill_list_surfaces_integrity(monkeypatch):
    from packages.aria_skills import loader

    monkeypatch.setattr(loader, "discover_external_skills",
                        lambda *a, **kw: [_fake_skill("a", "cat:a", integrity="unlocked")])
    result = await _call_skill_list({})
    assert result["skills"][0]["integrity"] == "unlocked"


@pytest.mark.asyncio
async def test_skill_get_returns_full_instructions_by_bare_name(monkeypatch):
    from packages.aria_skills import loader

    monkeypatch.setattr(loader, "discover_external_skills",
                        lambda *a, **kw: [_fake_skill("ui-design-system", "cat:ui-design-system",
                                                      instructions="THE WORKFLOW")])
    result = await _call_skill_get({"name": "ui-design-system"})
    assert result["success"] is True
    assert result["instructions"] == "THE WORKFLOW"


@pytest.mark.asyncio
async def test_skill_get_accepts_qualified_name(monkeypatch):
    from packages.aria_skills import loader

    monkeypatch.setattr(loader, "discover_external_skills",
                        lambda *a, **kw: [_fake_skill("ui-design-system", "cat:ui-design-system")])
    result = await _call_skill_get({"name": "cat:ui-design-system"})
    assert result["success"] is True


@pytest.mark.asyncio
async def test_skill_get_requires_name():
    result = await _call_skill_get({})
    assert result["success"] is False
    assert "name" in result["error"]


@pytest.mark.asyncio
async def test_skill_get_unknown_name_lists_available(monkeypatch):
    from packages.aria_skills import loader

    monkeypatch.setattr(loader, "discover_external_skills",
                        lambda *a, **kw: [_fake_skill("a", "cat:a")])
    result = await _call_skill_get({"name": "nope"})
    assert result["success"] is False
    assert result["available"] == ["cat:a"]


@pytest.mark.asyncio
async def test_skill_get_lists_bundled_reference_docs(tmp_path, monkeypatch):
    """Several skills' instructions say "read references/X.md first" — over
    MCP the caller can't touch this filesystem, so the names must be listed."""
    from packages.aria_skills import loader

    skill_dir = tmp_path / "my-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "deep.md").write_text("DEEP", encoding="utf-8")
    (skill_dir / "references" / "other.md").write_text("OTHER", encoding="utf-8")

    skill = _fake_skill("my-skill", "cat:my-skill")
    skill.path = skill_dir / "SKILL.md"
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: [skill])

    result = await _call_skill_get({"name": "my-skill"})
    assert result["references"] == ["deep.md", "other.md"]


@pytest.mark.asyncio
async def test_skill_get_fetches_reference_content(tmp_path, monkeypatch):
    from packages.aria_skills import loader

    skill_dir = tmp_path / "my-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "deep.md").write_text("DEEP CONTENT", encoding="utf-8")

    skill = _fake_skill("my-skill", "cat:my-skill")
    skill.path = skill_dir / "SKILL.md"
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: [skill])

    result = await _call_skill_get({"name": "my-skill", "reference": "deep.md"})
    assert result["success"] is True
    assert result["content"] == "DEEP CONTENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../../../etc/passwd", "../SKILL.md", "/etc/passwd", "../../secret.md"])
async def test_skill_get_reference_blocks_path_traversal(tmp_path, monkeypatch, bad):
    """`reference` is caller-controlled and lands in a filesystem path."""
    from packages.aria_skills import loader

    skill_dir = tmp_path / "my-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "ok.md").write_text("OK", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text("SHOULD NOT BE REACHABLE", encoding="utf-8")
    (tmp_path / "secret.md").write_text("SECRET", encoding="utf-8")

    skill = _fake_skill("my-skill", "cat:my-skill")
    skill.path = skill_dir / "SKILL.md"
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: [skill])

    result = await _call_skill_get({"name": "my-skill", "reference": bad})
    assert result["success"] is False
    assert "content" not in result


@pytest.mark.asyncio
async def test_skill_get_reference_unknown_name_lists_available(tmp_path, monkeypatch):
    from packages.aria_skills import loader

    skill_dir = tmp_path / "my-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "references" / "ok.md").write_text("OK", encoding="utf-8")

    skill = _fake_skill("my-skill", "cat:my-skill")
    skill.path = skill_dir / "SKILL.md"
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: [skill])

    result = await _call_skill_get({"name": "my-skill", "reference": "nope.md"})
    assert result["success"] is False
    assert result["references"] == ["ok.md"]


@pytest.mark.asyncio
async def test_skill_get_no_references_dir_returns_empty_list(tmp_path, monkeypatch):
    from packages.aria_skills import loader

    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir(parents=True)

    skill = _fake_skill("my-skill", "cat:my-skill")
    skill.path = skill_dir / "SKILL.md"
    monkeypatch.setattr(loader, "discover_external_skills", lambda *a, **kw: [skill])

    result = await _call_skill_get({"name": "my-skill"})
    assert result["references"] == []


# ── governance tools: what other agents ask Aria about a change ─────────────────

import asyncio
import json


def _run(coro):
    return asyncio.run(coro)


def test_governance_tools_are_listed_with_schemas_and_are_read_only():
    from packages.aria_mcp.bridge import default_exposures

    names = {"aria.risk.assess", "aria.contract.check", "aria.project.impact", "aria.project.commands"}
    listed = {t["name"]: t for t in TOOLS}
    assert names <= set(listed)
    for name in names:
        assert listed[name]["inputSchema"]["type"] == "object"
    for exposure in default_exposures():
        if exposure.name in names:
            assert exposure.read_only, f"{exposure.name} must stay read-only"


def test_risk_assess_rates_a_command_and_says_how_it_would_be_approved():
    from aria_code.packages.aria_mcp.server import _call_risk_assess

    deleting = _run(_call_risk_assess({"command": "rm -rf build"}))
    assert deleting["success"] and deleting["assessment"]["level"] == 4
    assert deleting["approval"] == {"manual": "always", "risk_mode": "always"}
    assert any("L4" in line for line in deleting["card"])
    reading = _run(_call_risk_assess({"command": "git status"}))
    assert reading["assessment"]["level"] == 0 and reading["approval"]["risk_mode"] == "auto"


def test_risk_assess_needs_a_command_or_a_tool_and_never_runs_anything(tmp_path):
    from aria_code.packages.aria_mcp.server import _call_risk_assess

    assert not _run(_call_risk_assess({}))["success"]
    marker = tmp_path / "ran"
    _run(_call_risk_assess({"command": f"touch {marker}"}))
    assert not marker.exists()
    out = _run(_call_risk_assess({"tool": "write_file", "params": {"path": "a.txt", "content": "x"}}))
    assert out["success"] and out["assessment"]["level"] >= 1


def test_contract_check_follows_the_projects_policy(tmp_path, monkeypatch):
    from aria_code.packages.aria_mcp.server import _call_contract_check

    monkeypatch.chdir(tmp_path)
    none = _run(_call_contract_check({"tool": "write_file", "params": {"path": "anything.py"}}))
    assert none["success"] and none["allowed"] and none["contract"] is None
    (tmp_path / ".aria").mkdir()
    (tmp_path / ".aria" / "policy.yaml").write_text(
        "contract:\n  allow: [src/**]\n  restrict: [src/secrets.py]\n  forbid: [git.push]\n  max_level: 3\n")
    ok = _run(_call_contract_check({"tool": "write_file", "params": {"path": "src/a.py", "content": "x"}}))
    assert ok["allowed"] is True
    outside = _run(_call_contract_check({"tool": "write_file", "params": {"path": "docs/a.md", "content": "x"}}))
    assert outside["allowed"] is False and outside["rule"] == "allow"
    push = _run(_call_contract_check({"tool": "run_command", "params": {"command": "git push origin main"}}))
    assert push["allowed"] is False and push["rule"] == "forbid"


def test_a_policy_that_cannot_be_read_allows_reads_only(tmp_path, monkeypatch):
    from aria_code.packages.aria_mcp.server import _call_contract_check

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".aria").mkdir()
    (tmp_path / ".aria" / "policy.yaml").write_text("contract: [not, a, mapping")
    write = _run(_call_contract_check({"tool": "write_file", "params": {"path": "a.py", "content": "x"}}))
    assert write["allowed"] is False
    read = _run(_call_contract_check({"tool": "run_command", "params": {"command": "git status"}}))
    assert read["allowed"] is True


def test_project_impact_lists_what_depends_on_a_file(tmp_path, monkeypatch):
    from aria_code.packages.aria_mcp.server import _call_project_impact

    monkeypatch.chdir(tmp_path)
    (tmp_path / "core.py").write_text("def f():\n    return 1\n")
    (tmp_path / "app.py").write_text("from core import f\n\nprint(f())\n")
    (tmp_path / "test_app.py").write_text("import app\n")
    out = _run(_call_project_impact({"targets": ["core.py"]}))
    assert out["success"] and out["impact"]["direct_dependents"] == ["app.py"]
    assert out["impact"]["tests"] == ["test_app.py"] and out["breadth"] == "narrow"
    assert not _run(_call_project_impact({"targets": []}))["success"]


def test_project_commands_reports_what_the_project_declares(tmp_path, monkeypatch):
    from aria_code.packages.aria_mcp.server import _call_project_commands

    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    out = _run(_call_project_commands({}))
    assert out["success"] and out["commands"] == {"test": "npm test"}


def test_governance_tools_refuse_a_root_outside_the_workspace_and_home(tmp_path, monkeypatch):
    from aria_code.packages.aria_mcp.server import _call_project_commands

    monkeypatch.chdir(tmp_path)
    out = _run(_call_project_commands({"root": "/etc"}))
    assert out["success"] is False and "inside" in out["error"]
    assert _run(_call_project_commands({"root": str(tmp_path / "missing")}))["success"] is False
