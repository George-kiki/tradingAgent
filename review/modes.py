"""复盘模式：可插拔、可自定义。

每个模式继承 ReviewMode，实现 run() 返回 (markdown文本, 写入共享facts的数据)。
通过 register_mode 注册即可在 config/review.json 的 enabled_modes 中按 name 启用。
用户可自行新增模式类并注册，实现高度自定义。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from core.indicators import latest_snapshot
from strategies.library import STRATEGY_REGISTRY


class ReviewMode(ABC):
    name: str = "base"
    cn_name: str = "复盘模块"

    @abstractmethod
    def run(self, ctx: "ReviewContext") -> str:
        """返回该模块的 Markdown 文本。"""


class ReviewContext:
    """复盘共享上下文，模式之间可读写 facts，供 AI 总结使用。"""

    def __init__(self, fetcher, options: dict, use_llm: set[str], llm=None):
        self.fetcher = fetcher
        self.options = options or {}
        self.use_llm = use_llm or set()
        self.llm = llm
        self.facts: list[str] = []  # 累积的事实，供 AI 总结

    def opt(self, mode: str, key: str, default=None):
        return self.options.get(mode, {}).get(key, default)

    def add_fact(self, text: str):
        self.facts.append(text)


# ---------------- 1. 大盘指数复盘 ----------------
class MarketReviewMode(ReviewMode):
    name = "market"
    cn_name = "大盘指数"

    def run(self, ctx: ReviewContext) -> str:
        indices = ctx.fetcher.get_index_spot()
        if not indices:
            return "## 📉 大盘指数\n\n（暂无指数数据）\n"
        lines = ["## 📉 大盘指数", "", "| 指数 | 最新 | 涨跌幅 |", "| --- | --- | --- |"]
        for idx in indices:
            arrow = "🔴" if (idx.get("pct") or 0) >= 0 else "🟢"
            lines.append(f"| {idx['name']} | {idx.get('price','-')} | {arrow} {idx.get('pct')}% |")
            ctx.add_fact(f"{idx['name']}涨跌{idx.get('pct')}%")
        lines.append("")

        if self.name in ctx.use_llm and ctx.llm and ctx.llm.available:
            facts = "；".join(ctx.facts)
            comment = ctx.llm.chat(
                "你是A股市场策略师，简明点评今日大盘走势，2-3句话。",
                f"今日主要指数表现：{facts}。请点评今日大盘强弱与风格。",
                max_tokens=300,
            )
            lines += ["**策略师点评：**", comment, ""]
        return "\n".join(lines)


# ---------------- 2. 市场广度复盘 ----------------
class BreadthReviewMode(ReviewMode):
    name = "breadth"
    cn_name = "市场广度"

    def run(self, ctx: ReviewContext) -> str:
        b = ctx.fetcher.get_market_breadth()
        if not b:
            return "## 📊 市场广度\n\n（暂无数据）\n"
        ctx.add_fact(f"涨{b['up']}家/跌{b['down']}家，涨停{b['limit_up']}家，平均涨幅{b['avg_pct']}%")
        sentiment = "偏强" if b["up"] > b["down"] else "偏弱"
        return (
            "## 📊 市场广度\n\n"
            f"- 上涨 **{b['up']}** 家 / 下跌 **{b['down']}** 家 / 平盘 {b['flat']} 家\n"
            f"- 涨停 **{b['limit_up']}** 家 / 跌停 **{b['limit_down']}** 家\n"
            f"- 全市场平均涨幅 **{b['avg_pct']}%**（中位数 {b['median_pct']}%）\n"
            f"- 市场情绪：**{sentiment}**\n"
        )


# ---------------- 3. 热点板块复盘 ----------------
class HotspotReviewMode(ReviewMode):
    name = "hotspot"
    cn_name = "热点板块"

    def run(self, ctx: ReviewContext) -> str:
        top_n = ctx.opt(self.name, "top_n", 8)
        sectors = ctx.fetcher.get_hot_sectors(limit=top_n)
        if not sectors:
            return "## 🔥 热点板块\n\n（暂无数据）\n"
        ctx.add_fact("领涨板块：" + "、".join(s["name"] for s in sectors[:3]))
        lines = ["## 🔥 热点板块（涨幅榜）", "", "| 板块 | 涨跌幅 | 领涨股 |", "| --- | --- | --- |"]
        for s in sectors:
            lines.append(f"| {s['name']} | {s['pct']}% | {s.get('leader','')} |")
        lines.append("")
        return "\n".join(lines)


# ---------------- 4. 自选股复盘 ----------------
class WatchlistReviewMode(ReviewMode):
    name = "watchlist"
    cn_name = "自选股"

    def run(self, ctx: ReviewContext) -> str:
        from config import get_watchlist
        symbols = get_watchlist()
        if not symbols:
            return "## ⭐ 自选股复盘\n\n（未配置自选股，请编辑 config/watchlist.json）\n"
        show_sig = ctx.opt(self.name, "show_signals", True)
        lines = ["## ⭐ 自选股复盘", "", "| 代码 | 名称 | 现价 | 涨跌 | 综合信号 |", "| --- | --- | --- | --- | --- |"]
        for sym in symbols:
            try:
                df = ctx.fetcher.get_kline(sym, days=120)
                if df.empty:
                    continue
                name = ctx.fetcher.get_name(sym)
                snap = latest_snapshot(df)
                sig_txt = "-"
                if show_sig:
                    scores = [s.current_signal(df).score for s in STRATEGY_REGISTRY.values()]
                    avg = sum(scores) / len(scores)
                    sig_txt = "偏多" if avg > 0.15 else "偏空" if avg < -0.15 else "中性"
                arrow = "🔴" if (snap.get("change_pct") or 0) >= 0 else "🟢"
                lines.append(f"| {sym} | {name} | {snap.get('close')} | {arrow}{snap.get('change_pct')}% | {sig_txt} |")
            except Exception:
                continue
        lines.append("")
        return "\n".join(lines)


# ---------------- 5. 持仓复盘 ----------------
class HoldingsReviewMode(ReviewMode):
    name = "holdings"
    cn_name = "持仓"

    def run(self, ctx: ReviewContext) -> str:
        from config import get_holdings
        holdings = get_holdings()
        if not holdings:
            return "## 💼 持仓复盘\n\n（未配置持仓，请编辑 config/watchlist.json）\n"
        lines = ["## 💼 持仓复盘", "", "| 代码 | 名称 | 成本 | 现价 | 盈亏% | 市值 |", "| --- | --- | --- | --- | --- | --- |"]
        total_cost = total_value = 0.0
        for h in holdings:
            try:
                sym = h["symbol"]
                df = ctx.fetcher.get_kline(sym, days=30)
                if df.empty:
                    continue
                price = float(df["close"].iloc[-1])
                cost = float(h.get("cost", 0))
                shares = float(h.get("shares", 0))
                pnl = (price / cost - 1) * 100 if cost else 0
                value = price * shares
                total_cost += cost * shares
                total_value += value
                arrow = "🔴" if pnl >= 0 else "🟢"
                lines.append(f"| {sym} | {ctx.fetcher.get_name(sym)} | {cost} | {round(price,2)} | "
                             f"{arrow}{round(pnl,2)}% | {round(value,0)} |")
            except Exception:
                continue
        if total_cost:
            total_pnl = (total_value / total_cost - 1) * 100
            ctx.add_fact(f"持仓总盈亏{round(total_pnl,2)}%")
            lines.append(f"\n**持仓总市值 {round(total_value,0)}，总盈亏 {round(total_pnl,2)}%**\n")
        return "\n".join(lines)


# ---------------- 6. AI 综合总结（自定义 prompt）----------------
class AiSummaryMode(ReviewMode):
    name = "ai_summary"
    cn_name = "AI综合总结"

    def run(self, ctx: ReviewContext) -> str:
        if not (ctx.llm and ctx.llm.available):
            return "## 🤖 AI 综合总结\n\n（未启用 LLM，配置 DEEPSEEK_API_KEY 后可用）\n"
        prompt = ctx.opt(self.name, "custom_prompt",
                         "请基于今日市场数据，总结盘面特征与明日操作建议。")
        facts = "\n".join(f"- {x}" for x in ctx.facts)
        content = ctx.llm.chat(
            "你是资深A股首席策略师，复盘客观专业，给出可执行建议。仅供研究，不构成投资建议。",
            f"今日市场关键数据：\n{facts}\n\n{prompt}",
            max_tokens=800,
        )
        return f"## 🤖 AI 综合总结\n\n{content}\n"


# ---------------- 注册表 ----------------
REVIEW_MODES: dict[str, type[ReviewMode]] = {}


def register_mode(cls: type[ReviewMode]):
    REVIEW_MODES[cls.name] = cls
    return cls


for _cls in [MarketReviewMode, BreadthReviewMode, HotspotReviewMode,
             WatchlistReviewMode, HoldingsReviewMode, AiSummaryMode]:
    register_mode(_cls)
