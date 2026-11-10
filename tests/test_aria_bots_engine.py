"""The bots answer through the Go engine and give it the logistics tools over MCP."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from aria_bots import engine
from aria_bots.logistics import mcp_server

WAYBILLS = [
    {"owner_id": "ACME", "carrier": "SF", "total_cost": 120, "origin": "SH", "destination": "BJ",
     "billed_weight_kg": 10, "is_on_time": True, "waybill_no": "A-1"},
    {"owner_id": "BETA", "carrier": "ZTO", "total_cost": 80, "origin": "SH", "destination": "BJ",
     "billed_weight_kg": 10, "is_on_time": False, "waybill_no": "B-1"},
]


def rpc(message, data_dir=None):
    return mcp_server.handle(message, data_dir)


def call(name, arguments, data_dir=None):
    result = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}}, data_dir)["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


class TestLogisticsMCPServer:
    def test_three_read_only_tools(self):
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init["result"]["protocolVersion"] == "2024-11-05"
        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert sorted(t["name"] for t in tools) == ["analyze_logistics_data", "plan_inventory_policy", "score_carriers"]
        assert all(t["annotations"]["readOnlyHint"] is True for t in tools)
        assert all(t["inputSchema"]["type"] == "object" for t in tools)
        assert rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert rpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["error"]["code"] == -32601

    def test_the_scope_comes_from_the_environment_not_the_model(self, monkeypatch):
        monkeypatch.delenv("ARIA_OWNER_SCOPE", raising=False)
        failed, body = call("score_carriers", {"waybills": WAYBILLS, "min_shipments": 1})
        assert failed and "shippers" in body["error"]
        monkeypatch.setenv("ARIA_OWNER_SCOPE", "ACME")
        failed, body = call("score_carriers", {"waybills": WAYBILLS, "min_shipments": 1})
        assert not failed and "ZTO" not in json.dumps(body["data"]["scorecard"])
        failed, body = call("score_carriers", {"waybills": WAYBILLS, "owner_id": "BETA"})
        assert failed, "a bound chat cannot ask for another shipper"
        failed, body = call("score_carriers", {"waybills": WAYBILLS, "all_owners": True})
        assert failed

    def test_files_only_from_the_data_folder(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ARIA_OWNER_SCOPE", raising=False)
        data = tmp_path / "data"
        data.mkdir()
        (data / "w.json").write_text(json.dumps(WAYBILLS[:1]))
        (tmp_path / "secret.json").write_text(json.dumps(WAYBILLS[:1]))
        failed, body = call("score_carriers", {"file_path": str(data / "w.json")})
        assert failed and "not available" in body["error"], "no data folder: no files at all"
        failed, body = call("score_carriers", {"file_path": "w.json", "min_shipments": 1}, data)
        assert not failed, body
        for outside in (str(tmp_path / "secret.json"), "../secret.json"):
            failed, body = call("score_carriers", {"file_path": outside}, data)
            assert failed and "inside" in body["error"], outside

    def test_unknown_tool_and_bad_json(self):
        result = rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope"}})["result"]
        assert result["isError"]
        import io
        out = io.StringIO()
        mcp_server.serve(io.StringIO("not json\n\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"), out, None)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        assert replies[0]["error"]["code"] == -32700 and replies[1]["result"] == {}


def test_the_server_speaks_utf8_whatever_the_console_encoding():
    """Windows defaults stdio to cp1252; the tool list has Chinese text."""
    import subprocess

    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    src = Path(mcp_server.__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(src)}
    out = subprocess.run([sys.executable, "-m", "aria_bots.logistics.mcp_server"], env=env, timeout=60,
                         input="".join(json.dumps(m) + "\n" for m in lines).encode(), capture_output=True)
    assert out.returncode == 0, out.stderr.decode(errors="replace")[-800:]
    replies = [json.loads(line) for line in out.stdout.decode("utf-8").split("\n") if line]
    assert len(replies[1]["result"]["tools"]) == 3 and b"\r" not in out.stdout


class TestEngine:
    def test_engine_lookup(self, tmp_path, monkeypatch):
        fake = tmp_path / "aria-runtime"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        other = tmp_path / "python3"
        other.write_text("")
        other.chmod(0o755)
        monkeypatch.setattr(engine.shutil, "which", lambda name: None)
        monkeypatch.setenv("ARIA_RUNTIME", str(fake))
        assert engine.find_engine() == str(fake)
        monkeypatch.setenv("ARIA_RUNTIME", str(other))
        assert engine.find_engine() is None, "ARIA_RUNTIME naming another program is ignored"
        monkeypatch.delenv("ARIA_RUNTIME")
        assert engine.find_engine() is None

    def test_command_environment_and_history(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ARIA_BOTS_HOME", str(tmp_path))
        monkeypatch.setenv("ARIA_OWNER_SCOPE", "LEAKED")
        monkeypatch.delenv("ARIA_BOT_ALLOW_TOOLS", raising=False)
        assert engine.command("/x/aria-runtime", "hi") == [
            "/x/aria-runtime", "stream", "--format=jsonl", "--quiet", "--no-banner", "--prompt=hi"]
        monkeypatch.setenv("ARIA_BOT_ALLOW_TOOLS", "web_fetch, ")
        assert engine.command("e", "hi")[-2:] == ["--allow-tools", "web_fetch"]
        env = engine.environment(None)
        assert "ARIA_OWNER_SCOPE" not in env, "an unbound chat never inherits a scope"
        assert engine.environment("ACME")["ARIA_OWNER_SCOPE"] == "ACME"
        config = json.loads(Path(env["ARIA_MCP_EXTRA_CONFIG"]).read_text())
        (server,) = config["servers"]
        assert server["name"] == "aria-logistics" and server["args"] == ["-m", "aria_bots.logistics.mcp_server"]
        prompt = engine.with_history("now?", [{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"}])
        assert prompt.index("User: first") < prompt.index("Assistant: ok") < prompt.index("now?")
        assert engine.with_history("now?", []) == "now?"

    def test_read_answer(self):
        lines = [{"type": "turn.started"}, {"type": "answer.delta", "text": "par"},
                 {"type": "answer.delta", "text": "tial"}]
        raw = "\n".join(json.dumps(e) for e in lines).encode()
        assert engine.read_answer(raw) == (False, "partial")
        done = raw + b"\n" + json.dumps({"type": "turn.completed", "success": True, "response": "final"}).encode()
        assert engine.read_answer(done) == (True, "final")


def fake_runtime(tmp_path: Path) -> Path:
    """Stands in for aria-runtime: checks the handshake, answers with what it was given."""
    script = tmp_path / "aria-runtime"
    script.write_text(f"""#!{sys.executable}
import json, os, sys
assert sys.stdin.read() == "\\n", "host acknowledgement then EOF"
prompt = [a for a in sys.argv if a.startswith("--prompt=")][0][9:]
reply = {{"prompt": prompt, "scope": os.environ.get("ARIA_OWNER_SCOPE"), "cwd": os.getcwd(),
          "argv": sys.argv[1:6], "mcp": os.environ.get("ARIA_MCP_EXTRA_CONFIG")}}
print(json.dumps({{"type": "turn.started"}}))
print(json.dumps({{"type": "turn.completed", "success": True, "response": json.dumps(reply)}}))
""")
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in executable")
def test_the_feishu_bot_answers_through_the_engine(tmp_path, monkeypatch):
    from aria_bots import feishu_bot as bot

    monkeypatch.setenv("ARIA_BOTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARIA_RUNTIME", str(fake_runtime(tmp_path)))

    async def go():
        token = bot._TURN_OWNER.set("ACME")
        try:
            with mock.patch.object(bot, "_ARIA_CODE_DIR", tmp_path / "no-cli-here"):
                command = await bot._query_aria_llm("/inventory", timeout=20)
                chat = await bot._query_aria_direct("now?", timeout=20,
                                                    history=[{"role": "user", "content": "first"}])
        finally:
            bot._TURN_OWNER.reset(token)
        return json.loads(command), json.loads(chat)

    command, chat = asyncio.run(go())
    assert command["prompt"] == "/inventory" and command["scope"] == "ACME"
    assert command["argv"] == ["stream", "--format=jsonl", "--quiet", "--no-banner", "--prompt=/inventory"]
    assert Path(command["cwd"]) == (tmp_path / "home" / "workspace").resolve()
    assert command["mcp"] == str(tmp_path / "home" / "mcp_servers.json")
    assert "User: first" in chat["prompt"] and chat["scope"] == "ACME"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in executable")
def test_a_failed_or_slow_engine_turn_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_BOTS_HOME", str(tmp_path / "home"))
    broken = tmp_path / "aria-runtime"
    broken.write_text("#!/bin/sh\necho 'no provider configured' >&2\nexit 1\n")
    broken.chmod(0o755)
    answer = asyncio.run(engine.run_turn("hi", engine=str(broken), timeout=20))
    assert answer.startswith("❌") and "no provider configured" in answer
    slow = tmp_path / "slow" / "aria-runtime"
    slow.parent.mkdir()
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    assert asyncio.run(engine.run_turn("hi", engine=str(slow), timeout=1)).startswith("⏱️")
    assert asyncio.run(engine.run_turn("x" * 70000, engine=str(slow))).startswith("❌")


class TestMarketCommands:
    def test_price_card(self):
        from aria_bots import market

        title, body, color = market.price_card({"symbol": "600036.SS", "price": 39.5, "change_pct": 1.2,
                                                "currency": "CNY", "source": "fixture"})
        assert title == "📈 600036.SS" and "¥39.500" in body and "+1.20%" in body and color == "red"
        assert market.price_card({"symbol": "X", "price": 1, "change_pct": -0.5})[2] == "green"
        assert market.price_card({"symbol": "X", "price": 1})[2] == "blue"

    def test_the_feishu_commands_use_the_go_daemon(self, monkeypatch):
        from aria_bots import feishu_bot as bot
        from aria_bots import market

        cards, calls = [], []

        async def card(message_id, title, body, *args, **kwargs):
            cards.append((title, body))

        async def text(message_id, body):
            cards.append(("text", body))

        async def daemon(*args, timeout=60):
            calls.append(args)
            if args[0] == "quote":
                return json.dumps({"symbol": "AAPL", "price": 201.5, "change_pct": 1.0, "currency": "USD"})
            if args[0] == "once":
                return json.dumps({"title": "📊 Aria 晨报", "body": "indices"})
            if args[:2] == ("alert", "list"):
                return "ID  SYMBOL\nab  AAPL"
            return "ok"

        monkeypatch.setattr(market, "daemon", daemon)
        monkeypatch.setattr(bot, "reply_card", card)
        monkeypatch.setattr(bot, "reply_text", text)

        async def go():
            for cmd in ("/price aapl", "/brief", "/alert add AAPL price_above 200",
                        "/alert add AAPL sideways 1", "/alerts", "/status"):
                await bot._handle_command(cmd, "m1", "ou_1", "c1")

        asyncio.run(go())
        assert calls == [("quote", "AAPL", "--json"), ("once", "morning-brief", "--json"),
                         ("alert", "add", "AAPL", "price_above", "200"), ("alert", "list"), ("status",)]
        titles = [t for t, _ in cards]
        assert "📈 AAPL" in titles and "📊 Aria 晨报" in titles and "✅ 预警已添加" in titles
        assert "❌ 无效条件" in titles, "a bad condition never reaches the daemon"
        assert any(t.startswith("📋 预警（1") for t in titles)


class TestTelegramCommands:
    def test_market_commands_and_engine_fallthrough(self, monkeypatch):
        from aria_bots import engine, market, telegram_bot

        calls = []

        async def daemon(*args, timeout=60):
            calls.append(args)
            if args[0] == "quote":
                return json.dumps({"symbol": "AAPL", "price": 201.5, "change_pct": -0.4, "currency": "USD"})
            if args[0] == "once":
                return json.dumps({"title": "📊 Aria 晨报", "body": "indices"})
            if args[:2] == ("alert", "add") and args[3] not in market.CONDITIONS:
                raise market.MarketError("condition must be ...")
            return "ID  SYMBOL\nab  AAPL" if args[:2] == ("alert", "list") else "ok"

        turns = []

        async def run_turn(prompt, *, engine, timeout=120, **kwargs):
            turns.append(prompt)
            return "engine says hi"

        monkeypatch.setattr(market, "daemon", daemon)
        monkeypatch.setattr(engine, "run_turn", run_turn)
        monkeypatch.setattr(engine, "find_engine", lambda: "/x/aria-runtime")

        async def go():
            return [await telegram_bot.handle_command(cmd, args, 1) for cmd, args in [
                ("price", "aapl"), ("brief", ""), ("alert", "AAPL price_above 200"), ("alert", "AAPL"),
                ("alert", "AAPL up 1"), ("alerts", ""), ("status", ""), ("report", "NVDA"), ("chat", "你好"),
                ("help", ""), ("screen", "")]]

        price, brief, added, usage, bad, alerts, status, report, chat, help_text, unknown = asyncio.run(go())
        assert "AAPL" in price and "-0.40%" in price
        assert "晨报" in brief and "✅" in added and "用法" in usage and bad.startswith("⚠️")
        assert "```" in alerts and status == "ok"
        assert report == chat == "engine says hi" and "NVDA" in turns[0] and turns[1] == "你好"
        assert "/price" in help_text and "screen" not in help_text
        assert "未知命令" in unknown
        assert ("quote", "AAPL", "--json") in calls

    def test_allowed_ids_and_missing_token(self, monkeypatch):
        from aria_bots import telegram_bot

        assert telegram_bot.allowed_ids("1, -100200, x,") == {1, -100200}
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert telegram_bot.main() == 2


def test_football_and_swarm_are_gone():
    from aria_bots import feishu_bot

    source = Path(feishu_bot.__file__).read_text(encoding="utf-8")
    for gone in ("football", "swarm", "yfinance", "aria_daemon"):
        assert gone not in source.lower(), gone
