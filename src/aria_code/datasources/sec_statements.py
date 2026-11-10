"""Period-aligned annual SEC facts; no estimates or cross-filing joins."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import date
import json
import math
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request


class SECError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


INCOME_TAGS = {
    'revenue': ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'),
    'net_income': ('NetIncomeLoss',),
    'gross_profit': ('GrossProfit',),
    'operating_income': ('OperatingIncomeLoss',),
    'cost_of_goods_sold': ('CostOfRevenue', 'CostOfGoodsAndServicesSold'),
}
BALANCE_TAGS = {
    'total_assets': ('Assets',),
    'total_equity': ('StockholdersEquity',),
    'current_assets': ('AssetsCurrent',),
    'current_liabilities': ('LiabilitiesCurrent',),
    'inventory': ('InventoryNet',),
    'retained_earnings': ('RetainedEarningsAccumulatedDeficit',),
}
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST = 0.0
_CACHE_LOCK = threading.Lock()
_CACHE = OrderedDict()
_MAX_BYTES = 20 * 1024 * 1024


def _value(entry):
    value = entry.get('val')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def _rows(facts, tags):
    for tag in tags:
        concept = facts.get(tag, {})
        units = concept.get('units', {}) if isinstance(concept, dict) else {}
        rows = units.get('USD', []) if isinstance(units, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and _value(row) is not None:
                yield tag, row


def parse_annual_statements(payload: dict, *, today: date | None = None) -> dict:
    """Require core income and balance values from one annual USD filing.

    A 10-K also repeats older years and sometimes contains quarterly facts.
    Match accession/start/end explicitly; never choose latest per metric.
    """
    today = today or date.today()
    if not isinstance(payload, dict) or not isinstance(payload.get('facts'), dict):
        raise SECError('sec_invalid_response')
    facts = payload['facts'].get('us-gaap', {})
    if not isinstance(facts, dict):
        raise SECError('sec_invalid_response')
    anchors = []
    for tag, row in _rows(facts, INCOME_TAGS['revenue']):
        try:
            start, end, filed = (date.fromisoformat(row[k]) for k in ('start', 'end', 'filed'))
        except (KeyError, TypeError, ValueError):
            continue
        if (row.get('form') in {'10-K', '10-K/A'} and row.get('accn')
                and 335 <= (end - start).days <= 395 and start <= end <= filed <= today):
            anchors.append((tag, row))
    anchors.sort(key=lambda item: (item[1]['end'], item[1]['filed']), reverse=True)
    for revenue_tag, anchor in anchors:
        accession = anchor['accn']
        def extract(mapping, duration):
            values, provenance = {}, {}
            for field, tags in mapping.items():
                for tag in tags:
                    rows = [row for _, row in _rows(facts, (tag,))
                            if row.get('accn') == accession and row.get('end') == anchor['end']
                            and row.get('form') in {'10-K', '10-K/A'} and row.get('filed') == anchor['filed']
                            and (row.get('start') == anchor['start'] if duration else not row.get('start'))]
                    if not rows:
                        continue
                    if len({_value(row) for row in rows}) != 1:
                        raise SECError('sec_conflicting_facts')
                    row = rows[0]
                    values[field] = _value(row)
                    provenance[field] = {'tag': tag, 'unit': 'USD', 'accession': accession,
                                         'start': row.get('start'), 'end': row['end'], 'filed': row['filed']}
                    break
            return values, provenance
        income, income_provenance = extract(INCOME_TAGS, True)
        balance, balance_provenance = extract(BALANCE_TAGS, False)
        if not {'revenue', 'net_income'} <= income.keys() or not {'total_assets', 'total_equity'} <= balance.keys():
            continue
        return {
            'income_statement': income, 'balance_sheet': balance, 'cashflow': {},
            'period': anchor['end'], 'period_start': anchor['start'], 'period_type': 'annual',
            'currency': 'USD', 'accession': accession, 'filed': anchor['filed'],
            'age_days': (today - date.fromisoformat(anchor['end'])).days,
            'stale': (today - date.fromisoformat(anchor['end'])).days > 550,
            'entity_name': payload.get('entityName'), 'cik': str(payload.get('cik', '')).zfill(10),
            'provenance': {**income_provenance, **balance_provenance},
        }
    raise SECError('sec_no_aligned_annual_statements')


def _get_json(url: str, deadline: float):
    agent = os.environ.get('EDGAR_USER_AGENT', '').strip()
    if not re.fullmatch(r'[^\r\n]{3,240}', agent) or not re.search(r'[^\s@]+@[^\s@]+\.[^\s@]+', agent) or 'example.com' in agent:
        raise SECError('sec_identity_required')
    global _LAST_REQUEST
    with _REQUEST_LOCK:
        delay = max(0.0, 0.5 - (time.monotonic() - _LAST_REQUEST))
        if time.monotonic() + delay >= deadline:
            raise SECError('sec_timeout')
        if delay:
            time.sleep(delay)
        _LAST_REQUEST = time.monotonic()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SECError('sec_timeout')
    request = urllib.request.Request(url, headers={'User-Agent': agent, 'Accept': 'application/json', 'Accept-Encoding': 'identity'})
    try:
        context = ssl.create_default_context()
        try:
            import certifi
            context.load_verify_locations(certifi.where())
        except ImportError:
            pass
        with urllib.request.urlopen(request, timeout=min(remaining, 6), context=context) as response:
            chunks, size = [], 0
            while size <= _MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise SECError('sec_timeout')
                chunk = response.read1(min(65536, _MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b''.join(chunks)
        if time.monotonic() >= deadline:
            raise SECError('sec_timeout')
        if len(raw) > _MAX_BYTES:
            raise SECError('sec_response_too_large')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise SECError('sec_invalid_response')
        return result
    except urllib.error.HTTPError as exc:
        raise SECError({403: 'sec_access_denied', 429: 'sec_rate_limited'}.get(exc.code, 'sec_http_error')) from None
    except TimeoutError:
        raise SECError('sec_timeout') from None
    except urllib.error.URLError as exc:
        raise SECError('sec_timeout' if isinstance(exc.reason, TimeoutError) else 'sec_source_unavailable') from None
    except OSError:
        raise SECError('sec_source_unavailable') from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SECError('sec_invalid_response') from None


def _cached_json(url, deadline, ttl):
    with _CACHE_LOCK:
        item = _CACHE.get(url)
        if item and time.monotonic() - item[0] < ttl:
            return deepcopy(item[1]), item[2], True
    payload = _get_json(url, deadline)
    retrieved = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with _CACHE_LOCK:
        _CACHE[url] = (time.monotonic(), deepcopy(payload), retrieved)
        _CACHE.move_to_end(url)
        while len(_CACHE) > 32:
            _CACHE.popitem(last=False)
    return payload, retrieved, False


def fetch_annual_statements(symbol: str, *, timeout: float = 10) -> dict:
    if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}', symbol):
        raise SECError('sec_invalid_ticker')
    deadline = time.monotonic() + timeout
    mapping, _, _ = _cached_json('https://www.sec.gov/files/company_tickers.json', deadline, 86400)
    matches = {str(item.get('cik_str')).zfill(10) for item in mapping.values()
               if isinstance(item, dict) and item.get('ticker') == symbol}
    if len(matches) != 1:
        raise SECError('sec_ticker_not_found')
    cik = matches.pop()
    if not re.fullmatch(r'\d{10}', cik):
        raise SECError('sec_invalid_response')
    url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json'
    payload, retrieved, cached = _cached_json(url, deadline, 300)
    try:
        returned_cik = str(int(payload.get('cik'))).zfill(10)
    except (ValueError, TypeError):
        raise SECError('sec_invalid_response') from None
    if returned_cik != cik:
        raise SECError('sec_company_mismatch')
    result = parse_annual_statements(payload)
    result.update(source_url=url, retrieved_at=retrieved, cache_hit=cached, symbol=symbol)
    return result
