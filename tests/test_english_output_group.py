"""English sessions get English output, real dates and real values.

Recorded in an English session: /macro headed "🇺🇸 美国宏观" with Chinese
labels, a flag emoji that broke the rule line, and the Bank of England rate
as 0.25% (FRED's BOERUKM stopped in January 2017); /screen showed no P/E for
any stock, so its value screen matched nothing; /carriers printed a raw
"[Errno 2]"; /warehouse gave its usage in Chinese; and /report, whose prompt
was routed back to /report, wrote everything twice.
"""

from __future__ import annotations

import asyncio
import io
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from rich.console import Console


def _console():
    return Console(file=io.StringIO(), width=100, color_system=None)


def test_a_missing_logistics_file_names_the_files_that_are_there(tmp_path):
    pytest.importorskip("erp_service")
    from erp_service.logistics.records import load_records

    (tmp_path / "waybills.csv").write_text("a\n1\n")
    with pytest.raises(ValueError) as info:
        load_records({"file_path": str(tmp_path / "shipments.csv")}, "waybills")
    assert "No such file" in str(info.value) and "waybills.csv" in str(info.value)
    assert "Errno" not in str(info.value)


def test_only_typed_text_is_routed(monkeypatch):
    """A prompt a command builds goes to the model, not back to a command."""
    import inspect

    from aria_code.apps.cli.chat_turn import ChatTurnMixin

    source = inspect.getsource(ChatTurnMixin.send_message)
    assert "route_text: bool = False" in source
    assert "route_text\n            and not system_override" in source
    import aria_code.aria_cli as cli
    assert "await self.send_message(user_input, route_text=True)" in inspect.getsource(cli)
