"""
strategy_vault.py — Arthera Strategy Version Control System

类似 Git，但专为量化策略设计：

  /strategy save   "v1.2 加入波动率过滤"   → 保存当前策略快照
  /strategy list                           → 列出所有版本
  /strategy diff   v1 v2                   → 显示两版本差异
  /strategy load   v3                      → 加载指定版本
  /strategy review                         → AI 审查当前策略（过拟合/前视偏差）
  /strategy bench  AAPL 2020-01-01         → 对标基准表现

数据存储: ~/.arthera/strategy_vault.db (SQLite)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from aria_code.packages.aria_core.paths import aria_home

_VAULT_DIR = aria_home() / "strategies"
_VAULT_DB  = _VAULT_DIR / "vault.db"

# ── Database setup ────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    _VAULT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_VAULT_DB))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS strategies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            version_tag TEXT NOT NULL,
            message     TEXT DEFAULT '',
            code        TEXT NOT NULL,
            metadata    TEXT DEFAULT '{}',
            backtest_result TEXT DEFAULT NULL,
            review_result   TEXT DEFAULT NULL,
            code_hash   TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_name ON strategies(name);
        CREATE INDEX IF NOT EXISTS idx_hash ON strategies(code_hash);
    """)
    conn.commit()
    return conn


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class StrategyVersion:
    id: int
    name: str
    version_tag: str
    message: str
    code: str
    metadata: Dict[str, Any]
    backtest_result: Optional[Dict]
    review_result: Optional[Dict]
    code_hash: str
    created_at: str

    @classmethod
    def from_row(cls, row) -> "StrategyVersion":
        meta = {}
        try:
            meta = json.loads(row["metadata"] or "{}")
        except Exception:
            pass
        bt = None
        try:
            bt = json.loads(row["backtest_result"]) if row["backtest_result"] else None
        except Exception:
            pass
        rv = None
        try:
            rv = json.loads(row["review_result"]) if row["review_result"] else None
        except Exception:
            pass
        return cls(
            id=row["id"], name=row["name"],
            version_tag=row["version_tag"], message=row["message"],
            code=row["code"], metadata=meta,
            backtest_result=bt, review_result=rv,
            code_hash=row["code_hash"], created_at=row["created_at"],
        )

    def summary_line(self) -> str:
        tag  = f"[{self.version_tag}]"
        ts   = self.created_at[:16]
        bt   = ""
        if self.backtest_result:
            br = self.backtest_result
            sharpe = br.get("sharpe_ratio")
            ret    = br.get("total_return_pct")
            if sharpe is not None:
                bt = f"  sharpe={sharpe:.2f}  ret={ret:.1f}%"
        reviewed = " ✓reviewed" if self.review_result else ""
        msg = f"  {self.message[:50]}" if self.message else ""
        return f"{self.id:4d}  {tag:12s}  {ts}{msg}{bt}{reviewed}"


# ═══════════════════════════════════════════════════════════════════════════
# StrategyVault
# ═══════════════════════════════════════════════════════════════════════════

class StrategyVault:

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(
        self,
        code: str,
        name: str = "strategy",
        message: str = "",
        metadata: Optional[Dict] = None,
    ) -> StrategyVersion:
        """Save a new version. Returns the saved StrategyVersion."""
        code_hash = hashlib.sha256(code.encode()).hexdigest()[:12]
        # Auto version tag: v{n+1}
        with _get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM strategies WHERE name=?", (name,)
            ).fetchone()
            n   = row["c"] if row else 0
            tag = f"v{n + 1}"
            conn.execute(
                """INSERT INTO strategies
                   (name, version_tag, message, code, metadata, code_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, tag, message, code,
                 json.dumps(metadata or {}),
                 code_hash, datetime.now().isoformat()),
            )
            conn.commit()
            last_id = conn.execute("SELECT last_insert_rowid() as lid").fetchone()["lid"]
            row = conn.execute("SELECT * FROM strategies WHERE id=?", (last_id,)).fetchone()
            return StrategyVersion.from_row(row)

    # ── List ──────────────────────────────────────────────────────────────────

    def list(self, name: str = "strategy", limit: int = 20) -> List[StrategyVersion]:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM strategies WHERE name=? ORDER BY id DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        return [StrategyVersion.from_row(r) for r in rows]

    def list_all_names(self) -> List[str]:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT name FROM strategies ORDER BY name"
            ).fetchall()
        return [r["name"] for r in rows]

    # ── Load ──────────────────────────────────────────────────────────────────

    def load(self, name: str = "strategy",
             version_tag: Optional[str] = None,
             version_id: Optional[int] = None) -> Optional[StrategyVersion]:
        with _get_db() as conn:
            if version_id:
                row = conn.execute(
                    "SELECT * FROM strategies WHERE id=?", (version_id,)
                ).fetchone()
            elif version_tag:
                row = conn.execute(
                    "SELECT * FROM strategies WHERE name=? AND version_tag=? ORDER BY id DESC LIMIT 1",
                    (name, version_tag),
                ).fetchone()
            else:
                # Latest
                row = conn.execute(
                    "SELECT * FROM strategies WHERE name=? ORDER BY id DESC LIMIT 1",
                    (name,),
                ).fetchone()
        return StrategyVersion.from_row(row) if row else None

    # ── Diff ──────────────────────────────────────────────────────────────────

    def diff(
        self,
        name: str = "strategy",
        tag_a: Optional[str] = None,
        tag_b: Optional[str] = None,
    ) -> str:
        """Unified diff between two versions (default: last two)."""
        versions = self.list(name, limit=10)
        if len(versions) < 2:
            return "需要至少2个版本才能比较差异。"

        if tag_a and tag_b:
            va = next((v for v in versions if v.version_tag == tag_a), None)
            vb = next((v for v in versions if v.version_tag == tag_b), None)
        elif tag_a:
            va = next((v for v in versions if v.version_tag == tag_a), None)
            vb = versions[0]
        else:
            vb = versions[0]
            va = versions[1]

        if not va or not vb:
            return f"找不到指定版本: {tag_a} / {tag_b}"

        diff_lines = list(difflib.unified_diff(
            va.code.splitlines(keepends=True),
            vb.code.splitlines(keepends=True),
            fromfile=f"{name} {va.version_tag} ({va.created_at[:10]})",
            tofile=f"{name} {vb.version_tag} ({vb.created_at[:10]})",
            n=3,
        ))
        if not diff_lines:
            return f"{va.version_tag} 与 {vb.version_tag} 代码完全相同。"
        return "".join(diff_lines)

    # ── Store backtest / review results ───────────────────────────────────────

    def save_backtest(self, version_id: int, result: Dict):
        with _get_db() as conn:
            conn.execute(
                "UPDATE strategies SET backtest_result=? WHERE id=?",
                (json.dumps(result), version_id),
            )
            conn.commit()

    def save_review(self, version_id: int, review: Dict):
        with _get_db() as conn:
            conn.execute(
                "UPDATE strategies SET review_result=? WHERE id=?",
                (json.dumps(review), version_id),
            )
            conn.commit()

    def validate_and_save_backtest(
        self,
        version_id: int,
        result: Dict[str, Any],
        *,
        n_trials: int = 1,
        pit_passed: bool = True,
        risk_policy: Optional[Any] = None,
        target_weight: float = 0.0,
    ):
        """Persist a backtest result and run the independent DSR/PIT/RiskPolicy gate."""
        from aria_code.domain.research_validation import validate_research_backtest

        self.save_backtest(version_id, result)
        sharpe = float(result.get("sharpe", result.get("sharpe_ratio", 0.0)) or 0.0)
        is_sharpe = float(result.get("is_sharpe", sharpe) or sharpe)
        oos_sharpe = float(result.get("oos_sharpe", sharpe) or sharpe)
        n_obs = int(
            result.get("n_obs")
            or result.get("trading_days")
            or len(result.get("equity_curve") or [])
            or 0
        )
        max_dd = abs(float(result.get("max_drawdown_pct", result.get("max_drawdown", 0.0)) or 0.0))
        if max_dd > 1.0:
            max_dd = max_dd / 100.0
        report = validate_research_backtest(
            is_sharpe=is_sharpe,
            oos_sharpe=oos_sharpe,
            n_obs=n_obs,
            n_trials=n_trials,
            max_drawdown_pct=max_dd,
            skew=float(result.get("skew", 0.0) or 0.0),
            kurtosis=float(result.get("kurtosis", 3.0) or 3.0),
            pit_passed=pit_passed,
            risk_policy=risk_policy,
            target_weight=target_weight,
        )
        existing_review: Dict[str, Any] = {}
        with _get_db() as conn:
            row = conn.execute("SELECT review_result FROM strategies WHERE id=?", (version_id,)).fetchone()
            if row and row["review_result"]:
                try:
                    existing_review = json.loads(row["review_result"]) or {}
                except Exception:
                    existing_review = {}
        existing_review["validation_gate"] = report.to_dict()
        self.save_review(version_id, existing_review)
        return report


# ═══════════════════════════════════════════════════════════════════════════
# AI Strategy Reviewer (过拟合 / 前视偏差 / 代码质量)
# ═══════════════════════════════════════════════════════════════════════════

# 静态规则检测（不需要 AI，速度快）

_LOOKAHEAD_PATTERNS = [
    (r"\.shift\(-\d+\)",           "负向shift：使用了未来数据"),
    (r"df\[.+\]\s*=.*\.shift\(0\)", "shift(0)可能引入当日收盘前视偏差"),
    (r"fillna\(method=['\"]bfill",  "后向填充(bfill)引入前视偏差"),
    (r"\.rolling\([^)]+\)\.apply\(", "rolling.apply 注意是否含未来数据"),
    (r"target\s*=\s*.+\[.+[+>]\d", "目标变量使用未来标签"),
]

_OVERFIT_PATTERNS = [
    (r"\.optimize\(",               "参数优化可能导致过拟合"),
    (r"for.*param.*range\(",        "网格搜索参数可能过拟合"),
    (r"if.*sharpe.*>[5-9]",         "Sharpe>5 在样本外几乎不可能，可能过拟合"),
    (r"n_estimators\s*=\s*[5-9]\d{2,}", "超大集成树数量，可能过拟合"),
    (r"in_sample.*backtest",        "样本内回测不代表真实表现"),
]

def static_review(code: str) -> Dict[str, Any]:
    """快速静态规则审查，不调用 AI。"""
    warnings = []
    errors   = []

    for pattern, msg in _LOOKAHEAD_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            errors.append({"type": "look_ahead_bias", "detail": msg, "pattern": pattern})

    for pattern, msg in _OVERFIT_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            warnings.append({"type": "overfit_risk", "detail": msg, "pattern": pattern})

    # 回测质量检查
    quality_checks = []
    if "train_test_split" not in code and "out_of_sample" not in code:
        quality_checks.append("未见样本外验证（train/test split 或 walk-forward）")
    if "commission" not in code and "slippage" not in code:
        quality_checks.append("未设置手续费/滑点，回测结果偏乐观")
    if "random_state" in code and "shuffle" in code:
        quality_checks.append("时序数据注意不要随机shuffle，会引入未来信息")

    risk_score = len(errors) * 3 + len(warnings) + len(quality_checks)
    grade = "A" if risk_score == 0 else "B" if risk_score <= 2 else "C" if risk_score <= 5 else "D"

    return {
        "grade":          grade,
        "risk_score":     risk_score,
        "errors":         errors,      # 严重（前视偏差）
        "warnings":       warnings,    # 中等（过拟合风险）
        "quality_checks": quality_checks,  # 建议改进
        "summary": (f"发现 {len(errors)} 个严重问题, "
                    f"{len(warnings)} 个警告, "
                    f"{len(quality_checks)} 条优化建议"),
    }


async def ai_review_strategy(
    code: str,
    backtest_result: Optional[Dict],
    ollama_url: str,
    model: str,
    on_token: Optional[callable] = None,
) -> Dict[str, Any]:
    """AI 深度审查策略代码（结合回测结果）."""
    import aiohttp

    # 先跑静态检测
    static = static_review(code)

    bt_summary = ""
    if backtest_result:
        br = backtest_result
        bt_summary = f"""
【回测结果】
  年化收益: {br.get('annual_return_pct', 'N/A')}%
  夏普比率: {br.get('sharpe_ratio', 'N/A')}
  最大回撤: {br.get('max_drawdown_pct', 'N/A')}%
  总收益: {br.get('total_return_pct', 'N/A')}%
  胜率: {br.get('win_rate_pct', 'N/A')}%
  交易次数: {br.get('total_trades', 'N/A')}"""

    static_issues = ""
    if static["errors"]:
        static_issues += "\n静态检测发现严重问题:\n"
        for e in static["errors"]:
            static_issues += f"  ❌ {e['detail']}\n"
    if static["warnings"]:
        static_issues += "静态检测警告:\n"
        for w in static["warnings"]:
            static_issues += f"  ⚠️  {w['detail']}\n"

    code_preview = code[:2000] + ("...(截断)" if len(code) > 2000 else "")

    prompt = f"""你是量化策略审查专家，请对以下策略进行深度代码审查。

{bt_summary}
{static_issues}

【策略代码】
```python
{code_preview}
```

请从以下5个维度审查（每项给出1-2个具体问题或确认通过）：

**1. 前视偏差 (Look-Ahead Bias)**
检查是否使用了未来数据（如未来收益、未来价格、负向lag等）

**2. 过拟合风险 (Overfitting)**
参数数量是否合理？是否有样本外验证？夏普比率是否过高？

**3. 执行可行性 (Execution Reality)**
交易成本、流动性、订单执行假设是否合理？

**4. 策略逻辑 (Logic Soundness)**
策略的核心假设是否有理论支撑？边界条件是否处理？

**5. 改进建议 (Improvements)**
最重要的2-3个改进点

最后给出:**综合评级** [A/B/C/D] 和 **是否推荐实盘测试** [是/否/待改进后]

中文回答，专业且直接。"""

    messages = [
        {"role": "system", "content": "你是严格的量化策略审查专家，专注于发现策略缺陷，防止真金白银的亏损。"},
        {"role": "user",   "content": prompt},
    ]

    full_text = ""
    try:
        async with aiohttp.ClientSession(trust_env=True) as sess:
            async with sess.post(
                f"{ollama_url}/api/chat",
                json={"model": model, "messages": messages, "stream": True,
                      "options": {"temperature": 0.2, "num_predict": 800}},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                async for line in resp.content:
                    text = line.decode("utf-8", errors="ignore").strip()
                    if not text:
                        continue
                    try:
                        data = json.loads(text)
                        tok = data.get("message", {}).get("content", "")
                        if tok:
                            full_text += tok
                            if on_token:
                                on_token(tok)
                    except Exception:
                        pass
    except Exception as e:
        full_text = f"[AI审查失败: {e}]"

    return {
        "static":     static,
        "ai_review":  full_text,
        "reviewed_at": datetime.now().isoformat(),
    }


# ── Singleton ─────────────────────────────────────────────────────────────────

_vault: Optional[StrategyVault] = None

def get_vault() -> StrategyVault:
    global _vault
    if _vault is None:
        _vault = StrategyVault()
    return _vault
