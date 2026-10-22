"""The Go runtime's acceptance gate must match runtime/acceptance.py.

Covered: the verification planner, reading command results, the report's
headline, repair message and summary, log trimming and file:line anchors, and
the .ariarc acceptance.default commands. The test baseline is not ported.
"""
import json
import os
import subprocess

import pytest

from aria_code.ariarc import AriaRC, _strip_comments
from aria_code.packs.rules import default_acceptance_commands
from aria_code.runtime.acceptance import AcceptanceReport, CheckResult, _anchors, _read_run_result, _trim
from aria_code.runtime.baseline import _keep_collecting, compare
from aria_code.workspace.verify import VerificationPlanner


def binary():
    value = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not value:
        pytest.skip("The native-runtime CI job builds this executable")
    return value


def go_acceptance(request):
    result = subprocess.run([binary(), "acceptance"], input=json.dumps(request), capture_output=True, text=True,
                            encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


PROJECTS = {
    "empty": {},
    "pytest-ini": {"pytest.ini": "[pytest]\n"},
    "pyproject-pytest": {"pyproject.toml": "[tool.pytest.ini_options]\n"},
    "pyproject-plain": {"pyproject.toml": "[project]\nname='x'\n"},
    "setup-cfg": {"setup.cfg": "[tool:pytest]\n"},
    "unittest": {"tests/test_a.py": "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"},
    "unittest-from": {"tests/sub/test_b.py": "from unittest import TestCase\n", "tests/helper.py": "import pytest\n"},
    "pytest-import": {"tests/test_a.py": "import os, pytest as pt\n"},
    "nested-import": {"tests/test_a.py": "import unittest\ndef helper():\n    import pytest\n"},
    "top-level-test": {"tests/test_a.py": "import unittest\n\ndef test_plain():\n    assert True\n"},
    "async-test": {"tests/test_a.py": "async def test_plain():\n    pass\n"},
    "root-tests": {"test_root.py": "import unittest\n", "pyproject.toml": "[project]\n"},
    # Tests beside the code: no config file and no tests/ directory.
    "flat-pytest": {"test_orders.py": "def test_x():\n    assert True\n"},
    "flat-unittest": {"test_orders.py": "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"},
    "flat-suffix": {"orders_test.py": "def test_x():\n    pass\n"},
    "flat-suffix-unittest": {"orders_test.py": "import unittest\n"},
    "flat-conftest": {"conftest.py": ""},
    "flat-subdir": {"pkg/test_orders.py": "def test_x():\n    pass\n"},
    "flat-two-deep": {"a/b/test_orders.py": "def test_x():\n    pass\n"},
    "flat-too-deep": {"a/b/c/test_orders.py": "def test_x():\n    pass\n"},
    "flat-ignored-dirs": {".venv/test_v.py": "", "node_modules/m/test_n.py": "", "venv/test_w.py": "", ".hidden/test_h.py": ""},
    "node-test-build": {"package.json": json.dumps({"scripts": {"test": "jest", "build": "tsc"}})},
    "node-tsc": {"package.json": json.dumps({"scripts": {}}), "tsconfig.json": "{}"},
    "node-build-only": {"package.json": json.dumps({"scripts": {"build": "vite build"}})},
    "node-bad": {"package.json": "{not json"},
    "both": {"package.json": json.dumps({"scripts": {"test": "jest"}}), "tests/test_a.py": "import pytest\n"},
}

PATHS = [
    [],
    ["src/app.py"],
    ["src/app.py", "src/web.tsx", "styles/site.css", ""],
    ["it's here.py", "数据/文件.py", "a b.py"] + [f"m{i}.py" for i in range(25)],
    ["README.md"],
    ["web/index.js"],
]


@pytest.mark.parametrize("project", sorted(PROJECTS))
def test_go_plans_verification_like_python(tmp_path, project):
    root = tmp_path / "workspace"
    root.mkdir()
    write(root, PROJECTS[project])
    for paths in PATHS[1:]:
        plan = VerificationPlanner(root).infer(paths)
        got = go_acceptance({"plan": {"root": str(root), "paths": paths}})["plan"]
        assert got == {"commands": plan.commands, "reason": plan.reason}, paths


RESULTS = [
    {},
    {"success": False},
    {"success": False, "error": "timed out", "output": "partial"},
    {"success": True, "data": {"exit_code": 0, "stdout": "ok\n", "stderr": ""}},
    {"success": True, "data": {"exit_code": 2, "stdout": "  ", "stderr": "boom"}},
    {"success": True, "data": {"returncode": "3", "output": "via output"}},
    {"data": {}, "exit_code": 1, "stdout": "top level"},
    {"data": {"code": None, "status": 4}},
    {"data": {"exit_code": "x", "code": 5}},
    {"data": {"stdout": "no status"}},
    {"success": True, "data": {"exit_code": 1.9}},
]


def test_go_reads_run_results_like_python():
    got = go_acceptance({"results": RESULTS})["results"]
    assert got == [list(_read_run_result(raw)) for raw in RESULTS]


LONG_LOG = "\n".join(f"line {i}" for i in range(100))
TRACEBACK = ('Traceback (most recent call last):\n  File "/runner/main.py", line 3, in <module>\n'
             '  File "src/app.py", line 42, in run\nsrc/app.py:42: AssertionError\n'
             "web/index.ts:7:5 - error TS2322\n  数据/模块.py:9: 失败\n")


def check(command, exit_code, passed, output="", error="", **baseline):
    return {"command": command, "exit_code": exit_code, "passed": passed, "output": output, "error": error, **baseline}


REPORTS = [
    dict(attempt=1, attempts_remaining=1, reason="none", paths=[], checks=[]),
    dict(attempt=1, attempts_remaining=1, reason="Python files changed", paths=["a.py"],
         checks=[check("python3 -m py_compile a.py", 0, True), check("python3 -m pytest -q", 0, True)]),
    dict(attempt=1, attempts_remaining=1, reason="Python files changed", paths=["a.py", "b.py"],
         checks=[check("python3 -m py_compile a.py", 0, True), check("python3 -m pytest -q", 1, False, TRACEBACK)]),
    dict(attempt=2, attempts_remaining=0, reason="r", paths=["a.py"],
         checks=[check("npm test", 1, False, LONG_LOG + "\n" + "x" * 7000, "runner error")]),
    dict(attempt=1, attempts_remaining=3, reason="r", paths=[], checks=[check("make", 0, False, "", "timed out")]),
    dict(attempt=1, attempts_remaining=1, reason="r", paths=["a.py"], checks=[
        check("python3 -m pytest -q", 1, False, TRACEBACK, preexisting=False, new_failures=["tests/t.py::b"],
              old_failures=["tests/t.py::a"], baseline_note="")]),
    dict(attempt=1, attempts_remaining=1, reason="r", paths=["a.py"], checks=[
        check("make lint", 0, True), check("python3 -m pytest -q", 2, False, "x", preexisting=True,
                                           old_failures=["tests/t.py::a"], baseline_note="failing before this change")]),
    dict(attempt=2, attempts_remaining=0, reason="r", paths=[], checks=[
        check("npm test", 1, False, "y", baseline_note="passed before this change")]),
]


def test_go_reports_acceptance_like_python():
    got = go_acceptance({"reports": REPORTS})["reports"]
    for data, actual in zip(REPORTS, got):
        report = AcceptanceReport(
            attempt=data["attempt"], attempts_remaining=data["attempts_remaining"], reason=data["reason"],
            paths=tuple(data["paths"]),
            checks=tuple(CheckResult(command=c["command"], exit_code=c["exit_code"], passed=c["passed"],
                                     output=c["output"], error=c["error"], preexisting=c.get("preexisting", False),
                                     new_failures=tuple(c.get("new_failures", ())),
                                     old_failures=tuple(c.get("old_failures", ())),
                                     baseline_note=c.get("baseline_note", "")) for c in data["checks"]))
        summary = report.summary()
        for item in summary["checks"]:
            item.pop("duration")
        assert actual == {"headline": report.headline(), "directive": report.repair_directive(), "summary": summary}


def test_go_trims_logs_and_finds_anchors_like_python():
    texts = ["", "  \n ", TRACEBACK, LONG_LOG, "界" * 7000, "a\r\nb\rc\n", "\n".join(f"f{i}.py:{i}" for i in range(9))]
    got = go_acceptance({"texts": texts})["texts"]
    assert got == [{"trim": _trim(t), "anchors": list(_anchors(t))} for t in texts]


@pytest.mark.parametrize("rc", [
    None,
    '{"acceptance": {"default": ["python3 -m pytest -q", " make check ", "", 5, "make check"]}}',
    '{"acceptance": {"default": " npm test "}}',
    '// comment\n{"acceptance": /* block */ {"default": ["go test ./..."]}, "url": "https://x.io//y"}',
    '{"acceptance": ["not", "a", "table"]}',
    '{"acceptance": {"payments": ["only for the pack"]}}',
    "{broken",
])
def test_go_reads_ariarc_acceptance_like_python(tmp_path, monkeypatch, rc):
    home = tmp_path / "home"
    project = home / "project" / "sub"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    if rc is not None:
        (home / "project" / ".ariarc").write_text(rc, encoding="utf-8")
    expected = list(default_acceptance_commands(rc=AriaRC.load(str(project))))
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    result = subprocess.run([binary(), "acceptance"], input=json.dumps({"ariarc": str(project)}), capture_output=True,
                            text=True, encoding="utf-8", timeout=10, env=env)
    assert result.returncode == 0, result.stderr
    assert (json.loads(result.stdout)["ariarc"] or []) == list(dict.fromkeys(expected))


def test_go_strips_jsonc_comments_like_python():
    texts = ['{"a": 1} // c', '{"u": "https://x.io/a//b", "v": "a://b"} // tail', "/* a */{/* b\n*/}", "x//y\nz", "a///b\n"]
    assert go_acceptance({"jsonc": texts})["jsonc"] == [_strip_comments(t) for t in texts]


PYTEST_AFTER = ("FAILED tests/test_a.py::test_x - assert 1\nERROR tests/test_b.py\nFAILED tests/test_c.py::test_z\n"
                "FAILED\ntests/test_d.py::test_w - spans a line\nERROR tests/数据.py::测试 - 失败\n")


@pytest.mark.parametrize("before", [
    None, [0, ""], [1, ""], [1, "FAILED tests/test_a.py::test_x - assert 1"], [2, PYTEST_AFTER], [1, "ERROR tests/数据.py::测试 - x"],
])
@pytest.mark.parametrize("after", [PYTEST_AFTER, "make: *** [all] Error 2", ""])
@pytest.mark.parametrize("failed", [True, False])
def test_go_compares_against_the_baseline_like_python(before, after, failed):
    comparison = compare(after, failed, tuple(before) if before is not None else None)
    got = go_acceptance({"compare": [{"after_output": after, "after_failed": failed, "before": before}]})["compare"][0]
    assert got == {"preexisting": comparison.preexisting, "new_failures": list(comparison.new_failures),
                   "old_failures": list(comparison.old_failures), "note": comparison.note}


def test_go_keeps_pytest_collecting_like_python():
    commands = ["python3 -m pytest -q", "pytest", "py.test tests", "/usr/bin/pytest -x", "pytest -q | tail -5",
                "pytest --continue-on-collection-errors", "npm test", "mypytest", "pytest\tx", "echo $(pytest)"]
    assert go_acceptance({"keep": commands})["keep"] == [_keep_collecting(c) for c in commands]
