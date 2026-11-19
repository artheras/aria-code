"""General-purpose web search tool (Brave → Tavily → Google → DuckDuckGo fallback)."""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List

logger = logging.getLogger(__name__)


def _resolve_search_key(env_var: str, provider: str) -> str:
    """Resolve a search-API key: env var first, then ~/.arthera/providers.json."""
    val = os.getenv(env_var, "")
    if val:
        return val
    try:
        import pathlib as _pl

        for _loc in ("~/.arthera/providers.json", "~/.aria/providers.json"):
            p = _pl.Path(_loc).expanduser()
            if not p.exists():
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
            for section in ("data", "llm"):
                entry = raw.get(section, {}).get(provider.lower(), {})
                if isinstance(entry, dict) and entry.get("api_key"):
                    return entry["api_key"]
                if isinstance(entry, str) and entry:
                    return entry
    except Exception:
        pass
    return ""


def _web_search(params: dict) -> dict:
    """Web search: Brave → Tavily → Google Programmable Search → DuckDuckGo."""
    if not isinstance(params, dict):
        params = {"query": str(params)}
    query = (
        params.get("query")
        or params.get("q")
        or params.get("search")
        or params.get("keyword")
        or params.get("keywords")
        or params.get("prompt")
        or params.get("text")
        or params.get("symbol")
        or params.get("ticker")
        or ""
    )
    if isinstance(query, (list, tuple)):
        query = " ".join(str(x) for x in query if x)
    elif isinstance(query, dict):
        query = " ".join(str(v) for v in query.values() if v)
    query = str(query).strip().strip("'\"`")
    if not query:
        return {"success": False, "query": "", "results": [], "error": "Missing search query"}

    num = min(int(params.get("num_results", params.get("max_results", 5)) or 5), 10)

    # 1. Brave Search API
    brave_key = _resolve_search_key("BRAVE_SEARCH_API_KEY", "brave")
    if brave_key:
        try:
            import gzip as _gzip
            import urllib.parse as _parse
            import urllib.request as _req

            _q_params = {"q": query, "count": num, "safesearch": "moderate"}
            if any("\u4e00" <= _c <= "\u9fff" for _c in query):
                _q_params["search_lang"] = "zh-hans"
            url = "https://api.search.brave.com/res/v1/web/search?" + _parse.urlencode(_q_params)
            req = _req.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": brave_key,
                },
            )
            with _req.urlopen(req, timeout=10) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = _gzip.decompress(raw)
                data = json.loads(raw)
            results = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("description", ""),
                }
                for item in data.get("web", {}).get("results", [])[:num]
            ]
            if results:
                return {"success": True, "query": query, "results": results, "provider": "brave"}
        except Exception as e:
            logger.debug("Brave search failed: %s; trying next provider", e)

    # 2. Tavily API
    tavily_key = _resolve_search_key("TAVILY_API_KEY", "tavily")
    if tavily_key:
        try:
            import urllib.request as _req2

            req2 = _req2.Request(
                "https://api.tavily.com/search",
                data=json.dumps(
                    {"api_key": tavily_key, "query": query, "max_results": num, "search_depth": "basic"}
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            with _req2.urlopen(req2, timeout=10) as r2:
                data2 = json.loads(r2.read())
            results = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", "")[:300],
                }
                for item in data2.get("results", [])[:num]
            ]
            if results:
                return {"success": True, "query": query, "results": results, "provider": "tavily"}
        except Exception as e:
            logger.debug("Tavily search failed: %s", e)

    # 3. Google Programmable Search
    google_key = _resolve_search_key("GOOGLE_SEARCH_API_KEY", "google")
    google_cx = _resolve_search_key("GOOGLE_SEARCH_ENGINE_ID", "google_cx")
    if google_key and google_cx:
        try:
            import urllib.parse as _parse3
            import urllib.request as _req3

            _g_params = {"key": google_key, "cx": google_cx, "q": query, "num": num}
            if any("\u4e00" <= _c <= "\u9fff" for _c in query):
                _g_params["lr"] = "lang_zh-CN"
            url3 = "https://www.googleapis.com/customsearch/v1?" + _parse3.urlencode(_g_params)
            with _req3.urlopen(url3, timeout=10) as r3:
                data3 = json.loads(r3.read())
            results = [
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                }
                for item in (data3.get("items") or [])[:num]
            ]
            if results:
                return {"success": True, "query": query, "results": results, "provider": "google"}
        except Exception as e:
            logger.debug("Google Programmable Search failed: %s; trying next provider", e)

    # 4. DuckDuckGo fallback
    try:
        import warnings as _w

        with _w.catch_warnings():
            _w.simplefilter("ignore")
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore[no-redef]
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("href", ""),
                "snippet": item.get("body", ""),
            }
            for item in DDGS().text(query, max_results=num)
        ]
        if results:
            return {"success": True, "query": query, "results": results, "provider": "duckduckgo"}
        return {
            "success": False,
            "query": query,
            "results": [],
            "error": (
                "DuckDuckGo returned no results (rate-limited). "
                "推荐配置: BRAVE_SEARCH_API_KEY、TAVILY_API_KEY 或 GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_ENGINE_ID"
            ),
        }
    except ImportError:
        pass
    except Exception as e:
        logger.debug("duckduckgo_search failed: %s", e)

    return {
        "success": False,
        "query": query,
        "results": [],
        "error": (
            "无可用搜索服务。推荐配置 BRAVE_SEARCH_API_KEY / TAVILY_API_KEY / "
            "GOOGLE_SEARCH_API_KEY + GOOGLE_SEARCH_ENGINE_ID，或安装 duckduckgo-search。"
        ),
    }


WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web via Brave, Tavily, Google Programmable Search, or DuckDuckGo.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "description": "Max results (1-10)"},
            },
            "required": ["query"],
        },
    },
}


def register_web_search_tool(tool_registry: Dict, schema_registry: List) -> int:
    """Register `web_search` in tool/schema registries if not already present."""
    added = 0
    if "web_search" not in tool_registry:
        tool_registry["web_search"] = (
            _web_search,
            "Web search via Brave Search API or DuckDuckGo fallback; set BRAVE_SEARCH_API_KEY for higher quota",
        )
        added = 1
    existing_schema_names = {s.get("function", {}).get("name") for s in schema_registry}
    if "web_search" not in existing_schema_names:
        schema_registry.append(WEB_SEARCH_SCHEMA)
    return added
