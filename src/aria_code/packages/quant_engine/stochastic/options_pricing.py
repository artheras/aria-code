#!/usr/bin/env python3
"""
Options Pricing & Greeks — Citadel / SIG 风格期权定价引擎
=========================================================
实现：
  Black-Scholes 解析定价（欧式）
  Greeks: Delta / Gamma / Theta / Vega / Rho / Vanna / Volga
  隐含波动率反推（Brent 法 + Newton-Raphson）
  二叉树（CRR）— 美式期权提前行权
  波动率曲面：SVI 参数化 + 隐波面插值
  Put-Call Parity 套利检验
  Greeks 聚合（组合层面 Delta / Gamma 汇总）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

import numpy as np

logger = logging.getLogger(__name__)

try:
    from scipy.optimize import brentq, minimize
    from scipy.stats import norm as sp_norm
    _SCIPY = True
    _norm_cdf = sp_norm.cdf
    _norm_pdf = sp_norm.pdf
except ImportError:
    _SCIPY = False
    # 纯 Python 备用（精度稍低）
    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
    def _norm_pdf(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── 数据类 ─────────────────────────────────────────────────────────────────────

@dataclass
class OptionSpec:
    """期权规格"""
    S: float          # 标的现价
    K: float          # 行权价
    T: float          # 到期时间（年，如 0.25 = 3 个月）
    r: float          # 无风险利率（年化，如 0.05）
    sigma: float      # 波动率（年化，如 0.20）
    option_type: str  # "call" | "put"
    q: float = 0.0    # 股息收益率（年化）

    @property
    def is_call(self) -> bool:
        return self.option_type.lower() in ("call", "c")

    @property
    def moneyness(self) -> float:
        """ln(F/K)，正值 = 实值"""
        F = self.S * math.exp((self.r - self.q) * self.T)
        return math.log(F / self.K) if self.K > 0 else 0.0


@dataclass
class BSResult:
    """Black-Scholes 定价结果"""
    price: float
    delta: float
    gamma: float
    theta: float      # 每日 theta（除以 365）
    vega: float       # 每 1% vol 的价格变化
    rho: float        # 每 1% 利率变化的价格变化
    vanna: float      # ∂Delta/∂σ
    volga: float      # ∂Vega/∂σ = ∂²V/∂σ²
    d1: float
    d2: float

    def greeks_summary(self) -> str:
        return (
            f"  价格: {self.price:.4f}\n"
            f"  Delta: {self.delta:+.4f}    Gamma: {self.gamma:.4f}\n"
            f"  Theta: {self.theta:+.4f}/日  Vega: {self.vega:.4f}/1%波动\n"
            f"  Rho:   {self.rho:+.4f}/1%利率  Vanna: {self.vanna:.4f}  Volga: {self.volga:.4f}"
        )


@dataclass
class ImpliedVolResult:
    """隐含波动率求解结果"""
    iv: float
    converged: bool
    iterations: int
    price_error: float     # |model_price - market_price|


@dataclass
class VolSurface:
    """波动率曲面（按到期 × 行权价网格）"""
    expiries: np.ndarray       # 到期时间（年），形如 [0.08, 0.25, 0.5, 1.0]
    strikes: np.ndarray        # 行权价
    ivs: np.ndarray            # 形状 (len(expiries), len(strikes))，隐含波动率
    S: float                   # 建立时标的现价
    svi_params: Optional[Dict] = None   # SVI 参数（若已拟合）

    def get_iv(self, T: float, K: float) -> float:
        """双线性插值查询隐波"""
        t_idx = np.searchsorted(self.expiries, T).clip(1, len(self.expiries) - 1)
        k_idx = np.searchsorted(self.strikes,  K).clip(1, len(self.strikes)  - 1)
        # 简单双线性插值
        t0, t1 = self.expiries[t_idx - 1], self.expiries[t_idx]
        k0, k1 = self.strikes[k_idx - 1],  self.strikes[k_idx]
        wt = (T - t0) / (t1 - t0 + 1e-10)
        wk = (K - k0) / (k1 - k0 + 1e-10)
        iv00 = self.ivs[t_idx - 1, k_idx - 1]
        iv01 = self.ivs[t_idx - 1, k_idx    ]
        iv10 = self.ivs[t_idx,     k_idx - 1]
        iv11 = self.ivs[t_idx,     k_idx    ]
        return float((1 - wt) * ((1 - wk) * iv00 + wk * iv01) + wt * ((1 - wk) * iv10 + wk * iv11))


@dataclass
class PortfolioGreeks:
    """组合 Greeks 汇总"""
    delta: float = 0.0    # 总 delta（以标的股数计）
    gamma: float = 0.0    # 总 gamma
    theta: float = 0.0    # 总每日 theta（$）
    vega:  float = 0.0    # 总 vega（per 1% vol, $）
    rho:   float = 0.0    # 总 rho

    def delta_dollars(self, S: float) -> float:
        """Delta 美元敞口"""
        return self.delta * S

    def one_pct_move_pnl(self, S: float) -> float:
        """标的价格上涨 1% 的 P&L 估计（一阶 + 二阶）"""
        dS = S * 0.01
        return self.delta * dS + 0.5 * self.gamma * dS ** 2

    def summary(self, S: float) -> str:
        lines = [
            "组合 Greeks 汇总",
            f"  Delta:    {self.delta:+.2f}  (${self.delta_dollars(S):+,.0f})",
            f"  Gamma:    {self.gamma:+.4f}",
            f"  Theta:    {self.theta:+.2f} $/日",
            f"  Vega:     {self.vega:+.2f} $/1%",
            f"  Rho:      {self.rho:+.2f} $/1%",
            f"  标的↑1%:  预估 ${self.one_pct_move_pnl(S):+,.0f}",
        ]
        return "\n".join(lines)


# ── Black-Scholes 解析定价 ─────────────────────────────────────────────────────

def black_scholes(opt: OptionSpec) -> BSResult:
    """
    Black-Scholes-Merton 解析定价（带股息）

    对 T ≤ 0 或 σ ≤ 0 做安全处理。
    """
    S, K, T, r, sigma, q = opt.S, opt.K, opt.T, opt.r, opt.sigma, opt.q

    if T <= 0:
        # 已到期
        intrinsic = max(S - K, 0.0) if opt.is_call else max(K - S, 0.0)
        return BSResult(price=intrinsic, delta=(1.0 if opt.is_call and S >= K else 0.0),
                        gamma=0.0, theta=0.0, vega=0.0, rho=0.0,
                        vanna=0.0, volga=0.0, d1=0.0, d2=0.0)

    if sigma <= 0:
        sigma = 1e-6

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    Nd1  = float(_norm_cdf(d1))
    Nd2  = float(_norm_cdf(d2))
    nd1  = float(_norm_pdf(d1))   # φ(d1)
    Nmd1 = 1.0 - Nd1
    Nmd2 = 1.0 - Nd2

    disc  = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    F      = S * disc_q / disc    # 前向价格（简化）
    F_full = S * math.exp((r - q) * T)

    if opt.is_call:
        price = S * disc_q * Nd1 - K * disc * Nd2
        delta = disc_q * Nd1
        rho   = K * T * disc * Nd2 / 100     # 每 1% 利率变化
    else:
        price = K * disc * Nmd2 - S * disc_q * Nmd1
        delta = -disc_q * Nmd1
        rho   = -K * T * disc * Nmd2 / 100

    gamma = disc_q * nd1 / (S * sigma * sqrt_T)
    vega  = S * disc_q * nd1 * sqrt_T / 100   # 每 1% vol
    theta = (
        -(S * sigma * disc_q * nd1) / (2 * sqrt_T)
        + (q * S * disc_q * (Nd1 if opt.is_call else Nmd1) * (-1 if opt.is_call else 1))
        - (r * K * disc * (Nd2 if opt.is_call else Nmd2) * (1 if opt.is_call else -1))
    ) / 365   # 日 theta

    # 高阶：Vanna = ∂Delta/∂σ = -(d2/σ) * γ * S (前向近似)
    vanna = -disc_q * nd1 * d2 / sigma

    # Volga = ∂Vega/∂σ = Vega * d1 * d2 / σ
    volga = (vega * 100) * d1 * d2 / sigma / 100   # 保持 /1% 单位

    return BSResult(
        price=float(price), delta=float(delta), gamma=float(gamma),
        theta=float(theta), vega=float(vega), rho=float(rho),
        vanna=float(vanna), volga=float(volga), d1=d1, d2=d2,
    )


# ── 隐含波动率反推 ─────────────────────────────────────────────────────────────

def implied_volatility(
    market_price: float,
    opt: OptionSpec,
    method: str = "brent",         # "brent" | "newton"
    tol: float = 1e-6,
    max_iter: int = 200,
) -> ImpliedVolResult:
    """
    反推隐含波动率

    Args:
        market_price: 市场观察期权价格
        opt:          OptionSpec（sigma 字段将被忽略）
        method:       "brent"（稳健）或 "newton"（快速）
    """
    # 内在价值检查
    intrinsic = max(opt.S - opt.K, 0.0) if opt.is_call else max(opt.K - opt.S, 0.0)
    disc = math.exp(-opt.r * opt.T)
    intrinsic_pv = intrinsic * disc

    if market_price < intrinsic_pv - tol:
        return ImpliedVolResult(iv=float("nan"), converged=False, iterations=0,
                                price_error=market_price - intrinsic_pv)

    def price_at_vol(sigma: float) -> float:
        spec = OptionSpec(S=opt.S, K=opt.K, T=opt.T, r=opt.r, sigma=sigma,
                          option_type=opt.option_type, q=opt.q)
        return black_scholes(spec).price - market_price

    if method == "brent" and _SCIPY:
        try:
            iv = brentq(price_at_vol, 1e-6, 10.0, xtol=tol, maxiter=max_iter)
            err = abs(price_at_vol(iv) + market_price - market_price)
            return ImpliedVolResult(iv=float(iv), converged=True, iterations=0, price_error=err)
        except ValueError:
            pass   # 区间无根，回退 Newton

    # Newton-Raphson（以 vega 为导数）
    sigma = 0.30   # 初始猜测
    for i in range(max_iter):
        spec = OptionSpec(S=opt.S, K=opt.K, T=opt.T, r=opt.r, sigma=sigma,
                          option_type=opt.option_type, q=opt.q)
        res   = black_scholes(spec)
        diff  = res.price - market_price
        # vega = ∂price/∂σ（注意 vega 是 /1%，转回 /1 unit）
        vega_1 = res.vega * 100
        if abs(vega_1) < 1e-10:
            break
        sigma -= diff / vega_1
        sigma  = max(1e-6, min(sigma, 10.0))
        if abs(diff) < tol:
            return ImpliedVolResult(iv=float(sigma), converged=True,
                                    iterations=i + 1, price_error=abs(diff))

    return ImpliedVolResult(iv=float(sigma), converged=False,
                            iterations=max_iter, price_error=abs(price_at_vol(sigma) + market_price - market_price))


# ── 美式期权 — CRR 二叉树 ──────────────────────────────────────────────────────

def binomial_american(
    opt: OptionSpec,
    n_steps: int = 200,
) -> float:
    """
    Cox-Ross-Rubinstein 二叉树定价美式期权

    Args:
        opt:     OptionSpec
        n_steps: 树的步数（越多越精确，通常 100~500 即可）

    Returns:
        美式期权价格
    """
    S, K, T, r, sigma, q = opt.S, opt.K, opt.T, opt.r, opt.sigma, opt.q
    dt     = T / n_steps
    u      = math.exp(sigma * math.sqrt(dt))
    d      = 1.0 / u
    disc   = math.exp(-r * dt)
    p_up   = (math.exp((r - q) * dt) - d) / (u - d)
    p_dn   = 1.0 - p_up

    # 终端节点价值
    prices = np.array([S * (u ** (n_steps - 2 * j)) for j in range(n_steps + 1)])
    if opt.is_call:
        values = np.maximum(prices - K, 0.0)
    else:
        values = np.maximum(K - prices, 0.0)

    # 向根节点折回，允许提前行权
    for i in range(n_steps - 1, -1, -1):
        prices  = prices[:-1] / u   # 节点标的价格
        values  = disc * (p_up * values[:-1] + p_dn * values[1:])
        if opt.is_call:
            intrinsic = np.maximum(prices - K, 0.0)
        else:
            intrinsic = np.maximum(K - prices, 0.0)
        values = np.maximum(values, intrinsic)

    return float(values[0])


# ── SVI 波动率微笑参数化 ────────────────────────────────────────────────────────

class SVISmile:
    """
    Gatheral SVI（Stochastic Volatility Inspired）参数化波动率微笑

    总方差：w(k) = a + b*(ρ*(k-m) + √((k-m)²+σ²))
    其中 k = ln(K/F)（对数货币性）

    参数：
      a  > 0：整体波动率水平
      b  > 0：翅膀陡峭度
      ρ  ∈ (-1,1)：偏斜（负值 = 左偏）
      m  ∈ ℝ：顶点偏移
      σ  > 0：顶点曲率（ATM 平滑度）
    """

    def __init__(
        self,
        a: float = 0.04,
        b: float = 0.10,
        rho: float = -0.30,
        m: float = 0.0,
        sigma: float = 0.10,
    ):
        self.a     = a
        self.b     = b
        self.rho   = rho
        self.m     = m
        self.sigma = sigma

    def total_variance(self, k: float) -> float:
        """w(k) = a + b*(ρ*(k-m) + √((k-m)²+σ²))"""
        km = k - self.m
        return self.a + self.b * (self.rho * km + math.sqrt(km ** 2 + self.sigma ** 2))

    def iv(self, k: float, T: float) -> float:
        """隐含波动率 = √(w(k)/T)"""
        w = self.total_variance(k)
        if w < 0 or T <= 0:
            return float("nan")
        return math.sqrt(w / T)

    def fit(
        self,
        log_moneyness: np.ndarray,
        market_ivs: np.ndarray,
        T: float,
    ) -> "SVISmile":
        """
        最小二乘拟合 SVI 参数

        Args:
            log_moneyness: k = ln(K/F) 数组
            market_ivs:    对应隐含波动率（年化）
            T:             到期时间（年）

        Returns:
            拟合后的新 SVISmile 实例
        """
        if not _SCIPY:
            return self  # 无 scipy 时不做拟合

        market_total_var = market_ivs ** 2 * T

        def objective(params: np.ndarray) -> float:
            a, b, rho, m, sigma = params
            if b < 0 or sigma < 1e-6 or abs(rho) >= 1 or a < 0:
                return 1e10
            model_var = np.array([
                a + b * (rho * (k - m) + math.sqrt((k - m) ** 2 + sigma ** 2))
                for k in log_moneyness
            ])
            return float(np.mean((model_var - market_total_var) ** 2))

        x0 = [self.a, self.b, self.rho, self.m, self.sigma]
        bounds = [(1e-6, 2.0), (1e-6, 2.0), (-0.999, 0.999), (-2.0, 2.0), (1e-4, 2.0)]
        res = minimize(objective, x0, method="L-BFGS-B", bounds=bounds,
                       options={"maxiter": 500, "ftol": 1e-12})
        if res.success:
            a, b, rho, m, sigma = res.x
            return SVISmile(a=a, b=b, rho=rho, m=m, sigma=sigma)
        return self


# ── Put-Call Parity 套利检验 ─────────────────────────────────────────────────

def parity_arbitrage(
    call_price: float,
    put_price: float,
    S: float, K: float, T: float, r: float,
    q: float = 0.0,
    threshold_bps: float = 5.0,
) -> Dict:
    """
    检验 Put-Call Parity 是否成立

    C - P = S·e^(-qT) - K·e^(-rT)

    Returns:
        dict 含: parity_lhs, parity_rhs, deviation_bps, is_arbitrage
    """
    lhs = call_price - put_price
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    dev = lhs - rhs
    dev_bps = abs(dev / S) * 1e4

    return {
        "parity_lhs":   lhs,
        "parity_rhs":   rhs,
        "deviation":    dev,
        "deviation_bps": dev_bps,
        "is_arbitrage": dev_bps > threshold_bps,
        "direction":    "buy_put_sell_call" if dev > 0 else "buy_call_sell_put",
    }


# ── 组合 Greeks 聚合 ──────────────────────────────────────────────────────────

class OptionsPortfolio:
    """
    期权组合 Greeks 聚合器

    支持多腿期权组合（Straddle / Strangle / Butterfly 等）。
    """

    def __init__(self):
        self._legs: List[Tuple[OptionSpec, float]] = []  # (spec, quantity)

    def add_leg(self, opt: OptionSpec, qty: float, multiplier: float = 100.0):
        """
        添加一条腿

        Args:
            opt:        期权规格
            qty:        手数（正 = 买入, 负 = 卖出）
            multiplier: 合约乘数（股票期权通常 100）
        """
        self._legs.append((opt, qty * multiplier))

    def greeks(self) -> PortfolioGreeks:
        """汇总所有腿的 Greeks"""
        pg = PortfolioGreeks()
        for opt, qty in self._legs:
            bs = black_scholes(opt)
            pg.delta += bs.delta * qty
            pg.gamma += bs.gamma * qty
            pg.theta += bs.theta * qty * opt.S   # 转为 $ per day
            pg.vega  += bs.vega  * qty * opt.S   # 转为 $ per 1% vol
            pg.rho   += bs.rho   * qty * opt.S
        return pg

    def total_value(self) -> float:
        """组合当前市值（未乘以名义价值，只是 BS 价格×手数）"""
        return sum(black_scholes(opt).price * qty for opt, qty in self._legs)

    def scenario_pnl(
        self,
        spot_moves: np.ndarray,    # 相对移动，如 np.linspace(-0.20, 0.20, 41)
        vol_shocks: np.ndarray,    # 波动率绝对变化，如 np.linspace(-0.05, 0.05, 11)
    ) -> "np.ndarray":
        """
        Greeks-based 情景 P&L 矩阵

        Returns:
            (len(spot_moves), len(vol_shocks)) 的 P&L 矩阵
        """
        S0 = self._legs[0][0].S if self._legs else 1.0
        pnl_matrix = np.zeros((len(spot_moves), len(vol_shocks)))
        base_val = self.total_value()

        for i, ds in enumerate(spot_moves):
            for j, dv in enumerate(vol_shocks):
                port = OptionsPortfolio()
                for opt, qty_total in self._legs:
                    new_opt = OptionSpec(
                        S=opt.S * (1 + ds),
                        K=opt.K,
                        T=opt.T,
                        r=opt.r,
                        sigma=max(opt.sigma + dv, 1e-4),
                        option_type=opt.option_type,
                        q=opt.q,
                    )
                    # 每手数量已 embed 在 qty_total，避免重乘 multiplier
                    port._legs.append((new_opt, qty_total))
                pnl_matrix[i, j] = port.total_value() - base_val

        return pnl_matrix


# ── 便利函数 ──────────────────────────────────────────────────────────────────

def bs_price(
    S: float, K: float, T: float, r: float, sigma: float,
    option_type: str = "call", q: float = 0.0
) -> float:
    """简洁 B-S 定价接口"""
    return black_scholes(OptionSpec(S=S, K=K, T=T, r=r, sigma=sigma,
                                    option_type=option_type, q=q)).price


def delta_hedge_ratio(
    S: float, K: float, T: float, r: float, sigma: float,
    option_type: str = "call", q: float = 0.0,
) -> float:
    """计算 Delta 对冲比率（每合约需要对冲的股数）"""
    return black_scholes(OptionSpec(S=S, K=K, T=T, r=r, sigma=sigma,
                                    option_type=option_type, q=q)).delta


def iv_surface(
    S: float,
    calls: "pd.DataFrame",    # columns: expiry(年), strike, price
    r: float = 0.05,
    q: float = 0.0,
) -> VolSurface:
    """
    从期权报价构建隐含波动率曲面

    Args:
        S:     标的现价
        calls: DataFrame 含 expiry, strike, price 列
        r:     无风险利率
        q:     股息收益率

    Returns:
        VolSurface 对象
    """
    import pandas as pd

    expiries = sorted(calls["expiry"].unique())
    strikes  = sorted(calls["strike"].unique())
    ivs      = np.full((len(expiries), len(strikes)), np.nan)

    for i, T in enumerate(expiries):
        for j, K in enumerate(strikes):
            row = calls[(calls["expiry"] == T) & (calls["strike"] == K)]
            if row.empty:
                continue
            mkt_price = float(row["price"].iloc[0])
            opt = OptionSpec(S=S, K=K, T=T, r=r, sigma=0.30, option_type="call", q=q)
            iv_res = implied_volatility(mkt_price, opt)
            if iv_res.converged:
                ivs[i, j] = iv_res.iv

    return VolSurface(
        expiries=np.array(expiries),
        strikes=np.array(strikes),
        ivs=ivs,
        S=S,
    )


def find_aria_quant_binary() -> Optional[str]:
    """Find native Rust aria-quant binary if built."""
    import os
    import shutil
    from pathlib import Path

    env_bin = os.getenv("ARIA_QUANT_BINARY")
    if env_bin and Path(env_bin).is_file():
        return env_bin
    cand = shutil.which("aria-quant")
    if cand:
        return cand
    # Check workspace rust target directory
    repo_cand = Path(__file__).resolve().parents[5] / "rust" / "target" / "release" / "aria-quant"
    if repo_cand.is_file():
        return str(repo_cand)
    return None


def native_monte_carlo(
    s0: float,
    r: float,
    sigma: float,
    t: float,
    num_paths: int = 100000,
    steps: int = 252,
    q: float = 0.0,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run high-performance multi-threaded parallel Monte Carlo simulation using native Rust aria-quant.
    Falls back to numpy if native binary is not built.
    """
    import json
    import subprocess
    bin_path = find_aria_quant_binary()
    if bin_path:
        payload = json.dumps({
            "s0": float(s0),
            "r": float(r),
            "q": float(q),
            "sigma": float(sigma),
            "t": float(t),
            "steps": int(steps),
            "num_paths": int(num_paths),
            "seed": seed,
        })
        try:
            res = subprocess.run([bin_path, "monte-carlo", payload], capture_output=True, text=True, check=True)
            out = json.loads(res.stdout)
            out["engine"] = "rust_native"
            return out
        except Exception as e:
            logger.debug("[options_pricing] native monte carlo failed, falling back to numpy: %s", e)

    # Fallback numpy simulation
    dt = t / steps
    drift = (r - q - 0.5 * sigma ** 2) * dt
    vol_sqrt = sigma * math.sqrt(dt)
    if seed is not None:
        np.random.seed(seed)
    z = np.random.normal(0, 1, (num_paths, steps))
    log_returns = drift + vol_sqrt * z
    paths = s0 * np.exp(np.sum(log_returns, axis=1))
    paths.sort()
    mean = float(np.mean(paths))
    std = float(np.std(paths))
    p05 = float(np.percentile(paths, 5))
    p01 = float(np.percentile(paths, 1))
    return {
        "mean_terminal": mean,
        "std_terminal": std,
        "var_95": max(0.0, s0 - p05),
        "var_99": max(0.0, s0 - p01),
        "p05": p05,
        "median": float(np.median(paths)),
        "p95": float(np.percentile(paths, 95)),
        "engine": "python_numpy",
    }
