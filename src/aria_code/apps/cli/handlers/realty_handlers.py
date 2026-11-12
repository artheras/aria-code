"""Deterministic real-estate query handler extracted from aria_cli.py."""
from __future__ import annotations

from typing import Callable


def handle_realty_query(
    message: str,
    *,
    is_realty_query: Callable[[str], bool],
    cn_cities: tuple | list,
    intl_cities: tuple | list,
) -> dict:
    """Deterministic handler for natural-language real-estate / housing questions.

    Detects city names + real-estate keywords and calls the realty data tools
    directly, returning a formatted response without needing the LLM to parse it.
    """
    if not is_realty_query(message):
        return {"success": False, "error": "not_realty_query"}

    try:
        from aria_code.tools.realty_data_tools import get_house_price_index, get_re_investment
    except ImportError:
        return {"success": False, "error": "realty_data_tools_not_available"}

    cities_found: list[str] = []
    for city in cn_cities:
        if city in message:
            cities_found.append(city)
        if len(cities_found) >= 2:
            break

    _low_msg = message.lower()
    _intl_found = [c for c in intl_cities if c in _low_msg]

    if _intl_found and not cities_found:
        _intl_name = _intl_found[0]
        return {
            "success": True,
            "response": (
                f"## 🌍 {_intl_name.title()} 房地产市场\n\n"
                f"国际城市房价数据目前依赖 LLM 知识库分析（无实时数据接入）。\n\n"
                f"**当前支持实时数据的城市：** 中国大陆 70 个主要城市（北上广深等）\n\n"
                f"如需国际市场数据，建议查阅：\n"
                f"- 美国：`/realty us` — 联邦住房数据（Case-Shiller 指数、新屋开工）\n"
                f"- 其他国际城市：可直接提问，Aria 将基于训练知识回答（数据截止至知识库更新时间）\n\n"
                f"---\n\n"
                f"请问您具体想了解 **{_intl_name.title()}** 哪方面的房地产信息？"
            ),
            "tools_used": ["realty_query"],
        }

    city1 = cities_found[0] if cities_found else "全国"
    city2 = cities_found[1] if len(cities_found) > 1 else ("上海" if city1 != "上海" else "北京")

    lines: list[str] = []
    lines.append(f"## 🏠 {city1} 房地产市场")
    if len(cities_found) > 1:
        lines[-1] += f" vs {city2}"
    lines.append("")

    try:
        r = get_house_price_index(city1, city2)
        if r.get("success"):
            for lbl, cd in (
                (city1, r.get("latest_city1", {})),
                (city2, r.get("latest_city2", {})),
            ):
                if not cd:
                    continue
                lines.append(f"### {lbl}")
                if cd.get("new_yoy") is not None:
                    lines.append(f"- **新房同比**：{float(cd['new_yoy']):+.2f}%")
                if cd.get("new_mom") is not None:
                    lines.append(f"- **新房环比**：{float(cd['new_mom']):+.2f}%")
                if cd.get("second_yoy") is not None:
                    lines.append(f"- **二手房同比**：{float(cd['second_yoy']):+.2f}%")
                if cd.get("second_mom") is not None:
                    lines.append(f"- **二手房环比**：{float(cd['second_mom']):+.2f}%")
                if cd.get("date"):
                    lines.append(f"- **数据期**：{cd['date']}")
                lines.append("")
        else:
            lines.append(f"房价指数数据暂时不可用（{r.get('error', '数据源未响应')}）")
            lines.append("")
    except Exception as _e:
        lines.append(f"获取房价数据失败: {_e}")
        lines.append("")

    try:
        ri = get_re_investment()
        if ri.get("success") and ri.get("latest"):
            lt = ri["latest"]
            lines.append("### 全国房地产开发投资")
            lines.append(f"- **最新值**：{lt.get('最新值', 'N/A')}")
            lines.append(f"- **日期**：{lt.get('日期', 'N/A')}")
            lines.append(f"- **涨跌幅**：{lt.get('涨跌幅', 'N/A')}")
            lines.append(f"- **近1年涨跌幅**：{lt.get('近1年涨跌幅', 'N/A')}")
            lines.append("")
    except Exception:
        pass

    lines.append("**更多操作**")
    lines.append(f"- `/realty market {city1}` — 完整城市房价走势图")
    lines.append(f"- `/realty compare {city1} {city2}` — 城市横向对比")
    lines.append("- `/realty rent` — 租金收益率计算")
    lines.append("- `/realty reit` — REIT 市场数据")

    return {
        "success": True,
        "response": "\n".join(lines),
        "tools_used": ["realty_query"],
    }
