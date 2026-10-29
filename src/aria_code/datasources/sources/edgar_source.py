"""
datasources/sources/edgar_source.py — SEC EDGAR 美国上市公司财务数据
====================================================================
完全免费，无需 API key。数据来源：https://data.sec.gov (SEC EDGAR API)
覆盖：10-K / 10-Q 财报、公司基本信息、财务报表、内幕交易披露。

用法:
    src = EDGARSource()
    facts = src.get_company_facts("AAPL")   # → 财务指标历史
    filings = src.get_recent_filings("MSFT") # → 最近10-K/10-Q
"""

from __future__ import annotations

import logging
import time
import urllib.request
from typing import Any, Dict, List, Optional

from ..base import BaseDataSource, FundamentalsResult, QuoteResult

import os as _os

logger = logging.getLogger(__name__)

_EDGAR_API    = "https://data.sec.gov"
_EDGAR_WWW    = "https://www.sec.gov"
_HEADERS      = {
    "User-Agent": _os.environ.get("EDGAR_USER_AGENT", "aria-code contact@example.com"),  # SEC requires self-identification
    "Accept-Encoding": "gzip, deflate",
}

# 主要美股 ticker → CIK 缓存（常用的直接查，避免每次API调用）
_TICKER_CIK_CACHE: Dict[str, str] = {}


def _fetch_json(url: str, timeout: int = 15) -> Optional[Dict]:
    try:
        import gzip
        import json
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except Exception as e:
        logger.debug(f"[edgar] fetch {url} 失败: {e}")
        return None


class EDGARSource(BaseDataSource):
    """
    SEC EDGAR 数据源 — 美国上市公司财报和披露文件。
    """

    name         = "edgar"
    markets      = ["us"]
    requires_key = False

    def __init__(self, config=None):
        super().__init__(config)
        self._tickers: Optional[Dict] = None
        self._cik_map: Dict[str, str] = {}

    def is_configured(self) -> bool:
        return True

    def _load_ticker_map(self) -> None:
        """加载 SEC 全量 ticker→CIK 映射（首次调用时下载一次）"""
        if self._tickers is not None:
            return
        data = _fetch_json(f"{_EDGAR_WWW}/files/company_tickers.json")
        if data:
            for v in data.values():
                ticker = v.get("ticker", "").upper()
                cik    = str(v.get("cik_str", "")).zfill(10)
                if ticker:
                    self._cik_map[ticker] = cik
        self._tickers = self._cik_map

    def ticker_to_cik(self, symbol: str) -> Optional[str]:
        self._load_ticker_map()
        return self._cik_map.get(symbol.upper())

    def get_company_facts(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取公司全部财务事实（XBRL 格式）。
        返回包含 EPS/Revenue/NetIncome/Assets 等历史序列的字典。
        """
        cik = self.ticker_to_cik(symbol)
        if not cik:
            return {"error": f"未找到 {symbol} 的 CIK"}
        data = _fetch_json(f"{_EDGAR_API}/api/xbrl/companyfacts/CIK{cik}.json")
        if not data:
            return None

        us_gaap = data.get("facts", {}).get("us-gaap", {})
        result  = {"symbol": symbol, "cik": cik, "metrics": {}}
        wanted  = {
            "Revenues": "revenue",
            "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
            "NetIncomeLoss": "net_income",
            "EarningsPerShareBasic": "eps_basic",
            "EarningsPerShareDiluted": "eps_diluted",
            "Assets": "total_assets",
            "StockholdersEquity": "total_equity",
            "OperatingIncomeLoss": "operating_income",
            "CommonStockSharesOutstanding": "shares_outstanding",
        }
        for gaap_key, alias in wanted.items():
            if gaap_key in us_gaap:
                units = us_gaap[gaap_key].get("units", {})
                unit_key = "USD" if "USD" in units else ("shares" if "shares" in units else next(iter(units), None))
                if unit_key and unit_key in units:
                    entries = [
                        {"end": e["end"], "val": e["val"], "form": e.get("form", "")}
                        for e in units[unit_key]
                        if e.get("form") in ("10-K", "10-Q", "20-F")
                    ]
                    if entries:
                        entries.sort(key=lambda x: x["end"], reverse=True)
                        result["metrics"][alias] = entries[:20]
        return result

    def get_recent_filings(self, symbol: str, form_types: List[str] = None) -> List[Dict]:
        """获取最近提交的财务报告（10-K、10-Q、8-K 等）。"""
        cik = self.ticker_to_cik(symbol)
        if not cik:
            return [{"error": f"未找到 {symbol} 的 CIK"}]
        form_types = form_types or ["10-K", "10-Q", "8-K"]
        data = _fetch_json(f"{_EDGAR_API}/submissions/CIK{cik}.json")
        if not data:
            return []

        recent   = data.get("filings", {}).get("recent", {})
        forms    = recent.get("form", [])
        dates    = recent.get("filingDate", [])
        accnos   = recent.get("accessionNumber", [])
        docs     = recent.get("primaryDocument", [])

        results = []
        for form, filing_date, accno, doc in zip(forms, dates, accnos, docs):
            if form in form_types:
                accno_clean = accno.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accno_clean}/{doc}"
                results.append({
                    "form": form,
                    "date": filing_date,
                    "accession": accno,
                    "url": url,
                    "cik": cik,
                })
            if len(results) >= 20:
                break
        return results

    def get_insider_trades(self, symbol: str, days: int = 90) -> List[Dict]:
        """获取内幕交易披露（Form 4）。"""
        cik = self.ticker_to_cik(symbol)
        if not cik:
            return []
        data = _fetch_json(f"{_EDGAR_API}/submissions/CIK{cik}.json")
        if not data:
            return []

        recent = data.get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        dates  = recent.get("filingDate", [])
        results = []
        from datetime import date as _date, timedelta as _td
        cutoff = (_date.today() - _td(days=days)).isoformat()

        for form, filing_date in zip(forms, dates):
            if form == "4" and filing_date >= cutoff:
                results.append({"form": "4", "date": filing_date, "type": "insider"})
            if len(results) >= 30:
                break
        return results

    def quote(self, symbol: str) -> Optional[QuoteResult]:
        facts = self.get_company_facts(symbol)
        if not facts or "metrics" in facts and not facts["metrics"]:
            return None
        m = facts.get("metrics", {})
        rev = m.get("revenue", [{}])[0].get("val", 0) if m.get("revenue") else 0
        return QuoteResult(
            symbol  = symbol,
            name    = f"EDGAR:{symbol}",
            price   = 0.0,
            market  = "us",
            source  = self.name,
            extra   = {"annual_revenue": rev},
        )

    def fundamentals(self, symbol: str) -> Optional[FundamentalsResult]:
        facts = self.get_company_facts(symbol)
        if not facts:
            return None
        m = facts.get("metrics", {})

        def _latest(key: str) -> float:
            entries = m.get(key, [])
            return float(entries[0]["val"]) if entries else 0.0

        net_income = _latest("net_income")
        revenue    = _latest("revenue")
        rev_yoy    = 0.0
        if m.get("revenue") and len(m["revenue"]) >= 2:
            cur  = float(m["revenue"][0]["val"])
            prev = float(m["revenue"][1]["val"])
            if prev:
                rev_yoy = (cur - prev) / abs(prev) * 100

        return FundamentalsResult(
            symbol           = symbol,
            revenue_growth   = rev_yoy,
            source           = self.name,
        )
