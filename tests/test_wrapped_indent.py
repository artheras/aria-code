"""Wrapped lines stay under their indent in a narrow terminal.

Recorded at 80 columns: /inventory and /carriers lines, the backtest summary
and the first-run note all wrapped back to column 0, because their indent was
typed as leading spaces that Rich does not carry onto the next line.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console


def test_a_logistics_line_wraps_under_its_indent():
    from aria_code.apps.cli.commands.warehouse_cmds import _emit

    console = Console(file=io.StringIO(), width=60, color_system=None)
    owner = SimpleNamespace(context=SimpleNamespace(has_rich=True, console=console))
    _emit(owner, "  Reorder A100: order 335 · reorder point 353 · safety stock 71 · 7.4 days of cover", "")
    lines = console.file.getvalue().rstrip("\n").splitlines()
    assert len(lines) > 1                                   # it did wrap
    assert all(line.startswith("  ") for line in lines)     # every row under the indent


def test_a_logistics_line_is_plain_text():
    from aria_code.apps.cli.commands.warehouse_cmds import _emit

    console = Console(file=io.StringIO(), width=80, color_system=None)
    owner = SimpleNamespace(context=SimpleNamespace(has_rich=True, console=console))
    _emit(owner, "SKU [A-100] in file [bold].csv", "bold")
    assert "SKU [A-100] in file [bold].csv" in console.file.getvalue()


def test_a_review_finding_wraps_under_its_title():
    """Hanging-indent helper keeps wrapped explanation rows under the 3-space indent."""
    from aria_code.ui.render.output import print_hanging

    console = Console(file=io.StringIO(), width=70, color_system=None)
    print_hanging(
        console,
        "   ",
        "When `amount` or `rate` is an infinite float, their product is Decimal('Infinity') and "
        "quantize() raises decimal.InvalidOperation; the function should handle that case.",
    )
    body = [line for line in console.file.getvalue().splitlines() if line]
    assert len(body) > 1 and all(line.startswith("   ") for line in body)
