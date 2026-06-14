"""每日荐股 + 反思迭代系统。

子模块：
- database   本地 SQLite 持久化（推荐/结果/胜率/反思/策略权重）
- winrate    胜率计算与结算（次日收益判定）
- reflection 反思机制（多维诊断 + 策略权重/约束自动调整 + LLM 反思结论）
- engine     荐股引擎（结算→反思→选股→持久化 全流程）
"""
from __future__ import annotations

from recommend.engine import RecommendEngine, run_recommend

__all__ = ["RecommendEngine", "run_recommend"]
