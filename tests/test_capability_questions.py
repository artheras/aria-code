import asyncio
import pytest

from aria_code.apps.cli.conversation_policy import is_capability_question, is_conversation_only_message
from aria_code.apps.cli.intent_signals import classify, INTENT_GENERAL
from aria_code.apps.cli.intent_router import detect_intents
from aria_code.apps.cli.prompts.select import build_turn_system_prompt
from aria_code.apps.cli.tool_scope import select_tool_schemas
from aria_code.packages.aria_services.research_protocol import requires_financial_evidence


QUESTIONS = [
    '你除了分析股票外还能帮我做什么',
    '你除了分析股票外还可以做什么？',
    '除了分析股票，你还能帮我做什么？',
    '你除了分析股票还会做什么',
    '你能帮我做什么？', '你还有哪些能力', '你有什么功能？',
    '介绍一下你的能力', '你除了读取 README.md 外还能帮我做什么？',
    'What else can you do besides analyzing stocks?',
    'Besides analyzing stocks, what else can you do?',
    'What can you help me with?', 'What are your capabilities?',
]


@pytest.mark.parametrize('question', QUESTIONS)
def test_capability_questions_are_conversation_not_financial_tasks(question):
    assert is_capability_question(question)
    assert is_conversation_only_message(question)
    assert classify(question) == INTENT_GENERAL
    assert not requires_financial_evidence(question)
    assert detect_intents(question) == ()
    assert select_tool_schemas([{'name':'list_files'},{'name':'get_market_data'}], question) == []
    prompt=build_turn_system_prompt(question, project_context='PRIVATE_PROJECT_CONTEXT')
    assert 'PRIVATE_PROJECT_CONTEXT' not in prompt
    assert 'FINANCIAL' not in prompt


@pytest.mark.parametrize('question', [
    '分析苹果股票走势和成交量', '你能分析 AAPL 今天的走势吗？',
    '你能做什么？然后分析 AAPL 今天的走势',
    'What can you do; analyze AAPL today',
    'What are your capabilities? Analyze NVDA stock today.',
    '除了分析股票，帮我读取 README.md',
    '你能帮我修复代码吗？', 'What can this function do?',
])
def test_real_or_mixed_tasks_keep_normal_routing(question):
    assert not is_capability_question(question)
    assert select_tool_schemas([{'name':'read_file'}], question)


@pytest.mark.parametrize('question', [
    '分析苹果股票走势和成交量', '你能分析 AAPL 今天的走势吗？',
    '你能做什么？然后分析 AAPL 今天的走势',
    'What can you do; analyze AAPL today',
])
def test_actual_financial_conclusions_still_require_evidence(question):
    assert requires_financial_evidence(question)


def test_capability_question_never_runs_deterministic_finance(monkeypatch):
    from aria_code.apps.cli import deterministic
    from aria_code.apps.cli.commands.market import route_top_level_text
    monkeypatch.setattr(deterministic, '_handle_broker_query', lambda *a: pytest.fail('broker queried'))
    question=QUESTIONS[0]
    assert deterministic.run_deterministic_chain(question, model_has_tools=False) == {'success':False}
    assert route_top_level_text(question, {'/analyze','/route'}) is None


def test_capability_turn_blocks_file_scans_and_stale_financial_gate(monkeypatch, tmp_path):
    from aria_code.apps.cli.providers import runtime_bridge as rb
    scanned, settings=[],[]
    async def provider(prompt, history, **kwargs):
        if not history:
            return {'success':True,'response':'','tool_calls_pending':[{'tool':'list_files','params':{'path':str(tmp_path)}}]}
        return {'success':True,'response':'我可以帮助你编写和调试代码、整理文档及设计工作流程。'}
    def factory(**kwargs):
        settings.append(kwargs)
        return provider
    monkeypatch.setattr(rb, 'make_provider_fn', factory)
    config={'acceptance_gate':True,'_session_workspace_root':str(tmp_path)}
    result=asyncio.run(rb.run_chat_via_runtime(
        prompt=QUESTIONS[0], history=[],
        local_tools={'list_files':(lambda p: scanned.append(p) or {'success':True},'List')},
        tool_schemas=[{'name':'list_files'}],model='fixture',config=config,
        api_url=None,ollama_url='',requires_evidence=True,
        project_context='PRIVATE_PROJECT_CONTEXT',max_rounds=3,return_result=True,
    ))
    assert result.ok
    assert '代码' in result.text
    assert not scanned
    assert settings[0]['tool_schemas'] == []
    assert settings[0]['project_context'] is None
    assert '_conversation_only' not in config
