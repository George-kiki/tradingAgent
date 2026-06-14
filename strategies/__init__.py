"""交易策略模块。"""
from .base import Strategy, SignalResult
from .library import STRATEGY_REGISTRY, get_strategy, list_strategies

__all__ = [
    "Strategy",
    "SignalResult",
    "STRATEGY_REGISTRY",
    "get_strategy",
    "list_strategies",
]
