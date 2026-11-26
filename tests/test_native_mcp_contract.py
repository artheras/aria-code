"""End-to-end stdio JSON-RPC 2.0 contract tests for `aria-native mcp-serve`."""
from __future__ import annotations

import json
import os
import subprocess
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ARIA_NATIVE_BINARY"),
    reason="build aria-native and set ARIA_NATIVE_BINARY to run native MCP contract tests",
)


def test_native_mcp_serve_handshake_and_write_policy(tmp_path):
    binary = os.environ["ARIA_NATIVE_BINARY"]
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "write_file",
                "arguments": {"path": "blocked.txt", "content": "should fail without --allow-write"},
            },
        }),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "unknown/method"}),
    ]) + "\n"

    proc = subprocess.run(
        [binary, "-C", str(tmp_path), "mcp-serve"],
        input=requests,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert [msg["id"] for msg in lines] == [1, 2, 3, 4]
    assert lines[0]["result"]["serverInfo"]["name"] == "aria-native"
    tool_names = {t["name"] for t in lines[1]["result"]["tools"]}
    assert {"read_file", "search_code", "sql_query_readonly"}.issubset(tool_names)
    assert lines[2]["result"]["isError"] is True
    assert not (tmp_path / "blocked.txt").exists()
    assert lines[3]["error"]["code"] == -32601


def test_native_mcp_serve_skills_list_and_audit(tmp_path):
    binary = os.environ["ARIA_NATIVE_BINARY"]
    skill_dir = tmp_path / ".aria" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: Demo MCP skill\n---\n\nHello\n")

    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "skills/list", "params": {}}),
        json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "sql_query_readonly",
                "arguments": {"db": "missing.db", "sql": "DROP TABLE users"},
            },
        }),
    ]) + "\n"

    proc = subprocess.run(
        [binary, "-C", str(tmp_path), "mcp-serve"],
        input=requests,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
    assert [msg["id"] for msg in lines] == [1, 2]
    skills = lines[0]["result"]["skills"]
    assert any(s["name"] == "demo-skill" for s in skills)
    assert lines[1]["result"]["isError"] is True
    audit_file = tmp_path / ".aria" / "audit.jsonl"
    assert audit_file.exists()
    assert "sql_query_readonly" in audit_file.read_text()
