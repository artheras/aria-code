import asyncio

from aria_code.packages.aria_sdk import AriaSDKClient, AriaAgentOptions
from aria_code.runtime import AgentEventCancelled, AgentEventToolResult


def test_sdk_task_events_use_fresh_results_only(monkeypatch):
    import aria_code.packages.aria_sdk.client as module
    record = {'status': 'done', 'verified': True, 'verification': {'status': 'verified', 'verified': True, 'checks': [{}]}}
    async def fake(*args, **kwargs):
        yield AgentEventToolResult('task_status', record, 0.1)
        yield AgentEventToolResult('task_result', record, 0.1)
        yield AgentEventToolResult('read_file', {'success': True}, 0.1)
    monkeypatch.setattr(module, 'run_agent', fake)
    client = AriaSDKClient(AriaAgentOptions(deterministic=False), tool_executor=object())
    async def collect():
        return [x async for x in client._run_agent('test', history=[])]
    events = asyncio.run(collect())
    assert events[0].data['task_outcome']['verified'] is None
    assert events[1].data['task_outcome']['verified'] is True
    assert events[1].data['result'] == record
    assert 'task_outcome' not in events[2].data


def test_sdk_cancellation_is_not_success(monkeypatch):
    import aria_code.packages.aria_sdk.client as module
    async def fake(*args, **kwargs):
        yield AgentEventCancelled('partial output')
    monkeypatch.setattr(module, 'run_agent', fake)
    client = AriaSDKClient(AriaAgentOptions(deterministic=False), tool_executor=object())
    async def collect():
        return [x async for x in client._run_agent('test', history=[])]
    event = asyncio.run(collect())[0]
    assert event.data['success'] is False
    assert event.data['cancelled'] is True
    assert event.content == 'partial output'
