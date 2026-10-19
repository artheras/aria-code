"""The approval request carries the risk assessment as data, so the frontend can draw it."""
import os
import sys
from pathlib import Path

import pytest

from aria_code.apps.cli import app_server


class _Server(app_server.Server):
    def __init__(self):  # no worker, no stdio: only the assessment and the request shape
        self.sent = []
        self.approval_risk = None

    def ask(self, kind, **fields):
        self.sent.append((kind, fields))
        return {"choice": 0}


def test_a_command_is_assessed_for_its_card(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    card = _Server().assess_for_approval("run_command", {"command": "npm install jose"})
    assert card["level"] == 2 and card["name"] and 0 < card["score"] <= 100
    assert "package.install" in card["capabilities"] and card["reversible"] in (True, False)
    assert all(isinstance(card[k], list) for k in ("hosts", "files", "systems", "reasons"))


def test_an_unassessable_call_gives_no_card_and_does_not_raise(monkeypatch):
    monkeypatch.setattr("aria_code.safety.risk.assess_tool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _Server().assess_for_approval("run_command", {"command": "ls"}) is None


def test_the_request_includes_the_risk_but_a_picker_does_not():
    server = _Server()
    server.approval_risk = {"level": 2, "name": "medium", "score": 43}
    server.select([("Allow once", ""), ("Deny", "")], 0, "Approve")
    kind, fields = server.sent[-1]
    assert kind == "approval.requested" and fields["risk"]["level"] == 2
    server.select([("Gemini", "id")], 0, "Select model", picker=True, groups=["Aria"])
    assert "risk" not in server.sent[-1][1] and server.sent[-1][1]["picker"] is True


def test_an_approval_keeps_its_context_text_next_to_the_risk():
    server = _Server()
    server.approval_context = "Tool: write_file\nFile: a.txt"
    server.approval_risk = {"level": 1, "name": "low", "score": 10}
    server.select([("Allow once", "")], 0, "Approve")
    fields = server.sent[-1][1]
    assert "a.txt" in fields["context"] and fields["risk"]["level"] == 1
