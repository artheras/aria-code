"""An analysis tool must not invent the data it analyses.

These three had a "sensible default sample dataset" behind an `if not data:`
branch. Called with nothing they returned a complete, ordinary-looking result:
a specific ARR and subscriber count, a freight spend, a full financial
diagnosis carrying the requested company's ticker — for businesses none of them
had ever seen. Nothing in the result said the numbers were invented.

That was survivable only while the tools had no schema and the model could not
call them. Making them visible made it a live risk, so the sample data had to
go with it.
"""

import unittest

import pytest

pytest.importorskip("erp_service")
from aria_bots.erp_logistics import tool_analyze_logistics_data  # noqa: E402


class NoDataMeansNoAnswerTests(unittest.TestCase):
    def test_no_module_still_carries_the_sample_numbers(self):
        import inspect

        from erp_service import freight

        code = "\n".join(
            line for line in inspect.getsource(freight).splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn("12000000.0", code)
        self.assertNotIn("Default representative", code)


class ProvenanceTests(unittest.TestCase):
    """Where a number came from is part of the number."""

    def test_supplied_records_are_labelled_as_such(self):
        result = tool_analyze_logistics_data({
            "waybills": [
                {"total_cost": 100.0, "is_on_time": True},
                {"total_cost": 50.0, "is_on_time": False},
            ],
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["data_source"], "caller-supplied records")
        self.assertIn("来源", result["summary"])


class ComputedNotAssertedTests(unittest.TestCase):
    def test_the_on_time_rate_is_computed_from_the_records(self):
        # It was hardcoded to 75.0 and returned beside a genuinely computed
        # freight total, so an invented figure travelled under the same roof
        # as a real one.
        result = tool_analyze_logistics_data({
            "waybills": [
                {"total_cost": 10.0, "is_on_time": True},
                {"total_cost": 10.0, "is_on_time": True},
                {"total_cost": 10.0, "is_on_time": True},
                {"total_cost": 10.0, "is_on_time": False},
            ],
        })
        self.assertEqual(result["data"]["overall_on_time_rate"], 75.0)

        flipped = tool_analyze_logistics_data({
            "waybills": [
                {"total_cost": 10.0, "is_on_time": False},
                {"total_cost": 10.0, "is_on_time": False},
            ],
        })
        self.assertEqual(flipped["data"]["overall_on_time_rate"], 0.0)

    def test_an_unknowable_rate_is_reported_as_unknown(self):
        result = tool_analyze_logistics_data({"waybills": [{"total_cost": 10.0}]})
        self.assertIsNone(result["data"]["overall_on_time_rate"])
        self.assertIn("不可得", result["summary"])


if __name__ == "__main__":
    unittest.main()
