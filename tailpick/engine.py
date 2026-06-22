"""尾盘选股引擎。

策略目标：交易日 13:00-15:00 盘中选出适合尾盘买入、次日早盘卖出的短线候选股。
多 Agent 分工：候选池过滤 → 市场情绪 → 资金流 → 技术形态 → 风控 → 解释与交易计划。
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from core.indicators import add_all_indicators
from data.fetcher import get_fetcher
from recommend.engine import eligible, resolve_name


TAIL_START = dt.time(13, 0)
TAIL_FORMAL_START = dt.time(14, 0)
TAIL_CAUTION = dt.time(14, 55)
TAIL_END = dt.time(15, 0)


def tailpick_status(now: dt.datetime | None = None) -> dict:
    """返回尾盘选股按钮状态。"""
    now = now or dt.datetime.now()
    t = now.time()
    is_weekday = now.weekday() < 5
    enabled = is_weekday and TAIL_START <= t <= TAIL_END
    if not is_weekday:
        phase = "closed"
        reason = "当前不是交易日，尾盘选股仅在交易日 13:00-15:00 开放。"
    elif t < TAIL_START:
        phase = "before"
        reason = "尾盘选股尚未到开放时间，13:00 后可开始预览候选池。"
    elif t < TAIL_FORMAL_START:
        phase = "preview"
        reason = "当前可预览候选池，但 14:00 后资金稳定性更高，正式买入信号更可靠。"
    elif t < TAIL_CAUTION:
        phase = "formal"
        reason = "当前处于尾盘选股最佳窗口，可基于实时情绪、资金流与技术形态生成 Top5。"
    elif t <= TAIL_END:
        phase = "caution"
        reason = "已接近收盘，仍可生成但不建议追高新开仓，需重点防尾盘回落。"
    else:
        phase = "closed"
        reason = "今日尾盘选股窗口已结束，该策略用于 13:00-15:00 买入、次日早盘卖出。"
    return {
        "enabled": enabled,
        "phase": phase,
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "window": "13:00-15:00",
        "recommended_window": "14:00-14:55",
        "reason": reason,
    }


def _num(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        if math.isnan(v):
            return default
        return float(v)
    s = str(v).replace(",", "").replace("%", "").replace("元", "").strip()
    if not s or s in {"-", "--", "nan", "None"}:
        return default
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return default


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _col(df: pd.DataFrame, *names: str) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    for c in df.columns:
        if any(n in str(c) for n in names):
            return c
    return None


@dataclass
class TailPickConfig:
    count: int = 5
    max_pct: float = 6.0
    min_pct: float = -3.0
    min_amount_yi: float = 1.0
    min_turnover: float = 2.0
    min_volume_ratio: float = 0.8
    exclude_gem_star: bool = True
    only_mainline: bool = False


class UniverseFilterAgent:
    """候选池过滤：涨幅、流动性、换手、板块等硬条件。"""

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def run(self, cfg: TailPickConfig) -> list[dict]:
        spot = self.fetcher.get_market_spot()
        if spot is None or spot.empty:
            return []
        code_c = _col(spot, "代码", "symbol", "code")
        name_c = _col(spot, "名称", "name")
        pct_c = _col(spot, "涨跌幅")
        price_c = _col(spot, "最新价", "最新", "现价")
        turn_c = _col(spot, "换手率", "换手")
        vr_c = _col(spot, "量比")
        amount_c = _col(spot, "成交额", "成交金额")
        mv_c = _col(spot, "总市值")
        main_c = _col(spot, "主力净流入", "主力净额")
        main_ratio_c = _col(spot, "主力净占比", "主力净比")

        if not code_c or not pct_c:
            return []
        rows: list[dict] = []
        for _, r in spot.iterrows():
            code = str(r.get(code_c, "")).strip().replace("sh", "").replace("sz", "").zfill(6)
            name = str(r.get(name_c, "")) if name_c else code
            if not code.isdigit() or len(code) != 6:
                continue
            if cfg.exclude_gem_star and not eligible(code):
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            pct = _num(r.get(pct_c))
            if pct > cfg.max_pct or pct < cfg.min_pct or pct >= 9.5 or pct <= -9.5:
                continue
            amount = _num(r.get(amount_c)) if amount_c else 0.0
            turnover = _num(r.get(turn_c)) if turn_c else 0.0
            vr = _num(r.get(vr_c), 1.0) if vr_c else 1.0
            # 无成交额字段时不硬杀，避免备用源缺字段导致全空
            if amount_c and amount < cfg.min_amount_yi * 1e8:
                continue
            if turn_c and turnover < cfg.min_turnover:
                continue
            if vr_c and vr < cfg.min_volume_ratio:
                continue
            rows.append({
                "symbol": code,
                "name": name,
                "price": round(_num(r.get(price_c)), 2) if price_c else None,
                "pct_change": round(pct, 2),
                "amount": amount,
                "amount_yi": round(amount / 1e8, 2) if amount else None,
                "turnover": round(turnover, 2) if turnover else None,
                "volume_ratio": round(vr, 2) if vr else None,
                "total_mv_yi": round(_num(r.get(mv_c)) / 1e8, 1) if mv_c else None,
                "spot_main_net": _num(r.get(main_c)) if main_c else 0.0,
                "spot_main_ratio": _num(r.get(main_ratio_c)) if main_ratio_c else None,
            })
        # 预排序：温和涨幅 + 放量 + 活跃优先，减少后续逐只取数压力
        rows.sort(key=lambda x: ((x["pct_change"] if x["pct_change"] > 0 else -2),
                                 x.get("volume_ratio") or 1,
                                 x.get("turnover") or 0,
                                 x.get("amount_yi") or 0), reverse=True)
        return rows[:50]


class MarketSentimentAgent:
    """市场情绪与题材热度 Agent（权重最高）。"""

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def run(self) -> dict:
        breadth = self.fetcher.get_market_breadth() or {}
        indices = self.fetcher.get_index_spot() or []
        # 尾盘场景优先使用实时行业涨幅榜，避免近一周历史榜逐板块取数导致盘中响应过慢
        sectors = self.fetcher.get_hot_sectors(limit=10) or []

        up = breadth.get("up") or 0
        down = breadth.get("down") or 0
        total = max(up + down + (breadth.get("flat") or 0), 1)
        up_ratio = up / total
        avg_pct = _num(breadth.get("avg_pct"))
        limit_up = breadth.get("limit_up") or 0
        limit_down = breadth.get("limit_down") or 0
        idx_pct = [i.get("pct") for i in indices if isinstance(i.get("pct"), (int, float))]
        idx_avg = sum(idx_pct) / len(idx_pct) if idx_pct else 0

        trend_score = _clip(50 + idx_avg * 12)
        breadth_score = _clip(up_ratio * 100 + avg_pct * 8)
        limit_score = _clip(50 + (limit_up - limit_down * 2) * 0.8)
        sector_score = _clip(50 + max([_num(s.get("week_pct", s.get("pct", 0))) for s in sectors] or [0]) * 5)
        score = round(trend_score * 0.3 + breadth_score * 0.25 + limit_score * 0.2 + sector_score * 0.25, 1)
        temp = "强" if score >= 75 else "中强" if score >= 60 else "偏弱" if score >= 45 else "弱"
        return {
            "score": score,
            "temperature": temp,
            "breadth": breadth,
            "indices": indices,
            "main_themes": sectors[:8],
            "note": f"指数均值{idx_avg:+.2f}%，上涨占比{up_ratio*100:.0f}%，涨停{limit_up}/跌停{limit_down}，市场情绪{temp}。",
        }

    def build_sector_map(self, sectors: list[dict], limit: int = 8) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for rank, s in enumerate(sectors[:limit], 1):
            name = s.get("name") or s.get("sector")
            if not name:
                continue
            cons = self.fetcher.get_board_cons(name, s.get("type", "行业"))
            for c in cons:
                code = str(c.get("代码") or "").zfill(6)
                if code and code not in out:
                    out[code] = {"sector": name, "rank": rank,
                                 "week_pct": s.get("week_pct", s.get("pct")),
                                 "day_pct": s.get("day_pct", s.get("pct")),
                                 "leader": s.get("leader", "")}
        return out


class FundFlowAgent:
    """资金流 Agent（权重第二）。"""

    def __init__(self, fetcher):
        self.fetcher = fetcher

    @staticmethod
    def _parse_flow(flow: dict) -> dict:
        def pick(*keys):
            for k in flow:
                sk = str(k)
                if all(x in sk for x in keys):
                    return _num(flow.get(k))
            return 0.0
        main = pick("主力", "净流入") or pick("主力", "净额")
        big = pick("大单", "净流入") or pick("大单", "净额")
        super_big = pick("超大单", "净流入") or pick("超大单", "净额")
        return {"main_net": main, "big_net": big, "super_big_net": super_big}

    def run(self, item: dict) -> dict:
        main = item.get("spot_main_net") or 0.0
        big = super_big = 0.0
        raw = {}
        # 盘中接口要求响应快：优先使用全市场快照里的 f62/f184 资金字段；
        # 若备用行情源缺失该字段，不逐只调用慢速资金接口，按中性偏弱处理。
        if not main:
            raw = {}
            main, big, super_big = 0.0, 0.0, 0.0
        amount = item.get("amount") or 0.0
        ratio = (main / amount * 100) if amount else (item.get("spot_main_ratio") or 0.0)
        main_score = _clip(50 + main / 1e8 * 18)
        big_score = _clip(50 + (big + super_big) / 1e8 * 15)
        ratio_score = _clip(50 + ratio * 5)
        accel_score = 65 if main > 0 and (item.get("pct_change") or 0) > 0 else 45
        score = round(main_score * 0.35 + big_score * 0.25 + accel_score * 0.2 + ratio_score * 0.2, 1)
        return {
            "score": score,
            "main_net": round(main / 1e8, 2),
            "big_net": round(big / 1e8, 2),
            "super_big_net": round(super_big / 1e8, 2),
            "inflow_ratio": round(ratio, 2) if ratio else None,
            "note": "主力净流入" if main > 0 else "主力净流出/数据不足",
            "raw": raw,
        }


class TechnicalAgent:
    """短线技术 Agent（权重第三）。"""

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def run(self, item: dict) -> dict:
        df = self.fetcher.get_kline(item["symbol"], days=80)
        if df is None or df.empty or len(df) < 30:
            return {"score": 50, "note": "K线数据不足", "kline_mini": None}
        d = add_all_indicators(df).fillna(0)
        last = d.iloc[-1]
        close = float(last.get("close", 0) or 0)
        ma5, ma10, ma20 = float(last.get("ma5", 0)), float(last.get("ma10", 0)), float(last.get("ma20", 0))
        rsi = float(last.get("rsi", 50) or 50)
        macd = float(last.get("macd", 0) or 0)
        vol = float(last.get("volume", 0) or 0)
        vol_ma5 = float(d["volume"].tail(6).head(5).mean() or 0) if "volume" in d else 0
        vol_ratio = vol / vol_ma5 if vol_ma5 else (item.get("volume_ratio") or 1)
        high20 = float(d["high"].tail(20).max() or close)
        pos20 = close / high20 if high20 else 0

        trend = 80 if close > ma5 > ma10 > ma20 else 65 if close > ma5 > ma10 else 48
        volume = _clip(45 + min(vol_ratio, 3) * 18)
        momentum = 75 if 0.97 <= pos20 <= 1.01 else 62 if pos20 > 0.92 else 50
        rsi_score = 75 if 45 <= rsi <= 72 else 55 if rsi < 80 else 35
        macd_score = 68 if macd > 0 else 50
        score = round(trend * 0.25 + volume * 0.2 + momentum * 0.2 + rsi_score * 0.2 + macd_score * 0.15, 1)

        k = d.tail(30)
        kmini = {
            "dates": k["date"].tolist(),
            "candles": [[round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                        for o, c, l, h in zip(k["open"], k["close"], k["low"], k["high"])],
        }
        notes = []
        if close > ma5 > ma10:
            notes.append("站上短期均线")
        if vol_ratio >= 1.2:
            notes.append("温和放量")
        if pos20 > 0.92:
            notes.append("接近阶段新高")
        if rsi > 78:
            notes.append("RSI偏热")
        return {"score": score, "rsi": round(rsi, 1), "vol_ratio": round(vol_ratio, 2),
                "ma_state": "多头" if close > ma5 > ma10 > ma20 else "偏强" if close > ma5 else "一般",
                "note": "、".join(notes) or "形态中性", "kline_mini": kmini}


class RiskControlAgent:
    """风控 Agent：尾盘冲高、弱势环境、资金背离等扣分。"""

    def run(self, item: dict, market: dict, fund: dict, tech: dict) -> dict:
        flags = []
        penalty = 0.0
        pct = item.get("pct_change") or 0
        if pct > 5.5:
            penalty += 8
            flags.append("涨幅接近6%上限，追高风险偏高")
        if market.get("score", 50) < 50:
            penalty += 8
            flags.append("市场情绪偏弱")
        if fund.get("main_net", 0) <= 0:
            penalty += 10
            flags.append("主力资金未明显净流入")
        if tech.get("rsi", 50) > 78:
            penalty += 6
            flags.append("短线RSI过热")
        can_buy = penalty < 18
        return {"penalty": round(penalty, 1), "flags": flags, "can_buy": can_buy}


class ExplainAgent:
    """解释与交易计划 Agent。"""

    @staticmethod
    def run(item: dict, market: dict, fund: dict, tech: dict, risk: dict) -> dict:
        sector = item.get("hot_sector", {}).get("sector") or item.get("industry") or "非主线"
        reason = (
            f"属于{sector}方向，市场情绪{market.get('temperature')}；"
            f"资金评分{fund['score']}（主力净流入{fund['main_net']}亿，净流入占比{fund.get('inflow_ratio') or '—'}%）；"
            f"技术评分{tech['score']}（{tech.get('note','形态中性')}）。"
        )
        if risk.get("flags"):
            reason += " 风险提示：" + "；".join(risk["flags"]) + "。"
        buy_plan = "14:30 后若仍站稳分时均价线且板块不退潮，可小仓试买；不追快速直线拉升。"
        sell_plan = "次日早盘高开2%-4%优先分批止盈；若低开且10分钟内无法翻红，控制风险。"
        stop_loss = "跌破尾盘均价线/买入价约2%或板块龙头明显走弱时止损。"
        return {"reason": reason, "buy_plan": buy_plan, "sell_plan": sell_plan, "stop_loss": stop_loss}


class TailPickEngine:
    def __init__(self):
        self.fetcher = get_fetcher()
        self.universe = UniverseFilterAgent(self.fetcher)
        self.market_agent = MarketSentimentAgent(self.fetcher)
        self.fund_agent = FundFlowAgent(self.fetcher)
        self.tech_agent = TechnicalAgent(self.fetcher)
        self.risk_agent = RiskControlAgent()
        self.explain_agent = ExplainAgent()

    @staticmethod
    def _fund_chart(picks: list[dict]) -> dict:
        return {
            "symbols": [f"{p['name']}\n{p['symbol']}" for p in picks],
            "series": [
                {"name": "主力净流入", "data": [p["fund"]["main_net"] for p in picks]},
                {"name": "大单净流入", "data": [p["fund"].get("big_net", 0) for p in picks]},
                {"name": "超大单净流入", "data": [p["fund"].get("super_big_net", 0) for p in picks]},
            ],
            "ratio": [p["fund"].get("inflow_ratio") for p in picks],
            "unit": "亿元",
        }

    def run(self, count: int = 5, max_pct: float = 6.0, force: bool = False,
            min_amount_yi: float = 1.0, min_turnover: float = 2.0,
            only_mainline: bool = False) -> dict:
        st = tailpick_status()
        if not force and not st["enabled"]:
            return {"error": st["reason"], "status": st}

        # 尾盘选股依赖盘中实时数据：强制清除全市场快照/指数/板块等关键缓存，
        # 确保每次点击都重新拉取最新行情、资金流与题材热度。
        from data.cache import invalidate_cache
        # 全市场快照
        invalidate_cache("spot:all:v2")
        invalidate_cache("spot:all:v3")
        invalidate_cache("ts_spot_all")
        invalidate_cache("as_spot_all")          # A-Stock 腾讯全市场快照
        # 指数
        invalidate_cache("index_spot")
        invalidate_cache("global_indices")
        # 板块/主线（之前遗漏，导致尾盘选股可能用旧缓存）
        invalidate_cache("as_sectors:10")        # A-Stock 板块涨幅榜
        invalidate_cache("as_sectors:5")         # 小数量变体
        invalidate_cache("as_sectors_snapshot")  # 自聚合板块兜底
        invalidate_cache("sectors:10")           # Fetcher AkShare 板块
        invalidate_cache("as_sectors_week:12")   # 近一周主线板块

        cfg = TailPickConfig(count=count, max_pct=max_pct,
                             min_amount_yi=min_amount_yi, min_turnover=min_turnover,
                             only_mainline=only_mainline)
        market = self.market_agent.run()
        sector_map = self.market_agent.build_sector_map(market.get("main_themes") or [], limit=4) if only_mainline else {}
        candidates = self.universe.run(cfg)
        out: list[dict] = []
        # 盘中响应优先：候选池已按涨幅/量比/换手/成交额预排序，只对前若干只做逐股K线技术确认
        scan_limit = max(12, min(25, count * 5))
        for item in candidates[:scan_limit]:
            item["name"] = item.get("name") or resolve_name(self.fetcher, item["symbol"])
            if item["symbol"] in sector_map:
                item["hot_sector"] = sector_map[item["symbol"]]
                item["industry"] = item["hot_sector"]["sector"]
            elif only_mainline:
                continue
            fund = self.fund_agent.run(item)
            # 资金是第二权重，明显净流出且分数低则跳过，减少低质量候选
            if fund["score"] < 42 and fund.get("main_net", 0) <= 0:
                continue
            tech = self.tech_agent.run(item)
            risk = self.risk_agent.run(item, market, fund, tech)
            if not risk["can_buy"]:
                continue

            theme_score = 88 if item.get("hot_sector") else 58
            market_stock_score = market["score"] * 0.7 + theme_score * 0.3
            final = round(market_stock_score * 0.40 + fund["score"] * 0.35 + tech["score"] * 0.20 - risk["penalty"] + (5 if item.get("hot_sector") else 0), 1)
            exp = self.explain_agent.run(item, market, fund, tech, risk)
            out.append({
                **item,
                "score": final,
                "market_score": round(market_stock_score, 1),
                "fund_score": fund["score"],
                "tech_score": tech["score"],
                "risk_penalty": risk["penalty"],
                "risk_flags": risk["flags"],
                "fund": fund,
                "tech": {k: v for k, v in tech.items() if k != "kline_mini"},
                "kline_mini": tech.get("kline_mini"),
                **exp,
            })

        out.sort(key=lambda x: x["score"], reverse=True)
        picks = out[:count]
        source = getattr(self.fetcher, "last_market_spot_source", "未知")
        return {
            "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "strategy": "今日13:00-15:00尾盘买入，明日早盘卖出",
            "data_source": source,
            "data_source_tip": f"本次尾盘选股行情快照来源：{source}。盘中优先东财实时行情，收盘后优先Tushare日级快照。",
            "status": st,
            "market": market,
            "weights": {"market": 0.40, "fund": 0.35, "technical": 0.20, "risk_penalty": "0~20"},
            "filters": {"max_pct": max_pct, "min_amount_yi": min_amount_yi, "min_turnover": min_turnover,
                        "exclude_gem_star": True, "only_mainline": only_mainline},
            "candidates_count": len(candidates),
            "picks": picks,
            "fund_chart": self._fund_chart(picks),
        }
