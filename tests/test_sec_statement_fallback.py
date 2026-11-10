"""Synthetic facts exercise period selection; never live investment data."""
from datetime import date
import io
import json
import time
import urllib.error

import pytest
from aria_code.datasources import sec_statements as sec
from aria_code.tools import enterprise_finance_tools as finance


def facts():
    base={'accn':'0000000001-25-000001','form':'10-K','filed':'2025-02-01','end':'2024-12-31'}
    def concept(value,duration):
        row=dict(base,val=value)
        if duration: row['start']='2024-01-01'
        return {'units':{'USD':[row]}}
    return {'cik':1,'entityName':'Synthetic Test Entity','facts':{'us-gaap':{
        'Revenues':concept(1000,True),'NetIncomeLoss':concept(100,True),
        'Assets':concept(2000,False),'StockholdersEquity':concept(800,False),
        'LiabilitiesAndStockholdersEquity':concept(2000,False),
    }}}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    sec._CACHE.clear()
    monkeypatch.delenv('EDGAR_USER_AGENT',raising=False)
    monkeypatch.setattr(sec,'_LAST_REQUEST',0)
    yield
    sec._CACHE.clear()


def test_core_values_and_provenance_share_filing_and_period():
    result=sec.parse_annual_statements(facts(),today=date(2025,3,1))
    assert result['balance_sheet']['total_equity']==800
    assert result['period']=='2024-12-31'
    assert result['currency']=='USD'
    assert result['cashflow']=={}
    assert len({r['accession'] for r in result['provenance'].values()})==1
    assert len({r['end'] for r in result['provenance'].values()})==1
    assert 'gross_profit' not in result['income_statement']


@pytest.mark.parametrize('change', ['quarter','different_accession','different_start','different_end','different_currency','future_filing','liabilities_as_equity','nonfinite'])
def test_incompatible_facts_are_never_joined(change):
    payload=facts(); gaap=payload['facts']['us-gaap'];net=gaap['NetIncomeLoss']['units']['USD'][0]
    if change=='quarter': net['start']='2024-10-01'
    if change=='different_accession': net['accn']='another-filing'
    if change=='different_start': net['start']='2024-01-02'
    if change=='different_end': net['end']='2023-12-31'
    if change=='different_currency': gaap['NetIncomeLoss']['units']={'CAD':[net]}
    if change=='future_filing': net['filed']='2035-01-01'
    if change=='liabilities_as_equity': del gaap['StockholdersEquity']
    if change=='nonfinite': net['val']=float('nan')
    with pytest.raises(sec.SECError,match='sec_no_aligned_annual_statements'):
        sec.parse_annual_statements(payload,today=date(2025,3,1))


def test_quarterly_revenue_in_a_10k_is_not_selected_as_annual():
    payload=facts();rows=payload['facts']['us-gaap']['Revenues']['units']['USD'];q=dict(rows[0],start='2024-10-01',val=200);rows.insert(0,q)
    assert sec.parse_annual_statements(payload)['income_statement']['revenue']==1000


def test_conflicting_same_filing_values_are_rejected():
    payload=facts();rows=payload['facts']['us-gaap']['NetIncomeLoss']['units']['USD'];rows.append(dict(rows[0],val=300))
    with pytest.raises(sec.SECError,match='sec_conflicting_facts'): sec.parse_annual_statements(payload)


def test_cache_provenance_and_company_match(monkeypatch):
    calls=[]
    def fetch(url,deadline):
        calls.append(url)
        return {'0':{'ticker':'TEST','cik_str':1}} if 'company_tickers' in url else facts()
    monkeypatch.setattr(sec,'_get_json',fetch)
    first=sec.fetch_annual_statements('TEST');second=sec.fetch_annual_statements('TEST')
    assert len(calls)==2
    assert not first['cache_hit'] and second['cache_hit']
    assert first['retrieved_at']==second['retrieved_at']
    assert first['source_url'].endswith('CIK0000000001.json')
    first['income_statement']['revenue']=99
    assert second['income_statement']['revenue']==1000


def test_wrong_company_response_is_rejected(monkeypatch):
    def fetch(url,deadline):
        return {'0':{'ticker':'TEST','cik_str':2}} if 'company_tickers' in url else facts()
    monkeypatch.setattr(sec,'_get_json',fetch)
    with pytest.raises(sec.SECError,match='sec_company_mismatch'):sec.fetch_annual_statements('TEST')


def test_identity_required_without_network(monkeypatch):
    monkeypatch.setattr(sec.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('network called'))
    with pytest.raises(sec.SECError,match='sec_identity_required'):sec._get_json('https://data.sec.gov/test',time.monotonic()+1)


@pytest.mark.parametrize('status,code',[(403,'sec_access_denied'),(429,'sec_rate_limited')])
def test_access_errors_do_not_retry_or_leak_identity(monkeypatch,status,code):
    monkeypatch.setenv('EDGAR_USER_AGENT','Aria Test ops@unit.test')
    calls=[]
    def fail(req,**kw):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url,status,'error',{},None)
    monkeypatch.setattr(sec.urllib.request,'urlopen',fail)
    with pytest.raises(sec.SECError,match=code) as error:sec._get_json('https://data.sec.gov/test',time.monotonic()+1)
    assert len(calls)==1
    assert 'ops@unit.test' not in str(error.value)


def test_expired_cache_is_not_silently_used_on_failure(monkeypatch):
    sec._CACHE['url']=(time.monotonic()-100,{'old':1},'old')
    def fail(*a):raise sec.SECError('sec_access_denied')
    monkeypatch.setattr(sec,'_get_json',fail)
    with pytest.raises(sec.SECError):sec._cached_json('url',time.monotonic()+1,1)


def test_yahoo_failure_uses_sec_and_retains_trace(monkeypatch):
    def yahoo(_):raise TimeoutError()
    monkeypatch.setattr(finance,'_fetch_yahoo_statements',yahoo)
    monkeypatch.setattr(sec,'fetch_annual_statements',lambda _:sec.parse_annual_statements(facts()))
    result=finance.tool_analyze_financial_statements({'company_name':'TEST'})
    assert result['success']
    assert result['data']['data_source']=='sec-edgar:TEST'
    assert result['data']['net_margin_pct']==10
    assert result['data']['dupont']['roe']==12.5
    assert result['data']['gross_margin_pct'] is None
    assert result['source_attempts'][0]['error_code']=='source_timeout'
    assert result['source_attempts'][1]['success']


def test_both_sources_fail_without_fabricated_metrics(monkeypatch):
    def fail(*a):raise TimeoutError()
    def denied(*a):raise sec.SECError('sec_access_denied')
    monkeypatch.setattr(finance,'_fetch_yahoo_statements',fail)
    monkeypatch.setattr(sec,'fetch_annual_statements',denied)
    result=finance.tool_analyze_financial_statements({'company_name':'TEST'})
    assert not result['success'] and 'data' not in result
    assert result['source_attempts'][1]['error_code']=='sec_access_denied'


def test_host_network_disabled_blocks_both_sources(monkeypatch):
    monkeypatch.setattr(sec,'fetch_annual_statements',lambda _:pytest.fail('SEC called'))
    monkeypatch.setattr(finance,'_fetch_yahoo_statements',lambda _:pytest.fail('Yahoo called'))
    assert finance.tool_analyze_financial_statements({'company_name':'TEST','network_enabled':False})['error_code']=='network_disabled'


def test_transport_reads_json_with_declared_identity_and_bounded_timeout(monkeypatch):
    monkeypatch.setenv('EDGAR_USER_AGENT','Arthera ops@unit.test')
    seen=[]
    def open_request(request,timeout,context):
        assert context.check_hostname
        assert context.verify_mode == sec.ssl.CERT_REQUIRED
        seen.append((request,timeout))
        return io.BytesIO(json.dumps({'ok':True}).encode())
    monkeypatch.setattr(sec.urllib.request,'urlopen',open_request)
    assert sec._get_json('https://data.sec.gov/test',time.monotonic()+3)=={'ok':True}
    assert 0 < seen[0][1] <= 3
    assert seen[0][0].get_header('User-agent')=='Arthera ops@unit.test'


def test_deadline_and_response_limit(monkeypatch):
    monkeypatch.setenv('EDGAR_USER_AGENT','Arthera ops@unit.test')
    monkeypatch.setattr(sec.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('network called'))
    with pytest.raises(sec.SECError,match='sec_timeout'):
        sec._get_json('https://data.sec.gov/test',time.monotonic()-1)
    monkeypatch.setattr(sec,'_MAX_BYTES',8)
    monkeypatch.setattr(sec.urllib.request,'urlopen',lambda *a,**kw:io.BytesIO(b'123456789'))
    with pytest.raises(sec.SECError,match='sec_response_too_large'):
        sec._get_json('https://data.sec.gov/test',time.monotonic()+2)


def test_old_annual_period_is_explicitly_stale():
    result=sec.parse_annual_statements(facts(),today=date(2026,10,29))
    assert result['stale']
    assert result['age_days'] > 550


def test_legacy_edgar_source_does_not_alias_total_liabilities_as_equity(monkeypatch):
    from aria_code.datasources.sources import edgar_source
    payload=facts()
    monkeypatch.setattr(edgar_source,'_fetch_json',lambda url: {'0':{'ticker':'TEST','cik_str':1}} if 'company_tickers' in url else payload)
    result=edgar_source.EDGARSource().get_company_facts('TEST')
    assert result['metrics']['total_equity'][0]['val']==800


@pytest.mark.parametrize('error',[TimeoutError(),urllib.error.URLError(TimeoutError())])
def test_transport_timeout_has_its_own_diagnosis(monkeypatch,error):
    monkeypatch.setenv('EDGAR_USER_AGENT','Arthera ops@unit.test')
    def fail(*a,**kw):raise error
    monkeypatch.setattr(sec.urllib.request,'urlopen',fail)
    with pytest.raises(sec.SECError,match='sec_timeout'):
        sec._get_json('https://data.sec.gov/test',time.monotonic()+1)


def test_oversized_numeric_facts_do_not_escape_validation():
    payload=facts();payload['facts']['us-gaap']['NetIncomeLoss']['units']['USD'][0]['val']=10**1000
    with pytest.raises(sec.SECError,match='sec_no_aligned_annual_statements'):
        sec.parse_annual_statements(payload)
    assert finance.tool_analyze_financial_statements({'financials':{'income_statement':{'revenue':10**1000}}})['error_code']=='invalid_financials'
