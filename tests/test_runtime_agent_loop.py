import unittest
import asyncio
from pathlib import Path

from aria_code.runtime import (
    AgentEventComplete,
    AgentEventError,
    AgentEventStatus,
    AgentErrorPresentation,
    AgentOptions,
    ApprovalDecision,
    AgentTurnState,
    AgentTurnResult,
    AgentTurnEnvelope,
    LoopGuard,
    ToolExecutor,
    ToolBatchState,
    ToolTurnPlan,
    build_next_turn_messages,
    build_tool_followup,
    collect_parallel_done,
    execute_tool_turn,
    record_tool_result,
    run_agent,
    run_parallel_tools,
    run_serial_tool,
    split_tool_calls,
)


class RuntimeAgentLoopTests(unittest.TestCase):
    def test_agent_error_presentation_no_provider(self):
        presentation = AgentErrorPresentation.from_error("no_provider")

        self.assertEqual(presentation.error, "no_provider")
        self.assertEqual(presentation.level, "warning")
        self.assertFalse(presentation.use_generic_error_prefix)
        self.assertEqual(presentation.lines[0], "没有可用的 AI 模型")
        self.assertTrue(any("ollama serve" in line for line in presentation.lines))

    def test_agent_error_presentation_all_providers_failed(self):
        presentation = AgentErrorPresentation.from_error("all_providers_failed")

        self.assertEqual(presentation.level, "warning")
        self.assertEqual(
            presentation.lines,
            ["所有云端 Provider 均请求失败，请检查网络或 API Key 是否有效。"],
        )

    def test_agent_error_presentation_empty_response_is_actionable(self):
        presentation = AgentErrorPresentation.from_error("empty_response")

        # 597899e lowered this to warning for a minimalist empty-response UI.
        self.assertEqual(presentation.level, "warning")
        self.assertFalse(presentation.use_generic_error_prefix)
        self.assertIn("空响应", presentation.lines[0])

    def test_agent_error_presentation_evidence_required_is_actionable(self):
        presentation = AgentErrorPresentation.from_error("[ARIA-4223] evidence required")

        self.assertEqual(presentation.level, "warning")
        self.assertFalse(presentation.use_generic_error_prefix)
        self.assertIn("可验证的金融数据", presentation.lines[0])

    def test_agent_error_presentation_respects_ui_language(self):
        presentation = AgentErrorPresentation.from_error("empty_response", lang="en")

        self.assertIn("no visible response", presentation.lines[0])
        self.assertIn("/health", presentation.lines[1])
        self.assertTrue(any("/health" in line for line in presentation.lines))

    def test_agent_error_presentation_unknown_error(self):
        presentation = AgentErrorPresentation.from_error("boom")

        # Catch-all branch, also lowered by 597899e.
        self.assertEqual(presentation.level, "warning")
        self.assertTrue(presentation.use_generic_error_prefix)
        self.assertEqual(presentation.lines, ["Error: boom"])

    def test_agent_turn_state_accumulates_model_results(self):
        state = AgentTurnState(provider="aws")

        state.apply_model_result({
            "response": "hello",
            "provider": "deepseek",
            "tools_used": ["read_file", "read_file", "run_command"],
            "sources": [{"url": "x"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "thinking_tokens": 2,
            },
        })
        state.apply_model_result({
            "response": " world",
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        })

        self.assertEqual(state.total_response, "hello world")
        self.assertEqual(state.provider, "deepseek")
        self.assertEqual(state.sources, [{"url": "x"}])
        self.assertEqual(state.usage["prompt_tokens"], 13)
        self.assertEqual(state.usage["completion_tokens"], 9)
        self.assertEqual(state.usage["thinking_tokens"], 2)
        self.assertEqual(state.unique_tools(), ["read_file", "run_command"])

    def test_agent_turn_state_token_counts_use_fallbacks(self):
        state = AgentTurnState()

        self.assertEqual(
            state.token_counts(token_count=7, thinking_tokens=3),
            (0, 7, 3, 10),
        )

        state.add_usage({"prompt_tokens": 2, "completion_tokens": 5, "thinking_tokens": 1})

        self.assertEqual(
            state.token_counts(token_count=7, thinking_tokens=3),
            (2, 5, 1, 8),
        )

    def test_agent_turn_state_tool_time_and_final_text(self):
        state = AgentTurnState()

        state.add_tool_time(1.5)
        state.append_response("")

        self.assertEqual(state.generation_time(5.0), 3.5)
        self.assertEqual(state.final_text("fallback"), "fallback")

        state.append_response("done")
        self.assertEqual(state.final_text("fallback"), "done")
        state.reset_response()
        self.assertEqual(state.total_response, "")

    def test_agent_turn_state_builds_metadata_from_usage(self):
        state = AgentTurnState(provider="deepseek")
        state.add_usage({"prompt_tokens": 100, "completion_tokens": 50, "thinking_tokens": 10})
        state.add_tool_time(1.0)
        state.tools_used.extend(["read_file", "read_file", "run_command"])

        metadata = state.build_metadata(elapsed=3.0)

        self.assertEqual(metadata.prompt_tokens, 100)
        self.assertEqual(metadata.completion_tokens, 50)
        self.assertEqual(metadata.thinking_tokens, 10)
        self.assertEqual(metadata.total_tokens, 160)
        self.assertEqual(metadata.generation_time, 2.0)
        self.assertEqual(metadata.provider, "deepseek")
        self.assertEqual(metadata.tools, ["read_file", "run_command"])
        self.assertEqual(metadata.system_prompt_estimate("hello"), 99)
        self.assertEqual(metadata.parts, [
            "3.0s",
            "160 tokens (in: 100, out: 50, think: 10)",
            "25 t/s",
            "tools: 1.0s",
            "deepseek",
            "read_file run_command",
        ])

    def test_agent_turn_state_builds_metadata_from_token_fallback(self):
        state = AgentTurnState()

        metadata = state.build_metadata(elapsed=2.0, token_count=20)

        self.assertEqual(metadata.prompt_tokens, 0)
        self.assertEqual(metadata.completion_tokens, 20)
        self.assertEqual(metadata.total_tokens, 20)
        self.assertEqual(metadata.parts, ["2.0s", "20 tokens (out: 20)", "10 t/s"])

    def test_agent_turn_state_builds_result(self):
        state = AgentTurnState(provider="openai")
        state.append_response("final")
        state.sources.append({"id": "source-1"})
        state.tools_used.extend(["read_file", "read_file"])
        state.add_usage({"prompt_tokens": 4, "completion_tokens": 6})

        result = state.build_result(elapsed=2.0)

        self.assertTrue(result.success)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.error, "")
        self.assertEqual(result.final_text, "final")
        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.tools, ["read_file"])
        self.assertEqual(result.sources, [{"id": "source-1"}])
        self.assertEqual(result.metadata.total_tokens, 10)
        self.assertEqual(result.to_dict()["metadata"]["parts"], result.metadata.parts)

    def test_agent_turn_state_keeps_streamed_text_when_result_text_is_empty(self):
        state = AgentTurnState(provider="ollama")

        state.apply_model_result(
            {"success": True, "response": "", "provider": "ollama"},
            fallback_response="streamed answer",
        )

        self.assertEqual(state.total_response, "streamed answer")

    def test_agent_turn_state_builds_cancelled_result(self):
        state = AgentTurnState(provider="ollama")
        state.append_response("partial")
        state.add_usage({"completion_tokens": 3})

        result = state.build_cancelled_result(elapsed=1.0)

        self.assertTrue(result.success)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.error, "")
        self.assertEqual(result.final_text, "partial")
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.metadata.parts, ["1.0s", "3 tokens (out: 3)", "3 t/s", "ollama"])

    def test_agent_turn_state_builds_error_result(self):
        state = AgentTurnState()

        result = state.build_error_result(None, elapsed=0.25, fallback_response="partial")

        self.assertFalse(result.success)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.error, "Unknown error")
        self.assertEqual(result.final_text, "partial")

    def test_agent_turn_result_factories(self):
        cancelled = AgentTurnResult.cancelled_result(final_text="partial")
        failed = AgentTurnResult.error_result("no_provider")

        self.assertTrue(cancelled.success)
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(cancelled.final_text, "partial")
        self.assertFalse(failed.success)
        self.assertEqual(failed.error, "no_provider")

    def test_agent_turn_result_to_envelope_is_stable(self):
        state = AgentTurnState(provider="deepseek")
        state.append_response("done")
        state.add_usage({"prompt_tokens": 2, "completion_tokens": 4})

        result = state.build_result(elapsed=1.2)
        env = result.to_envelope()

        self.assertIsInstance(env, AgentTurnEnvelope)
        self.assertEqual(env.status, "ok")
        self.assertEqual(env.provider, "deepseek")
        self.assertIn("1.2s", env.summary)
        self.assertIn("6 tokens (in: 2, out: 4)", env.summary)
        self.assertIn("deepseek", env.summary)
        self.assertTrue(env.to_dict()["success"])

    def test_split_tool_calls_keeps_write_tools_serial(self):
        read = {"tool": "read_file", "params": {}}
        search = {"tool": "search_code", "params": {}}
        write = {"tool": "write_file", "params": {}}
        run = {"tool": "run_command", "params": {}}

        parallel, serial = split_tool_calls([read, write, search, run])

        self.assertEqual(parallel, [read, search])
        self.assertEqual(serial, [write, run])

    def test_collect_parallel_done_preserves_original_indices(self):
        read = {"tool": "read_file", "params": {}}
        write = {"tool": "write_file", "params": {}}
        search = {"tool": "search_code", "params": {}}
        result_read = {"success": True, "data": "read"}
        result_search = {"success": True, "data": "search"}

        done = collect_parallel_done(
            [read, write, search],
            [(read, result_read), (search, result_search)],
        )

        self.assertEqual(done, {0: result_read, 2: result_search})

    def test_build_tool_followup(self):
        followup = build_tool_followup([
            {"tool": "read_file", "result": "OK: 10 lines"},
            {"tool": "run_command", "result": "exit_code=0"},
        ])
        self.assertIn("### [read_file] ✓ Success", followup)
        self.assertIn("OK: 10 lines", followup)
        self.assertIn("### [run_command] ✓ Success", followup)
        self.assertIn("exit_code=0", followup)
        self.assertTrue(followup.endswith("Please continue your analysis using these results."))

    def test_build_tool_followup_does_not_duplicate_result(self):
        # Regression: results used to be embedded twice per block, doubling
        # context usage. Each result must appear exactly once.
        followup = build_tool_followup([
            {"tool": "read_file", "result": "UNIQUE_TOKEN_42"},
        ])
        self.assertEqual(followup.count("UNIQUE_TOKEN_42"), 1)

    def test_build_tool_followup_truncates_huge_result(self):
        # Regression: an oversized tool result (e.g. a long pip/install log)
        # must be capped so it can't overflow the model context and cut the
        # task short mid-run.
        from aria_code.runtime.agent_loop import _MAX_TOOL_RESULT_CHARS
        huge = "Z" * (_MAX_TOOL_RESULT_CHARS * 4)
        followup = build_tool_followup([{"tool": "run_command", "result": huge}])
        self.assertIn("已截断", followup)
        # The Z-run must be capped well under the original size.
        self.assertLess(followup.count("Z"), _MAX_TOOL_RESULT_CHARS + 100)

    def test_build_tool_followup_flags_errors(self):
        followup = build_tool_followup([
            {"tool": "run_command", "result": "Error: command failed"},
        ])
        self.assertIn("### [run_command] ❌ Error", followup)
        self.assertIn("returned errors", followup)

    def test_record_tool_result_uses_formatter(self):
        records = []

        def formatter(tool, result):
            return f"{tool}:{result['data']}"

        record = record_tool_result(records, "read_file", {"success": True, "data": "ok"}, formatter)

        self.assertEqual(record, {"tool": "read_file", "result": "read_file:ok"})
        self.assertEqual(records, [record])

    def test_build_next_turn_messages(self):
        assistant, user, followup = build_next_turn_messages(
            "assistant text",
            [{"tool": "read_file", "result": "OK"}],
        )

        self.assertEqual(assistant, {"role": "assistant", "content": "assistant text"})
        self.assertEqual(user["role"], "user")
        self.assertEqual(user["content"], followup)
        self.assertIn("### [read_file] ✓ Success", followup)
        self.assertIn("OK", followup)

    def test_tool_batch_state_records_results_and_elapsed_time(self):
        batch = ToolBatchState()

        def formatter(tool, result):
            return f"{tool}:{result['data']}"

        record = batch.add_result(
            "run_command",
            {"success": True, "data": "ok"},
            formatter,
            elapsed=1.25,
        )

        self.assertEqual(record, {"tool": "run_command", "result": "run_command:ok"})
        self.assertEqual(batch.tool_results, [record])
        self.assertEqual(batch.elapsed_total, 1.25)
        self.assertFalse(batch.cancelled)

    def test_execute_tool_turn_applies_approval_and_builds_next_turn(self):
        captured = {}

        def run_command(params):
            captured.update(params)
            return {"success": True, "data": {"exit_code": 0, "stdout": "ok"}}

        async def approval(tool_name, params):
            self.assertEqual(tool_name, "run_command")
            self.assertEqual(params["command"], "pytest -q")
            return ApprovalDecision.allow(policy="balanced", user_approved=True)

        executor = ToolExecutor({"run_command": (run_command, "Run")})
        result = asyncio.run(execute_tool_turn(
            [{"tool": "run_command", "params": {"command": "pytest -q"}}],
            total_response="assistant text",
            tool_executor=executor,
            formatter=lambda tool, res: f"{tool}:{res['data']['stdout']}",
            confirm_tools={"run_command"},
            approval_callback=approval,
        ))

        self.assertFalse(result.cancelled)
        self.assertEqual(captured["policy"], "balanced")
        self.assertTrue(captured["user_approved"])
        self.assertEqual(result.activities[0].tool, "run_command")
        self.assertEqual(result.assistant_message["role"], "assistant")
        self.assertEqual(result.assistant_message["content"], "assistant text")
        self.assertEqual(
            result.assistant_message["tool_calls"][0]["function"],
            {"name": "run_command", "arguments": {"command": "pytest -q"}},
        )
        self.assertEqual(result.tool_messages[0]["role"], "tool")
        self.assertEqual(result.tool_messages[0]["name"], "run_command")
        self.assertEqual(result.user_message["role"], "user")
        self.assertIn("run_command:ok", result.followup)

    def test_tool_turn_approval_does_not_trust_model_policy_fields(self):
        captured = {}
        executor = ToolExecutor(
            {"run_command": (lambda params: captured.update(params) or {"success": True}, "Run")},
            config={"command_policy": "safe", "permission_mode": "read-only", "network_enabled": False},
        )

        asyncio.run(execute_tool_turn(
            [{"tool": "run_command", "params": {
                "command": "pytest -q",
                "policy": "full",
                "permission_mode": "full-access",
                "network_enabled": True,
            }}],
            total_response="",
            tool_executor=executor,
            formatter=lambda _tool, _result: "ok",
            confirm_tools={"run_command"},
            approval_callback=lambda _tool, _params: ApprovalDecision.allow(
                policy="balanced", user_approved=True
            ),
        ))

        self.assertEqual(captured["policy"], "balanced")
        self.assertEqual(captured["permission_mode"], "read-only")
        self.assertFalse(captured["network_enabled"])
        self.assertTrue(captured["user_approved"])

    def test_execute_tool_turn_denied_approval_cancels_without_running(self):
        ran = False

        def write_file(_params):
            nonlocal ran
            ran = True
            return {"success": True}

        executor = ToolExecutor({"write_file": (write_file, "Write")})
        result = asyncio.run(execute_tool_turn(
            [{"tool": "write_file", "params": {"path": "x", "content": "y"}}],
            total_response="",
            tool_executor=executor,
            formatter=lambda _tool, res: str(res),
            confirm_tools={"write_file"},
            approval_callback=lambda _tool, _params: ApprovalDecision.deny("no"),
        ))

        self.assertTrue(result.cancelled)
        self.assertFalse(ran)
        self.assertEqual(result.activities, [])

    def test_tool_requiring_approval_stops_if_reviewer_is_unavailable(self):
        calls = []
        executor = ToolExecutor({
            "write_file": (lambda params: calls.append(params) or {"success": True}, "Write")
        })

        for callback in (None, lambda _tool, _params: None):
            result = asyncio.run(execute_tool_turn(
                [{"tool": "write_file", "params": {"path": "x.py", "content": "x"}}],
                total_response="",
                tool_executor=executor,
                formatter=lambda _tool, _result: "ok",
                confirm_tools={"write_file"},
                approval_callback=callback,
            ))
            self.assertTrue(result.cancelled)

        self.assertEqual(calls, [])

    def test_execute_tool_turn_loop_guard_appends_retry_directive(self):
        def failing_tool(_params):
            return {"success": False, "error": "boom"}

        executor = ToolExecutor({"run_command": (failing_tool, "Run")})
        guard = LoopGuard(soft_threshold=2, hard_threshold=4)
        pending = [{"tool": "run_command", "params": {"command": "pytest -q"}}]

        first = asyncio.run(execute_tool_turn(
            pending,
            total_response="",
            tool_executor=executor,
            formatter=lambda _tool, res: f"Error: {res['error']}",
            loop_guard=guard,
        ))
        second = asyncio.run(execute_tool_turn(
            pending,
            total_response="",
            tool_executor=executor,
            formatter=lambda _tool, res: f"Error: {res['error']}",
            loop_guard=guard,
        ))

        self.assertEqual(first.guard_directives, [])
        self.assertTrue(second.guard_directives)
        self.assertIn("不要再用相同参数重试", second.followup)

    def test_run_agent_uses_runtime_tool_turn_and_emits_loop_guard_status(self):
        calls = {"provider": 0}

        async def provider_fn(message, history, **kwargs):
            calls["provider"] += 1
            if calls["provider"] <= 2:
                return {
                    "success": True,
                    "response": "need tool",
                    "provider": "fake",
                    "tool_calls_pending": [
                        {"tool": "run_command", "params": {"command": "pytest -q"}}
                    ],
                }
            return {"success": True, "response": "done", "provider": "fake"}

        def failing_tool(_params):
            return {"success": False, "error": "boom"}

        async def collect_events():
            events = []
            async for event in run_agent(
                "start",
                [],
                provider_fn=provider_fn,
                tool_executor=ToolExecutor({"run_command": (failing_tool, "Run")}),
                options=AgentOptions(max_rounds=3),
                tool_result_formatter=lambda _tool, res: f"Error: {res['error']}",
            ):
                events.append(event)
            return events

        events = asyncio.run(collect_events())

        self.assertTrue(any(isinstance(event, AgentEventStatus) and event.state == "loop_guard" for event in events))
        self.assertIsInstance(events[-1], AgentEventComplete)
        self.assertEqual(events[-1].result.provider, "fake")

    def test_run_agent_blocks_ungrounded_financial_answer_and_suppresses_tokens(self):
        streamed = []
        prompts = []

        async def provider_fn(message, history, **kwargs):
            prompts.append(message)
            kwargs["on_token"]("unverified answer")
            return {
                "success": True,
                "response": "unverified answer",
                "provider": "fake",
            }

        async def collect_events():
            events = []
            async for event in run_agent(
                "Analyze AAPL today",
                [],
                provider_fn=provider_fn,
                tool_executor=ToolExecutor({}),
                options=AgentOptions(
                    requires_evidence=True,
                    grounding_tools=frozenset({"get_market_data"}),
                ),
                on_token=streamed.append,
            ):
                events.append(event)
            return events

        events = asyncio.run(collect_events())

        self.assertEqual(streamed, [])
        self.assertIn("Grounding requirement", prompts[0])
        self.assertIn("get_market_data", prompts[0])
        self.assertIsInstance(events[-1], AgentEventError)
        self.assertIn("ARIA-4223", events[-1].error)

    def test_run_agent_releases_output_after_grounding_tool_succeeds(self):
        streamed = []
        calls = {"provider": 0}

        async def provider_fn(message, history, **kwargs):
            calls["provider"] += 1
            if calls["provider"] == 1:
                kwargs["on_token"]("fetching")
                return {
                    "success": True,
                    "response": "fetching",
                    "provider": "fake",
                    "tool_calls_pending": [
                        {"tool": "get_market_data", "params": {"symbol": "AAPL"}}
                    ],
                }
            kwargs["on_token"]("grounded answer")
            return {
                "success": True,
                "response": "grounded answer",
                "provider": "fake",
            }

        def market_tool(_params):
            return {"success": True, "data": {"symbol": "AAPL", "price": 200.0}}

        async def collect_events():
            events = []
            async for event in run_agent(
                "Analyze AAPL today",
                [],
                provider_fn=provider_fn,
                tool_executor=ToolExecutor({"get_market_data": (market_tool, "Market")}),
                options=AgentOptions(
                    requires_evidence=True,
                    grounding_tools=frozenset({"get_market_data"}),
                ),
                on_token=streamed.append,
            ):
                events.append(event)
            return events

        events = asyncio.run(collect_events())

        self.assertEqual(streamed, ["grounded answer"])
        self.assertIsInstance(events[-1], AgentEventComplete)
        self.assertEqual(events[-1].result.final_text, "grounded answer")

    def test_run_agent_accepts_verified_evidence_from_preflight(self):
        streamed = []

        async def provider_fn(message, history, **kwargs):
            kwargs["on_token"]("analysis from verified snapshot")
            return {
                "success": True,
                "response": "analysis from verified snapshot",
                "provider": "fake",
            }

        async def collect_events():
            events = []
            async for event in run_agent(
                "Analyze the verified snapshot",
                [],
                provider_fn=provider_fn,
                tool_executor=ToolExecutor({}),
                options=AgentOptions(
                    requires_evidence=True,
                    grounding_tools=frozenset({"get_market_data"}),
                    evidence_already_grounded=True,
                ),
                on_token=streamed.append,
            ):
                events.append(event)
            return events

        events = asyncio.run(collect_events())

        self.assertEqual(streamed, ["analysis from verified snapshot"])
        self.assertIsInstance(events[-1], AgentEventComplete)

    def test_tool_batch_state_cancel_and_next_turn(self):
        batch = ToolBatchState()
        batch.cancel()
        batch.add_result("read_file", {"success": True, "data": "ok"}, lambda _tool, _result: "OK")

        assistant, user, followup = batch.build_next_turn("assistant text")

        self.assertTrue(batch.cancelled)
        self.assertEqual(assistant, {"role": "assistant", "content": "assistant text"})
        self.assertEqual(user["content"], followup)
        self.assertIn("### [read_file] ✓ Success", followup)
        self.assertIn("OK", followup)

    def test_tool_turn_plan_preserves_order_and_parallel_results(self):
        read = {"tool": "read_file", "params": {"path": "a.py"}}
        write = {"tool": "write_file", "params": {"path": "a.py"}}
        search = {"tool": "search_code", "params": {"query": "x"}}
        read_result = {"success": True, "data": "read"}
        search_result = {"success": True, "data": "search"}

        plan = ToolTurnPlan(
            pending=[read, write, search],
            parallel_done={0: read_result, 2: search_result},
        )
        tasks = plan.tasks()

        self.assertEqual([task.tool_name for task in tasks], ["read_file", "write_file", "search_code"])
        self.assertIs(tasks[0].parallel_result, read_result)
        self.assertTrue(tasks[0].has_parallel_result)
        self.assertFalse(tasks[1].has_parallel_result)
        self.assertIs(tasks[2].parallel_result, search_result)
        self.assertIs(tasks[1].params, write["params"])

    def test_tool_call_task_progress_label(self):
        plan = ToolTurnPlan(pending=[
            {"tool": "read_file", "params": {}},
            {"tool": "run_command", "params": {}},
        ])
        first, second = plan.tasks()

        self.assertEqual(first.progress_label(2), "  [1/2] Running read_file...")
        self.assertEqual(second.progress_label(2), "  [2/2] Running run_command...")
        self.assertEqual(first.progress_label(1), "  Running read_file...")


class RuntimeAgentLoopAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_parallel_tools_executes_only_parallel_safe_tools(self):
        calls = []

        def read_tool(params):
            calls.append(("local", params["path"]))
            return {"success": True, "data": {"path": params["path"]}}

        async def remote_runner(tool, params):
            calls.append(("remote", tool))
            return {"success": True, "data": {"tool": tool}}

        read = {"tool": "read_file", "params": {"path": "a.py"}}
        write = {"tool": "write_file", "params": {"path": "a.py", "content": "x"}}
        remote = {"tool": "remote_tool", "params": {}}
        executor = ToolExecutor({"read_file": (read_tool, "Read")})

        done = await run_parallel_tools(
            [read, write, remote],
            executor,
            remote_runner=remote_runner,
        )

        self.assertEqual(set(done.keys()), {0, 2})
        self.assertEqual(done[0]["data"]["path"], str(Path.cwd() / "a.py"))
        self.assertEqual(done[2]["data"]["tool"], "remote_tool")
        self.assertNotIn(("local", "write_file"), calls)

    async def test_run_parallel_tools_converts_remote_exception_to_result(self):
        async def remote_runner(_tool, _params):
            raise RuntimeError("remote failed")

        remote = {"tool": "remote_tool", "params": {}}
        executor = ToolExecutor({})

        done = await run_parallel_tools([remote], executor, remote_runner=remote_runner)

        self.assertFalse(done[0]["success"])
        self.assertIn("remote failed", done[0]["error"])

    async def test_run_serial_tool_local(self):
        def echo(params):
            return {"success": True, "data": params}

        executor = ToolExecutor({"echo": (echo, "Echo")})
        result, elapsed = await run_serial_tool("echo", {"x": 1}, executor)

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["x"], 1)
        self.assertGreaterEqual(elapsed, 0)

    async def test_run_serial_tool_remote_with_hooks(self):
        hook_events = []

        async def remote_runner(tool, params):
            self.assertEqual(tool, "remote_tool")
            return {"success": True, "data": params}

        def hook(event, tool, params, result=None):
            hook_events.append((event, tool, bool(result)))

        executor = ToolExecutor({})
        result, _elapsed = await run_serial_tool(
            "remote_tool",
            {"y": 2},
            executor,
            remote_runner=remote_runner,
            hook=hook,
        )

        self.assertTrue(result["success"])
        self.assertEqual(hook_events, [("pre_tool", "remote_tool", False), ("post_tool", "remote_tool", True)])

    async def test_run_serial_tool_remote_exception(self):
        async def remote_runner(_tool, _params):
            raise RuntimeError("remote failed")

        executor = ToolExecutor({})
        result, _elapsed = await run_serial_tool("remote_tool", {}, executor, remote_runner=remote_runner)

        self.assertFalse(result["success"])
        self.assertIn("remote failed", result["error"])


if __name__ == "__main__":
    unittest.main()


class LoopGuardTests(unittest.TestCase):
    def test_warns_at_soft_threshold_and_breaks_at_hard(self):
        from aria_code.runtime import LoopGuard
        g = LoopGuard(soft_threshold=2, hard_threshold=4)
        fail = {"success": False, "error": "File not found: /x.py"}
        self.assertIsNone(g.record("read_file", {"path": "/x.py"}, fail))   # 1
        warn = g.record("read_file", {"path": "/x.py"}, fail)              # 2
        self.assertIn("read_file", warn)
        self.assertFalse(g.should_break)
        self.assertIsNone(g.record("read_file", {"path": "/x.py"}, fail))  # 3 (already warned)
        hard = g.record("read_file", {"path": "/x.py"}, fail)             # 4
        self.assertTrue(g.should_break)
        self.assertIn("停止", hard)

    def test_success_clears_counter(self):
        from aria_code.runtime import LoopGuard
        g = LoopGuard(soft_threshold=2)
        fail = {"success": False, "error": "error"}
        g.record("t", {"a": 1}, fail)
        g.record("t", {"a": 1}, {"success": True})
        # counter cleared → next failure is the first again, no warning
        self.assertIsNone(g.record("t", {"a": 1}, fail))

    def test_distinct_params_tracked_separately(self):
        from aria_code.runtime import LoopGuard
        g = LoopGuard(soft_threshold=2)
        fail = {"success": False, "error": "error"}
        self.assertIsNone(g.record("t", {"a": 1}, fail))
        self.assertIsNone(g.record("t", {"a": 2}, fail))  # different params → not a repeat

    def test_tool_level_advisory_fires_on_varying_param_failures(self):
        # The observed blind spot: peer_comparison failed 4x with slightly
        # different args and no directive ever fired. Tool-level counting
        # advises at the third failure regardless of params.
        from aria_code.runtime import LoopGuard
        g = LoopGuard(soft_threshold=2, hard_threshold=4, tool_soft_threshold=3)
        fail = {"success": False, "error": "yfinance network error"}
        self.assertIsNone(g.record("peer_comparison", {"symbol": "AAPL"}, fail))
        self.assertIsNone(g.record("peer_comparison", {"symbol": "AAPL", "peers": ["MSFT"]}, fail))
        advisory = g.record("peer_comparison", {"peers": ["GOOGL", "NVDA"]}, fail)
        self.assertIsNotNone(advisory)
        self.assertIn("peer_comparison", advisory)
        self.assertFalse(g.should_break)                       # advisory only — no hard break
        # fires once per tool
        self.assertIsNone(g.record("peer_comparison", {"symbol": "TSLA"}, fail))

    def test_tool_level_counter_cleared_by_success(self):
        from aria_code.runtime import LoopGuard
        g = LoopGuard(tool_soft_threshold=3)
        fail = {"success": False, "error": "err"}
        g.record("t", {"a": 1}, fail)
        g.record("t", {"a": 2}, fail)
        g.record("t", {"a": 3}, {"success": True})             # success resets tool count
        self.assertIsNone(g.record("t", {"a": 4}, fail))       # back to 1 → no advisory
        self.assertIsNone(g.record("t", {"a": 5}, fail))       # 2 → still none

    def test_exact_signature_hard_break_unaffected_by_tool_layer(self):
        from aria_code.runtime import LoopGuard
        g = LoopGuard(soft_threshold=2, hard_threshold=3, tool_soft_threshold=99)
        fail = {"success": False, "error": "err"}
        g.record("t", {"a": 1}, fail)
        g.record("t", {"a": 1}, fail)
        g.record("t", {"a": 1}, fail)
        self.assertTrue(g.should_break)


class TodoTrackerTests(unittest.TestCase):
    def test_update_and_normalize(self):
        from apps.cli.todo_tracker import update_todos, get_active_todos, clear_todos
        clear_todos()
        r = update_todos({"todos": [
            {"content": "step 1", "status": "completed"},
            {"content": "step 2", "status": "doing"},   # synonym → in_progress
            {"content": "step 3", "status": "pending"},
        ]})
        self.assertTrue(r["success"])
        self.assertEqual(r["data"]["completed"], 1)
        self.assertEqual(r["data"]["in_progress"], 1)
        self.assertEqual(get_active_todos()[1]["status"], "in_progress")

    def test_only_one_in_progress(self):
        from apps.cli.todo_tracker import update_todos, clear_todos
        clear_todos()
        r = update_todos({"todos": [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]})
        self.assertEqual(r["data"]["in_progress"], 1)

    def test_empty_rejected(self):
        from apps.cli.todo_tracker import update_todos
        self.assertFalse(update_todos({"todos": []})["success"])


class MultiEditTests(unittest.TestCase):
    def _tmp(self, text):
        import tempfile
        import pathlib
        d = tempfile.mkdtemp()
        p = pathlib.Path(d) / "m.py"
        p.write_text(text)
        return str(p), p

    def test_atomic_success(self):
        from apps.cli.tools.write_tools import tool_multi_edit
        path, p = self._tmp("a = 1\nb = 2\nc = 3\n")
        r = tool_multi_edit({"path": path, "edits": [
            {"old_string": "a = 1", "new_string": "a = 10"},
            {"old_string": "c = 3", "new_string": "c = 30"},
        ]})
        self.assertTrue(r["success"])
        self.assertEqual(r["data"]["edits_applied"], 2)
        self.assertIn("a = 10", p.read_text())
        self.assertIn("c = 30", p.read_text())

    def test_atomic_rollback_on_missing(self):
        from apps.cli.tools.write_tools import tool_multi_edit
        path, p = self._tmp("a = 1\nb = 2\n")
        before = p.read_text()
        r = tool_multi_edit({"path": path, "edits": [
            {"old_string": "a = 1", "new_string": "a = 99"},
            {"old_string": "NOPE", "new_string": "x"},
        ]})
        self.assertFalse(r["success"])
        self.assertEqual(p.read_text(), before)  # nothing applied

    def test_ambiguous_requires_replace_all(self):
        from apps.cli.tools.write_tools import tool_multi_edit
        path, p = self._tmp("x = 1\nx = 1\n")
        r = tool_multi_edit({"path": path, "edits": [
            {"old_string": "x = 1", "new_string": "x = 2"},
        ]})
        self.assertFalse(r["success"])
        self.assertIn("replace_all", r["error"])

    def test_replace_all(self):
        from apps.cli.tools.write_tools import tool_multi_edit
        path, p = self._tmp("x = 1\nx = 1\n")
        r = tool_multi_edit({"path": path, "edits": [
            {"old_string": "x = 1", "new_string": "x = 2", "replace_all": True},
        ]})
        self.assertTrue(r["success"])
        self.assertEqual(p.read_text().count("x = 2"), 2)

    def test_syntax_warning_on_broken_python(self):
        from apps.cli.tools.write_tools import tool_multi_edit
        path, p = self._tmp("def f():\n    return 1\n")
        r = tool_multi_edit({"path": path, "edits": [
            {"old_string": "def f():", "new_string": "def f("},
        ]})
        self.assertTrue(r["success"])
        self.assertTrue(r.get("warning"))
