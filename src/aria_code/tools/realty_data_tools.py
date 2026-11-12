"""
realty_data_tools.py — 不动产市场数据层
==========================================
数据来源：
  - AKShare    — 中国房价指数/房地产投资/REITs
  - FRED       — 美国住房数据 (Case-Shiller/新屋开工/NAHB)
  - 本地计算    — 租金收益率/物业估值/资产评级

全部函数返回 {"success": bool, ...} 统一格式。

安装依赖（可选）：
    pip install akshare          # 中国数据
    pip install openpyxl         # Excel 导出
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import akshare as ak
    _HAS_AK = True
# Not just ImportError: akshare reads bundled data files and calendars on
# import, and a partial install (or a frozen binary without its data) raises
# FileNotFoundError or worse. Optional means optional — never crash the CLI.
except Exception:  # noqa: BLE001
    _HAS_AK = False

try:
    import pandas as pd
    _HAS_PD = True
except ImportError:
    _HAS_PD = False

try:
    import numpy as np
    _HAS_NP = True
except ImportError:
    _HAS_NP = False

try:
    import requests as _req
    _HAS_REQ = True
except ImportError:
    _HAS_REQ = False

# ── 70城房价指数 ─────────────────────────────────────────────────────────────

# akshare 支持的城市（70城新房价格指数覆盖范围）
CN_CITIES_TIER1 = ["北京", "上海", "广州", "深圳"]
CN_CITIES_TIER2 = [
    "成都", "杭州", "武汉", "重庆", "南京", "西安", "天津", "苏州",
    "长沙", "郑州", "青岛", "沈阳", "宁波", "合肥", "厦门", "济南",
    "东莞", "佛山", "福州", "南宁", "昆明", "贵阳", "大连", "哈尔滨",
    "长春", "石家庄", "太原", "南昌", "兰州", "银川", "西宁", "乌鲁木齐",
    "海口", "三亚", "珠海", "温州", "泉州", "烟台", "洛阳", "唐山",
    "南通", "常州", "徐州", "扬州", "嘉兴", "金华", "绍兴",
    "湖州", "台州", "芜湖", "湘潭", "株洲", "常德", "桂林", "柳州",
    "汕头", "惠州", "江门", "湛江", "中山", "保定", "廊坊",
]
CN_CITIES_ALL = CN_CITIES_TIER1 + CN_CITIES_TIER2


def get_house_price_index(city: str = "北京", city2: str = "上海") -> dict:
    """
    获取中国 70 城新房 / 二手房价格指数。

    返回同比、环比数据（最近 12 个月）。
    city: 主要城市（默认北京）
    city2: 对比城市（默认上海）
    """
    if not _HAS_AK:
        return {"success": False, "error": "akshare 未安装"}
    try:
        df = ak.macro_china_new_house_price(city_first=city, city_second=city2)
        if df is None or df.empty:
            return {"success": False, "error": "无数据"}

        # Keep last 24 months
        df = df.tail(24).copy()
        df.columns = [c.strip() for c in df.columns]
        records = df.to_dict("records")

        # Split by city
        city1_records = [r for r in records if str(r.get("城市","")) == city][-12:]
        city2_records = [r for r in records if str(r.get("城市","")) == city2][-12:]

        def _latest(recs):
            if not recs: return {}
            r = recs[-1]
            return {
                "date":       str(r.get("日期",""))[:7],
                "new_yoy":    r.get("新建商品住宅价格指数-同比"),
                "new_mom":    r.get("新建商品住宅价格指数-环比"),
                "second_yoy": r.get("二手住宅价格指数-同比"),
                "second_mom": r.get("二手住宅价格指数-环比"),
            }

        return {
            "success":   True,
            "city1":     city,
            "city2":     city2,
            "latest_city1":   _latest(city1_records),
            "latest_city2":   _latest(city2_records),
            "series_city1":   city1_records,
            "series_city2":   city2_records,
            "provider":  "akshare_NBS",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_re_investment() -> dict:
    """
    中国房地产开发投资额（月度累计同比）。
    反映房地产行业景气度。
    """
    if not _HAS_AK:
        return {"success": False, "error": "akshare 未安装"}
    try:
        df = ak.macro_china_real_estate()
        if df is None or df.empty:
            return {"success": False, "error": "无数据"}
        df = df.tail(12)
        records = df.to_dict("records")
        latest  = records[-1] if records else {}
        return {
            "success":   True,
            "latest":    latest,
            "series":    records,
            "provider":  "akshare_NBS",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_multi_city_comparison(cities: Optional[List[str]] = None) -> dict:
    """
    多城市房价指数对比（同比涨跌幅热力图数据）。
    默认对比 8 个核心城市（两两调用拼合）。
    """
    if not _HAS_AK:
        return {"success": False, "error": "akshare 未安装"}

    if cities is None:
        cities = ["北京", "上海", "深圳", "广州", "成都", "杭州", "武汉", "南京",
                  "重庆", "西安", "长沙", "天津", "苏州", "郑州", "厦门", "青岛"]

    results: List[Dict] = []
    # akshare requires city_first + city_second; batch by pairs
    city_pairs = list(zip(cities[::2], cities[1::2]))
    if len(cities) % 2 == 1:
        city_pairs.append((cities[-1], cities[0]))  # pair last with first

    seen: set = set()
    for c1, c2 in city_pairs:
        try:
            df = ak.macro_china_new_house_price(city_first=c1, city_second=c2)
            if df is None or df.empty:
                continue
            df = df.tail(4)
            for city in (c1, c2):
                if city in seen:
                    continue
                sub = df[df["城市"] == city]
                if sub.empty:
                    continue
                row = sub.iloc[-1]
                results.append({
                    "city":       city,
                    "tier":       "一线" if city in CN_CITIES_TIER1 else "二线",
                    "date":       str(row.get("日期",""))[:7],
                    "new_yoy":    _safe_float(row.get("新建商品住宅价格指数-同比")),
                    "new_mom":    _safe_float(row.get("新建商品住宅价格指数-环比")),
                    "second_yoy": _safe_float(row.get("二手住宅价格指数-同比")),
                    "second_mom": _safe_float(row.get("二手住宅价格指数-环比")),
                })
                seen.add(city)
        except Exception as e:
            logger.debug("City pair (%s,%s) failed: %s", c1, c2, e)

    if not results:
        return {"success": False, "error": "无法获取城市数据"}

    # Sort by new_yoy descending
    results.sort(key=lambda x: -(x.get("new_yoy") or 0))

    return {
        "success":   True,
        "cities":    results,
        "top_riser": results[0]["city"] if results else "",
        "top_faller":results[-1]["city"] if results else "",
        "provider":  "akshare_NBS",
    }


# ── REITs 分析 ───────────────────────────────────────────────────────────────

def get_reits_list() -> dict:
    """获取中国 REITs 实时行情列表（东方财富）。"""
    if not _HAS_AK:
        return {"success": False, "error": "akshare 未安装"}
    try:
        df = ak.reits_realtime_em()
        if df is None or df.empty:
            return {"success": False, "error": "无 REIT 数据"}
        cols = [c for c in ["代码","名称","最新价","涨跌额","涨跌幅","昨收","成交量","成交额"] if c in df.columns]
        records = df[cols].to_dict("records")
        return {
            "success":   True,
            "count":     len(records),
            "reits":     records,
            "provider":  "akshare_em",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_reit_analysis(code: str) -> dict:
    """
    单只 REIT 深度分析：历史行情 + 估值指标。

    code: REIT 代码，如 "508603" (唯品商业)
    """
    if not _HAS_AK:
        return {"success": False, "error": "akshare 未安装"}
    try:
        import yfinance as yf
        _HAS_YF = True
    except ImportError:
        _HAS_YF = False

    result: Dict[str, Any] = {"success": True, "code": code}

    # Historical prices
    try:
        df_hist = ak.reits_hist_em(symbol=code, period="daily",
                                   start_date="20230101",
                                   end_date=datetime.now().strftime("%Y%m%d"),
                                   adjust="qfq")
        if df_hist is not None and not df_hist.empty:
            df_hist = df_hist.tail(252)  # 1 year
            last = df_hist.iloc[-1]
            cols = df_hist.columns.tolist()
            close_col = next((c for c in cols if "收盘" in c or "close" in c.lower()), cols[-1])
            prices = df_hist[close_col].astype(float)
            cur_price = float(last[close_col])
            result["price"] = round(cur_price, 3)

            # Performance
            if len(prices) >= 2:
                result["return_1y"]  = round((prices.iloc[-1]/prices.iloc[0] - 1)*100, 2)
            if len(prices) >= 20:
                result["return_1m"]  = round((prices.iloc[-1]/prices.iloc[-20] - 1)*100, 2)

            # Volatility
            if _HAS_NP and len(prices) >= 20:
                rets = prices.pct_change().dropna()
                result["volatility_annual"] = round(float(rets.std() * np.sqrt(252) * 100), 2)

            result["history_tail"] = df_hist.tail(5).to_dict("records")
    except Exception as e:
        logger.debug("REIT hist failed %s: %s", code, e)

    # Realtime info from list
    try:
        df_rt = ak.reits_realtime_em()
        if df_rt is not None and not df_rt.empty:
            row = df_rt[df_rt["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                result["name"]     = str(r.get("名称",""))
                result["price"]    = result.get("price") or _safe_float(r.get("最新价"))
                result["chg_pct"]  = _safe_float(r.get("涨跌幅"))
                result["prev_close"] = _safe_float(r.get("昨收"))
    except Exception as e:
        logger.debug("REIT realtime failed: %s", e)

    return result


# ── 租金收益率计算器 ──────────────────────────────────────────────────────────

def calc_rental_yield(params: dict) -> dict:
    """
    物业租金收益率计算（毛/净收益率）。

    参数：
      purchase_price: 购入价格（万元）
      monthly_rent:   月租金（元）
      annual_costs:   年维护成本（元，默认0）
      tax_rate:       租金税率（小数，默认0.05）
      loan_ratio:     贷款成数（0-1，默认0=全款）
      loan_rate:      贷款年利率（小数，默认0.04）
      loan_years:     贷款年数（默认30）
    """
    price_wan   = float(params.get("purchase_price", 0))
    monthly_rent= float(params.get("monthly_rent", 0))
    annual_costs= float(params.get("annual_costs", 0))
    tax_rate    = float(params.get("tax_rate", 0.05))
    loan_ratio  = float(params.get("loan_ratio", 0))
    loan_rate   = float(params.get("loan_rate", 0.04))
    loan_years  = int(params.get("loan_years", 30))

    if price_wan <= 0 or monthly_rent <= 0:
        return {"success": False, "error": "purchase_price 和 monthly_rent 均为必填项"}

    price_yuan    = price_wan * 10000
    annual_rent   = monthly_rent * 12
    tax_deduction = annual_rent * tax_rate
    net_rent      = annual_rent - tax_deduction - annual_costs

    # Gross yield
    gross_yield = annual_rent / price_yuan * 100

    # Net yield (without leverage)
    net_yield = net_rent / price_yuan * 100

    # Leveraged yield (with mortgage)
    equity  = price_yuan * (1 - loan_ratio)
    monthly_payment = 0.0
    annual_interest = 0.0
    if loan_ratio > 0:
        loan_amount = price_yuan * loan_ratio
        monthly_rate = loan_rate / 12
        n = loan_years * 12
        if monthly_rate > 0:
            monthly_payment = loan_amount * monthly_rate * (1 + monthly_rate)**n / ((1 + monthly_rate)**n - 1)
            annual_interest = monthly_payment * 12 - loan_amount / loan_years  # approximate

    leveraged_net_rent = net_rent - (monthly_payment * 12 if loan_ratio > 0 else 0)
    leveraged_yield    = (leveraged_net_rent / equity * 100) if equity > 0 else 0

    # Payback period
    payback_years = price_yuan / net_rent if net_rent > 0 else 999

    # Cap rate (NOI / price)
    noi = annual_rent - annual_costs
    cap_rate = noi / price_yuan * 100

    return {
        "success":          True,
        "purchase_price_wan": price_wan,
        "monthly_rent":     monthly_rent,
        "annual_rent":      annual_rent,
        "gross_yield_pct":  round(gross_yield, 2),
        "net_yield_pct":    round(net_yield, 2),
        "cap_rate_pct":     round(cap_rate, 2),
        "leveraged_yield_pct": round(leveraged_yield, 2) if loan_ratio > 0 else None,
        "monthly_payment":  round(monthly_payment, 2) if loan_ratio > 0 else None,
        "payback_years":    round(payback_years, 1),
        "equity_invested":  round(equity / 10000, 2),
        "assessment":       (
            "优质标的" if gross_yield >= 5
            else "合理收益" if gross_yield >= 3
            else "收益偏低"
        ),
        "benchmark": "一线城市租金收益率通常 1.5-3%，二线 2-4%，商业地产 4-7%",
    }


# ── 物业估值模型 ──────────────────────────────────────────────────────────────

def property_valuation(params: dict) -> dict:
    """
    物业估值三合一模型：收益法 + DCF + 市场比较法。

    必填：
      area_sqm:       建筑面积（平米）
      monthly_rent:   当前月租金（元）
      location_tier:  区位层级（"tier1"/"tier2"/"tier3"）

    可选：
      vacancy_rate:   空置率（默认0.05）
      cap_rate:       市场资本化率（默认由区位自动推算）
      annual_growth:  租金年增长率（默认0.02）
      discount_rate:  折现率（默认0.06）
      hold_years:     持有年数（默认10）
      price_per_sqm:  参考市场单价（元/m²，不填则用租金反推）
    """
    area         = float(params.get("area_sqm", 100))
    monthly_rent = float(params.get("monthly_rent", 0))
    tier         = str(params.get("location_tier", "tier2")).lower()
    vacancy      = float(params.get("vacancy_rate", 0.05))
    annual_growth= float(params.get("annual_growth", 0.02))
    hold_years   = int(params.get("hold_years", 10))

    # Tier-based defaults
    _TIER_DEFAULTS = {
        "tier1": {"cap_rate": 0.025, "discount_rate": 0.055, "price_range": (80000, 200000)},
        "tier2": {"cap_rate": 0.04,  "discount_rate": 0.065, "price_range": (20000, 60000)},
        "tier3": {"cap_rate": 0.06,  "discount_rate": 0.08,  "price_range": (5000,  20000)},
    }
    defaults = _TIER_DEFAULTS.get(tier, _TIER_DEFAULTS["tier2"])
    cap_rate      = float(params.get("cap_rate", defaults["cap_rate"]))
    discount_rate = float(params.get("discount_rate", defaults["discount_rate"]))
    price_range   = defaults["price_range"]

    annual_rent   = monthly_rent * 12
    noi           = annual_rent * (1 - vacancy)

    # ── 1. 收益法 (Income Approach) ────────────────────────────────────────
    income_value = noi / cap_rate if cap_rate > 0 else 0

    # ── 2. DCF 法 ──────────────────────────────────────────────────────────
    dcf_value = 0.0
    if _HAS_NP:
        cf = noi
        for yr in range(1, hold_years + 1):
            dcf_value += cf / (1 + discount_rate) ** yr
            cf *= (1 + annual_growth)
        # Terminal value (Gordon growth)
        terminal_growth = min(annual_growth, 0.015)
        terminal_cf = noi * (1 + annual_growth) ** hold_years
        terminal_val = terminal_cf / (discount_rate - terminal_growth)
        dcf_value += terminal_val / (1 + discount_rate) ** hold_years
    else:
        dcf_value = income_value  # fallback

    # ── 3. 市场比较法 (Market Comparable) ─────────────────────────────────
    ref_price_per_sqm = float(params.get("price_per_sqm", 0))
    if ref_price_per_sqm <= 0:
        # Derive from rent using typical rent-to-price ratio
        rent_per_sqm_monthly = monthly_rent / area if area > 0 else 0
        # Typical P/R ratio: 300-600 months for residential, 150-250 for commercial
        ratio = 400 if tier == "tier1" else 300 if tier == "tier2" else 200
        ref_price_per_sqm = rent_per_sqm_monthly * ratio

    market_value = ref_price_per_sqm * area

    # ── Weighted average ───────────────────────────────────────────────────
    weights = {"income": 0.4, "dcf": 0.4, "market": 0.2}
    blended = (income_value * weights["income"] +
               dcf_value    * weights["dcf"] +
               market_value * weights["market"])

    # Price range from market benchmarks
    lo = price_range[0] * area
    hi = price_range[1] * area

    return {
        "success":            True,
        "area_sqm":           area,
        "monthly_rent":       monthly_rent,
        "noi_annual":         round(noi, 0),
        "income_approach":    round(income_value / 10000, 2),  # 万元
        "dcf_approach":       round(dcf_value    / 10000, 2),
        "market_approach":    round(market_value / 10000, 2),
        "blended_value_wan":  round(blended / 10000, 2),
        "market_range_wan":   [round(lo/10000, 0), round(hi/10000, 0)],
        "price_per_sqm":      round(blended / area, 0) if area > 0 else 0,
        "gross_yield_pct":    round(annual_rent / blended * 100, 2) if blended > 0 else 0,
        "cap_rate_used":      round(cap_rate * 100, 2),
        "discount_rate_used": round(discount_rate * 100, 2),
        "hold_years":         hold_years,
        "verdict": (
            "价值被低估" if market_value < blended * 0.85
            else "价值被高估" if market_value > blended * 1.15
            else "定价合理"
        ),
    }


# ── 资产综合评分 ─────────────────────────────────────────────────────────────

def asset_location_score(params: dict) -> dict:
    """
    资产区位 + 业态潜力综合评分（0-100）。
    结合区位因素、空间条件、业态适配度生成量化评分。

    参数：
      city:           城市名称
      district:       区域/商圈
      area_sqm:       面积（平米）
      floor:          楼层
      foot_traffic:   客流量评估 ("high"/"medium"/"low")
      competition:    周边竞争 ("high"/"medium"/"low")
      renovation_allowed: 是否允许改造 (bool)
      open_fire_allowed:  是否允许明火 (bool)
      vacant_days:    空置天数
      expected_rent:  期望租金（元/月）
    """
    city        = str(params.get("city",""))
    area        = float(params.get("area_sqm", 100))
    floor       = int(params.get("floor", 1))
    traffic     = str(params.get("foot_traffic", "medium")).lower()
    competition = str(params.get("competition", "medium")).lower()
    reno_ok     = bool(params.get("renovation_allowed", True))
    fire_ok     = bool(params.get("open_fire_allowed", False))
    vacant_days = int(params.get("vacant_days", 0))
    exp_rent    = float(params.get("expected_rent", 0))

    score = 50  # base
    breakdown = {}

    # 区位得分 (0-30)
    city_bonus = 30 if city in CN_CITIES_TIER1 else 20 if city in CN_CITIES_TIER2 else 10
    score += city_bonus - 20  # normalize around 50
    breakdown["区位城市"] = f"{city_bonus - 20:+d}"

    # 客流量 (0-15)
    traffic_score = {"high": 15, "medium": 8, "low": 2}.get(traffic, 8)
    score += traffic_score - 8
    breakdown["客流量"] = f"{traffic_score - 8:+d}"

    # 楼层因素 (0-10)
    floor_score = 10 if floor == 1 else 6 if floor <= 3 else 2
    score += floor_score - 5
    breakdown["楼层"] = f"{floor_score - 5:+d}"

    # 面积适配 (0-10) — 50-300m² 最优
    area_score = 10 if 50 <= area <= 300 else 5 if area < 50 else 7
    score += area_score - 5
    breakdown["面积适配"] = f"{area_score - 5:+d}"

    # 竞争强度（负分）
    comp_penalty = {"high": -8, "medium": -3, "low": 0}.get(competition, -3)
    score += comp_penalty
    breakdown["竞争环境"] = f"{comp_penalty:+d}"

    # 改造灵活性
    if reno_ok:
        score += 5; breakdown["可改造"] = "+5"
    if fire_ok:
        score += 3; breakdown["允许明火"] = "+3"

    # 空置惩罚
    if vacant_days > 180:
        score -= 8; breakdown["空置惩罚"] = "-8"
    elif vacant_days > 90:
        score -= 3; breakdown["空置惩罚"] = "-3"

    # 租金合理性（与城市基准对比）
    if exp_rent > 0 and area > 0:
        per_sqm = exp_rent / area
        benchmarks = {"tier1": 300, "tier2": 120, "tier3": 50}
        tier = "tier1" if city in CN_CITIES_TIER1 else "tier2" if city in CN_CITIES_TIER2 else "tier3"
        bench = benchmarks[tier]
        if per_sqm < bench * 0.7:
            score += 5; breakdown["低于市场租金"] = "+5"
        elif per_sqm > bench * 1.3:
            score -= 5; breakdown["高于市场租金"] = "-5"

    score = max(0, min(100, score))

    if score >= 75:
        rating = "A级 — 优质资产"
    elif score >= 60:
        rating = "B级 — 良好资产"
    elif score >= 45:
        rating = "C级 — 一般资产"
    else:
        rating = "D级 — 需改善"

    # Suitable business types based on conditions
    suitable = []
    if fire_ok and area >= 80:
        suitable.extend(["餐饮/火锅/烤肉"])
    if traffic == "high" and area <= 150:
        suitable.extend(["连锁零售/便利店/奶茶"])
    if reno_ok and area >= 200:
        suitable.extend(["健身房/儿童乐园/培训机构"])
    if floor == 1 and area >= 100:
        suitable.extend(["美容美发/洗车/宠物店"])
    if not suitable:
        suitable = ["轻餐饮", "服务类商铺"]

    return {
        "success":       True,
        "score":         score,
        "rating":        rating,
        "breakdown":     breakdown,
        "suitable_businesses": list(set(suitable))[:5],
        "city":          city,
        "area_sqm":      area,
    }


# ── 美国住房数据 (FRED) ──────────────────────────────────────────────────────

def get_us_housing_data() -> dict:
    """
    美国住房市场数据：
    - 新屋开工 (HOUST)
    - NAHB 建筑商信心指数
    - S&P/Case-Shiller 20城房价指数 (SPCS20RSA)
    - 30年固定按揭利率 (MORTGAGE30US)
    """
    from aria_code.tools.macro_tools import _fred_series  # reuse existing FRED helper

    indicators = {
        "housing_starts":    ("HOUST",       "新屋开工（千套，季调年化）"),
        "nahb_index":        ("NAHB REALTORS", "NAHB建筑商信心指数"),
        "case_shiller_20":   ("SPCS20RSA",   "Case-Shiller 20城房价指数"),
        "mortgage_30y":      ("MORTGAGE30US", "30年固定按揭利率(%)"),
        "existing_home_sales":("EXHOSLUSM495S","成屋销售（季调年化）"),
    }

    # Fallback NAHB via yfinance-equivalent approximation
    _FRED_MAP = {
        "housing_starts":     "HOUST",
        "case_shiller_20":    "SPCS20RSA",
        "mortgage_30y":       "MORTGAGE30US",
        "existing_home_sales":"EXHOSLUSM495S",
    }

    results = {}
    for key, series_id in _FRED_MAP.items():
        data = _fred_series(series_id, limit=12)
        if data:
            results[key] = {
                "label":  indicators[key][1],
                "latest": data[-1],
                "series": data[-6:],
            }

    # NAHB via hardcoded label (no clean FRED key)
    nahb_data = _fred_series("NHSUSSPT", limit=12)
    if nahb_data:
        results["nahb_index"] = {
            "label": "NAHB建筑商信心指数 (>50=乐观)",
            "latest": nahb_data[-1],
            "series": nahb_data[-6:],
        }

    if not results:
        return {"success": False, "error": "FRED 数据获取失败"}

    # Market assessment
    mortgage = (results.get("mortgage_30y", {}).get("latest") or {}).get("value") or 0
    cs_yoy   = None  # Case-Shiller is level index, need to compute YoY separately

    assessment = []
    if mortgage > 7:
        assessment.append("按揭利率偏高（>7%），购房负担较重")
    elif mortgage < 4:
        assessment.append("按揭利率较低（<4%），购房环境宽松")
    else:
        assessment.append(f"按揭利率 {mortgage:.2f}%，属正常区间")

    return {
        "success":    True,
        "country":    "US",
        "data":       results,
        "assessment": assessment,
        "provider":   "FRED",
    }


# ── Helper ────────────────────────────────────────────────────────────────────

def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None and str(val) not in ("nan","None","") else None
    except (ValueError, TypeError):
        return None
