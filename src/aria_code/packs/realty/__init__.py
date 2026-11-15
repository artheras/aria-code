"""Real estate — a city's housing market.

This pack is the contract's hard case, and worth reading as one.  Its domain
has no ``AAPL`` and no ``ch_3Pq…``: the thing a housing question is *about* is
a city, and a city name on its own is a perfectly ordinary word.  "北京" appears
in travel plans, in address parsing, in a comment about a datacenter region.

So the entity this pack resolves is not the city.  It is **this city's housing
market**, which takes two parts to name: a city from a real table, and a
housing term in the same message.  A city alone resolves at a confidence below
the activation threshold — reported, so the UI can ask, but never enough to
answer a code question with a house-price index on its own initiative.

That is the same rule finance applies to a bare uppercase run, arrived at from
the opposite direction: finance has to *demote* a shape that looks like an
identifier, and realty has to *combine* two things neither of which is one.
"""

from __future__ import annotations

from typing import Sequence

from aria_code.packs.base import BaseDomainPack, EntityMatch, PackActivation

PACK_NAME = "realty"

REALTY_TOOLS: tuple[str, ...] = (
    "get_house_price_index",
    "get_re_investment",
    "get_reits_list",
    "calc_rental_yield",
)

_RESOLVED = 0.95
_CITY_ONLY = 0.3

# Terms that name a housing market by themselves, with no city attached.
# "全国房价走势" is a real question about a real market, and refusing it would
# lose behaviour the ungated chain used to provide.
#
# The line drawn here is narrower than the handler's keyword list on purpose.
# "房价" and "楼市" name a market; "房产", "地产" and "物业" name a *topic*, and
# they appear in the source of any property-management codebase — which is
# exactly the kind of message that must not come back as a price index.
_NATIONAL_MARKET_TERMS = (
    "房价", "楼市", "楼价", "二手房", "新房",
    "house price", "home price", "housing market", "property price",
)


def _tables() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """City and keyword tables, borrowed rather than duplicated.

    They live in ``market_detect`` because the legacy chain used them; a copy
    here would drift, and the failure that drift produces (a city Aria stopped
    recognising after someone edited one list) is silent.
    """
    try:
        from aria_code.apps.cli.utils.market_detect import (
            _CN_CITIES,
            _INTL_CITIES,
            _REALTY_QUERY_KEYWORDS,
        )
    except Exception:
        return (), (), ()
    return tuple(_CN_CITIES), tuple(_INTL_CITIES), tuple(_REALTY_QUERY_KEYWORDS)


class RealtyPack(BaseDomainPack):
    """Recognises a named city's housing market."""

    name = PACK_NAME

    def resolve_entities(self, message: str) -> Sequence[EntityMatch]:
        text = message or ""
        if not text.strip():
            return ()

        cn_cities, intl_cities, keywords = _tables()
        if not cn_cities and not intl_cities:
            return ()

        lowered = text.lower()
        has_housing_term = any(keyword in lowered for keyword in keywords)

        found: list[tuple[str, int]] = []
        for city in cn_cities:
            position = text.find(city)
            if position >= 0:
                found.append((city, position))
        for city in intl_cities:
            position = lowered.find(city)
            if position >= 0:
                found.append((city, position))

        if not found:
            # No city named, but the message may still name the national
            # market outright.
            for term in _NATIONAL_MARKET_TERMS:
                position = lowered.find(term)
                if position >= 0:
                    return (EntityMatch(
                        pack=PACK_NAME, kind="housing_market", value="全国",
                        surface=term, position=position, confidence=_RESOLVED,
                    ),)
            return ()

        # Earliest mention first, and cap it: a message listing fifteen cities
        # is a table, not a question about a housing market.
        found.sort(key=lambda item: item[1])
        confidence = _RESOLVED if has_housing_term else _CITY_ONLY

        seen: set[str] = set()
        entities: list[EntityMatch] = []
        for city, position in found[:6]:
            if city in seen:
                continue
            seen.add(city)
            entities.append(EntityMatch(
                pack=PACK_NAME, kind="housing_market", value=city,
                surface=city, position=position, confidence=confidence,
            ))
        return tuple(entities)

    def handlers(self) -> Sequence[object]:
        """The realty handler, now reachable only when a market is named."""
        try:
            from aria_code.apps.cli.handlers.realty_handlers import handle_realty_query
            from aria_code.apps.cli.utils.market_detect import (
                _CN_CITIES,
                _INTL_CITIES,
                _is_realty_query,
            )
        except Exception:
            return ()

        def _handle(message: str) -> dict:
            return handle_realty_query(
                message,
                is_realty_query=_is_realty_query,
                cn_cities=_CN_CITIES,
                intl_cities=_INTL_CITIES,
            )

        return (_handle,)

    def tool_names(self) -> Sequence[str]:
        return REALTY_TOOLS

    def prompt_fragment(self, activation: PackActivation) -> str:
        cities = ", ".join(sorted({e.value for e in activation.entities}))
        # No tool instruction here: this pack exposes none. The price index is
        # fetched by the deterministic handler above, so if the model is the one
        # answering, it has no data source and must say so rather than produce
        # a plausible number.
        return (
            f"房地产市场已识别：{cities}。\n"
            "你没有房价数据工具。除非上文已给出取回的数据，否则不要给出具体房价、"
            "涨跌幅或指数值——说明数据不可得，并建议用 /realty 命令获取；"
            "标注统计口径（新房/二手房、环比/同比）与数据时间；输出不构成投资建议。"
        )

    def acceptance_commands(self, activation: PackActivation) -> Sequence[str]:
        # Looked up through the module rather than a name bound at import, so
        # what this workspace declares is read when the turn runs — a .ariarc
        # edited mid-session takes effect on the next message, not the next
        # process.
        from aria_code.packs import rules

        return rules.acceptance_commands_for(PACK_NAME)


REALTY_PACK = RealtyPack()


def register() -> RealtyPack:
    """Register the realty pack.  Idempotent."""
    from aria_code.packs.registry import register_pack

    register_pack(REALTY_PACK)
    return REALTY_PACK


__all__ = [
    "PACK_NAME",
    "REALTY_PACK",
    "REALTY_TOOLS",
    "RealtyPack",
    "register",
]
