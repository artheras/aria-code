"""Google ADK entrypoint: ``adk web adk_apps`` discovers ``aria_research``."""

from __future__ import annotations

import os

from google.adk.agents.llm_agent import Agent

from aria_code.packages.adk_bridge import MarketResearchTools


_tools = MarketResearchTools()

root_agent = Agent(
    model=os.getenv("ARIA_ADK_MODEL", "gemini-3.8-flash"),
    name="aria_research",
    description="A read-only market-research assistant for Aria Code and Arthera.",
    instruction="""You are Aria Research, a careful financial-research assistant.

Use get_market_snapshot for live market facts and explicitly identify its data
quality, timestamp, warnings, and source limitations. Use get_market_data_health
when a data result is missing, stale, or conflicting. Do not invent prices,
returns, news, citations, positions, or tool output.

This agent is research-only. Never claim to execute, recommend, place, modify,
or schedule trades. Do not request credentials. State clearly that your output
is research assistance, not investment advice. For quantitative conclusions,
separate observed facts from hypotheses and mention material uncertainty.
""",
    tools=[_tools.get_market_snapshot, _tools.get_market_data_health],
)
