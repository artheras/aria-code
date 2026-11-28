"""Host isolation and MCP JSON-RPC contract tests for services/aria-bots/erp_logistics.py."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

BOTS_SRC = Path(__file__).resolve().parents[1] / "services" / "aria-bots" / "src"
if str(BOTS_SRC) not in sys.path:
    sys.path.insert(0, str(BOTS_SRC))

from aria_bots import engine as bots_engine  # noqa: E402
from aria_bots import erp_logistics  # noqa: E402


class TestErpLogisticsHost(unittest.TestCase):
    def test_confine_refuses_files_without_data_dir(self):
        with self.assertRaises(ValueError):
            erp_logistics.confine({"file_path": "waybills.csv"}, None)

    def test_confine_blocks_traversal_and_allows_nested_file(self):
        root = Path("/tmp/aria-logistics-root").resolve()
        inside = erp_logistics.confine({"file_path": "sub/waybills.csv"}, root)
        self.assertEqual(inside["file_path"], str(root / "sub" / "waybills.csv"))

        with self.assertRaises(ValueError):
            erp_logistics.confine({"file_path": "../outside.csv"}, root)

        with self.assertRaises(ValueError):
            erp_logistics.confine({"file_path": "/etc/passwd"}, root)

    def test_mcp_initialize_and_tools_list(self):
        with mock.patch.dict(os.environ, {"ARIA_OWNER_SCOPE": "shipper-a"}):
            init = erp_logistics.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, None)
        self.assertIsNotNone(init)
        self.assertEqual(init["result"]["serverInfo"]["name"], "aria-logistics")
        self.assertIn("one shipper", init["result"]["instructions"])

        listed = erp_logistics.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)
        names = [t["name"] for t in listed["result"]["tools"]]
        self.assertTrue(
            {"analyze_logistics_data", "score_carriers", "plan_inventory_policy"}.issubset(names),
            names,
        )
        for tool in listed["result"]["tools"]:
            self.assertTrue(tool["annotations"]["readOnlyHint"])

    def test_mcp_tools_call_rejects_escaped_file_and_unknown_method(self):
        resp = erp_logistics.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "analyze_logistics_data", "arguments": {"file_path": "../secret.csv"}},
            },
            Path("/tmp/aria-logistics-root"),
        )
        self.assertTrue(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(payload["success"])

        unknown = erp_logistics.handle({"jsonrpc": "2.0", "id": 4, "method": "nonexistent"}, None)
        self.assertEqual(unknown["error"]["code"], -32601)

    def test_bots_find_engine_falls_back_to_aria_native(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "aria-native"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            with mock.patch.dict(os.environ, {"ARIA_RUNTIME": "", "ARIA_NATIVE_BINARY": str(fake_bin)}):
                with mock.patch("shutil.which", return_value=None):
                    self.assertEqual(bots_engine.find_engine(), str(fake_bin))


if __name__ == "__main__":
    unittest.main()
