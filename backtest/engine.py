"""向量化回测引擎（A股长多策略）。

特性：
- 次日开盘成交（信号当日收盘产生，T+1 开盘执行，更贴近 A股实际）
- 双边手续费 + 印花税
- 完整绩效指标：累计收益、年化、最大回撤、夏普、胜率、交易次数
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategies.base import Strategy
from strategies.library import get_strategy


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    total_return: float          # 策略累计收益率
    benchmark_return: float      # 买入持有基准收益率
    annual_return: float
    max_drawdown: float
    sharpe: float
    win_rate: float
    trade_count: int
    equity_curve: pd.Series = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "total_return": round(self.total_return * 100, 2),
            "benchmark_return": round(self.benchmark_return * 100, 2),
            "excess_return": round((self.total_return - self.benchmark_return) * 100, 2),
            "annual_return": round(self.annual_return * 100, 2),
            "max_drawdown": round(self.max_drawdown * 100, 2),
            "sharpe": round(self.sharpe, 3),
            "win_rate": round(self.win_rate * 100, 2),
            "trade_count": self.trade_count,
        }


class BacktestEngine:
    def __init__(
        self,
        commission: float = 0.0003,   # 佣金（双边）
        stamp_tax: float = 0.0005,    # 印花税（卖出单边）
        slippage: float = 0.0005,     # 滑点
        init_cash: float = 1_000_000.0,
    ):
        self.commission = commission
        self.stamp_tax = stamp_tax
        self.slippage = slippage
        self.init_cash = init_cash

    def run(self, df: pd.DataFrame, strategy: Strategy) -> BacktestResult:
        if df is None or len(df) < 30:
            raise ValueError("数据不足（至少需要 30 个交易日）")

        d = df.reset_index(drop=True).copy()
        positions = strategy.generate_positions(d).reset_index(drop=True)

        # T+1 执行：今日信号决定明日持仓
        exec_pos = positions.shift(1).fillna(0.0)

        # 个股日收益（基于收盘价）
        ret = d["close"].pct_change().fillna(0.0)

        # 策略每日收益 = 昨日持仓 * 当日收益
        strat_ret = exec_pos * ret

        # 交易成本：仓位变化时扣费
        pos_change = exec_pos.diff().abs().fillna(exec_pos.abs())
        buy_cost = (exec_pos.diff().clip(lower=0).fillna(exec_pos.clip(lower=0))) * (self.commission + self.slippage)
        sell_cost = (-exec_pos.diff().clip(upper=0).fillna(0)) * (self.commission + self.stamp_tax + self.slippage)
        cost = buy_cost + sell_cost
        strat_ret = strat_ret - cost

        equity = (1 + strat_ret).cumprod()
        equity.index = pd.to_datetime(d["date"])

        # ---- 绩效指标 ----
        total_return = float(equity.iloc[-1] - 1)
        benchmark_return = float(d["close"].iloc[-1] / d["close"].iloc[0] - 1)
        n_days = len(d)
        years = max(n_days / 244.0, 1e-6)
        annual_return = float((equity.iloc[-1]) ** (1 / years) - 1)

        # 最大回撤
        roll_max = equity.cummax()
        drawdown = equity / roll_max - 1
        max_drawdown = float(drawdown.min())

        # 夏普（年化，无风险利率按 0 计）
        if strat_ret.std() > 0:
            sharpe = float(strat_ret.mean() / strat_ret.std() * np.sqrt(244))
        else:
            sharpe = 0.0

        # 交易统计：每段持仓为一笔交易
        trades = self._extract_trades(exec_pos, d["close"])
        trade_count = len(trades)
        win_rate = float(np.mean([t > 0 for t in trades])) if trades else 0.0

        return BacktestResult(
            strategy=strategy.cn_name,
            symbol=str(d.get("symbol", [""])[0]) if "symbol" in d else "",
            total_return=total_return,
            benchmark_return=benchmark_return,
            annual_return=annual_return,
            max_drawdown=max_drawdown,
            sharpe=sharpe,
            win_rate=win_rate,
            trade_count=trade_count,
            equity_curve=equity,
        )

    @staticmethod
    def _extract_trades(pos: pd.Series, close: pd.Series) -> list[float]:
        """提取每笔完整交易的收益率。"""
        trades = []
        entry_price = None
        pos = pos.reset_index(drop=True)
        close = close.reset_index(drop=True)
        for i in range(len(pos)):
            if pos[i] > 0 and entry_price is None:
                entry_price = close[i]
            elif pos[i] == 0 and entry_price is not None:
                trades.append(close[i] / entry_price - 1)
                entry_price = None
        if entry_price is not None:  # 末尾仍持仓，按最后收盘平仓
            trades.append(close.iloc[-1] / entry_price - 1)
        return trades


def run_backtest(df: pd.DataFrame, strategy_name: str, **kwargs) -> BacktestResult:
    """便捷入口。"""
    engine = BacktestEngine(**kwargs)
    return engine.run(df, get_strategy(strategy_name))
