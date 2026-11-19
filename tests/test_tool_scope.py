"""A domain tool is offered only when its domain claimed the message.

The pack contract has three contributions: handlers, a prompt fragment, and
tools. Two were wired. active_tool_names() was written, exported, and called by
nothing — so a request to fix a failing test reached Gemini carrying all 74
tool schemas, 34 of them market quotes, broker orders, backtests and freight
reconciliation.

Wasted context is the smaller cost. A tool in the list is a tool the model may
call, and the incident the pack contract exists to prevent — a repository
question answered with a stock quote — is exactly a domain tool firing on a
message that named nothing in its domain.
"""

import unittest

from aria_code.apps.cli.tool_scope import domain_tool_names, select_tool_schemas


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


CORE = ["read_file", "write_file", "edit_file", "run_command", "repo_map", "find_symbol"]
DOMAIN = ["get_market_data", "broker_order"]
ALL = [_schema(n) for n in CORE + DOMAIN]


def _names(schemas):
    return {(s.get("function") or s).get("name") for s in schemas}


class ScopeTests(unittest.TestCase):
    def test_a_coding_message_keeps_core_and_drops_domain(self):
        kept = _names(select_tool_schemas(ALL, "这个项目的测试挂了，找出原因并修好"))
        self.assertEqual(kept, set(CORE))

    def test_a_named_instrument_restores_the_finance_tools(self):
        kept = _names(select_tool_schemas(ALL, "分析 $AAPL 的走势"))
        self.assertIn("get_market_data", kept)
        self.assertTrue(set(CORE) <= kept)

    def test_each_domain_unlocks_only_its_own(self):
        finance = _names(select_tool_schemas(ALL, "分析 $AAPL 的走势"))
        self.assertIn("get_market_data", finance)
        self.assertIn("broker_order", finance)

    def test_the_message_that_motivated_the_contract_gets_no_domain_tools(self):
        kept = _names(select_tool_schemas(ALL, "根据以上分析和建议开始完善"))
        self.assertEqual(kept, set(CORE))

    def test_core_tools_are_never_gated(self):
        for message in ("", "分析 $AAPL", "修好这个 bug", "查 SF1234567890123"):
            with self.subTest(message=message):
                self.assertTrue(set(CORE) <= _names(select_tool_schemas(ALL, message)))

    def test_an_unclaimed_tool_stays_available(self):
        # The default must be safe: adding a tool nobody assigns to a domain
        # keeps working, and the burden is on a domain to claim it.
        schemas = ALL + [_schema("brand_new_tool")]
        self.assertIn("brand_new_tool", _names(select_tool_schemas(schemas, "修好这个 bug")))

    def test_always_forces_a_tool_in(self):
        kept = _names(select_tool_schemas(ALL, "修好这个 bug", always=["get_market_data"]))
        self.assertIn("get_market_data", kept)

    def test_an_empty_list_is_returned_unchanged(self):
        self.assertEqual(select_tool_schemas([], "anything"), [])

    def test_bare_schemas_are_matched_by_name_too(self):
        bare = [{"name": "get_market_data"}, {"name": "read_file"}]
        self.assertEqual(_names(select_tool_schemas(bare, "修好这个 bug")), {"read_file"})


class SafeDegradationTests(unittest.TestCase):
    """Offering too many tools is a tax; offering too few removes a capability."""

    def test_a_broken_pack_layer_falls_back_to_the_full_list(self):
        import aria_code.apps.cli.tool_scope as scope

        original = scope.domain_tool_names
        scope.domain_tool_names = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(len(select_tool_schemas(ALL, "修好这个 bug")), len(ALL))
        finally:
            scope.domain_tool_names = original


class ClaimedToolsTests(unittest.TestCase):
    def test_finance_claims_are_derived_not_hand_listed(self):
        # A hand-written tuple drifts silently in both directions: a tool left
        # out stays exposed forever, and a misspelled one promises a capability
        # that does not exist.
        import aria_code.aria_cli as cli
        from aria_code.packs.finance import FINANCE_TOOLS

        self.assertGreaterEqual(len(FINANCE_TOOLS), 5)
        for name in FINANCE_TOOLS:
            with self.subTest(tool=name):
                self.assertIn(name, cli.LOCAL_TOOLS)

    def test_gateable_tools_all_exist_and_are_not_everything(self):
        import aria_code.aria_cli as cli

        registered = {s["function"]["name"] for s in cli.LOCAL_TOOL_SCHEMAS}
        gateable = domain_tool_names()

        self.assertTrue(gateable)
        # Claiming a tool that has no schema hides it from the model rather
        # than gating it — that is how get_funding_rates_compare stayed
        # invisible.
        self.assertEqual(gateable - registered, set())
        self.assertTrue(registered - gateable, "every tool would be gateable")
        self.assertNotIn("read_file", gateable)
        self.assertNotIn("run_command", gateable)


class RegistryConsistencyTests(unittest.TestCase):
    """Registered, callable, and invisible is a recurring failure here.

    Three enterprise tools went that way through the schema deduplicator, and
    get_funding_rates_compare through a missing schema entry. The handler map
    and the schema list are maintained by hand in several modules, so the only
    thing that keeps them in step is a check that compares them.
    """

    def test_every_registered_handler_is_described_or_a_declared_stub(self):
        import aria_code.aria_cli as cli
        from aria_code.tools.extended_tools import _STUB_TOOLS

        described = {s["function"]["name"] for s in cli.LOCAL_TOOL_SCHEMAS}
        undescribed = sorted(set(cli.LOCAL_TOOLS) - described - set(_STUB_TOOLS))
        self.assertEqual(
            undescribed, [],
            f"registered but invisible to the model: {undescribed}",
        )

    def test_the_stubs_are_kept_out_of_the_model_facing_list(self):
        # Describing one would let the model call it and relay its fabricated
        # success — "posted to #trading-desk" when nothing was sent.
        import aria_code.aria_cli as cli
        from aria_code.tools.extended_tools import _STUB_TOOLS

        described = {s["function"]["name"] for s in cli.LOCAL_TOOL_SCHEMAS}
        self.assertEqual(described & set(_STUB_TOOLS), set())

    def test_a_stub_refuses_instead_of_fabricating_success(self):
        import aria_code.aria_cli as cli
        from aria_code.runtime.tool_executor import ToolExecutor

        executor = ToolExecutor(cli.LOCAL_TOOLS)
        for name in ("get_quickbooks_pnl", "query_snowflake_data"):
            with self.subTest(tool=name):
                result = executor.execute_local(name, {})
                self.assertFalse(result["success"])
                self.assertTrue(
                    "not configured" in result["error"] or "requires" in result["error"],
                    msg=f"unexpected error message for unconfigured connector {name}: {result['error']}",
                )

    def test_every_handler_is_stored_as_a_handler_description_pair(self):
        # ToolExecutor indexes local_tools[name][0]; a bare function raises
        # TypeError: 'function' object is not subscriptable.
        import aria_code.aria_cli as cli

        for name, entry in cli.LOCAL_TOOLS.items():
            with self.subTest(tool=name):
                self.assertIsInstance(entry, tuple, f"{name} is not a (handler, description) pair")
                self.assertTrue(callable(entry[0]))

    def test_every_schema_has_a_handler_behind_it(self):
        import aria_code.aria_cli as cli

        described = {s["function"]["name"] for s in cli.LOCAL_TOOL_SCHEMAS}
        unbacked = sorted(described - set(cli.LOCAL_TOOLS))
        self.assertEqual(
            unbacked, [],
            f"described to the model but not callable: {unbacked}",
        )


class CloudPathWiringTests(unittest.TestCase):
    def test_the_provider_receives_the_scoped_list(self):
        import inspect

        from aria_code.apps.cli.providers import runtime_bridge

        source = inspect.getsource(runtime_bridge.make_provider_fn)
        self.assertIn("tools=_scoped", source)
        self.assertNotIn("tools=tool_schemas", source)


if __name__ == "__main__":
    unittest.main()
