"""
Tests for aria_cli.py core utilities.
Run from apps/cli/: python3 -m pytest tests/ -v
"""
import sys
import os
import pathlib
import unittest
import json
import tempfile

import pytest

# Allow importing from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aria_code.packages.aria_core import build_session_diagnostic_bundle
from aria_code.apps.cli.session_export import build_session_export_payload
from aria_code.apps.cli.commands.market import route_top_level_text
from aria_code.aria_cli import (
    _strip_markdown_fences,
    _is_safe_path,
    _tool_read_file,
    _tool_write_file,
    _tool_edit_file,
    _tool_list_files,
    load_config,
    save_config,
    resolve_model_key,
    MODEL_ALIASES,
    MODELS,
    DEFAULT_CONFIG,
    SessionManager,
    SESSIONS_DIR,
    _is_coding_request,
    _is_analysis_request,
    _ACTIVE_WRITE_POLICY,
    _ACTIVE_PERMISSION_MODE,
    _ACTIVE_NETWORK_ENABLED,
    _sync_write_policy,
    _is_stock_chart_analysis_request,
    _chart_period_from_ta_days,
    _is_market_artifact_followup,
    _is_artifact_location_followup,
    _natural_language_visual_artifact_route,
)
from aria_code.apps.cli.utils.market_detect import _is_visual_market_artifact_request
from aria_code.change_store import ChangeConflictError, GLOBAL_CHANGE_STORE
from aria_code.runtime import AgentTurnState, RuntimeTrace


# ============================================================================
# _strip_markdown_fences
# ============================================================================

class TestStripMarkdownFences(unittest.TestCase):
    def test_strips_python_fence(self):
        code = "```python\nprint('hello')\n```"
        result = _strip_markdown_fences(code)
        self.assertNotIn("```", result)
        self.assertIn("print('hello')", result)

    def test_strips_plain_fence(self):
        code = "```\nx = 1\n```"
        result = _strip_markdown_fences(code)
        self.assertNotIn("```", result)
        self.assertIn("x = 1", result)

    def test_no_fence_unchanged(self):
        code = "x = 1\nprint(x)"
        result = _strip_markdown_fences(code)
        self.assertIn("x = 1", result)

    def test_trailing_newline_preserved(self):
        code = "```python\nfoo()\n```"
        result = _strip_markdown_fences(code)
        self.assertTrue(result.endswith("\n"))

    def test_ta_days_map_to_chart_period(self):
        self.assertEqual(_chart_period_from_ta_days(30), "1mo")
        self.assertEqual(_chart_period_from_ta_days(90), "3mo")
        self.assertEqual(_chart_period_from_ta_days(120), "6mo")
        self.assertEqual(_chart_period_from_ta_days(365), "1y")

    def test_market_artifact_followup_detection(self):
        self.assertTrue(_is_market_artifact_followup("那你直接运行"))
        self.assertTrue(_is_market_artifact_followup("继续以上任务"))
        self.assertFalse(_is_market_artifact_followup("/chart AAPL"))

    def test_artifact_location_followup_detection(self):
        self.assertTrue(_is_artifact_location_followup("那文件在哪"))
        self.assertTrue(_is_artifact_location_followup("保存到哪里了"))
        self.assertTrue(_is_artifact_location_followup("where is the file"))
        self.assertFalse(_is_artifact_location_followup("/artifacts"))


# ============================================================================
# _is_safe_path
# ============================================================================

class TestIsSafePath(unittest.TestCase):
    @pytest.mark.usefixtures("fake_home")
    def test_home_directory_is_safe(self):
        p = pathlib.Path.home().resolve() / "test.txt"
        self.assertTrue(_is_safe_path(p))

    def test_tmp_is_safe(self):
        p = pathlib.Path("/tmp/test_file.txt").resolve()
        self.assertTrue(_is_safe_path(p))

    def test_etc_is_blocked(self):
        p = pathlib.Path("/etc/passwd").resolve()
        self.assertFalse(_is_safe_path(p))

    def test_dev_is_blocked(self):
        p = pathlib.Path("/dev/null").resolve()
        self.assertFalse(_is_safe_path(p))

    def test_cwd_is_safe(self):
        p = pathlib.Path.cwd().resolve() / "some_file.py"
        self.assertTrue(_is_safe_path(p))


# ============================================================================
# _tool_read_file / _tool_write_file / _tool_edit_file
# ============================================================================

class TestFileTools(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test.py")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_and_read_file(self):
        content = "import os\nimport sys\n\nx = 42\nprint('result:', x)\n"
        result = _tool_write_file({"path": self.test_file, "content": content})
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(result["data"]["applied"])
        self.assertIn("change_id", result["data"])
        read = _tool_read_file({"path": self.test_file})
        self.assertTrue(read["success"])
        self.assertIn("x = 42", read["data"]["content"])

    def test_relative_write_goes_to_the_working_directory(self):
        """read_file, edit_file and run_command work in the cwd; write_file must too."""
        project = pathlib.Path(self.tmpdir) / "project"
        project.mkdir()
        old_cwd = os.getcwd()
        os.chdir(project)
        try:
            content = "import os\n\nx = 7\nprint('result:', x)\n"
            result = _tool_write_file({"path": "relative_strategy.py", "content": content})
        finally:
            os.chdir(old_cwd)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(pathlib.Path(result["data"]["path"]), (project / "relative_strategy.py").resolve())

    def test_relative_write_from_home_goes_to_user_generated_dir(self):
        old_root = os.environ.get("ARIA_USER_OUTPUT_ROOT")
        os.environ["ARIA_USER_OUTPUT_ROOT"] = self.tmpdir
        old_cwd = os.getcwd()
        os.chdir(pathlib.Path.home())
        try:
            content = "import os\n\nx = 7\nprint('result:', x)\n"
            result = _tool_write_file({"path": "relative_strategy.py", "content": content})
        finally:
            os.chdir(old_cwd)
            if old_root is None:
                os.environ.pop("ARIA_USER_OUTPUT_ROOT", None)
            else:
                os.environ["ARIA_USER_OUTPUT_ROOT"] = old_root

        self.assertTrue(result["success"], result.get("error"))
        expected = pathlib.Path(self.tmpdir) / "generated" / "relative_strategy.py"
        self.assertEqual(pathlib.Path(result["data"]["path"]), expected.resolve())
        self.assertTrue(expected.exists())
        self.assertTrue(result["data"]["artifact_registered"])
        self.assertTrue(pathlib.Path(result["data"]["artifact_metadata_path"]).exists())

    def test_stage_only_write_requires_explicit_apply(self):
        content = "import os\nimport sys\n\nx = 99\nprint('result:', x)\n"
        result = _tool_write_file({"path": self.test_file, "content": content, "stage_only": True})
        self.assertTrue(result["success"], result.get("error"))
        self.assertFalse(result["data"]["applied"])
        self.assertFalse(os.path.exists(self.test_file))

        change_id = result["data"]["change_id"]
        GLOBAL_CHANGE_STORE.apply(change_id)
        self.assertTrue(os.path.exists(self.test_file))
        read = _tool_read_file({"path": self.test_file})
        self.assertIn("x = 99", read["data"]["content"])

    def test_read_nonexistent_file(self):
        result = _tool_read_file({"path": "/tmp/nonexistent_aria_test_xyz.txt"})
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"].lower())

    def test_write_empty_content_rejected(self):
        result = _tool_write_file({"path": self.test_file, "content": "   "})
        self.assertFalse(result["success"])

    def test_write_placeholder_content_rejected(self):
        result = _tool_write_file({"path": self.test_file, "content": "<placeholder>"})
        self.assertFalse(result["success"])

    def test_edit_file(self):
        _tool_write_file({"path": self.test_file, "content": "import os\n\nx = 1\nprint(x)\n"})
        result = _tool_edit_file({
            "path": self.test_file,
            "old_string": "x = 1",
            "new_string": "x = 42",
        })
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(result["data"]["applied"])
        read = _tool_read_file({"path": self.test_file})
        self.assertIn("x = 42", read["data"]["content"])

    def test_stage_only_edit_conflict_detection(self):
        _tool_write_file({"path": self.test_file, "content": "import os\n\nx = 1\nprint(x)\n"})
        result = _tool_edit_file({
            "path": self.test_file,
            "old_string": "x = 1",
            "new_string": "x = 7",
            "stage_only": True,
        })
        self.assertTrue(result["success"], result.get("error"))
        change_id = result["data"]["change_id"]
        with open(self.test_file, "w", encoding="utf-8") as f:
            f.write("import os\n\nx = 3\nprint(x)\n")
        with self.assertRaises(ChangeConflictError):
            GLOBAL_CHANGE_STORE.apply(change_id)

    def test_edit_file_old_string_not_found(self):
        _tool_write_file({"path": self.test_file, "content": "x = 1\nprint(x)\n"})
        result = _tool_edit_file({
            "path": self.test_file,
            "old_string": "nonexistent string xyz",
            "new_string": "replacement",
        })
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"].lower())

    def test_symlink_traversal_blocked(self):
        """Creating a symlink to /etc should be blocked by _is_safe_path."""
        link_path = os.path.join(self.tmpdir, "etc_link")
        try:
            os.symlink("/etc", link_path)
            target = os.path.join(link_path, "passwd")
            result = _tool_read_file({"path": target})
            self.assertFalse(result["success"])
            self.assertIn("Access denied", result.get("error", ""))
        except PermissionError:
            pass  # Some systems prevent this symlink
        except OSError:
            pass

    def test_read_file_with_offset_and_limit(self):
        lines = "\n".join(f"line {i}" for i in range(20))
        _tool_write_file({"path": self.test_file, "content": lines})
        result = _tool_read_file({"path": self.test_file, "offset": 5, "limit": 3})
        self.assertTrue(result["success"])
        content = result["data"]["content"]
        self.assertIn("line 5", content)
        self.assertNotIn("line 0", content)

    def test_read_file_default_limit_caps_large_context_reads(self):
        lines = "\n".join(f"line {i}" for i in range(220))
        _tool_write_file({"path": self.test_file, "content": lines})
        result = _tool_read_file({"path": self.test_file})

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["lines"], 160)
        self.assertIn("line 159", result["data"]["content"])
        self.assertNotIn("line 200", result["data"]["content"])
        self.assertIn("use offset/limit to read more", result["data"]["content"])

    def test_list_files(self):
        for name in ["a.py", "b.py", "c.txt"]:
            open(os.path.join(self.tmpdir, name), "w").close()
        result = _tool_list_files({"path": self.tmpdir, "pattern": "*.py"})
        self.assertTrue(result["success"])
        names = [item["name"] for item in result["data"]["items"]]
        self.assertIn("a.py", names)
        self.assertIn("b.py", names)
        self.assertNotIn("c.txt", names)


# ============================================================================
# Config system
# ============================================================================

# ============================================================================
# Prompt routing
# ============================================================================

class TestPromptRouting(unittest.TestCase):
    def test_coding_request_python_script(self):
        self.assertTrue(_is_coding_request("write a python backtest for SPY"))

    def test_coding_request_chinese_code(self):
        self.assertTrue(_is_coding_request("生成一个动量策略的代码"))

    def test_analysis_request_stock(self):
        self.assertTrue(_is_analysis_request("分析苹果股票"))
        self.assertFalse(_is_coding_request("分析苹果股票"))

    def test_analysis_request_english(self):
        self.assertTrue(_is_analysis_request("analyze AAPL fundamentals"))
        self.assertFalse(_is_coding_request("analyze AAPL fundamentals"))

    def test_coding_takes_priority_over_analysis(self):
        # "生成" is coding keyword, so should NOT be analysis even if "分析" present
        self.assertTrue(_is_coding_request("分析苹果股票并生成图表"))
        self.assertFalse(_is_analysis_request("分析苹果股票并生成图表"))

    def test_chat_is_neither(self):
        self.assertFalse(_is_coding_request("what is the federal funds rate?"))
        self.assertFalse(_is_analysis_request("what is the federal funds rate?"))

    def test_shenzhen_realty_not_analysis(self):
        """'深圳房价走势和折旧价' 不应路由到股票分析模板（房地产排除修复验证）。"""
        # 含 "房价"/"折旧" → _ANALYSIS_NON_STOCK_TOPICS 排除，返回 False
        self.assertFalse(_is_analysis_request("深圳房价走势和折旧价"))
        self.assertFalse(_is_coding_request("深圳房价走势和折旧价"))

    def test_generate_strategy_is_coding(self):
        """'生成一个动量策略回测代码' 应被识别为编码请求。"""
        self.assertTrue(_is_coding_request("生成一个动量策略回测代码"))

    def test_visual_artifact_is_coding(self):
        self.assertTrue(_is_coding_request("生成今日A股晨报看板"))
        self.assertTrue(_is_coding_request("生成市场热力图报告"))

    def test_chart_generation_request_is_chart_intent(self):
        self.assertTrue(_is_stock_chart_analysis_request("生成Apple公司的股票k线图要近一年的图表"))
        self.assertTrue(_is_stock_chart_analysis_request("画 AAPL 的近一年走势图"))

    def test_chart_keyword_without_symbol_does_not_route(self):
        # Regression: "你会制作图表和根据我的现有数据去分析吗" (a capability
        # question) was routed as `/chart <whole sentence> 1y`, which then
        # tried to fetch market data for the sentence and failed. A chart
        # keyword with no resolvable symbol must fall through to the LLM.
        from unittest import mock
        # Same module route_top_level_text is imported from at the top of this
        # file; patching the bare copy's _route_symbols did nothing, and the
        # sibling 'does not route' test passed vacuously because of it.
        from aria_code.apps.cli.commands import market as market_mod
        with mock.patch.object(market_mod, "_route_symbols", return_value=[]):
            routed = route_top_level_text(
                "你会制作图表和根据我的现有数据去分析吗",
                {"/chart", "/dashboard", "/report"},
            )
        self.assertIsNone(routed)

    def test_report_keyword_without_symbol_does_not_route(self):
        from unittest import mock
        from aria_code.apps.cli.commands import market as market_mod
        with mock.patch.object(market_mod, "_route_symbols", return_value=[]):
            routed = route_top_level_text(
                "你能生成研究报告吗", {"/chart", "/dashboard", "/report"}
            )
        self.assertIsNone(routed)

    def test_chart_keyword_with_symbol_still_routes(self):
        from unittest import mock
        from aria_code.apps.cli.commands import market as market_mod
        with mock.patch.object(market_mod, "_route_symbols", return_value=["AAPL"]):
            routed = route_top_level_text(
                "画一张K线图看看", {"/chart", "/dashboard", "/report"}
            )
        self.assertIsNotNone(routed)
        self.assertEqual(routed.command, "/chart")
        self.assertIn("AAPL", routed.args)

    def test_visual_market_artifact_request_routes_away_from_snapshot(self):
        self.assertTrue(_is_visual_market_artifact_request("生成Apple公司的股票K线图"))
        self.assertTrue(_is_visual_market_artifact_request("生成今日A股晨报看板"))
        self.assertFalse(_is_visual_market_artifact_request("分析苹果股票基本面"))

    def test_top_level_visual_route(self):
        routed = route_top_level_text("生成Apple公司的股票K线图", {"/chart", "/dashboard", "/report"})
        self.assertIsNotNone(routed)
        self.assertEqual(routed.command, "/chart")
        self.assertIn("AAPL", routed.text)
        self.assertIn("1y", routed.text)

        routed_dash = route_top_level_text("生成今日A股晨报看板", {"/chart", "/dashboard", "/report"})
        self.assertIsNotNone(routed_dash)
        self.assertEqual(routed_dash.command, "/dashboard")

        routed_report = route_top_level_text("生成AAPL研究报告", {"/chart", "/dashboard", "/report"})
        self.assertIsNotNone(routed_report)
        self.assertEqual(routed_report.command, "/report")
        self.assertIn("--type standard", routed_report.text)
        self.assertIn("--format html", routed_report.text)

    def test_send_message_visual_route_helper(self):
        routed = _natural_language_visual_artifact_route(
            "生成Apple公司的股票K线图要近一年的图表",
            {"/chart", "/dashboard", "/report"},
        )
        self.assertIsNotNone(routed)
        self.assertEqual(routed.command, "/chart")
        self.assertIn("AAPL", routed.text)

        dashboard = _natural_language_visual_artifact_route(
            "生成今日A股晨报看板",
            {"/chart", "/dashboard", "/report"},
        )
        self.assertIsNotNone(dashboard)
        self.assertEqual(dashboard.command, "/dashboard")

        self.assertIsNone(_natural_language_visual_artifact_route(
            "分析苹果股票基本面",
            {"/chart", "/dashboard", "/report"},
        ))


class TestConfig(unittest.TestCase):
    def test_default_config_has_required_keys(self):
        for key in (
            "api_url", "model", "thinking_mode", "command_policy",
            "permission_mode", "network_enabled", "watchlist",
            "response_footer", "auto_compact_context", "auto_compact_threshold",
        ):
            self.assertIn(key, DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["response_footer"], "compact")

    def test_resolve_model_key_aliases(self):
        # Model aliases updated: "sonata" maps to "qwen7b", "prelude" to "qwen-fast"
        self.assertEqual(resolve_model_key("s"),  "qwen7b")
        self.assertEqual(resolve_model_key("p"),  "qwen-fast")
        self.assertEqual(resolve_model_key("st"), "deepseek-r1")   # sonata-thinking
        # "pt" not in aliases — community sentinel
        self.assertEqual(resolve_model_key("pt"), "_community_")

    def test_resolve_model_key_full_id(self):
        # Legacy Aria model IDs forward-mapped to current Ollama keys
        self.assertEqual(resolve_model_key("aria-sonata:4.5"), "qwen7b")
        self.assertEqual(resolve_model_key("aria-sonata:4.6"), "qwen7b")
        # aria-prelude:4.1 not in aliases — community sentinel
        self.assertIn(resolve_model_key("aria-prelude:4.1"), ("_community_", "qwen-fast"))

    def test_resolve_model_key_unknown_returns_community(self):
        # Unknown models return the community sentinel (not "prelude") since 4.6 rename
        self.assertEqual(resolve_model_key("unknown-model-xyz"), "_community_")

    def test_models_have_required_fields(self):
        for key, model in MODELS.items():
            for field in ("id", "name", "speed", "intelligence", "description"):
                self.assertIn(field, model, f"MODELS[{key}] missing '{field}'")


# ============================================================================
# SessionManager
# ============================================================================

class TestSessionManager(unittest.TestCase):
    def setUp(self):
        self.mgr = SessionManager()
        self.test_session_id = "test_session_aria_cli_9999"

    def tearDown(self):
        self.mgr.delete_session(self.test_session_id)

    def test_save_and_load_session(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        self.mgr.save_session(self.test_session_id, messages, metadata={"title": "Test Session"})
        data = self.mgr.load_session(self.test_session_id)
        self.assertIsNotNone(data)
        self.assertEqual(data["id"], self.test_session_id)
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["metadata"]["title"], "Test Session")

    def test_load_nonexistent_session(self):
        result = self.mgr.load_session("nonexistent_session_xyz_12345")
        self.assertIsNone(result)

    def test_list_sessions_includes_saved(self):
        messages = [{"role": "user", "content": "test"}]
        self.mgr.save_session(self.test_session_id, messages, metadata={"title": "Test"})
        sessions = self.mgr.list_sessions()
        ids = [s["id"] for s in sessions]
        self.assertIn(self.test_session_id, ids)

    def test_delete_session(self):
        messages = [{"role": "user", "content": "test"}]
        self.mgr.save_session(self.test_session_id, messages)
        deleted = self.mgr.delete_session(self.test_session_id)
        self.assertTrue(deleted)
        self.assertIsNone(self.mgr.load_session(self.test_session_id))

    def test_list_sessions_title_field(self):
        """list_sessions() must return 'title' from metadata."""
        messages = [{"role": "user", "content": "test"}]
        self.mgr.save_session(self.test_session_id, messages, metadata={"title": "My Title"})
        sessions = self.mgr.list_sessions()
        matching = [s for s in sessions if s["id"] == self.test_session_id]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["title"], "My Title")

    def test_build_session_diagnostic_bundle_redacts_sensitive_config(self):
        trace = RuntimeTrace()
        turn = AgentTurnState(provider="deepseek")
        turn.append_response("done")
        trace.add_turn_result(turn.build_result(elapsed=1.0).to_envelope().to_dict())

        bundle = build_session_diagnostic_bundle(
            session_id=self.test_session_id,
            conversation=[{"role": "user", "content": "hello"}],
            config={
                "api_token": "secret-token",
                "model": "qwen2.5:7b",
            },
            paths={"config_dir": "/tmp/aria"},
            trace=trace,
            provider_health=[{"provider": "yfinance", "status": "ok"}],
        )

        self.assertEqual(bundle["schema"], "aria.session_diagnostic_bundle.v1")
        self.assertEqual(bundle["session_id"], self.test_session_id)
        self.assertEqual(bundle["conversation_count"], 1)
        self.assertEqual(bundle["config"]["api_token"], "***")
        self.assertEqual(bundle["config"]["model"], "qwen2.5:7b")
        self.assertEqual(bundle["paths"]["config_dir"], "/tmp/aria")
        self.assertIn("runtime_trace", bundle)
        self.assertIn("turn_results", bundle["runtime_trace"])
        self.assertEqual(bundle["provider_health"][0]["provider"], "yfinance")
        self.assertEqual(bundle["provider_health_summary"]["schema"], "aria.provider_health_summary.v1")
        self.assertEqual(bundle["provider_health_summary"]["total"], 1)
        self.assertIn("artifact_summary", bundle)
        self.assertEqual(bundle["architecture"]["schema_version"], "aria.agent-architecture.v1")

    def test_bundle_carries_doctor_and_mcp_status_when_provided(self):
        from types import SimpleNamespace
        fake_doctor = SimpleNamespace(
            status="warn", passed=3, warnings=1, errors=0,
            checks=[SimpleNamespace(name="python", status="ok", detail="3.12", suggestion="")],
        )
        bundle = build_session_diagnostic_bundle(
            session_id=self.test_session_id,
            conversation=[],
            config={},
            doctor_report=fake_doctor,
            mcp_status=[{"name": "quant", "alive": True, "circuit": "closed (0 recent failures)"}],
        )
        self.assertEqual(bundle["doctor"]["status"], "warn")
        self.assertEqual(bundle["doctor"]["checks"][0]["name"], "python")
        self.assertEqual(bundle["mcp_servers"][0]["circuit"], "closed (0 recent failures)")

    def test_bundle_omits_doctor_and_mcp_when_absent(self):
        bundle = build_session_diagnostic_bundle(
            session_id=self.test_session_id, conversation=[], config={},
        )
        self.assertNotIn("doctor", bundle)
        self.assertNotIn("mcp_servers", bundle)
        self.assertNotIn("context_health", bundle)

    def test_bundle_carries_context_health_when_provided(self):
        bundle = build_session_diagnostic_bundle(
            session_id=self.test_session_id, conversation=[], config={},
            context_health={"estimated_tokens": 42, "max_tokens": 1000, "fill_ratio": 0.042},
        )
        self.assertEqual(bundle["context_health"]["estimated_tokens"], 42)

    def test_build_session_export_payload_supports_bundle_and_sft(self):
        conversation = [
            {"role": "user", "content": "How is AAPL doing?"},
            {"role": "assistant", "content": "AAPL is up with a clear positive trend and improving momentum."},
        ]
        trace = RuntimeTrace()
        trace.add_turn_result(AgentTurnState(provider="ollama").build_result(elapsed=1.0).to_envelope().to_dict())

        content, ext, prefix = build_session_export_payload(
            "bundle",
            conversation,
            session_id=self.test_session_id,
            config={"api_token": "secret"},
            paths={"config_dir": "/tmp/aria"},
            trace=trace,
            provider_health=[{"provider": "yfinance", "status": "ok"}],
        )

        bundle = json.loads(content)
        self.assertEqual(ext, "json")
        self.assertEqual(prefix, "aria_bundle")
        self.assertEqual(bundle["schema"], "aria.session_diagnostic_bundle.v1")
        self.assertEqual(bundle["config"]["api_token"], "***")
        self.assertEqual(bundle["paths"]["config_dir"], "/tmp/aria")
        self.assertIn("runtime_trace", bundle)
        self.assertEqual(bundle["provider_health_summary"]["status"], "ok")
        self.assertIn("artifact_summary", bundle)
        self.assertIn("architecture", bundle)

        sft_content, sft_ext, sft_prefix = build_session_export_payload("sft", conversation)
        pairs = json.loads(sft_content)
        self.assertEqual(sft_ext, "json")
        self.assertEqual(sft_prefix, "aria_sft")
        self.assertEqual(pairs[0]["instruction"], "How is AAPL doing?")


# ============================================================================
# Tool parameter validation (execute_aria_tool pre-validation)
# ============================================================================

class TestAriaToolValidation(unittest.TestCase):
    """
    These tests check client-side parameter validation without hitting the API.
    We monkey-patch execute_aria_tool's validation logic indirectly by calling
    the parts that can be unit-tested.
    """

    def test_valid_symbol_passes_regex(self):
        import re
        pattern = r'^[A-Z0-9.\-/=]{1,12}$'
        for sym in ("AAPL", "BTC-USD", "SPY", "NVDA", "EUR/USD", "GC=F"):
            self.assertTrue(re.match(pattern, sym), f"Expected {sym} to be valid")

    def test_invalid_symbol_fails_regex(self):
        import re
        pattern = r'^[A-Z0-9.\-/]{1,12}$'
        for sym in ("", "AAPL MSFT", "aapl", "TOO_LONG_SYMBOL_123"):
            self.assertFalse(re.match(pattern, sym), f"Expected {sym} to be invalid")

    def test_valid_date_format(self):
        import re
        pattern = r'^\d{4}-\d{2}-\d{2}$'
        for date in ("2024-01-01", "2023-12-31"):
            self.assertTrue(re.match(pattern, date))

    def test_invalid_date_format(self):
        import re
        pattern = r'^\d{4}-\d{2}-\d{2}$'
        for date in ("2024/01/01", "Jan 1 2024", "20240101", ""):
            self.assertFalse(re.match(pattern, date))


# ============================================================================
# Write policy
# ============================================================================

class TestWritePolicy(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_policy = _ACTIVE_WRITE_POLICY[0]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _ACTIVE_WRITE_POLICY[0] = self._orig_policy

    def test_desktop_write_skips_confirm(self):
        """Writes to ~/Desktop should always succeed without user interaction."""
        import pathlib
        from unittest import mock
        # Its own home, so the test never writes to the real Desktop.
        home = mock.patch.dict(os.environ, {"HOME": self.tmpdir, "USERPROFILE": self.tmpdir})
        home.start()
        self.addCleanup(home.stop)
        desktop = pathlib.Path.home() / "Desktop"
        target = str(desktop / "_aria_test_write_policy_xyz.py")
        _ACTIVE_WRITE_POLICY[0] = "desktop_only"
        content = "import os\nimport sys\n\nprint('hello')\n"
        result = _tool_write_file({"path": target, "content": content})
        # Clean up immediately whether or not it succeeded
        try:
            pathlib.Path(target).unlink()
        except Exception:
            pass
        self.assertTrue(result["success"], result.get("error"))

    def test_skip_confirm_flag_bypasses_policy(self):
        """_skip_confirm=True should bypass all confirmation (used by /scaffold)."""
        _ACTIVE_WRITE_POLICY[0] = "always_confirm"
        target = os.path.join(self.tmpdir, "scaffold_test.py")
        content = "import os\nimport sys\n\nprint('scaffold')\n"
        result = _tool_write_file({"path": target, "content": content, "_skip_confirm": True})
        self.assertTrue(result["success"], result.get("error"))

    def test_sync_write_policy(self):
        """_sync_write_policy should update _ACTIVE_WRITE_POLICY."""
        _sync_write_policy({"write_policy": "always_confirm"})
        self.assertEqual(_ACTIVE_WRITE_POLICY[0], "always_confirm")
        _sync_write_policy({"write_policy": "confirm_outside"})
        self.assertEqual(_ACTIVE_WRITE_POLICY[0], "confirm_outside")
        _sync_write_policy({})  # missing key → defaults to desktop_only
        self.assertEqual(_ACTIVE_WRITE_POLICY[0], "desktop_only")

    def test_sync_permission_and_network_policy(self):
        _sync_write_policy({"permission_mode": "read-only", "network_enabled": False})
        self.assertEqual(_ACTIVE_PERMISSION_MODE[0], "read-only")
        self.assertFalse(_ACTIVE_NETWORK_ENABLED[0])
        _sync_write_policy({})
        self.assertEqual(_ACTIVE_PERMISSION_MODE[0], "workspace-write")
        self.assertTrue(_ACTIVE_NETWORK_ENABLED[0])

    def test_default_config_has_write_policy(self):
        self.assertIn("write_policy", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["write_policy"], "desktop_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCommandRegistryContract(unittest.TestCase):
    """Guard against the registration/crash bug class found in the audit:
    cmd_* handlers existing but never wired into the dispatch dict (e.g. /team,
    /portfolio went to the LLM), and handlers with wrong signatures."""

    @classmethod
    def setUpClass(cls):
        import aria_code.aria_cli as _ac
        cls._ac = _ac
        cls.term = _ac.ArtheraTerminal(dict(_ac.DEFAULT_CONFIG))
        cls.cmds = cls.term.commands.commands

    def test_every_registered_command_is_callable(self):
        import inspect
        for name, entry in self.cmds.items():
            handler = entry[0]
            self.assertTrue(callable(handler), f"{name} handler not callable")
            # Handler must accept exactly one positional arg (args) besides bound self
            sig = inspect.signature(handler)
            params = [p for p in sig.parameters.values()
                      if p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)]
            self.assertGreaterEqual(len(params), 1,
                                    f"{name} handler must accept an args parameter")

    def test_declared_commands_are_registered(self):
        """Any command advertised via a usage hint (shown in /help, tab-complete)
        MUST be wired into the dispatch dict. This is the exact audit bug:
        /team, /portfolio, /apply-plan etc. had hints but fell through to the
        LLM, which then hallucinated."""
        S = self._ac.SlashCommands
        hints = getattr(S, "_COMMAND_HELP", {}) or {}
        if not hints:
            self.skipTest("_COMMAND_HELP not found")
        missing = []
        for name in hints:
            base = name.split()[0]            # "/broker add" → "/broker"
            if base in self.cmds:
                continue
            method = "cmd_" + base.lstrip("/").replace("-", "_")
            if hasattr(S, method):            # a real handler exists but isn't wired
                missing.append(f"{base} → {method}")
        self.assertEqual(missing, [],
                         f"commands have usage hints but no dispatch entry: {missing}")
