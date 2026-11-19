"""Actual native bridge effects and compatibility with existing file handlers."""
import json
import os
import subprocess

import pytest

from aria_code.apps.cli.native_bridge import dispatch


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    binary = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not binary:
        pytest.skip("Build the native Go application before exercising the bridge")
    project = tmp_path / "project"
    state = tmp_path / "state"
    project.mkdir()
    state.mkdir()
    monkeypatch.setenv("ARIA_HOME", str(state))
    env = dict(os.environ, ARIA_PYTHON="missing-python-for-this-test")

    def call(request, approved=""):
        args = [binary, "bridge", "--workspace", str(project)]
        if approved:
            args += ["--approve-tool", approved]
        result = subprocess.run(args, input="\n" + json.dumps(request) + "\n", env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout) if result.stdout else None
    return project, state, call


def request(name, **arguments):
    return dict(jsonrpc="2.0", id="request-你好", method="tools.call",
                params=dict(name=name, arguments=arguments))


@pytest.mark.parametrize("name,arguments", [
    ("read_file", dict(path="你好.txt")),
    ("read_file", dict(path="你好.txt", offset=1, limit=1)),
    ("list_files", dict(path=".", pattern="*")),
    ("search_code", dict(path=".", pattern="needle", glob="**/*.rs")),
])
def test_native_bridge_matches_existing_read_results(bridge, name, arguments):
    project, _, call = bridge
    (project / "你好.txt").write_text("first line\n你好 needle\nthird line\n", encoding="utf-8")
    (project / "source.rs").write_text("// needle evidence\nfn main() {}\n", encoding="utf-8")
    req = request(name, **arguments)
    want = dispatch(req, project)
    got = call(req)
    assert want["result"]["success"], want
    assert got == want


def test_native_bridge_approved_edits_are_actual_and_keep_crlf(bridge):
    project, state, call = bridge
    path = project / "note.txt"
    req = request("write_file", path="note.txt", content="A complete file body.\r\n")
    assert not call(req)["result"]["success"]
    assert not path.exists()
    assert call(req, "write_file")["result"]["success"]
    assert path.read_bytes() == b"A complete file body.\r\n"
    edit = request("edit_file", path="note.txt", old_string="complete", new_string="verified")
    assert call(edit, "edit_file")["result"]["success"]
    assert path.read_bytes() == b"A verified file body.\r\n"
    (state / "tool_policy.json").write_text(json.dumps(dict(denied=["edit_file"])), encoding="utf-8")
    edit["params"]["arguments"].update(old_string="verified", new_string="blocked")
    assert not call(edit, "edit_file")["result"]["success"]
    assert path.read_bytes() == b"A verified file body.\r\n"


def test_native_bridge_notification_and_forged_scope_never_execute(bridge):
    project, _, call = bridge
    notification = request("write_file", path="new.txt", content="A complete file body.")
    del notification["id"]
    assert call(notification, "write_file") is None
    assert not (project / "new.txt").exists()
    outside = project.parent / "private.txt"
    outside.write_text("private document", encoding="utf-8")
    forged = request("read_file", path="../private.txt", _allowed_read_roots=[str(project.parent)])
    assert not call(forged)["result"]["success"]
    write = request("write_file", path="../private.txt", content="Overwrite private document.")
    assert not call(write, "write_file")["result"]["success"]
    assert outside.read_text(encoding="utf-8") == "private document"


def test_actual_rust_tool_routes_to_go_without_python(bridge):
    native = os.environ.get("ARIA_NATIVE_BINARY")
    if not native:
        pytest.skip("Build the Rust frontend for the complete process-boundary test")
    project, _, _ = bridge
    (project / "note.txt").write_text("native process boundary evidence", encoding="utf-8")
    env = dict(os.environ, ARIA_RUNTIME=os.environ["ARIA_GO_RUNTIME_BINARY"], ARIA_PYTHON="missing-python-for-this-test")
    result = subprocess.run([native, "-C", str(project), "tool", "read_file", json.dumps(dict(path="note.txt"))],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert result.returncode == 0, result.stderr
    assert "native process boundary evidence" in json.loads(result.stdout)["result"]["data"]["content"]
