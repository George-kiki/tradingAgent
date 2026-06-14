"""策略基类。

约定：A股不支持裸卖空，因此策略输出为「目标仓位」序列，取值 0（空仓）或 1（满仓）。
- generate_positions(df): 用于回测，返回与 df 等长的 0/1 仓位 Series。
- current_signal(df): 用于实时分析，返回最新交易信号与理由。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from core.indicators import add_all_indicators


@dataclass
class SignalResult:
    """单只股票的最新策略信号。"""
    strategy: str
    signal: str          # BUY / SELL / HOLD
    reason: str
    score: float         # -1 ~ 1，正为看多强度

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "signal": self.signal,
            "reason": self.reason,
            "score": round(self.score, 3),
        }


class Strategy(ABC):
    """策略抽象基类。"""

    name: str = "base"
    cn_name: str = "基础策略"
    description: str = ""

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """统一附加技术指标。"""
        return add_all_indicators(df)

    @abstractmethod
    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        """返回 0/1 目标仓位序列（index 与 df 对齐）。"""

    @abstractmethod
    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        """返回最新交易信号。"""
