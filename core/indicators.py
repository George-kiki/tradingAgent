"""技术指标库（纯 pandas/numpy 实现，无需 TA-Lib）。

输入约定：传入的 DataFrame 至少包含列 ['open','high','low','close','volume']，
按日期升序排列。所有函数返回与输入等长的 Series/DataFrame。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    """简单移动平均。"""
    return series.rolling(window=window, min_periods=1).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    """指数移动平均。"""
    return series.ewm(span=window, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD：返回 (dif, dea, macd_hist)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """相对强弱指标 RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9, m1: int = 3, m2: int = 3):
    """KDJ 指标：返回 (k, d, j)。"""
    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def boll(close: pd.Series, window: int = 20, num_std: float = 2.0):
    """布林带：返回 (mid, upper, lower)。"""
    mid = sma(close, window)
    std = close.rolling(window=window, min_periods=1).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """平均真实波幅 ATR（衡量波动率，用于止损）。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / window, adjust=False).mean()


def momentum(close: pd.Series, window: int = 10) -> pd.Series:
    """动量（涨跌幅百分比）。"""
    return close.pct_change(periods=window) * 100


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """一次性附加常用指标列，返回新 DataFrame。"""
    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]

    out["ma5"] = sma(close, 5)
    out["ma10"] = sma(close, 10)
    out["ma20"] = sma(close, 20)
    out["ma60"] = sma(close, 60)

    out["dif"], out["dea"], out["macd"] = macd(close)
    out["rsi"] = rsi(close)
    out["k"], out["d"], out["j"] = kdj(high, low, close)
    out["boll_mid"], out["boll_up"], out["boll_low"] = boll(close)
    out["atr"] = atr(high, low, close)
    out["mom"] = momentum(close)
    out["vol_ma5"] = sma(out["volume"], 5)
    return out


def latest_snapshot(df: pd.DataFrame) -> dict:
    """提取最新一根 K 线的指标快照，供 Agent / 报告使用。"""
    d = add_all_indicators(df)
    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else last

    def f(x):
        return round(float(x), 3) if pd.notna(x) else None

    return {
        "date": str(last.get("date", "")),
        "close": f(last["close"]),
        "change_pct": f((last["close"] / prev["close"] - 1) * 100) if prev["close"] else None,
        "ma5": f(last["ma5"]),
        "ma10": f(last["ma10"]),
        "ma20": f(last["ma20"]),
        "ma60": f(last["ma60"]),
        "macd_dif": f(last["dif"]),
        "macd_dea": f(last["dea"]),
        "macd_hist": f(last["macd"]),
        "rsi": f(last["rsi"]),
        "kdj_k": f(last["k"]),
        "kdj_d": f(last["d"]),
        "kdj_j": f(last["j"]),
        "boll_up": f(last["boll_up"]),
        "boll_low": f(last["boll_low"]),
        "atr": f(last["atr"]),
        "momentum": f(last["mom"]),
        "volume": f(last["volume"]),
        "vol_ma5": f(last["vol_ma5"]),
    }
