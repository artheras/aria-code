import asyncio
import math
from unittest.mock import patch

import pandas as pd
import pytest

from aria_code.tools import enterprise_finance_tools as finance
from aria_code.runtime.agent_loop import LoopGuard, run_parallel_tools
from aria_code.runtime.tool_executor import ToolExecutor


def test_missing_inputs_and_zero_denominators_are_not_invented():
    metrics = finance.compute_financial_metrics({'revenue': 0, 'net_income': 10}, {}, {})
    assert metrics['net_margin_pct'] is None
    assert metrics['gross_margin_pct'] is None
    assert metrics['dupont']['roe'] is None
    assert metrics['working_capital']['quick_ratio'] is None
    assert metrics['altman_z_score'] is None
    assert metrics['solvency_risk'] == 'UNKNOWN'


@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), 'not-a-number'])
def test_invalid_financial_values_fail(bad):
    result = finance.tool_analyze_financial_statements({'financials': {'income_statement': {'revenue': bad}}})
    assert result['success'] is False
    assert result['error_code'] == 'invalid_financials'


@pytest.mark.parametrize('error,code', [(ImportError(), 'dependency_missing'), (TimeoutError(), 'source_timeout'), (ValueError('empty_statements'), 'empty_statements'), (RuntimeError(), 'source_unavailable')])
def test_recoverable_source_errors_are_diagnosed(monkeypatch, error, code):
    def fail(_): raise error
    monkeypatch.setattr(finance, '_fetch_yahoo_statements', fail)
    result = finance.tool_analyze_financial_statements({'company_name': 'NVDA'})
    assert not result['success']
    assert result['error_code'] == code
    assert result['next_actions']
    assert 'does not estimate' in result['error']


def test_network_disabled_never_fetches(monkeypatch):
    monkeypatch.setattr(finance, '_fetch_yahoo_statements', lambda _: pytest.fail('network called'))
    assert finance.tool_analyze_financial_statements({'company_name':'AAPL','network_enabled':False})['error_code'] == 'network_disabled'


def test_financial_tool_uses_host_network_setting(monkeypatch):
    monkeypatch.setattr(finance, '_fetch_yahoo_statements', lambda _: pytest.fail('network called'))
    executor = ToolExecutor({'analyze_financial_statements': (finance.tool_analyze_financial_statements, '')}, config={'network_enabled': False})
    result = executor.execute_local('analyze_financial_statements', {'company_name': 'AAPL', 'network_enabled': True})
    assert result['error_code'] == 'network_disabled'


def test_yahoo_aligns_periods_and_keeps_missing_cells_unknown(monkeypatch):
    import yfinance as yf
    latest, older = pd.Timestamp('2025-12-31'), pd.Timestamp('2024-12-31')
    class Ticker:
        def __init__(self, symbol, session): pass
        income_stmt = pd.DataFrame({latest: [999, 50], older: [100, 10]}, index=['Total Revenue','Net Income'])
        balance_sheet = pd.DataFrame({older: [200, float('nan')]}, index=['Total Assets','Stockholders Equity'])
    monkeypatch.setattr(yf, 'Ticker', Ticker)
    income, balance, period = finance._fetch_yahoo_statements('AAPL')
    assert period == '2024-12-31'
    assert income['revenue'] == 100
    assert 'total_equity' not in balance


def test_yahoo_request_timeout_is_clamped_and_total_deadline_enforced(monkeypatch):
    import yfinance as yf
    from curl_cffi.requests import Session
    calls=[]
    now=[0]
    monkeypatch.setattr(finance.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(Session, 'request', lambda self, method, url, **kw: calls.append(kw['timeout']))
    class Ticker:
        def __init__(self, symbol, session):
            session.get('https://example.invalid', timeout=30)
            now[0]=21
            with pytest.raises(TimeoutError): session.get('https://example.invalid', timeout=30)
        income_stmt=pd.DataFrame()
        balance_sheet=pd.DataFrame()
    monkeypatch.setattr(yf,'Ticker',Ticker)
    with pytest.raises(TimeoutError): finance._fetch_yahoo_statements('NVDA')
    assert calls == [8]


def test_financial_source_failure_does_not_cancel_other_analysis():
    executor=ToolExecutor({
        'analyze_financial_statements':(lambda _: {'success':False,'error':'source_timeout'},''),
        'get_market_data':(lambda _: {'success':True,'data':{'symbol':'AAPL'}},''),
        'get_risk_metrics':(lambda _: {'success':True,'data':{'observations':100}},''),
    })
    results=asyncio.run(run_parallel_tools([{'tool':name,'params':{}} for name in executor.local_tools],executor))
    assert results[0]['success'] is False
    assert results[1]['success'] is True
    assert results[2]['success'] is True


def test_repeated_successful_report_writes_are_bounded():
    guard=LoopGuard()
    params={'path':'REPORT.md','content':'Done'}
    assert guard.record('write_file',params,{'success':True}) is None
    assert 'already succeeded' in guard.record('write_file',params,{'success':True})
    guard.record('write_file',params,{'success':True})
    guard.record('write_file',params,{'success':True})
    assert guard.should_break


def test_distinct_edits_are_not_treated_as_a_loop():
    guard=LoopGuard()
    for i in range(8): guard.record('write_file',{'path':'REPORT.md','content':str(i)},{'success':True})
    assert not guard.should_break


def test_task_prompt_and_tool_scope_stay_stable_across_tool_results(monkeypatch):
    from aria_code.apps.cli.providers import runtime_bridge, base
    from aria_code.packages.aria_sdk import streaming
    from aria_code.apps.cli.prompts import select
    from aria_code.apps.cli import tool_scope
    seen, scoped=[],[]
    monkeypatch.setattr(select,'build_turn_system_prompt',lambda prompt,**kw: seen.append(prompt) or 'system')
    monkeypatch.setattr(tool_scope,'select_tool_schemas',lambda schemas,prompt,**kw: scoped.append(prompt) or [])
    class Provider:
        def __init__(self,*args,**kwargs): pass
    monkeypatch.setattr(base,'ConfiguredProvider',Provider)
    async def stream(*args,**kwargs): return {'success':True,'response':'verified'}
    monkeypatch.setattr(streaming,'stream_provider_result',stream)
    fn=runtime_bridge.make_provider_fn(model='nvidia/nemotron-3-super-120b-a12b',config={'local_provider':'custom'},api_url=None,ollama_url='',tool_schemas=[])
    async def run():
        await fn('Implement inventory.py',[])
        await fn('## Tool Results: write_file succeeded',[])
    asyncio.run(run())
    assert seen == ['Implement inventory.py']*2
    assert scoped == ['Implement inventory.py']*2
