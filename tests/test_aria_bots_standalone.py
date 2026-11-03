"""services/aria-bots stands on its own: no aria_code import, and the old paths still resolve."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOTS = ROOT / "services" / "aria-bots" / "src"

MODULES = [
    "aria_bots.feishu_bot", "aria_bots.telegram_bot", "aria_bots.relay_client",
    "aria_bots.channels.conversation", "aria_bots.channels.approvals",
    "aria_bots.channels.digest", "aria_bots.channels.registry",
    "aria_bots.logistics.tenancy", "aria_bots.logistics.records",
    "aria_bots.logistics.inventory", "aria_bots.logistics.carriers",
    "aria_bots.logistics.freight",
]


def test_the_package_imports_without_aria_code(tmp_path):
    code = (
        "import importlib, sys\n"
        f"for name in {MODULES!r}: importlib.import_module(name)\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('aria_code', 'aria_cli', 'aria_daemon', 'apps', 'tools', 'clients', 'providers'))\n"
        "print(leaked)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(BOTS)
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip() == "[]", "aria_bots imported the Python core: " + out.stdout


def test_old_names_are_the_moved_modules():
    import importlib

    pairs = {
        "aria_feishu_bot": "aria_bots.feishu_bot",
        "aria_code.aria_feishu_bot": "aria_bots.feishu_bot",
        "aria_telegram_bot": "aria_bots.telegram_bot",
        "aria_code.clients.aria_relay_client": "aria_bots.relay_client",
        "aria_relay_client": "aria_bots.relay_client",
        "aria_code.apps.channels.conversation": "aria_bots.channels.conversation",
        "apps.channels.approvals": "aria_bots.channels.approvals",
        "aria_code.tools.logistics_tenancy": "aria_bots.logistics.tenancy",
        "tools.logistics_tools": "aria_bots.logistics.freight",
    }
    for old, new in pairs.items():
        assert importlib.import_module(old) is importlib.import_module(new), old


def test_the_shim_runs_the_relay_client_as_a_script():
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "ARIA_RELAY_URL")}
    env["PYTHONPATH"] = str(ROOT / "src")
    out = subprocess.run([sys.executable, "-m", "aria_code.clients.aria_relay_client", "--help"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-1500:]
    assert "--once" in out.stdout
