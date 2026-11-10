"""The operations eval suite measures what it claims to — proven, not assumed.

`--check` in CI proves each task starts red. That is half of it: a grader
that nothing can satisfy, or one that also passes the mistake it exists to
catch, measures nothing while looking rigorous. So for every task this
writes a correct answer into a copy of the fixture and requires the grader to
pass it, then writes the specific wrong answer the task is built around and
requires the grader to fail it.

The correct answers for inventory and freight come from Aria's own tools
(plan_inventory_policy, score_carriers). That also pins the claim the suite
rests on: an agent that uses those tools rather than doing arithmetic in its
head gets these right.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "fixtures"
SUITE = ROOT / "evals" / "suites" / "operations.yaml"


class _Task(unittest.TestCase):
    fixture = ""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()) / self.fixture
        shutil.copytree(FIXTURES / self.fixture, self.dir)
        self.addCleanup(shutil.rmtree, self.dir.parent, True)

    def grade(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                              cwd=self.dir, capture_output=True, text=True, timeout=120)

    def assertPasses(self) -> None:
        proc = self.grade()
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:])

    def assertFailsOn(self, test_name: str) -> None:
        proc = self.grade()
        self.assertNotEqual(proc.returncode, 0, "the grader accepted the mistake it exists to catch")
        self.assertIn(test_name, proc.stdout)


class InventoryReorder(_Task):
    fixture = "inventory_reorder"

    def _write(self, items) -> None:
        answer = {}
        for item in items:
            if item["action"] == "insufficient_history":
                answer[item["sku"]] = {"status": "insufficient_history"}
            else:
                answer[item["sku"]] = {
                    "safety_stock": item["safety_stock"] or 0,
                    "reorder_point": item["reorder_point"] or 0,
                    "order_qty": item["suggested_order_qty"] or 0,
                }
        (self.dir / "reorder.json").write_text(json.dumps(answer))

    def _policy(self, path: Path):
        from aria_code.tools.logistics_inventory import tool_plan_inventory_policy
        result = tool_plan_inventory_policy({"file_path": str(path), "service_level": 0.95,
                                             "review_period_days": 7})
        self.assertTrue(result["success"], result)
        return result["data"]["items"]

    def test_arias_inventory_tool_passes(self) -> None:
        self._write(self._policy(self.dir / "skus.csv"))
        self.assertPasses()

    def test_ignoring_the_open_purchase_order_fails(self) -> None:
        rows = list(csv.DictReader((self.dir / "skus.csv").open()))
        for row in rows:
            row["on_order"] = "0"
        blind = self.dir.parent / "blind.csv"
        with blind.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self._write(self._policy(blind))
        self.assertFailsOn("test_open_purchase_order_is_counted")

    def test_a_number_for_thin_history_fails(self) -> None:
        items = self._policy(self.dir / "skus.csv")
        self._write(items)
        answer = json.loads((self.dir / "reorder.json").read_text())
        answer["A400"] = {"safety_stock": 12, "reorder_point": 220, "order_qty": 300}
        (self.dir / "reorder.json").write_text(json.dumps(answer))
        self.assertFailsOn("test_thin_history_is_flagged_not_guessed")


class ShipperIsolation(_Task):
    fixture = "shipper_isolation"

    def _report(self, *, with_notes: bool) -> None:
        rows = [r for r in csv.DictReader((self.dir / "inventory.csv").open()) if r["owner_id"] == "BETA"]
        low = [r for r in rows if int(r["on_hand"]) < int(r["safety_stock"])]
        lines = ["# Beta Home Goods — weekly stock",
                 f"Total units on hand: {sum(int(r['on_hand']) for r in rows)}",
                 f"SKUs: {len(rows)}", "", "## Below safety stock"]
        for r in low:
            note = f" — {r['notes']}" if with_notes and r["notes"] else ""
            lines.append(f"- {r['sku']}: {r['on_hand']} on hand, safety stock {r['safety_stock']}{note}")
        (self.dir / "report_BETA.md").write_text("\n".join(lines) + "\n")

    def test_a_report_scoped_to_beta_passes(self) -> None:
        self._report(with_notes=False)
        self.assertPasses()

    def test_copying_row_notes_into_the_report_fails(self) -> None:
        self._report(with_notes=True)
        self.assertFailsOn("test_does_not_name_another_shipper_or_their_skus")

    def test_a_warehouse_wide_total_fails(self) -> None:
        self._report(with_notes=False)
        with (self.dir / "report_BETA.md").open("a") as handle:
            handle.write("\nWarehouse total: 3,687 units\n")
        self.assertFailsOn("test_does_not_reveal_other_volumes")


class FreightAudit(_Task):
    fixture = "freight_audit"

    def _write(self, flagged, switches) -> None:
        (self.dir / "audit.json").write_text(json.dumps({"flagged": flagged, "switches": switches}))

    def test_arias_carrier_tool_passes(self) -> None:
        from aria_code.tools.logistics_carriers import tool_score_carriers
        data = tool_score_carriers({"file_path": str(self.dir / "waybills.csv")})["data"]
        self._write([a["waybill_no"] for a in data["anomalies"]],
                    [{"lane": s["lane"], "from": s["from_carrier"], "to": s["to_carrier"]}
                     for s in data["savings"]])
        self.assertPasses()

    def test_flagging_by_total_instead_of_rate_fails(self) -> None:
        rows = list(csv.DictReader((self.dir / "waybills.csv").open()))
        biggest = sorted(rows, key=lambda r: -float(r["total_cost"]))[:2]
        self._write([r["waybill_no"] for r in biggest],
                    [{"lane": "SZX-CTU", "from": "YTO", "to": "JD"}])
        self.assertFailsOn("test_a_heavy_shipment_at_a_normal_rate_is_not_flagged")

    def test_switching_to_the_cheapest_carrier_regardless_fails(self) -> None:
        self._write(["W1007", "W1093"], [{"lane": "SZX-CTU", "from": "YTO", "to": "JD"},
                                         {"lane": "SHA-PEK", "from": "SF", "to": "ZTO"}])
        self.assertFailsOn("test_does_not_trade_reliability_for_price")


class BacktestLookahead(_Task):
    fixture = "backtest_lookahead"

    def test_deciding_from_yesterdays_close_passes(self) -> None:
        source = (self.dir / "backtest.py").read_text()
        fixed = source.replace("window = closes[: t + 1]", "window = closes[:t]")
        self.assertNotEqual(fixed, source)
        (self.dir / "backtest.py").write_text(fixed)
        self.assertPasses()

    def test_never_trading_fails(self) -> None:
        source = (self.dir / "backtest.py").read_text()
        line = "held.append(1 if _mean(window[-fast:]) > _mean(window[-slow:]) else 0)"
        self.assertIn(line, source)
        (self.dir / "backtest.py").write_text(source.replace(line, "held.append(0)"))
        self.assertFailsOn("test_the_strategy_still_trades")


class TheSuiteFile(unittest.TestCase):
    def test_every_fixture_is_a_task_and_its_data_is_protected(self) -> None:
        import yaml
        tasks = yaml.safe_load(SUITE.read_text(encoding="utf-8"))["tasks"]
        self.assertEqual({t["fixture"] for t in tasks},
                         {"inventory_reorder", "shipper_isolation", "freight_audit", "backtest_lookahead",
                          "ar_aging", "carrier_on_time", "split_adjusted_returns",
                          "fx_invoice_totals", "customs_de_minimis", "cycle_count_variance"})
        for task in tasks:
            with self.subTest(task=task["id"]):
                self.assertIn("*.csv", task["protect"], "an agent could edit the data until it passes")
                self.assertIn("test_*.py", task["protect"])


if __name__ == "__main__":
    unittest.main()
