"""Tests for the domain packs and their link to the acceptance gate.

The rule every one of these guards is the contract's: a pack activates only
when it resolves a *concrete entity* from the user's own message. Domain
vocabulary must never be enough, because the failure that produces — a
repository question answered with a stock quote — is the incident the pack
layer was built to end.
"""

import unittest

from aria_code.packs import (
    activate_packs,
    active_acceptance_commands,
    load_builtin_packs,
    registered_packs,
    reset_builtin_packs,
)
from aria_code.packs.logistics import LOGISTICS_PACK, is_valid_container
from aria_code.packs.payments import PAYMENTS_PACK
from aria_code.packs.realty import REALTY_PACK
from aria_code.packs.rules import acceptance_commands_for, default_acceptance_commands


def _active(message: str) -> set:
    return {a.pack for a in activate_packs(message)}


class BuiltinPackLoadingTests(unittest.TestCase):
    def setUp(self):
        reset_builtin_packs()
        self.addCleanup(reset_builtin_packs)

    def test_all_five_domains_register(self):
        names = set(load_builtin_packs())
        self.assertEqual(names, {"finance", "logistics", "payments", "realty", "healthcare"})

    def test_loading_is_idempotent(self):
        load_builtin_packs()
        load_builtin_packs()
        self.assertEqual(len(registered_packs()), 5)


class ActivationBoundaryTests(unittest.TestCase):
    """What must and must not switch a pack on."""

    def setUp(self):
        reset_builtin_packs()
        load_builtin_packs()
        self.addCleanup(reset_builtin_packs)

    def test_each_domain_activates_on_its_own_identifier(self):
        self.assertEqual(_active("查一下 SF1234567890123 到哪了"), {"logistics"})
        self.assertEqual(_active("refund ch_3PqR8s2eZvKYlo2C0aBcDeFg"), {"payments"})
        self.assertEqual(_active("杭州房价怎么样"), {"realty"})
        self.assertEqual(_active("分析 $AAPL"), {"finance"})

    def test_the_message_that_motivated_the_contract_activates_nothing(self):
        # Contains 分析 and 建议 but names nothing concrete. This exact phrasing
        # is what once returned a MongoDB stock quote.
        self.assertEqual(_active("根据以上分析和建议开始完善"), set())

    def test_domain_vocabulary_alone_is_never_enough(self):
        for message in (
            "帮我看看物流成本怎么优化",       # logistics words, no waybill
            "how should I handle refunds",   # payments words, no object id
            "这个房子值得买吗",               # housing words, no city
            "我想分析一下市场",               # finance words, no ticker
        ):
            with self.subTest(message=message):
                self.assertEqual(_active(message), set())

    def test_a_code_question_mentioning_a_city_does_not_activate_realty(self):
        # A city name is an ordinary word; only city + housing term resolves.
        self.assertEqual(_active("杭州机房的部署脚本在哪"), set())

    def test_an_unrelated_underscore_identifier_is_not_a_stripe_object(self):
        self.assertEqual(_active("看看 in_memory_cache_manager 的实现"), set())

    def test_packs_do_not_claim_each_others_messages(self):
        self.assertNotIn("finance", _active("查一下 SF1234567890123"))
        self.assertNotIn("logistics", _active("refund ch_3PqR8s2eZvKYlo2C0aBcDeFg"))


class LogisticsPackTests(unittest.TestCase):
    def test_iso6346_check_digit_is_verified_not_shape_matched(self):
        self.assertTrue(is_valid_container("CSQU3054383"))   # the standard's own example
        self.assertFalse(is_valid_container("CSQU3054384"))  # one digit off
        self.assertFalse(is_valid_container("MSKU1234567"))
        self.assertFalse(is_valid_container("TOOSHORT"))
        self.assertFalse(is_valid_container("12345678901"))

    def test_a_valid_container_activates_but_an_invalid_one_does_not(self):
        valid = LOGISTICS_PACK.resolve_entities("CSQU3054383 这个箱子")
        self.assertEqual(valid[0].confidence, 0.95)

        invalid = LOGISTICS_PACK.resolve_entities("MSKU1234567 这个箱子")
        self.assertLess(invalid[0].confidence, 0.5)

    def test_a_bare_digit_run_stays_below_the_threshold(self):
        # Also the shape of an order id, an invoice number, a millisecond
        # timestamp — reported so the UI can ask, never enough to answer.
        entities = LOGISTICS_PACK.resolve_entities("订单号 1699999999999 呢")
        self.assertEqual(len(entities), 1)
        self.assertLess(entities[0].confidence, 0.5)

    def test_digits_inside_a_verified_waybill_are_not_a_second_entity(self):
        entities = LOGISTICS_PACK.resolve_entities("1Z999AA10123456784 到哪了")
        self.assertEqual([e.kind for e in entities], ["waybill:ups"])

    def test_carrier_prefixes_are_recognised(self):
        for text, kind in (
            ("1Z999AA10123456784", "waybill:ups"),
            ("SF1234567890123", "waybill:sf"),
        ):
            with self.subTest(text=text):
                found = LOGISTICS_PACK.resolve_entities(text)
                self.assertEqual(found[0].kind, kind)
                self.assertEqual(found[0].confidence, 0.95)


class PaymentsPackTests(unittest.TestCase):
    def test_object_ids_are_typed_by_prefix(self):
        found = PAYMENTS_PACK.resolve_entities(
            "看看 cus_NffrFeUfNV2Hib 和 sub_1MowQVLkdIwHu7ixeRlqHVzs"
        )
        self.assertEqual({e.kind for e in found}, {"customer", "subscription"})

    def test_test_mode_ids_resolve_too(self):
        found = PAYMENTS_PACK.resolve_entities("ch_test_3PqR8s2eZvKYlo2C")
        self.assertEqual(found[0].kind, "charge")

    def test_a_message_with_no_underscore_skips_the_scan_entirely(self):
        self.assertEqual(PAYMENTS_PACK.resolve_entities("refund this charge"), ())

    def test_a_leaked_key_produces_a_rotation_warning(self):
        # Assembled at runtime, and the body spells out that it is not a key.
        # A literal of the real shape is indistinguishable from a live
        # credential to a scanner — GitHub push protection rejected this file
        # for it, which is the correct behaviour and not something to suppress.
        fake_key = "sk_" + "live_" + "EXAMPLEONLY" + "0" * 12
        activation = activate_packs(fake_key, packs=[PAYMENTS_PACK])[0]
        fragment = PAYMENTS_PACK.prompt_fragment(activation)
        self.assertIn("轮换", fragment)

    def test_an_ordinary_charge_gets_no_key_warning(self):
        activation = activate_packs(
            "ch_3PqR8s2eZvKYlo2C0aBcDeFg",
            packs=[PAYMENTS_PACK],
        )[0]
        self.assertNotIn("轮换", PAYMENTS_PACK.prompt_fragment(activation))


class RealtyPackTests(unittest.TestCase):
    def test_city_plus_housing_term_resolves(self):
        found = REALTY_PACK.resolve_entities("深圳二手房价格走势")
        self.assertEqual(found[0].value, "深圳")
        self.assertEqual(found[0].confidence, 0.95)

    def test_city_alone_stays_below_the_threshold(self):
        found = REALTY_PACK.resolve_entities("下周去成都出差")
        self.assertEqual(found[0].value, "成都")
        self.assertLess(found[0].confidence, 0.5)

    def test_international_cities_are_recognised(self):
        found = REALTY_PACK.resolve_entities("london house price trend")
        self.assertEqual(found[0].confidence, 0.95)

    def test_a_long_city_list_is_capped(self):
        message = "房价 " + " ".join(
            ["北京", "上海", "深圳", "广州", "成都", "杭州", "武汉", "南京", "重庆"]
        )
        self.assertLessEqual(len(REALTY_PACK.resolve_entities(message)), 6)

    def test_realty_contributes_its_handler_only_through_the_pack(self):
        self.assertEqual(len(REALTY_PACK.handlers()), 1)


class _StubRC:
    def __init__(self, acceptance):
        self.acceptance = acceptance


class AcceptanceRulesTests(unittest.TestCase):
    """Where a pack learns what green means in this workspace."""

    def test_commands_are_read_per_pack(self):
        rc = _StubRC({"logistics": ["python3 scripts/reconcile.py"], "default": ["make check"]})
        self.assertEqual(
            acceptance_commands_for("logistics", rc=rc),
            ("python3 scripts/reconcile.py",),
        )
        self.assertEqual(default_acceptance_commands(rc=rc), ("make check",))

    def test_a_bare_string_is_accepted_as_one_command(self):
        rc = _StubRC({"payments": "python3 verify.py"})
        self.assertEqual(acceptance_commands_for("payments", rc=rc), ("python3 verify.py",))

    def test_a_malformed_declaration_yields_nothing_rather_than_failing(self):
        self.assertEqual(acceptance_commands_for("logistics", rc=_StubRC({"logistics": 42})), ())
        self.assertEqual(acceptance_commands_for("logistics", rc=_StubRC("nonsense")), ())
        self.assertEqual(acceptance_commands_for("nothing-declared", rc=_StubRC({})), ())

    def test_blank_entries_are_dropped(self):
        rc = _StubRC({"realty": ["  ", "make check", ""]})
        self.assertEqual(acceptance_commands_for("realty", rc=rc), ("make check",))


class PackAcceptanceWiringTests(unittest.TestCase):
    """A declared check reaches the gate only for the domain that claimed the message."""

    def setUp(self):
        reset_builtin_packs()
        load_builtin_packs()
        self.addCleanup(reset_builtin_packs)

    def test_only_the_active_packs_commands_are_collected(self):
        import aria_code.packs.rules as rules

        declared = {
            "logistics": ("python3 reconcile.py",),
            "payments": ("python3 verify_stripe.py",),
        }
        original = rules.acceptance_commands_for
        rules.acceptance_commands_for = lambda name, rc=None: declared.get(name, ())
        self.addCleanup(setattr, rules, "acceptance_commands_for", original)

        self.assertEqual(
            active_acceptance_commands(activate_packs("查一下 SF1234567890123")),
            ("python3 reconcile.py",),
        )
        self.assertEqual(
            active_acceptance_commands(activate_packs("退款 ch_3PqR8s2eZvKYlo2C0aBcDeFg")),
            ("python3 verify_stripe.py",),
        )
        self.assertEqual(active_acceptance_commands(activate_packs("重构一下这个函数")), ())

    def test_a_pack_that_raises_does_not_break_the_turn(self):
        class Exploding:
            name = "exploding"

            def resolve_entities(self, message):
                from aria_code.packs.base import EntityMatch
                return (EntityMatch(pack="exploding", kind="thing", value="X1", confidence=1.0),)

            def acceptance_commands(self, activation):
                raise RuntimeError("boom")

        from aria_code.packs import register_pack, unregister_pack

        register_pack(Exploding())
        self.addCleanup(unregister_pack, "exploding")

        self.assertEqual(active_acceptance_commands(activate_packs("anything X1")), ())


class GateWiringTests(unittest.TestCase):
    """The CLI's precedence between session config, packs, and the workspace."""

    def setUp(self):
        reset_builtin_packs()
        load_builtin_packs()
        self.addCleanup(reset_builtin_packs)

    def test_session_config_overrides_everything(self):
        from aria_code.apps.cli.providers.runtime_bridge import _declared_acceptance_commands

        self.assertEqual(
            _declared_acceptance_commands(
                {"acceptance_commands": ["make check"]},
                "查一下 SF1234567890123",
            ),
            ("make check",),
        )

    def test_pack_commands_apply_when_the_pack_claims_the_message(self):
        import aria_code.packs.rules as rules
        from aria_code.apps.cli.providers.runtime_bridge import _declared_acceptance_commands

        original = rules.acceptance_commands_for
        rules.acceptance_commands_for = lambda name, rc=None: (
            ("python3 reconcile.py",) if name == "logistics" else ()
        )
        self.addCleanup(setattr, rules, "acceptance_commands_for", original)

        self.assertIn(
            "python3 reconcile.py",
            _declared_acceptance_commands({}, "查一下 SF1234567890123"),
        )
        self.assertEqual(_declared_acceptance_commands({}, "重构这个函数"), ())


if __name__ == "__main__":
    unittest.main()


class RealtyNationalMarketTests(unittest.TestCase):
    """The national market is nameable without a city — but only just."""

    def test_a_market_term_alone_names_the_national_market(self):
        found = REALTY_PACK.resolve_entities("全国房价走势")
        self.assertEqual(found[0].value, "全国")
        self.assertEqual(found[0].confidence, 0.95)

    def test_a_property_domain_word_alone_does_not(self):
        # These appear in the source of every property-management codebase,
        # which is why the pack's list is narrower than the handler's.
        for message in ("这个物业管理系统怎么改", "重构 RealtyService 这个类", "地产行业的数据模型"):
            with self.subTest(message=message):
                self.assertEqual(REALTY_PACK.resolve_entities(message), ())

    def test_realty_no_longer_runs_ungated_in_the_deterministic_chain(self):
        import inspect
        from aria_code.apps.cli import deterministic

        source = inspect.getsource(deterministic.run_deterministic_chain)
        self.assertNotIn("_handle_realty_query", source)


class PackHandlerShapeTests(unittest.TestCase):
    """Every handler a pack contributes must be callable as handler(message).

    The deterministic chain calls them uniformly, so one that needs extra
    collaborators is not a handler — it is a TypeError waiting for the first
    message that activates its pack.
    """

    def setUp(self):
        reset_builtin_packs()
        load_builtin_packs()
        self.addCleanup(reset_builtin_packs)

    def test_every_builtin_handler_accepts_a_bare_message(self):
        for pack in registered_packs():
            for handler in pack.handlers() or ():
                with self.subTest(pack=pack.name, handler=getattr(handler, "__name__", handler)):
                    result = handler("分析 AAPL 的日线图")
                    self.assertIsInstance(result, dict)
                    self.assertIn("success", result)

    def test_the_chain_survives_a_finance_activation(self):
        from aria_code.apps.cli.deterministic import run_deterministic_chain

        # Previously raised TypeError: handle_stock_chart_analysis() missing 2
        # required keyword-only arguments.
        result = run_deterministic_chain("分析 $AAPL 的日线图", model_has_tools=True)
        self.assertIsInstance(result, dict)


class SelfGatedHandlerTests(unittest.TestCase):
    """Not every domain handler depends on an entity.

    The entity gate exists to stop a handler that ANSWERS WITH DATA from firing
    when no instrument was named. handle_strategy_advice answers with static
    methodology text and fetches nothing, so gating it simply broke it: the
    message it exists for names no ticker.
    """

    def test_a_methodology_question_still_reaches_its_handler(self):
        from aria_code.apps.cli.deterministic import run_deterministic_chain

        message = "如果我要写一个美股量化策略，你觉得要从几个角度去写"
        self.assertEqual(activate_packs(message), ())   # no entity, no pack
        result = run_deterministic_chain(message, model_has_tools=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["tools_used"], ["strategy_advice"])

    def test_software_uses_of_the_same_words_fall_through(self):
        from aria_code.apps.cli.deterministic import run_deterministic_chain

        for message in (
            "帮我写一个缓存策略",
            "数据库索引策略有什么建议",
            "给我一些代码风格建议",
            "根据以上分析和建议开始完善",
        ):
            with self.subTest(message=message):
                self.assertFalse(
                    run_deterministic_chain(message, model_has_tools=True)["success"]
                )

    def test_the_finance_pack_keeps_only_its_entity_dependent_handler(self):
        from aria_code.packs.finance import FINANCE_PACK

        self.assertEqual(len(FINANCE_PACK.handlers()), 1)

    def test_the_handler_list_is_resolved_at_call_time(self):
        # A module-level tuple of function objects ignores anything that
        # replaces the attribute later, defeating monkeypatching silently.
        import aria_code.apps.cli.deterministic as deterministic

        original = deterministic.handle_strategy_advice
        deterministic.handle_strategy_advice = lambda _m: {"success": False}
        try:
            self.assertIs(deterministic._self_gated_handlers()[0],
                          deterministic.handle_strategy_advice)
        finally:
            deterministic.handle_strategy_advice = original


class DeclaredToolsExistTests(unittest.TestCase):
    """A pack must not name a tool the model cannot call.

    Declaring one costs a wasted round and an "unknown tool" error, and the
    model has no way to tell the difference between a capability that is
    missing and one it used wrongly.
    """

    def test_every_declared_tool_is_registered(self):
        import aria_code.aria_cli as cli

        registered = set(cli.LOCAL_TOOLS)
        reset_builtin_packs()
        load_builtin_packs()
        self.addCleanup(reset_builtin_packs)

        for pack in registered_packs():
            for name in pack.tool_names() or ():
                with self.subTest(pack=pack.name, tool=name):
                    self.assertIn(name, registered)

    def test_every_registered_handler_has_a_visible_schema(self):
        # A handler with no schema is registered, callable, and invisible —
        # exactly how three enterprise tools went missing when the schema
        # deduplicator dropped anything not in OpenAI wrapper form.
        import aria_code.aria_cli as cli

        described = {
            (s.get("function") or s).get("name")
            for s in cli.LOCAL_TOOL_SCHEMAS
        }
        for name in ("analyze_logistics_data", "analyze_stripe_data",
                     "analyze_financial_statements"):
            with self.subTest(tool=name):
                self.assertIn(name, cli.LOCAL_TOOLS)
                self.assertIn(name, described)


class SchemaShapeTests(unittest.TestCase):
    """One shape reaches the providers, whatever shape was registered."""

    def test_every_schema_uses_the_openai_envelope(self):
        # Bare {name, description, parameters} is what several register_*
        # helpers emit. Ollama and the OpenAI-compatible providers reject it,
        # and consumers that assume the envelope break on it.
        import aria_code.aria_cli as cli

        for schema in cli.LOCAL_TOOL_SCHEMAS:
            with self.subTest(schema=str(schema)[:60]):
                self.assertIn("function", schema)
                self.assertTrue(schema["function"].get("name"))

    def test_dedup_normalises_a_bare_schema(self):
        import aria_code.aria_cli as cli
        from aria_code.apps.cli.tool_executor import _dedup_tool_schemas

        original = list(cli.LOCAL_TOOL_SCHEMAS)
        try:
            cli.LOCAL_TOOL_SCHEMAS.append(
                {"name": "probe_tool", "description": "d", "parameters": {}}
            )
            _dedup_tool_schemas()
            match = [
                s for s in cli.LOCAL_TOOL_SCHEMAS
                if s.get("function", {}).get("name") == "probe_tool"
            ]
            self.assertEqual(len(match), 1)
            self.assertEqual(match[0]["type"], "function")
        finally:
            cli.LOCAL_TOOL_SCHEMAS[:] = original

    def test_a_nameless_schema_is_dropped(self):
        import aria_code.aria_cli as cli
        from aria_code.apps.cli.tool_executor import _dedup_tool_schemas

        original = list(cli.LOCAL_TOOL_SCHEMAS)
        try:
            cli.LOCAL_TOOL_SCHEMAS.append({"description": "no name at all"})
            _dedup_tool_schemas()
            self.assertEqual(len(cli.LOCAL_TOOL_SCHEMAS), len(original))
        finally:
            cli.LOCAL_TOOL_SCHEMAS[:] = original
