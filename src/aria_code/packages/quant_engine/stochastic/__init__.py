"""
Arthera Stochastic Calculus Module
随机微积分模块

Components:
  - ito_calculus.py      : 伊藤引理 / 伊藤积分 / 随机微分方程
  - gbm_enhanced.py      : 增强几何布朗运动（多资产/跳跃扩散/随机波动率）
  - stochastic_processes : OU / CIR / Vasicek / Hull-White 过程
  - monte_carlo_advanced : 方差缩减蒙特卡罗（Antithetic/Control/Quasi-MC）
  - kelly_criterion      : 凯利公式（连续时间 / 多资产 / Robust版本）
"""

from .ito_calculus import ItoCalculus, ItoProcess, apply_ito_lemma
from .gbm_enhanced import EnhancedGBM
from .stochastic_processes import (
    OrnsteinUhlenbeck, CIRProcess, VasicekModel, HullWhiteModel
)
from .monte_carlo_advanced import MonteCarloEngine, VarianceReduction
from .kelly_criterion import KellyCriterion

__all__ = [
    "ItoCalculus", "ItoProcess", "apply_ito_lemma",
    "EnhancedGBM",
    "OrnsteinUhlenbeck", "CIRProcess", "VasicekModel", "HullWhiteModel",
    "MonteCarloEngine", "VarianceReduction",
    "KellyCriterion",
]

# Optional explicit backend modules never override this distribution.
from .._namespace import extend as _extend_namespace
_extend_namespace(__path__, "stochastic")
