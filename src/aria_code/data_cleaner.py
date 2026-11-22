"""Data quality result types (full OHLCV audit migrated to artheras/financial-service `financial_ohlcv_quality_audit`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class ValidationIssue:
    row_index: object
    column: str
    issue_type: str
    value: float
    description: str


@dataclass
class DataGap:
    start: str
    end: str
    days: int
    kind: str


@dataclass
class CleanResult:
    df: Any
    issues: List[ValidationIssue] = field(default_factory=list)
    gaps: List[DataGap] = field(default_factory=list)
    outlier_count: int = 0
    fill_count: int = 0
    quality_score: float = 100.0

    @property
    def real_gap_days(self) -> int:
        return sum(g.days for g in self.gaps if g.kind == "data_gap")

    def summary(self) -> str:
        return (
            f"质量评分 {self.quality_score:.1f}/100 · "
            f"异常值 {self.outlier_count} 条 · "
            f"数据缺口 {self.real_gap_days} 天 · "
            f"填充 {self.fill_count} 行"
        )
