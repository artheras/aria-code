"""A bot turn on the real Go engine reaches the logistics MCP server, scoped to the chat's shipper."""
import asyncio
import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WAYBILLS = [
    {"owner_id": "ACME", "carrier": "SF", "total_cost": 120, "origin": "SH", "destination": "BJ",
     "billed_weight_kg": 10, "is_on_time": True, "waybill_no": "A-1"},
    {"owner_id": "BETA", "carrier": "ZTO", "total_cost": 80, "origin": "SH", "destination": "BJ",
     "billed_weight_kg": 10, "is_on_time": False, "waybill_no": "B-1"},
]


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


@pytest.fixture
def model(tmp_path, monkeypatch):
    binary = os.environ.get("ARIA_GO_RUNTIME_BINARY")
    if not binary:
        pytest.skip("Build the native Go application first")
    state = tmp_path / "state"
    state.mkdir()
    (state / "config.json").write_text(json.dumps(dict(local_provider="google", model="gemini-test",
                                                       use_vertexai=False, api_key="fixture-only-key")))
    seen = {"tools": [], "results": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen["tools"] = [f["name"] for t in body.get("tools") or [] for f in t.get("functionDeclarations") or []]
            part = body["contents"][-1]["parts"][0]
            if "functionResponse" in part:
                result = part["functionResponse"]["response"]
                seen["results"].append(result)
                output = [{"text": "RESULT " + json.dumps(result, ensure_ascii=False)}]
            else:
                output = [{"functionCall": {"name": "mcp__aria-logistics__score_carriers",
                                            "args": {"waybills": WAYBILLS, "min_shipments": 1}}}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            record = dict(candidates=[dict(content=dict(role="model", parts=output), finishReason="STOP")])
            self.wfile.write(("data: " + json.dumps(record) + "\n\n").encode())

    server = LocalServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for key, value in dict(ARIA_HOME=str(state), ARIA_BOTS_HOME=str(tmp_path / "bots"), ARIA_SELF_MCP="0",
                           GOOGLE_GEMINI_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                           GOOGLE_GENAI_USE_VERTEXAI="false", NO_PROXY="127.0.0.1,localhost",
                           no_proxy="127.0.0.1,localhost",
                           PYTHONPATH=os.pathsep.join([str(ROOT / "services" / "aria-bots" / "src"),
                                                       os.environ.get("PYTHONPATH", "")])).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ARIA_OWNER_SCOPE", raising=False)
    monkeypatch.delenv("ARIA_BOT_ALLOW_TOOLS", raising=False)
    yield binary, seen
    server.shutdown()
    server.server_close()


def test_a_bound_chat_gets_only_its_shippers_data(model):
    from aria_bots import engine

    binary, seen = model
    answer = asyncio.run(engine.run_turn("score our carriers", engine=binary, owner="ACME", timeout=60))
    assert "mcp__aria-logistics__score_carriers" in seen["tools"], seen["tools"]
    (result,) = seen["results"]
    assert result["success"], result
    assert "SF" in result["data"]["content"] and "ZTO" not in result["data"]["content"]
    assert answer.startswith("RESULT"), answer


def test_an_unbound_chat_cannot_mix_shippers(model):
    from aria_bots import engine

    binary, seen = model
    asyncio.run(engine.run_turn("score our carriers", engine=binary, timeout=60))
    (result,) = seen["results"]
    assert not result["success"] and "shippers" in result["error"]
