"""选股器：用量化策略对股票池打分排序，并对入选股做多维富化。

流程：
1. 全池快速打分（仅用 K线，速度快）
2. 排序取 Top N
3. 仅对 Top N 富化：板块、大单资金流、K线迷你图、估值、技术标签、详细理由
"""
from __future__ import annotations

import pandas as pd

from core.indicators import add_all_indicators, latest_snapshot
from data.fetcher import get_fetcher
from strategies.library import STRATEGY_REGISTRY


class StockSelector:
    def __init__(self):
        self.fetcher = get_fetcher()

    # ---------- 1. 快速打分 ----------
    def score_stock(self, symbol: str) -> dict | None:
        df = self.fetcher.get_kline(symbol, days=120)
        if df.empty or len(df) < 60:
            return None
        signals = []
        for strat in STRATEGY_REGISTRY.values():
            try:
                signals.append(strat.current_signal(df).to_dict())
            except Exception:
                pass
        if not signals:
            return None
        avg = sum(s["score"] for s in signals) / len(signals)
        buy = sum(1 for s in signals if s["signal"] == "BUY")
        sell = sum(1 for s in signals if s["signal"] == "SELL")
        last = df.iloc[-1]
        return {
            "symbol": symbol,
            "name": self.fetcher.get_name(symbol),
            "close": round(float(last["close"]), 2),
            "pct_change": round(float(last.get("pct_change", 0) or 0), 2),
            "score": round(avg, 3),
            "buy_signals": buy,
            "sell_signals": sell,
            "buy_strategies": [s["strategy"] for s in signals if s["signal"] == "BUY"],
            "buy_reasons": [f"{s['strategy']}：{s['reason']}" for s in signals if s["signal"] == "BUY"],
            "_df": df,  # 临时保留，富化后删除
        }

    # ---------- 2. 富化（仅对入选股）----------
    def enrich(self, item: dict) -> dict:
        symbol = item["symbol"]
        df = item.pop("_df", None)
        if df is None:
            df = self.fetcher.get_kline(symbol, days=120)

        # 技术快照 + 标签
        snap = latest_snapshot(df)
        d = add_all_indicators(df)
        last, prev = d.iloc[-1], d.iloc[-2] if len(d) > 1 else d.iloc[-1]
        tags = list(item.get("buy_strategies", []))
        if last["ma5"] > last["ma10"] > last["ma20"] > last["ma60"]:
            tags.append("均线多头排列")
        if last["volume"] > last["vol_ma5"] * 1.5:
            tags.append("放量")
        if last["rsi"] < 35:
            tags.append("超跌")
        if last["close"] > prev["close"]:
            tags.append("当日上涨")

        # 板块/估值（个股信息，best-effort）
        info = self.fetcher.get_basic_info(symbol)
        industry = str(info.get("行业") or info.get("所处行业") or "—")
        total_mv = info.get("总市值")
        try:
            total_mv = f"{float(total_mv)/1e8:.1f}亿" if total_mv else "—"
        except Exception:
            total_mv = "—"

        # 大单/主力资金流
        flow = self.fetcher.get_fund_flow(symbol)
        main_net = flow.get("主力净流入-净额") or flow.get("主力净流入") or "—"
        big_net = flow.get("大单净流入-净额") or flow.get("超大单净流入-净额") or "—"
        flow_date = flow.get("日期", "")

        def _fmt_money(v):
            try:
                v = float(v)
                sign = "+" if v >= 0 else ""
                return f"{sign}{v/1e8:.2f}亿" if abs(v) >= 1e8 else f"{sign}{v/1e4:.0f}万"
            except Exception:
                return str(v)

        # 52周位置
        hi52, lo52 = float(d["high"].max()), float(d["low"].min())
        pos52 = round((snap["close"] - lo52) / (hi52 - lo52) * 100, 0) if hi52 > lo52 else 50

        # K线迷你数据（近60日收盘 + 日期）
        tail = df.tail(60)
        kline_mini = {
            "dates": tail["date"].tolist(),
            "close": [round(float(x), 2) for x in tail["close"]],
            "candles": [[round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                        for o, c, l, h in zip(tail["open"], tail["close"], tail["low"], tail["high"])],
            "volume": [round(float(x), 0) for x in tail["volume"]],
        }

        # 详细理由（综合）
        reason = self._gen_reason(item, tags, snap)

        item.update({
            "industry": industry,
            "total_mv": total_mv,
            "turnover": snap.get("vol_ma5") and snap.get("volume"),
            "rsi": snap.get("rsi"),
            "ma_trend": "多头" if last["ma5"] > last["ma20"] else "空头",
            "main_net_inflow": _fmt_money(main_net) if main_net != "—" else "—",
            "big_order_net": _fmt_money(big_net) if big_net != "—" else "—",
            "flow_date": flow_date,
            "pos_52w": pos52,
            "tags": tags,
            "reason": reason,
            "kline_mini": kline_mini,
        })
        return item

    @staticmethod
    def _gen_reason(item: dict, tags: list, snap: dict) -> str:
        parts = []
        if item["buy_reasons"]:
            parts.append("触发买点：" + "；".join(item["buy_reasons"][:3]))
        feats = [t for t in tags if t in ("均线多头排列", "放量", "超跌")]
        if feats:
            parts.append("技术特征：" + "、".join(feats))
        parts.append(f"综合评分 {item['score']}（{item['buy_signals']}买/{item['sell_signals']}卖），"
                     f"RSI={snap.get('rsi')}")
        return "；".join(parts) + "。"

    # ---------- 3. 选股入口 ----------
    def select(self, pool: list[str], top_n: int = 10) -> list[dict]:
        scored = []
        for sym in pool:
            try:
                r = self.score_stock(sym)
                if r:
                    scored.append(r)
            except Exception:
                continue
        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:top_n]
        # 仅富化入选股
        enriched = []
        for item in top:
            try:
                enriched.append(self.enrich(item))
            except Exception:
                item.pop("_df", None)
                enriched.append(item)
        return enriched


# 默认候选池：部分主流活跃股（仅示例，可自行替换/扩展）
DEFAULT_POOL = [
    "600519", "000858", "601318", "600036", "000333",
    "002594", "300750", "600276", "000651", "601012",
    "600900", "002415", "000001", "600030", "601888",
    "300059", "002230", "600887", "601899", "000725",
]


def select_stocks(pool: list[str] | None = None, top_n: int = 10) -> list[dict]:
    return StockSelector().select(pool or DEFAULT_POOL, top_n)
