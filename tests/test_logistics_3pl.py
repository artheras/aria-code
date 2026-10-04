"""Logistics analyses for third-party-logistics clients.

Two properties matter more than any single figure:

1. Shipper isolation. A 3PL holds many clients' data side by side; an
   analysis that mixes them hands one client's commercial data to another.
   Every analysis must refuse mixed data unless told which shipper — and a
   refusal must not itself name the other shippers.

2. Every number can be recomputed by hand. The expected values below were
   worked out independently of the code, from the formulas stated in the
   results, so these tests check the arithmetic rather than restating it.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "src" / "aria_code"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aria_code.tools.logistics_carriers import tool_score_carriers, wilson_lower_bound  # noqa: E402
from aria_code.tools.logistics_inventory import tool_plan_inventory_policy  # noqa: E402
from aria_code.tools.logistics_tenancy import TenancyError, scope_records  # noqa: E402
from aria_code.tools.logistics_tools import (  # noqa: E402
    register_logistics_tools,
    tool_analyze_logistics_data,
)

OTHER_CLIENT = "GLOBEX-CONFIDENTIAL"


def _item(result, sku):
    return next(i for i in result["data"]["items"] if i["sku"] == sku)


# ── 1. Shipper isolation ─────────────────────────────────────────────────────

class ShipperDataIsNeverMixed(unittest.TestCase):
    MIXED = [{"owner_id": "ACME", "x": 1}, {"owner_id": OTHER_CLIENT, "x": 2}]

    def test_mixed_shippers_are_refused_by_default(self) -> None:
        with self.assertRaises(TenancyError):
            scope_records(self.MIXED)

    def test_the_refusal_does_not_name_the_other_shippers(self) -> None:
        with self.assertRaises(TenancyError) as caught:
            scope_records(self.MIXED)
        self.assertNotIn(OTHER_CLIENT, str(caught.exception))
        self.assertNotIn("ACME", str(caught.exception))

    def test_an_unknown_owner_does_not_reveal_who_is_present(self) -> None:
        with self.assertRaises(TenancyError) as caught:
            scope_records(self.MIXED, "NOPE")
        self.assertNotIn(OTHER_CLIENT, str(caught.exception))

    def test_naming_a_shipper_returns_only_that_shippers_records(self) -> None:
        records, scope = scope_records(self.MIXED, "ACME")
        self.assertEqual([r["owner_id"] for r in records], ["ACME"])
        self.assertTrue(scope["client_facing"])

    def test_the_internal_view_is_explicit_and_marked(self) -> None:
        records, scope = scope_records(self.MIXED, all_owners=True)
        self.assertEqual(len(records), 2)
        self.assertFalse(scope["client_facing"])
        self.assertIn("not for client distribution", scope["marker"])

    def test_records_without_any_owner_are_single_tenant(self) -> None:
        records, scope = scope_records([{"x": 1}, {"x": 2}])
        self.assertEqual(len(records), 2)
        self.assertEqual(scope["mode"], "single_tenant")

    def test_partly_labelled_records_are_refused(self) -> None:
        # The unlabelled ones cannot be attributed, so they cannot be mixed in.
        with self.assertRaises(TenancyError):
            scope_records([{"owner_id": "ACME"}, {"x": 1}])

    def test_every_analysis_applies_it(self) -> None:
        mixed_skus = [
            {"owner_id": "ACME", "sku": "A", "on_hand": 1, "lead_time_days": 2, "daily_demand": [1] * 14},
            {"owner_id": OTHER_CLIENT, "sku": "B", "on_hand": 1, "lead_time_days": 2, "daily_demand": [1] * 14},
        ]
        mixed_waybills = [
            {"owner_id": "ACME", "carrier": "X", "total_cost": 10},
            {"owner_id": OTHER_CLIENT, "carrier": "Y", "total_cost": 12},
        ]
        for name, call in (
            ("plan_inventory_policy", lambda: tool_plan_inventory_policy({"skus": mixed_skus})),
            ("score_carriers", lambda: tool_score_carriers({"waybills": mixed_waybills})),
            ("analyze_logistics_data", lambda: tool_analyze_logistics_data({"waybills": mixed_waybills})),
        ):
            with self.subTest(tool=name):
                result = call()
                self.assertFalse(result["success"], f"{name} mixed two shippers' data")
                self.assertNotIn(OTHER_CLIENT, result["error"])

    def test_a_scoped_result_contains_no_trace_of_another_shipper(self) -> None:
        waybills = [
            {"owner_id": "ACME", "carrier": "X", "total_cost": 10, "billed_weight_kg": 2},
            {"owner_id": OTHER_CLIENT, "carrier": "SECRET-CARRIER", "total_cost": 999, "billed_weight_kg": 1},
        ]
        result = tool_score_carriers({"waybills": waybills, "owner_id": "ACME"})
        dumped = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(OTHER_CLIENT, dumped)
        self.assertNotIn("SECRET-CARRIER", dumped)
        self.assertNotIn("999", dumped)


# ── 2. Inventory policy, against hand-computed values ────────────────────────

class InventoryPolicyMatchesTheFormulas(unittest.TestCase):
    """SKU A: 7 days at 10 then 7 at 20 → d̄ = 15, sample σ_d = √(350/13) = 5.18874.

    L = 4, σ_L = 0, 95% → z = 1.644854.
      safety stock  = 1.644854 · √(4 · 26.92308)        = 17.0696 → 18
      reorder point = 15 · 4 + 17.0696                  = 77.0696 → 78
      order-up-to   = 77.0696 + 15 · 7                  = 182.0696 → 183
      position 50 ≤ 77.07, so order ⌈182.0696 − 50⌉     = 133
    """

    def setUp(self) -> None:
        self.result = tool_plan_inventory_policy({"skus": [
            {"sku": "A", "on_hand": 50, "lead_time_days": 4,
             "daily_demand": [10] * 7 + [20] * 7, "unit_cost": 10},
        ]})
        self.assertTrue(self.result["success"], self.result.get("error"))
        self.a = _item(self.result, "A")

    def test_safety_stock(self) -> None:
        self.assertEqual(self.a["safety_stock"], 18)

    def test_reorder_point(self) -> None:
        self.assertEqual(self.a["reorder_point"], 78)

    def test_order_up_to_and_quantity(self) -> None:
        self.assertEqual(self.a["order_up_to"], 183)
        self.assertEqual(self.a["suggested_order_qty"], 133)
        self.assertEqual(self.a["action"], "reorder")

    def test_days_of_cover(self) -> None:
        self.assertEqual(self.a["days_of_cover"], round(50 / 15, 1))

    def test_lead_time_variability_raises_safety_stock(self) -> None:
        """σ_L = 1 adds d̄²·σ_L² = 225 under the root:
        1.644854 · √(107.6923 + 225) = 1.644854 · 18.2399 = 30.0024 → 31."""
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "A", "on_hand": 50, "lead_time_days": 4, "lead_time_std_days": 1,
             "daily_demand": [10] * 7 + [20] * 7},
        ]})
        self.assertEqual(_item(result, "A")["safety_stock"], 31)

    def test_stock_above_the_reorder_point_is_left_alone(self) -> None:
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "A", "on_hand": 500, "lead_time_days": 4, "daily_demand": [10] * 7 + [20] * 7},
        ]})
        item = _item(result, "A")
        self.assertEqual(item["action"], "ok")
        self.assertEqual(item["suggested_order_qty"], 0)

    def test_the_formulas_are_stated_in_the_result(self) -> None:
        text = " ".join(self.result["data"]["assumptions"])
        self.assertIn("z = 1.645", text)
        self.assertIn("√(L·σ_d² + d̄²·σ_L²)", text)


class InventoryPolicyRefusesToGuess(unittest.TestCase):
    def test_short_history_gets_no_policy(self) -> None:
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "B", "on_hand": 5, "lead_time_days": 4, "daily_demand": [3, 4]},
        ]})
        item = _item(result, "B")
        self.assertEqual(item["action"], "insufficient_history")
        self.assertIsNone(item["reorder_point"])
        self.assertEqual(item["suggested_order_qty"], 0)

    def test_missing_lead_time_is_an_error_naming_the_row(self) -> None:
        result = tool_plan_inventory_policy({"skus": [{"sku": "A", "on_hand": 1, "daily_demand": [1] * 14}]})
        self.assertFalse(result["success"])
        self.assertIn("Row 1", result["error"])

    def test_negative_stock_is_an_error(self) -> None:
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "A", "on_hand": -3, "lead_time_days": 2, "daily_demand": [1] * 14},
        ]})
        self.assertFalse(result["success"])

    def test_empty_input_is_an_error_not_a_sample(self) -> None:
        self.assertFalse(tool_plan_inventory_policy({"skus": []})["success"])


class InventoryClassification(unittest.TestCase):
    def test_abc_by_annual_value_within_one_shipper(self) -> None:
        """Annual values: A1 = 10·365·80 = 292000, A2 = 10·365·15 = 54750,
        A3 = 10·365·5 = 18250; total 365000. Cumulative share before each:
        A1 0% → A, A2 80% → B, A3 95% → C."""
        demand = [10] * 14
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "A1", "on_hand": 100, "lead_time_days": 2, "daily_demand": demand, "unit_cost": 80},
            {"sku": "A2", "on_hand": 100, "lead_time_days": 2, "daily_demand": demand, "unit_cost": 15},
            {"sku": "A3", "on_hand": 100, "lead_time_days": 2, "daily_demand": demand, "unit_cost": 5},
        ]})
        self.assertEqual({i["sku"]: i["abc"] for i in result["data"]["items"]},
                         {"A1": "A", "A2": "B", "A3": "C"})

    def test_xyz_by_coefficient_of_variation(self) -> None:
        steady = [10] * 14                      # CV 0      → X
        lumpy = [0] * 13 + [140]                # CV 3.74   → Z
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "S", "on_hand": 50, "lead_time_days": 2, "daily_demand": steady},
            {"sku": "L", "on_hand": 50, "lead_time_days": 2, "daily_demand": lumpy},
        ]})
        self.assertEqual(_item(result, "S")["xyz"], "X")
        self.assertEqual(_item(result, "L")["xyz"], "Z")

    def test_movement_status(self) -> None:
        demand = [1] * 14
        result = tool_plan_inventory_policy({"skus": [
            {"sku": "D", "on_hand": 9, "lead_time_days": 2, "daily_demand": demand, "days_since_last_movement": 120},
            {"sku": "W", "on_hand": 9, "lead_time_days": 2, "daily_demand": demand, "days_since_last_movement": 45},
            {"sku": "N", "on_hand": 9, "lead_time_days": 2, "daily_demand": demand, "days_since_last_movement": 3},
        ]})
        self.assertEqual({i["sku"]: i["movement"] for i in result["data"]["items"]},
                         {"D": "dead", "W": "slow", "N": "active"})


class InventoryFromAFile(unittest.TestCase):
    def test_csv_with_semicolon_separated_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "skus.csv"
            path.write_text(
                "owner_id,sku,on_hand,lead_time_days,daily_demand\n"
                "ACME,A,50,4," + ";".join(["10"] * 7 + ["20"] * 7) + "\n",
                encoding="utf-8",
            )
            result = tool_plan_inventory_policy({"file_path": str(path)})
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(_item(result, "A")["reorder_point"], 78)
        self.assertEqual(result["data"]["scope"]["owner_id"], "ACME")


# ── 3. Carriers ──────────────────────────────────────────────────────────────

class WilsonLowerBound(unittest.TestCase):
    def test_known_values(self) -> None:
        # n = 1, p̂ = 1, z = 1.959964: (1 + z²/2 − z·√(z²/4)) / (1 + z²) = 1 / (1 + z²)
        self.assertAlmostEqual(wilson_lower_bound(1, 1), 1 / (1 + 1.959964 ** 2), places=5)
        self.assertAlmostEqual(wilson_lower_bound(98, 100), 0.9300, places=3)

    def test_a_long_record_beats_a_lucky_one(self) -> None:
        self.assertGreater(wilson_lower_bound(98, 100), wilson_lower_bound(1, 1))


def _waybills(carrier, n, on_time, cost_per_kg, kg=10.0, lane=("SH", "BJ")):
    return [{"carrier": carrier, "origin": lane[0], "destination": lane[1],
             "total_cost": cost_per_kg * kg, "billed_weight_kg": kg,
             "is_on_time": i < on_time, "waybill_no": f"{carrier}-{i}"}
            for i in range(n)]


class CarrierScorecard(unittest.TestCase):
    def test_ranking_uses_the_lower_bound_not_the_raw_rate(self) -> None:
        # LUCKY: 20/20 on time at 6/kg. STEADY: 95/100 at 5/kg.
        # Lower bounds: 20/20 → 0.8389, 95/100 → 0.8882, so STEADY ranks first.
        waybills = _waybills("LUCKY", 20, 20, 6.0) + _waybills("STEADY", 100, 95, 5.0)
        result = tool_score_carriers({"waybills": waybills})
        ranks = {c["carrier"]: c["rank_in_lane"] for c in result["data"]["scorecard"]}
        self.assertEqual(ranks, {"STEADY": 1, "LUCKY": 2})

    def test_too_few_shipments_are_not_ranked(self) -> None:
        waybills = _waybills("TINY", 3, 3, 1.0) + _waybills("BIG", 30, 28, 5.0)
        result = tool_score_carriers({"waybills": waybills})
        tiny = next(c for c in result["data"]["scorecard"] if c["carrier"] == "TINY")
        self.assertFalse(tiny["rankable"])
        self.assertIsNone(tiny["rank_in_lane"])

    def test_savings_need_cheaper_and_at_least_as_reliable(self) -> None:
        """PRICEY: 30 × 10 kg at 8/kg, 27/30 on time. CHEAP: 30 at 5/kg, 29/30.
        Saving = 300 kg × (8 − 5) = 900. Nothing is suggested the other way."""
        waybills = _waybills("PRICEY", 30, 27, 8.0) + _waybills("CHEAP", 30, 29, 5.0)
        savings = tool_score_carriers({"waybills": waybills})["data"]["savings"]
        self.assertEqual(len(savings), 1)
        self.assertEqual((savings[0]["from_carrier"], savings[0]["to_carrier"]), ("PRICEY", "CHEAP"))
        self.assertAlmostEqual(savings[0]["estimated_saving"], 900.0)

    def test_a_cheaper_but_less_reliable_carrier_is_not_suggested(self) -> None:
        waybills = _waybills("RELIABLE", 30, 30, 8.0) + _waybills("FLAKY", 30, 15, 5.0)
        self.assertEqual(tool_score_carriers({"waybills": waybills})["data"]["savings"], [])

    def test_lanes_are_never_compared_with_each_other(self) -> None:
        waybills = (_waybills("A", 30, 30, 8.0, lane=("SH", "BJ"))
                    + _waybills("B", 30, 30, 2.0, lane=("SH", "GZ")))
        self.assertEqual(tool_score_carriers({"waybills": waybills})["data"]["savings"], [])

    def test_a_cost_outlier_on_a_contracted_lane_is_flagged(self) -> None:
        """Seven shipments at exactly 5/kg, one at 5.2, one at 4.8, one at 30.

        More than half share one rate, so MAD = 0 — the normal shape of a
        contracted lane. The first version skipped such lanes and missed the
        6x overcharge. With the MeanAD fallback: MeanAD = (0.2+0.2+25)/10 =
        2.54, scale = 1.253314 · 2.54 = 3.1834, so 30/kg scores 25/3.1834 =
        7.85 (flagged) and 5.2/kg scores 0.06 (not flagged).
        """
        waybills = _waybills("X", 9, 9, 5.0)
        waybills[0]["total_cost"] = 5.2 * 10
        waybills[0]["waybill_no"] = "SLIGHTLY-HIGH"
        waybills[1]["total_cost"] = 4.8 * 10
        waybills.append({"carrier": "X", "origin": "SH", "destination": "BJ", "total_cost": 300,
                         "billed_weight_kg": 10, "waybill_no": "OUTLIER"})
        flagged = [a["waybill_no"] for a in tool_score_carriers({"waybills": waybills})["data"]["anomalies"]
                   if a["kind"] == "cost_per_kg"]
        self.assertEqual(flagged, ["OUTLIER"])

    def test_a_lane_where_every_rate_is_identical_flags_nothing(self) -> None:
        anomalies = tool_score_carriers({"waybills": _waybills("X", 10, 10, 5.0)})["data"]["anomalies"]
        self.assertEqual([a for a in anomalies if a["kind"] == "cost_per_kg"], [])

    def test_overbilled_weight_is_flagged(self) -> None:
        waybills = [{"carrier": "X", "total_cost": 50, "actual_weight_kg": 10,
                     "billed_weight_kg": 15, "waybill_no": "W1"}]
        anomalies = tool_score_carriers({"waybills": waybills})["data"]["anomalies"]
        self.assertEqual([a["kind"] for a in anomalies], ["billed_weight"])


class RegisteredAsTools(unittest.TestCase):
    def test_all_three_logistics_tools_are_registered(self) -> None:
        tools, schemas = {}, []
        self.assertEqual(register_logistics_tools(tools, schemas), 3)
        self.assertEqual(set(tools), {"analyze_logistics_data", "plan_inventory_policy", "score_carriers"})
        for schema in schemas:
            with self.subTest(tool=schema["name"]):
                self.assertIn("owner_id", schema["parameters"]["properties"])

    def test_the_pack_offers_them(self) -> None:
        from aria_code.packs.logistics import LOGISTICS_TOOLS
        self.assertIn("plan_inventory_policy", LOGISTICS_TOOLS)
        self.assertIn("score_carriers", LOGISTICS_TOOLS)


if __name__ == "__main__":
    unittest.main()


# ── 4. Direct commands ───────────────────────────────────────────────────────

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class _Cli:
    def __init__(self, lang: str = "zh"):
        from aria_code.apps.cli.commands.warehouse_cmds import LogisticsCommandsMixin

        class Stub(LogisticsCommandsMixin):
            context = SimpleNamespace(has_rich=False, console=None)
            terminal = SimpleNamespace(config={"ui_lang": lang})

        self.stub = Stub()

    def run(self, command: str, args: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            asyncio.run(getattr(self.stub, f"cmd_{command}")(args))
        return buffer.getvalue()


class DirectCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = pathlib.Path(self.tmp.name)
        history = ";".join(["10"] * 7 + ["20"] * 7)
        self.skus = folder / "skus file.csv"   # a space, so quoting is exercised
        self.skus.write_text(
            "owner_id,sku,on_hand,lead_time_days,daily_demand,days_since_last_movement\n"
            f"ACME,A,50,4,{history},2\n"
            f"ACME,OLD,30,4,{history},200\n"
            f"{OTHER_CLIENT},Z,1,4,{history},1\n",
            encoding="utf-8",
        )
        self.cli = _Cli()

    def test_inventory_for_one_shipper(self) -> None:
        out = self.cli.run("inventory", f'"{self.skus}" --owner ACME')
        self.assertIn("补货 A: 建议 133 件", out)
        self.assertIn("呆滞 OLD", out)
        self.assertNotIn(OTHER_CLIENT, out)

    def test_inventory_refuses_mixed_shippers_without_a_flag(self) -> None:
        out = self.cli.run("inventory", f'"{self.skus}"')
        self.assertIn("2 different shippers", out)
        self.assertNotIn(OTHER_CLIENT, out)

    def test_the_internal_view_prints_its_marker_first(self) -> None:
        out = self.cli.run("inventory", f'"{self.skus}" --all-owners')
        self.assertTrue(out.lstrip().startswith("INTERNAL"), out[:120])

    def test_json_output_is_the_tool_result(self) -> None:
        out = self.cli.run("inventory", f'"{self.skus}" --owner ACME --json')
        self.assertEqual(json.loads(out)["data"]["scope"]["owner_id"], "ACME")

    def test_misuse_prints_usage(self) -> None:
        self.assertIn("用法: /inventory", self.cli.run("inventory", ""))
        self.assertIn("用法: /carriers", self.cli.run("carriers", "--owner"))

    def test_carriers_command(self) -> None:
        waybills = pathlib.Path(self.tmp.name) / "waybills.json"
        waybills.write_text(json.dumps(
            _waybills("PRICEY", 30, 27, 8.0) + _waybills("CHEAP", 30, 29, 5.0)), encoding="utf-8")
        out = self.cli.run("carriers", str(waybills))
        self.assertIn("可节省 900.00", out)
        self.assertIn("#1 CHEAP", out)

    def test_english_ui_prints_english_labels(self) -> None:
        """The labels were Chinese whatever ui_lang said, inside an English summary."""
        out = _Cli("en").run("inventory", f'"{self.skus}" --owner ACME')
        self.assertIn("Reorder A: order 133 · reorder point", out)
        self.assertIn("ABC/XYZ –/X", out)        # no unit cost, so ABC is unknown — not "-X"
        self.assertIn("Dead stock OLD", out)
        self.assertIn("Basis: service level", out)
        self.assertIsNone(re.search(r"[\u4e00-\u9fff]", out), out)
        self.assertIn("Usage: /carriers", _Cli("en").run("carriers", "--owner"))

    def test_counts_are_not_pluralised_blindly(self) -> None:
        waybills = pathlib.Path(self.tmp.name) / "waybills.json"
        waybills.write_text(json.dumps(
            _waybills("PRICEY", 30, 27, 8.0) + _waybills("CHEAP", 30, 29, 5.0)), encoding="utf-8")
        out = _Cli("en").run("carriers", str(waybills))
        self.assertIn("1 savings opportunity,", out)
        self.assertIn("Save 900.00: ", out)

    def test_both_are_registered_in_the_cli(self) -> None:
        from aria_code import aria_cli

        commands = aria_cli.SlashCommands(SimpleNamespace(config={})).commands
        self.assertEqual(commands["/inventory"][0].__name__, "cmd_inventory")
        self.assertEqual(commands["/carriers"][0].__name__, "cmd_carriers")
