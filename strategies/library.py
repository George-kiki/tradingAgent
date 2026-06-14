"""策略库：8 种常用量化交易策略。

每种策略既可用于回测（generate_positions），也可用于实时信号（current_signal）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.base import SignalResult, Strategy


def _cross_up(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """金叉：上一根 fast<=slow，当根 fast>slow。"""
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def _cross_down(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """死叉。"""
    return (fast < slow) & (fast.shift(1) >= slow.shift(1))


def _positions_from_cross(up: pd.Series, down: pd.Series) -> pd.Series:
    """由金叉/死叉事件推导持续仓位（金叉买入持有，死叉清仓）。"""
    pos = pd.Series(np.nan, index=up.index)
    pos[up] = 1.0
    pos[down] = 0.0
    pos = pos.ffill().fillna(0.0)
    return pos


# ============================================================
# 1. 均线金叉策略
# ============================================================
class MaCrossStrategy(Strategy):
    name = "ma_cross"
    cn_name = "均线金叉"
    description = "MA5 上穿 MA20 金叉买入，下穿死叉卖出。经典趋势跟踪。"

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        return _positions_from_cross(_cross_up(d["ma5"], d["ma20"]),
                                     _cross_down(d["ma5"], d["ma20"]))

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        up, down = _cross_up(d["ma5"], d["ma20"]), _cross_down(d["ma5"], d["ma20"])
        ma5, ma20 = d["ma5"].iloc[-1], d["ma20"].iloc[-1]
        if up.iloc[-1]:
            return SignalResult(self.cn_name, "BUY", f"MA5({ma5:.2f})金叉MA20({ma20:.2f})", 0.8)
        if down.iloc[-1]:
            return SignalResult(self.cn_name, "SELL", f"MA5({ma5:.2f})死叉MA20({ma20:.2f})", -0.8)
        trend = "多头" if ma5 > ma20 else "空头"
        return SignalResult(self.cn_name, "HOLD", f"无交叉，当前{trend}排列", 0.3 if ma5 > ma20 else -0.3)


# ============================================================
# 2. MACD 策略
# ============================================================
class MacdStrategy(Strategy):
    name = "macd"
    cn_name = "MACD"
    description = "DIF 上穿 DEA（金叉）买入，下穿卖出，结合零轴判断强弱。"

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        return _positions_from_cross(_cross_up(d["dif"], d["dea"]),
                                     _cross_down(d["dif"], d["dea"]))

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        up, down = _cross_up(d["dif"], d["dea"]), _cross_down(d["dif"], d["dea"])
        dif, hist = d["dif"].iloc[-1], d["macd"].iloc[-1]
        above_zero = dif > 0
        if up.iloc[-1]:
            return SignalResult(self.cn_name, "BUY",
                                f"DIF金叉DEA，{'零轴上方强势' if above_zero else '零轴下方反弹'}",
                                0.85 if above_zero else 0.6)
        if down.iloc[-1]:
            return SignalResult(self.cn_name, "SELL", "DIF死叉DEA，动能转弱", -0.8)
        return SignalResult(self.cn_name, "HOLD", f"红绿柱={hist:.3f}，无交叉",
                            0.2 if hist > 0 else -0.2)


# ============================================================
# 3. KDJ 策略
# ============================================================
class KdjStrategy(Strategy):
    name = "kdj"
    cn_name = "KDJ超买超卖"
    description = "K 上穿 D 且处于低位（<30）买入；高位（>70）死叉卖出。"

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        up = _cross_up(d["k"], d["d"]) & (d["d"] < 40)
        down = _cross_down(d["k"], d["d"]) & (d["d"] > 60)
        return _positions_from_cross(up, down)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        k, dd, j = d["k"].iloc[-1], d["d"].iloc[-1], d["j"].iloc[-1]
        if _cross_up(d["k"], d["d"]).iloc[-1] and dd < 40:
            return SignalResult(self.cn_name, "BUY", f"低位金叉 K={k:.1f} D={dd:.1f}", 0.75)
        if _cross_down(d["k"], d["d"]).iloc[-1] and dd > 60:
            return SignalResult(self.cn_name, "SELL", f"高位死叉 K={k:.1f} D={dd:.1f}", -0.75)
        if j < 0:
            return SignalResult(self.cn_name, "BUY", f"J值={j:.1f}超卖", 0.5)
        if j > 100:
            return SignalResult(self.cn_name, "SELL", f"J值={j:.1f}超买", -0.5)
        return SignalResult(self.cn_name, "HOLD", f"K={k:.1f} D={dd:.1f} J={j:.1f}", 0.0)


# ============================================================
# 4. 布林带策略（均值回归）
# ============================================================
class BollStrategy(Strategy):
    name = "boll"
    cn_name = "布林带回归"
    description = "价格触及下轨买入（超跌反弹），触及上轨卖出（均值回归）。"

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        up = (d["close"] <= d["boll_low"]) & (d["close"].shift(1) > d["boll_low"].shift(1))
        down = (d["close"] >= d["boll_up"]) & (d["close"].shift(1) < d["boll_up"].shift(1))
        return _positions_from_cross(up, down)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        close, up, low, mid = d["close"].iloc[-1], d["boll_up"].iloc[-1], d["boll_low"].iloc[-1], d["boll_mid"].iloc[-1]
        if close <= low:
            return SignalResult(self.cn_name, "BUY", f"价格({close:.2f})触及下轨({low:.2f})超跌", 0.7)
        if close >= up:
            return SignalResult(self.cn_name, "SELL", f"价格({close:.2f})触及上轨({up:.2f})", -0.7)
        pos = (close - low) / (up - low) if up > low else 0.5
        return SignalResult(self.cn_name, "HOLD", f"价格位于布林带{pos*100:.0f}%位置", (0.5 - pos))


# ============================================================
# 5. 多头趋势策略（均线多头排列）
# ============================================================
class TrendFollowStrategy(Strategy):
    name = "trend"
    cn_name = "多头趋势"
    description = "MA5>MA10>MA20>MA60 多头排列且价格在 MA20 上方时持有。"

    def _bull(self, d: pd.DataFrame) -> pd.Series:
        return (d["ma5"] > d["ma10"]) & (d["ma10"] > d["ma20"]) & (d["ma20"] > d["ma60"]) & (d["close"] > d["ma20"])

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        return self._bull(d).astype(float)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        bull = self._bull(d)
        if bull.iloc[-1] and not bull.iloc[-2]:
            return SignalResult(self.cn_name, "BUY", "形成均线多头排列", 0.9)
        if bull.iloc[-1]:
            return SignalResult(self.cn_name, "HOLD", "维持多头排列，持有", 0.7)
        if not bull.iloc[-1] and bull.iloc[-2]:
            return SignalResult(self.cn_name, "SELL", "多头排列被破坏", -0.7)
        return SignalResult(self.cn_name, "HOLD", "非多头排列，观望", -0.2)


# ============================================================
# 6. RSI 策略
# ============================================================
class RsiStrategy(Strategy):
    name = "rsi"
    cn_name = "RSI反转"
    description = "RSI<30 超卖买入，RSI>70 超买卖出。"

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        up = (d["rsi"] > 30) & (d["rsi"].shift(1) <= 30)
        down = (d["rsi"] < 70) & (d["rsi"].shift(1) >= 70)
        return _positions_from_cross(up, down)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        r = d["rsi"].iloc[-1]
        if r < 30:
            return SignalResult(self.cn_name, "BUY", f"RSI={r:.1f}超卖", 0.7)
        if r > 70:
            return SignalResult(self.cn_name, "SELL", f"RSI={r:.1f}超买", -0.7)
        return SignalResult(self.cn_name, "HOLD", f"RSI={r:.1f}中性", (50 - r) / 50)


# ============================================================
# 7. 突破策略（唐奇安通道）
# ============================================================
class BreakoutStrategy(Strategy):
    name = "breakout"
    cn_name = "高点突破"
    description = "突破近 20 日最高价买入，跌破近 10 日最低价卖出。"
    upper_n = 20
    lower_n = 10

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        hh = d["high"].rolling(self.upper_n).max().shift(1)
        ll = d["low"].rolling(self.lower_n).min().shift(1)
        up = d["close"] > hh
        down = d["close"] < ll
        return _positions_from_cross(up, down)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        hh = d["high"].rolling(self.upper_n).max().shift(1).iloc[-1]
        ll = d["low"].rolling(self.lower_n).min().shift(1).iloc[-1]
        close = d["close"].iloc[-1]
        if close > hh:
            return SignalResult(self.cn_name, "BUY", f"突破{self.upper_n}日高点({hh:.2f})", 0.85)
        if close < ll:
            return SignalResult(self.cn_name, "SELL", f"跌破{self.lower_n}日低点({ll:.2f})", -0.8)
        return SignalResult(self.cn_name, "HOLD", f"区间内运行({ll:.2f}~{hh:.2f})", 0.0)


# ============================================================
# 8. 量价突破策略
# ============================================================
class VolumeBreakStrategy(Strategy):
    name = "vol_break"
    cn_name = "放量突破"
    description = "放量（量>5日均量1.5倍）且价格上涨突破 MA20 时买入。"

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        vol_surge = d["volume"] > d["vol_ma5"] * 1.5
        up = vol_surge & (d["close"] > d["ma20"]) & (d["close"] > d["open"])
        down = d["close"] < d["ma20"]
        return _positions_from_cross(up, down)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        vol, vma = d["volume"].iloc[-1], d["vol_ma5"].iloc[-1]
        ratio = vol / vma if vma else 1.0
        close, ma20, op = d["close"].iloc[-1], d["ma20"].iloc[-1], d["open"].iloc[-1]
        if ratio > 1.5 and close > ma20 and close > op:
            return SignalResult(self.cn_name, "BUY", f"放量{ratio:.1f}倍突破MA20", 0.8)
        if close < ma20:
            return SignalResult(self.cn_name, "SELL", "跌破MA20", -0.6)
        return SignalResult(self.cn_name, "HOLD", f"量比={ratio:.1f}", 0.1)


# ============================================================
# 9. 缠论（简化版：分型 + 笔 买卖点）
# ============================================================
class ChanStrategy(Strategy):
    name = "chan"
    cn_name = "缠论分型"
    description = "简化缠论：识别顶/底分型构成的笔，底分型买入、顶分型卖出。"
    gap = 3  # 分型确认所需的左右间隔

    def _fractals(self, d: pd.DataFrame):
        """返回 (底分型布尔序列, 顶分型布尔序列)。

        底分型：第 i 根低点是其前后 gap 根中的最低，且为局部低点。
        顶分型：第 i 根高点是其前后 gap 根中的最高。
        """
        high, low = d["high"], d["low"]
        n = len(d)
        bottom = pd.Series(False, index=d.index)
        top = pd.Series(False, index=d.index)
        g = self.gap
        for i in range(g, n - g):
            win_low = low.iloc[i - g:i + g + 1]
            win_high = high.iloc[i - g:i + g + 1]
            if low.iloc[i] == win_low.min():
                bottom.iloc[i] = True
            if high.iloc[i] == win_high.max():
                top.iloc[i] = True
        return bottom, top

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        bottom, top = self._fractals(d)
        # 底分型买入（信号延后 gap 根才能确认），顶分型卖出
        up = bottom.shift(self.gap).fillna(False)
        down = top.shift(self.gap).fillna(False)
        return _positions_from_cross(up.astype(bool), down.astype(bool))

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        bottom, top = self._fractals(d)
        g = self.gap
        # 最近一个被确认的分型（位于倒数第 g 根附近）
        recent_bottom = bool(bottom.iloc[-(g + 1):].any())
        recent_top = bool(top.iloc[-(g + 1):].any())
        close = d["close"].iloc[-1]
        ma20 = d["ma20"].iloc[-1]
        if recent_bottom and not recent_top:
            strength = 0.8 if close > ma20 else 0.6
            return SignalResult(self.cn_name, "BUY", "出现底分型，构成潜在买点", strength)
        if recent_top and not recent_bottom:
            return SignalResult(self.cn_name, "SELL", "出现顶分型，构成潜在卖点", -0.75)
        trend = "上升笔" if close > ma20 else "下降笔"
        return SignalResult(self.cn_name, "HOLD", f"未出现明确分型，{trend}中", 0.2 if close > ma20 else -0.2)


# ============================================================
# 10. 网格交易（区间内低买高卖，连续仓位）
# ============================================================
class GridStrategy(Strategy):
    name = "grid"
    cn_name = "网格交易"
    description = "在近 N 日价格区间内分档：价格越低仓位越高（低吸高抛），适合震荡市。"
    lookback = 60
    grids = 5

    def _grid_position(self, d: pd.DataFrame) -> pd.Series:
        """根据价格在区间中的相对位置，给出 0~1 连续仓位（分档）。"""
        high_n = d["high"].rolling(self.lookback, min_periods=10).max()
        low_n = d["low"].rolling(self.lookback, min_periods=10).min()
        rng = (high_n - low_n).replace(0, np.nan)
        rel = (d["close"] - low_n) / rng  # 0(底)~1(顶)
        rel = rel.clip(0, 1).fillna(0.5)
        # 越低仓位越高，并量化为档位
        raw = 1 - rel
        pos = (np.floor(raw * self.grids) / self.grids).clip(0, 1)
        return pos

    def generate_positions(self, df: pd.DataFrame) -> pd.Series:
        d = self.prepare(df)
        return self._grid_position(d)

    def current_signal(self, df: pd.DataFrame) -> SignalResult:
        d = self.prepare(df)
        pos = self._grid_position(d)
        cur, prev = pos.iloc[-1], pos.iloc[-2] if len(pos) > 1 else pos.iloc[-1]
        level = int(round(cur * self.grids))
        if cur > prev:
            return SignalResult(self.cn_name, "BUY", f"价格回落至低档，加仓至{level}/{self.grids}档", 0.6)
        if cur < prev:
            return SignalResult(self.cn_name, "SELL", f"价格上行至高档，减仓至{level}/{self.grids}档", -0.6)
        return SignalResult(self.cn_name, "HOLD", f"维持{level}/{self.grids}档仓位", (cur - 0.5))


# ---------------- 注册表 ----------------
_STRATEGY_CLASSES = [
    MaCrossStrategy, MacdStrategy, KdjStrategy, BollStrategy,
    TrendFollowStrategy, RsiStrategy, BreakoutStrategy, VolumeBreakStrategy,
    ChanStrategy, GridStrategy,
]

STRATEGY_REGISTRY: dict[str, Strategy] = {cls.name: cls() for cls in _STRATEGY_CLASSES}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGY_REGISTRY:
        raise KeyError(f"未知策略: {name}，可用: {list(STRATEGY_REGISTRY)}")
    return STRATEGY_REGISTRY[name]


def list_strategies() -> list[dict]:
    return [
        {"name": s.name, "cn_name": s.cn_name, "description": s.description}
        for s in STRATEGY_REGISTRY.values()
    ]
