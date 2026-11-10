"""Exercise coding startup with the optional finance stack genuinely unavailable."""
import os
from pathlib import Path
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_finance_is_optional_without_removing_full_install_capabilities():
    from packaging.requirements import Requirement
    def names(reqs):
        return {Requirement(req).name.replace("_", "-").lower() for req in reqs}
    finance = {"numpy", "pandas", "scipy", "pandas-ta", "yfinance"}
    assert not finance.intersection(names(PROJECT["dependencies"]))
    for profile in ("finance", "full", "all", "dev"):
        assert finance <= names(PROJECT["optional-dependencies"][profile])


def test_feature_extras_keep_their_dataframe_dependencies():
    from packaging.requirements import Requirement
    extras = PROJECT["optional-dependencies"]
    for profile in ("cn", "crypto", "charts", "data", "files"):
        assert "pandas" in {Requirement(req).name for req in extras[profile]}
    assert "yfinance" in {Requirement(req).name for req in extras["backtest"]}
    assert "numpy" in {Requirement(req).name for req in extras["sports"]}


def test_coding_terminal_and_file_read_work_without_finance(tmp_path):
    code = r'''
import asyncio
import importlib.abc
import socket
import sys
from pathlib import Path

blocked = {"numpy", "pandas", "scipy", "pandas_ta", "yfinance"}
class MissingFinance(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError("Optional package unavailable", name=fullname)
sys.meta_path.insert(0, MissingFinance())
def no_network(*args, **kwargs):
    raise AssertionError("This coding smoke test must not contact a service")
socket.socket.connect = no_network
socket.socket.connect_ex = no_network

import aria_code.aria_cli as cli
terminal = cli.ArtheraTerminal(dict(cli.DEFAULT_CONFIG))
Path("notes.txt").write_text("Local coding task evidence / 本地文件", encoding="utf-8")
result = terminal.tool_executor.execute_local("read_file", {"path": "notes.txt"})
assert result["success"], result
assert "Local coding task evidence" in str(result)

# The real agent loop still handles a model response; inference is a fixture.
from aria_code.runtime.gateway import run_turn
async def provider(*args, **kwargs):
    return {"success": True, "response": "Coding session ready", "tool_calls_pending": []}
turn = asyncio.run(run_turn("Explain this project", [], provider_fn=provider,
                            tool_executor=terminal.tool_executor))
assert turn.ok and turn.text == "Coding session ready", turn

# Calling a finance tool fails honestly with an installation hint, before IO.
market = terminal.tool_executor.execute_local("get_market_data", {"symbol": "AAPL"})
assert market["success"] is False, market
assert "aria-code[finance]" in market["error"], market
from aria_code.doctor import run_doctor
report = {row.name: row for row in run_doctor({}).checks}
for name in ("numpy", "pandas", "yfinance"):
    assert report["package:" + name].status == "skip"
assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)
print("coding, local files, agent loop and optional finance refusal passed")
'''
    env = dict(os.environ, HOME=str(tmp_path), ARIA_HOME=str(tmp_path / "state"),
               ARIA_USER_OUTPUT_ROOT=str(tmp_path / "outputs"), ARIA_FRONTEND="python",
               ARIA_OFFLINE="1",
               PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT / "src/aria_code"),
                                          os.environ.get("PYTHONPATH", "")]))
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


def test_doctor_does_not_report_missing_finance_as_a_broken_install(monkeypatch):
    from aria_code import doctor
    absent = {"numpy", "pandas", "yfinance"}
    real = doctor._has_module
    monkeypatch.setattr(doctor, "_has_module", lambda name: False if name in absent else real(name))
    checks = {row.name: row for row in doctor.run_doctor({}).checks}
    for name in absent:
        row = checks["package:" + name]
        assert row.status == "skip"
        assert "aria-code[finance]" in row.suggestion
