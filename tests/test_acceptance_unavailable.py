"""A check whose tool is missing is not a red check: nothing was verified, and nobody should 'fix' it."""
import asyncio

import pytest

from aria_code.runtime.acceptance import AcceptanceGate, CheckResult, unavailable_reason
from aria_code.runtime.delivery import DeliveryLedger

NO_PYTEST = ("", "/opt/homebrew/opt/python@3.14/bin/python3.14: No module named pytest\n")


@pytest.mark.parametrize("exit_code,output,error,expected", [
    (1, "", NO_PYTEST[1], "pytest is not installed for this python"),
    (1, NO_PYTEST[1], "", "pytest is not installed for this python"),
    (127, "", "zsh: command not found: npm", "npm is not installed"),
    (1, "bash: npx: command not found", "", "npx is not installed"),
    (127, "", "", "the command is not installed"),
    (1, "E   ModuleNotFoundError: No module named 'requests'\nFAILED test_x.py::test_a", "", ""),
    (1, "3 failed, 2 passed in 0.1s", "", ""),
    (0, "", "", ""),
])
def test_only_a_missing_tool_is_unavailable(exit_code, output, error, expected):
    assert unavailable_reason(exit_code, output, error) == expected


def gate(results):
    """A gate whose runner answers with the given (exit_code, output) per command, in order."""
    answers = iter(results)

    def runner(command):  # the shape tool_run_command returns
        exit_code, output, error = next(answers)
        return {"success": True, "data": {"exit_code": exit_code, "stdout": output, "stderr": error}}

    commands = tuple(f"cmd{i}" for i in range(len(results)))
    g = AcceptanceGate(root=".", commands=commands, runner=runner, max_attempts=2)
    g.record_tool("write_file", {"success": True, "data": {"path": "a.py"}})
    return g


def run(coro):
    return asyncio.run(coro)


def test_a_missing_tool_does_not_fail_the_gate_or_trigger_a_repair():
    g = gate([(0, "ok", ""), (1, "", "python3: No module named pytest")])
    report = run(g.run())
    assert report.passed and not report.failures and len(report.not_run) == 1
    assert "验收部分通过" in report.headline() and "pytest is not installed" in report.headline()
    summary = g.summary()
    assert summary["verified"] is None, "part of the plan did not run, so it is not verified"
    assert summary["reports"][0]["checks"][1]["not_run"] == "pytest is not installed for this python"


def test_when_nothing_could_run_nothing_ran():
    g = gate([(1, "", "python3: No module named pytest")])
    report = run(g.run())
    assert not report.ran and not report.passed and not report.failures
    assert "无法运行" in report.headline() and g.summary()["verified"] is None


def test_a_real_failure_still_stops_the_gate_and_asks_for_a_repair():
    g = gate([(1, "FAILED test_a.py::test_x", ""), (0, "never reached", "")])
    report = run(g.run())
    assert not report.passed and len(report.failures) == 1 and len(report.checks) == 1
    assert g.summary()["verified"] is False


def test_a_missing_tool_does_not_hide_a_later_failure():
    g = gate([(1, "", "python3: No module named ruff"), (1, "FAILED test_a.py::test_x", "")])
    report = run(g.run())
    assert len(report.not_run) == 1 and len(report.failures) == 1 and not report.passed


def test_the_delivery_report_says_the_tests_did_not_run():
    from aria_code.runtime.delivery import DeliveryReport

    g = gate([(0, "ok", ""), (1, "", "python3: No module named pytest")])
    run(g.run())
    summary = g.summary()
    # Build the report the way the ledger does, from the acceptance summary.
    ledger = DeliveryLedger()
    ledger._changed["a.py"] = __import__("aria_code.runtime.delivery", fromlist=["ChangedFile"]).ChangedFile(path="a.py", added=1, removed=0, created=False)
    report = ledger.report(acceptance=summary)
    text = report.render()
    assert report.status == "done" and report.verified is None
    assert "not run: cmd1 — pytest is not installed for this python" in text
    assert "Run the tests yourself" in report.next
    assert [c["command"] for c in report.checks] == ["cmd0"], "the missing tool is not listed as a check that ran"
    assert report.as_dict()["not_run"][0]["command"] == "cmd1"
    assert DeliveryReport.from_dict(report.as_dict()).not_run == report.not_run


def test_a_report_without_unavailable_checks_has_no_new_field():
    g = gate([(0, "ok", "")])
    run(g.run())
    ledger = DeliveryLedger()
    ledger._changed["a.py"] = __import__("aria_code.runtime.delivery", fromlist=["ChangedFile"]).ChangedFile(path="a.py", added=1, removed=0, created=False)
    assert "not_run" not in ledger.report(acceptance=g.summary()).as_dict(), "the Go report is compared field by field"
