"""自定义条件选股。

- engine：结构化条件筛选引擎（全市场快照初筛 + K线条件验证 + 资金流稳定性）
- templates：选股模板的本地持久化（可命名保存/加载/删除）
"""
from __future__ import annotations

from screener.engine import ScreenerEngine, run_screener
from screener.templates import (
    DEFAULT_TEMPLATE,
    delete_template,
    list_templates,
    save_template,
)

__all__ = [
    "ScreenerEngine", "run_screener",
    "DEFAULT_TEMPLATE", "list_templates", "save_template", "delete_template",
]
