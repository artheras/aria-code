"""Tests for ecosystem MCP configuration helpers in packages.aria_mcp.config."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from aria_code.packages.aria_mcp.config import (
    erp_service_server_config,
    healthcare_service_server_config,
    install_ecosystem_service_configs,
)


def test_ecosystem_server_configs_use_isolated_mode():
    py = Path(sys.executable)
    hc = healthcare_service_server_config(py)
    erp = erp_service_server_config(py)

    assert hc["name"] == "aria-healthcare"
    assert hc["args"] == ["-I", "-m", "aria_healthcare.server"]
    assert "env" not in hc

    assert erp["name"] == "erp-service"
    assert erp["args"] == ["-I", "-m", "erp_service.mcp_server"]
    assert "env" not in erp


def test_install_ecosystem_service_configs_preserves_existing_servers(tmp_path):
    py = Path(sys.executable)
    cfg_path = tmp_path / "mcp_servers.json"
    cfg_path.write_text(
        json.dumps({"servers": [{"name": "custom-tool", "command": "echo", "args": ["ok"]}]}),
        encoding="utf-8",
    )

    install_ecosystem_service_configs(
        cfg_path,
        financial_python=py,
        include_trading=True,
        healthcare_python=py,
        erp_python=py,
    )

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    names = [entry["name"] for entry in saved["servers"]]
    assert names == [
        "custom-tool",
        "arthera-finance",
        "arthera-trading",
        "aria-healthcare",
        "erp-service",
    ]
