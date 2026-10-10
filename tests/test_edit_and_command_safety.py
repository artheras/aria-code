"""An edit changes the place it names, and a failed command changes nothing.

edit_file replaced the first match of old_string however many there were, so
an old_string that also appeared earlier in the file edited the wrong place
and reported success. run_command, when a `python3 script.py` failed, edited
the script itself and ran `pip3 install <module from the error>`, then re-ran
the command: writes and installs with no approval, no checkpoint and no
regard for network off.
"""

from __future__ import annotations

import os
import pathlib

import pytest

SOURCE = (
    "def first(x):\n"
    "    if x is None:\n"
    "        return None\n"
    "    return x\n"
    "\n"
    "def second(y):\n"
    "    if y is None:\n"
    "        return None\n"
    "    return y * 2\n"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    path = tmp_path / "calc.py"
    path.write_text(SOURCE)
    return path


def edit(params):
    from aria_code.aria_cli import _tool_edit_file
    return _tool_edit_file(params)


def test_an_ambiguous_old_string_changes_nothing(workspace):
    result = edit({"path": str(workspace), "old_string": "        return None\n",
                   "new_string": "        return 0\n"})
    assert result["success"] is False
    assert "matches 2 places (lines 3, 8)" in result["error"]
    assert workspace.read_text() == SOURCE


def test_more_context_picks_the_one_place(workspace):
    result = edit({"path": str(workspace), "old_string": "    if y is None:\n        return None\n",
                   "new_string": "    if y is None:\n        return 0\n"})
    assert result["success"] is True and result["data"]["replacements"] == 1
    text = workspace.read_text()
    assert text.count("return None") == 1 and "        return 0\n    return y * 2" in text


def test_replace_all_changes_every_match(workspace):
    result = edit({"path": str(workspace), "old_string": "return None",
                   "new_string": "return 0", "replace_all": True})
    assert result["success"] is True and result["data"]["replacements"] == 2
    assert "return None" not in workspace.read_text()


def test_single_and_multi_edit_preserve_utf8_under_legacy_locale(workspace, monkeypatch):
    from aria_code.apps.cli.tools.write_tools import tool_multi_edit

    source = "# 中文注释\nvalue = '你好'\n"
    workspace.write_text(source, encoding="utf-8")
    original = pathlib.Path.read_text

    def legacy_default(path, *args, **kwargs):
        if path == workspace:
            kwargs.setdefault("encoding", "cp1252")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", legacy_default)
    result = edit({"path": str(workspace), "old_string": "你好", "new_string": "世界"})
    assert result["success"], result
    assert workspace.read_text(encoding="utf-8") == source.replace("你好", "世界")
    result = tool_multi_edit({"path": str(workspace), "edits": [{"old_string": "世界", "new_string": "再见"}]})
    assert result["success"], result
    assert workspace.read_text(encoding="utf-8") == source.replace("你好", "再见")


def test_the_schema_offers_replace_all():
    from aria_code.apps.cli.local_tool_schemas import build_local_tool_schemas

    schemas = build_local_tool_schemas(todo_schema={"type": "function", "function": {"name": "todo"}},
                                       config_dir="~/.arthera")
    schema = next(s["function"] for s in schemas if s["function"]["name"] == "edit_file")
    assert "replace_all" in schema["parameters"]["properties"]
    assert "exactly one place" in schema["description"]


def run(command, cwd):
    from aria_code.apps.cli.tools.system_tools import tool_run_command

    return tool_run_command({"command": command, "policy": "balanced", "cwd": str(cwd),
                             "permission_mode": "workspace-write", "network_enabled": False},
                            has_rich=False)


def test_a_failing_script_is_left_as_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    script = tmp_path / "chart.py"
    body = "print(json.dumps({'a': 1}))\ndf = None\n"
    script.write_text(body)

    result = run("python3 chart.py", tmp_path)
    assert result["data"]["exit_code"] != 0
    assert script.read_text() == body
    assert "add the missing import" in result["data"]["hint"]


def test_a_missing_module_is_not_installed(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    (tmp_path / "uses.py").write_text("import aria_no_such_module_xyz\n")
    calls = []
    real_run = subprocess.run

    def watch(cmd, *args, **kwargs):
        calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", watch)
    result = run("python3 uses.py", tmp_path)
    assert len(calls) == 1, calls
    assert "pip" not in " ".join(map(str, calls))
    assert "Module 'aria_no_such_module_xyz' is not installed" in result["data"]["hint"]


def test_a_command_that_succeeds_carries_no_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARIA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    assert "hint" not in run("python3 -c 'print(1)'", tmp_path)["data"]
