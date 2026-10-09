"""The review gate: a fresh-context reviewer reads the diff before "done"."""

import asyncio
import json

from aria_code.runtime import AgentEventComplete, AgentEventStatus, AgentOptions, ToolExecutor, run_agent
from aria_code.runtime.review import ReviewGate, build_review_prompt, parse_review

PASS = json.dumps({"verdict": "pass", "summary": "ok", "findings": [
    {"severity": "suggestion", "file": "src/a.py", "line": 3, "issue": "Name could be clearer."}]})
BLOCK = json.dumps({"verdict": "blocking", "summary": "race", "findings": [
    {"severity": "blocking", "file": "src/a.py", "line": 7, "issue": "Two 401s refresh the token twice."}]})


def test_parse():
    report = parse_review(f"Here you go:\n```json\n{BLOCK}\n```")
    assert report.verdict == "blocking"
    assert report.blocking[0].where() == "src/a.py:7"
    assert parse_review(PASS).verdict == "pass" and len(parse_review(PASS).suggestions) == 1


def test_the_verdict_follows_the_findings():
    says_blocking = json.dumps({"verdict": "blocking", "findings": [{"severity": "suggestion", "issue": "x"}]})
    says_pass = json.dumps({"verdict": "pass", "findings": [{"severity": "blocking", "issue": "y"}]})
    assert parse_review(says_blocking).verdict == "pass"
    assert parse_review(says_pass).verdict == "blocking"


def test_an_unusable_answer_is_an_error_that_never_blocks():
    for answer in ("", "looks fine to me", "[1, 2]"):
        report = parse_review(answer)
        assert report.verdict == "error" and not report.blocking
        assert report.lines()[0].startswith("⚠ did not complete")


def test_the_prompt_carries_only_what_an_outside_reviewer_would_see():
    prompt = build_review_prompt(goal="fix refresh", diff="+x\n" * 10, contract="CHANGE CONTRACT\n…",
                                 checks=[{"command": "pytest -q", "passed": True}], max_diff_chars=12)
    assert "GOAL\nfix refresh" in prompt and "CONTRACT" in prompt and "PASS  pytest -q" in prompt
    assert "characters of diff omitted" in prompt


EDIT = {"success": True, "data": {"path": "src/a.py", "applied": True,
                                   "diff": "--- a/src/a.py\n+++ b/src/a.py\n@@\n-old\n+new\n"}}


def _run(reviewer_answers, edits_per_round=(1, 1, 0, 0, 0)):
    reviews = []
    prompts = []
    rounds = {"n": 0}

    async def reviewer(prompt):
        reviews.append(prompt)
        return reviewer_answers[min(len(reviews), len(reviewer_answers)) - 1]

    async def provider_fn(message, history, **kwargs):
        prompts.append(message)
        rounds["n"] += 1
        if rounds["n"] in (1, 3):
            return {"success": True, "response": "", "provider": "fake",
                    "tool_calls_pending": [{"tool": "edit_file", "params": {"path": "src/a.py"}}]}
        return {"success": True, "response": "done", "provider": "fake"}

    counter = {"n": 0}

    def edit(params):
        counter["n"] += 1
        data = dict(EDIT["data"], diff=EDIT["data"]["diff"] + f"+v{counter['n']}\n")
        return {"success": True, "data": data}

    gate = ReviewGate(reviewer=reviewer, max_attempts=1)

    async def collect():
        return [e async for e in run_agent("fix refresh", [], provider_fn=provider_fn,
                                           tool_executor=ToolExecutor({"edit_file": (edit, "edit")}),
                                           options=AgentOptions(review=gate))]

    return asyncio.run(collect()), reviews, prompts


def test_a_clean_review_ends_the_turn_and_fills_the_report():
    events, reviews, _ = _run([PASS])
    assert len(reviews) == 1 and "fix refresh" in reviews[0] and "+new" in reviews[0]
    assert any(isinstance(e, AgentEventStatus) and e.state == "review_passed" for e in events)
    delivery = events[-1].result.delivery
    assert delivery["status"] == "done"
    assert delivery["review_lines"][0] == "✓ no blocking findings · 1 suggestion"


def test_blocking_findings_go_back_to_the_builder_once():
    events, reviews, prompts = _run([BLOCK, PASS])
    assert any(p.startswith("[Independent review]") and "Two 401s" in p for p in prompts)
    assert len(reviews) == 2, "the repaired change is reviewed again"
    assert events[-1].result.delivery["status"] == "done"


def test_findings_that_survive_the_repair_leave_the_turn_incomplete():
    events, reviews, _ = _run([BLOCK, BLOCK])
    assert len(reviews) == 2
    delivery = events[-1].result.delivery
    assert delivery["status"] == "incomplete"
    assert delivery["next"] == "Address the review's blocking findings"
    assert isinstance(events[-1], AgentEventComplete) and events[-1].result.success, \
        "the report says incomplete; the turn itself did not fail"


def test_no_change_no_review():
    calls = []

    async def reviewer(prompt):
        calls.append(prompt)
        return PASS

    async def provider_fn(message, history, **kwargs):
        return {"success": True, "response": "It does X.", "provider": "fake"}

    async def collect():
        return [e async for e in run_agent("explain", [], provider_fn=provider_fn, tool_executor=ToolExecutor({}),
                                           options=AgentOptions(review=ReviewGate(reviewer=reviewer)))]

    asyncio.run(collect())
    assert calls == []


def test_bridge_builds_a_gate_only_when_asked():
    from aria_code.apps.cli.providers.runtime_bridge import build_review_gate

    kwargs = dict(model="m", api_url=None, ollama_url="http://localhost:11434")
    assert build_review_gate({}, **kwargs) is None
    assert build_review_gate({"review_gate": True, "permission_mode": "read-only"}, **kwargs) is None
    assert isinstance(build_review_gate({"review_gate": True}, **kwargs), ReviewGate)
