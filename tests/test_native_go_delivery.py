"""The Go runtime's delivery report must match runtime/delivery.py.

Symbol names need the changed file on disk, and tested_by needs a graph cache.
These scenarios use paths that are not files here and have no cache, so both
sides leave those fields empty. The impact line is compared the same way.
"""
import json
import os
import subprocess

import pytest

from aria_code.runtime.delivery import DeliveryLedger, report_from_activities


def binary():
    value = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not value:
        pytest.skip("The native-runtime CI job builds this executable")
    return value


EDIT = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,4 @@\n a\n-b\n+B\n+c\n d\n"
NEW = "--- /dev/null\n+++ b/src/新文件.py\n@@ -0,0 +1,2 @@\n+x\n+y"


def ok(path, diff="", **extra):
    return {"success": True, "data": {"path": path, "diff": diff, **extra}}


SCENARIOS = {
    "question": dict(activities=[("read_file", {"path": "a"}, {"success": True, "data": {"content": "x"}})]),
    "edit-unverified": dict(activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT, checkpoint_id="cp1"))]),
    "edit-verified": dict(
        activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT, checkpoint_id="cp1")),
                    ("write_file", {"path": "src/新文件.py"}, ok("src/新文件.py", NEW, checkpoint_id="cp2")),
                    ("run_command", {"command": "pytest -q"}, {"success": True, "data": {"exit_code": 0}})],
        acceptance={"verified": True, "reports": [{"checks": [{"command": "pytest -q", "passed": True, "exit_code": 0}]}]},
        rewind_hint="/rewind code abc"),
    "red": dict(
        activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT))],
        acceptance={"verified": False, "reports": [{"checks": [
            {"command": "pytest -q", "passed": False, "exit_code": 1, "new_failures": ["t1", "t2", "t3", "t4"]},
            {"command": "ruff check .", "passed": True, "exit_code": 0}]}]}),
    "preexisting": dict(
        activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT))],
        acceptance={"verified": False, "reports": [{"checks": [
            {"command": "pytest -q", "passed": False, "exit_code": 2, "preexisting": True}]}]}),
    "stale": dict(
        activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT))],
        acceptance={"reports": [{"checks": [{"command": "pytest -q", "passed": True, "exit_code": 0}]}]}),
    "stopped": dict(activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT))], stop_reason="max_rounds"),
    "stopped-other": dict(activities=[], stop_reason="cancelled",
                          acceptance={"reports": [{"checks": [{"command": "go test ./...", "passed": True, "exit_code": 0}]}]}),
    "refused": dict(
        activities=[("write_file", {"path": "billing/x.py"}, {"success": False, "error": "Blocked", "contract_violation": {"rule": "allow"}}),
                    ("run_command", {"command": "git push"}, {"success": False, "error": "Blocked"})],
        contract={"refused": [{"tool": "write_file", "rule": "allow", "reason": "billing/x.py is outside the allowed paths (src/**)", "level": 1}]}),
    "refused-after-change": dict(
        activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT))],
        contract={"refused": [{"tool": "run_command", "reason": "uses git.push"}]}),
    "review-blocking": dict(
        activities=[("edit_file", {"path": "src/app.py"}, ok("src/app.py", EDIT))],
        acceptance={"verified": True, "reports": [{"checks": [{"command": "pytest", "passed": True, "exit_code": 0}]}]},
        review={"verdict": "blocking", "headline": "1 blocking finding", "lines": ["✗ refresh() drops the token", "· style"],
                "behaviour": {"before": "expired tokens failed", "after": "they refresh", "why": "bug", "impact": ""}}),
    "risky": dict(activities=[
        ("run_command", {"command": "npm install left-pad"}, {"success": True, "data": {"exit_code": 0}}),
        ("run_command", {"command": "rm -rf build"}, {"success": False, "error": "declined"}),
        ("multi_edit", {"path": "a.py"}, {"success": True, "data": {"path": "a.py", "edits": [{"path": "a.py"}, {"path": "b.py"}], "action": "Created"}}),
        ("write_file", {"path": "staged.py"}, {"success": True, "data": {"path": "staged.py", "staged": True}}),
        ("write_file", {"path": "unapplied.py"}, {"success": True, "data": {"path": "unapplied.py", "applied": False}}),
        ("mcp__fs__write_file", {"path": "c.py"}, {"success": True, "path": "c.py", "data": {}}),
        ("apply_patch", {"patch": "+++ b/d.py\n"}, {"success": True, "data": {"paths": ["d.py", "e.py", ""], "diff": "+1\n+2\n-3"}}),
    ]),
}


def go_report(root, scenario):
    request = {
        "root": str(root) if root else "",
        "activities": [{"tool": t, "params": p, "result": r} for t, p, r in scenario.get("activities", [])],
        "acceptance": scenario.get("acceptance"), "contract": scenario.get("contract"), "review": scenario.get("review"),
        "stop_reason": scenario.get("stop_reason", "completed"), "rewind_hint": scenario.get("rewind_hint", ""),
    }
    result = subprocess.run([binary(), "delivery"], input=json.dumps(request), capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("with_root", [False, True])
def test_go_delivery_matches_python(tmp_path, monkeypatch, name, with_root):
    monkeypatch.chdir(tmp_path)
    root = tmp_path if with_root else None
    scenario = SCENARIOS[name]
    ledger = DeliveryLedger(root=str(root) if root else None)
    for tool, params, result in scenario.get("activities", []):
        ledger.record(tool, params, result)
    report = ledger.report(acceptance=scenario.get("acceptance"), contract=scenario.get("contract"),
                           review=scenario.get("review"), stop_reason=scenario.get("stop_reason", "completed"))
    go = go_report(root, scenario)
    expected = json.loads(json.dumps(report.as_dict()))
    assert go["report"] == expected
    assert go["text"] == report.render(rewind_hint=scenario.get("rewind_hint", ""), root=root)
    assert go["worth_showing"] == report.worth_showing
    assert go["diff"] == ledger.diff_text()


def test_go_delivery_reports_paths_inside_the_root_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inside = str(tmp_path / "src" / "app.py")
    scenario = {"activities": [("edit_file", {"path": inside}, ok(inside, EDIT))]}
    expected = report_from_activities(scenario["activities"], root=str(tmp_path)).render(root=tmp_path)
    assert "M src/app.py" in expected
    assert go_report(tmp_path, scenario)["text"] == expected
