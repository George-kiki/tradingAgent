"""每日荐股引擎：结算 → 反思 → 选股 → 持久化 全流程。

== 基于昨日数据的选股指标与策略 ==
对候选池中每只（已过滤科创板 688 与创业板 300/301）个股，使用截至前一日(base_date)
的 K线，综合以下信号给出加权评分：
1. 多策略共识：复用 10 个交易策略的 current_signal，按"动态权重"加权综合（权重由反思迭代）。
2. 趋势：均线多头排列(ma5>ma10>ma20) 加分。
3. 量能：温和放量(1.2~3倍) 加分；异常巨量(>4倍) 减分。
4. 强弱：RSI 超跌(<35) 小幅加分；超买(>78) 明显减分（防追高）。
5. 位置：52周价格位置过高(>90%) 减分。
6. 动量：近5日涨幅过大(>18%) 减分（防追高）。
再叠加"约束条件(filters)"做硬过滤（RSI上限/位置上限/最低分/资金流入/板块上限/大盘择时）。

整个流程每次运行：先结算历史推荐 → 计算昨日胜率 → 不达标则反思并自动调整策略权重与约束
→ 在新约束下选股 → 落库。
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Optional

import pandas as pd

from core.config import settings
from core.indicators import add_all_indicators
from data.fetcher import get_fetcher
from recommend.database import RecommendDB
from recommend.llm_review import llm_review_candidates
from recommend.reflection import default_filters, default_weights, reflect
from recommend.winrate import settle_all_pending
from strategies.library import STRATEGY_REGISTRY


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


# 趋势门槛（防逆势/背离选股）。可经 .env 调节激进/保守程度。
REQUIRE_UPTREND = _env_bool("REC_REQUIRE_UPTREND", True)   # 启用趋势硬过滤
MIN_MOM5 = _env_float("REC_MIN_MOM5", -3.0)                # 5日动量低于此值且破20日线 -> 淘汰
STRICT_NO_FILL = _env_bool("REC_STRICT_NO_FILL", True)     # 宁缺毋滥：补位也须顺势，不足即少推
REQUIRE_INFLOW = _env_bool("REC_REQUIRE_INFLOW", False)    # 资金流硬筛：主力净流出的票直接剔除


# 候选池：沪深主板活跃股（科创/创业板会被 eligible 过滤）+ 静态名称兜底
# 内置名称表确保即便数据源限流，推荐卡片也始终能显示中文名称
POOL_NAMES = {
    "600519": "贵州茅台", "600036": "招商银行", "601318": "中国平安", "600276": "恒瑞医药",
    "600900": "长江电力", "601012": "隆基绿能", "600887": "伊利股份", "601888": "中国中免",
    "600030": "中信证券", "601899": "紫金矿业", "600028": "中国石化", "601166": "兴业银行",
    "600000": "浦发银行", "600585": "海螺水泥", "600309": "万华化学", "601398": "工商银行",
    "601668": "中国建筑", "600048": "保利发展", "600104": "上汽集团", "603259": "药明康德",
    "603288": "海天味业", "600436": "片仔癀", "601088": "中国神华", "600050": "中国联通",
    "601628": "中国人寿", "600009": "上海机场", "600406": "国电南瑞", "601857": "中国石油",
    "000858": "五粮液", "000333": "美的集团", "000651": "格力电器", "000001": "平安银行",
    "002594": "比亚迪", "000725": "京东方A", "002415": "海康威视", "002230": "科大讯飞",
    "000002": "万科A", "000063": "中兴通讯", "002304": "洋河股份", "002142": "宁波银行",
    "000568": "泸州老窖", "002352": "顺丰控股", "600196": "复星医药", "601225": "陕西煤业",
    "600660": "福耀玻璃",
}

RECOMMEND_POOL = list(POOL_NAMES.keys())

# 候选池静态行业映射（贴近东财行业板块命名），用于行业景气度匹配的兜底
POOL_INDUSTRY = {
    "600519": "酿酒行业", "600036": "银行", "601318": "保险", "600276": "化学制药",
    "600900": "电力行业", "601012": "光伏设备", "600887": "食品饮料", "601888": "旅游酒店",
    "600030": "证券", "601899": "有色金属", "600028": "石油行业", "601166": "银行",
    "600000": "银行", "600585": "水泥建材", "600309": "化学原料", "601398": "银行",
    "601668": "工程建设", "600048": "房地产开发", "600104": "汽车整车", "603259": "医疗服务",
    "603288": "食品饮料", "600436": "中药", "601088": "煤炭行业", "600050": "通信服务",
    "601628": "保险", "600009": "航空机场", "600406": "电网设备", "601857": "石油行业",
    "000858": "酿酒行业", "000333": "家电行业", "000651": "家电行业", "000001": "银行",
    "002594": "汽车整车", "000725": "光学光电子", "002415": "安防设备", "002230": "软件开发",
    "000002": "房地产开发", "000063": "通信设备", "002304": "酿酒行业", "002142": "银行",
    "000568": "酿酒行业", "002352": "物流行业", "600196": "化学制药", "601225": "煤炭行业",
    "600660": "汽车零部件",
}


def resolve_name(fetcher, symbol: str) -> str:
    """名称解析：优先在线获取，失败则用静态兜底表，最终回退代码。"""
    try:
        nm = fetcher.get_name(symbol)
        if nm and nm != symbol:
            return nm
    except Exception:
        pass
    return POOL_NAMES.get(symbol, symbol)


def eligible(symbol: str) -> bool:
    """过滤：剔除科创板(688)、创业板(300/301)、北交所(8x/4x)、B股(9x)。"""
    s = symbol.strip()
    if len(s) != 6 or not s.isdigit():
        return False
    if s.startswith("688"):       # 科创板
        return False
    if s.startswith(("300", "301")):  # 创业板
        return False
    if s.startswith(("8", "4")):  # 北交所
        return False
    if s.startswith("9"):         # B股
        return False
    # 仅保留沪深主板/中小板
    return s.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


class RecommendEngine:
    def __init__(self, db: Optional[RecommendDB] = None):
        self.fetcher = get_fetcher()
        self.db = db or RecommendDB()
        self._sector_map: dict[str, dict] = {}  # symbol -> 所属热门板块信息（run 时构建）

    # ------------------------------------------------------------------
    # 交易日定位
    # ------------------------------------------------------------------
    def _trading_dates(self, ref: str = "600519", days: int = 400) -> list[str]:
        df = self.fetcher.get_kline(ref, days=days)
        if df is None or df.empty:
            return []
        return df["date"].tolist()

    def _resolve_base_date(self, as_of: Optional[str]) -> Optional[str]:
        dates = self._trading_dates()
        if not dates:
            return None
        if as_of:
            cand = [d for d in dates if d <= as_of]
            return cand[-1] if cand else None
        return dates[-1]

    # ------------------------------------------------------------------
    # 斐波那契黄金分割回撤
    # ------------------------------------------------------------------
    @staticmethod
    def _fib_retracement(d, lookback: int = 90, tol: float = 0.02) -> dict:
        """计算最近一段上涨的斐波那契回撤位，判断当前价是否回踩到黄金分割位。

        方法：在最近 lookback 日内找显著摆动低点(swing low)与其后的摆动高点(swing high)，
        以这段涨幅为基准计算回撤位：
            level(r) = high - (high - low) * r,  r ∈ {0.236, 0.382, 0.5, 0.618, 0.786}
        当前收盘价落在某回撤位 ±tol（默认2%）区间内即视为"回踩该位"。
        50% 是最受关注的强支撑/回调买点。

        返回 {swing_low, swing_high, up_pct, retrace_pct, levels{...},
              at_382, at_50, at_618, nearest, on_uptrend}。数据不足返回 {}。
        """
        try:
            if d is None or len(d) < 20:
                return {}
            seg = d.tail(lookback).reset_index(drop=True)
            highs = seg["high"].astype(float)
            lows = seg["low"].astype(float)
            close = float(seg["close"].iloc[-1])

            # 摆动高点 = 区间内最高价位置；摆动低点 = 高点之前的最低价（确保是"先低后高"的上涨段）
            hi_idx = int(highs.idxmax())
            swing_high = float(highs.iloc[hi_idx])
            if hi_idx <= 0:
                return {}
            lo_idx = int(lows.iloc[:hi_idx + 1].idxmin())
            swing_low = float(lows.iloc[lo_idx])
            rng = swing_high - swing_low
            if rng <= 0 or swing_low <= 0:
                return {}

            up_pct = round((swing_high / swing_low - 1) * 100, 2)
            # 涨幅太小不构成有效摆动（噪声），不计黄金回撤
            if up_pct < 8:
                return {"up_pct": up_pct, "valid": False}

            ratios = {"0.236": 0.236, "0.382": 0.382, "0.5": 0.5, "0.618": 0.618, "0.786": 0.786}
            levels = {k: round(swing_high - rng * r, 2) for k, r in ratios.items()}

            # 当前价相对该段的回撤比例（0=在高点，1=回到低点）
            retrace = (swing_high - close) / rng
            retrace = max(0.0, min(1.5, retrace))

            def _near(ratio):
                lvl = swing_high - rng * ratio
                return abs(close - lvl) / close <= tol

            at_382, at_50, at_618 = _near(0.382), _near(0.5), _near(0.618)
            # 最接近的黄金位
            nearest = min(ratios.items(), key=lambda kv: abs(retrace - kv[1]))[0]
            on_uptrend = bool(swing_low < swing_high and hi_idx > lo_idx)

            return {
                "valid": True,
                "swing_low": round(swing_low, 2),
                "swing_high": round(swing_high, 2),
                "up_pct": up_pct,
                "retrace_pct": round(retrace * 100, 1),
                "levels": levels,
                "fib_50": levels["0.5"],
                "at_382": at_382,
                "at_50": at_50,
                "at_618": at_618,
                "nearest": nearest,
                "on_uptrend": on_uptrend,
            }
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 单股评分（基于截至 base_date 的数据）
    # ------------------------------------------------------------------
    def _score_stock(self, symbol: str, base_date: str, weights: dict, filters: dict) -> Optional[dict]:
        df_full = self.fetcher.get_kline(symbol, days=400)
        if df_full is None or df_full.empty:
            return None
        df = df_full[df_full["date"] <= base_date].reset_index(drop=True)
        if len(df) < 60:
            return None

        # 多策略加权
        signals = {}
        for name, strat in STRATEGY_REGISTRY.items():
            try:
                signals[name] = strat.current_signal(df).to_dict()
            except Exception:
                continue
        if not signals:
            return None
        num = sum(weights.get(n, 1.0) * s["score"] for n, s in signals.items())
        den = sum(weights.get(n, 1.0) for n in signals) or 1.0
        strat_score = num / den
        buy_strats = [n for n, s in signals.items() if s["signal"] == "BUY"]
        buy_reasons = [f"{s['strategy']}：{s['reason']}" for s in signals.values()
                       if s["signal"] == "BUY"]

        # 指标因子
        d = add_all_indicators(df)
        last, prev = d.iloc[-1], d.iloc[-2]
        rsi = float(last["rsi"])
        ma_bull = bool(last["ma5"] > last["ma10"] > last["ma20"])
        vol_ratio = round(float(last["volume"] / (last["vol_ma5"] or 1)), 2)
        mom5 = round(float(d["close"].iloc[-1] / d["close"].iloc[-6] - 1) * 100, 2) if len(d) > 6 else 0.0
        tail = d.tail(250)
        hi52, lo52 = float(tail["high"].max()), float(tail["low"].min())
        pos52 = round((float(last["close"]) - lo52) / (hi52 - lo52) * 100, 0) if hi52 > lo52 else 50.0

        # 斐波那契黄金比例回撤：以近一段上涨（摆动低→摆动高）为基准，
        # 判断当前价是否回踩到 50%（及 0.382/0.618）黄金分割位附近——经典回调买点。
        fib = self._fib_retracement(d)

        close_now = float(last["close"])
        ma20_now = float(last["ma20"])
        below_ma20 = close_now < ma20_now

        bonus = 0.0
        if ma_bull:
            bonus += 0.10
        if 1.2 <= vol_ratio <= 3:
            bonus += 0.06
        elif vol_ratio > 4:
            bonus -= 0.05
        if rsi < 35:
            bonus += 0.05
        elif rsi > 78:
            bonus -= 0.12
        if pos52 > 90:
            bonus -= 0.08
        # 动量奖惩（修复A 修正版）：奖励健康强势上涨，惩罚下跌/过热。
        # 主线启动票常是"刚突破、未走出多头排列"，靠正动量给分，避免被低估。
        if 4 <= mom5 <= 18:
            bonus += 0.10            # 健康强势上涨（顺势主线启动）加分
        elif mom5 > 18:
            bonus -= 0.08            # 过热追高减分
        elif mom5 < -3:
            bonus -= 0.10            # 明显下跌减分（拒绝逆势）
        elif mom5 < 0:
            bonus -= 0.04            # 轻微走弱小幅减分
        # 跌破20日线（右侧交易，弱势）减分；但放量启动初期可能短暂在20日线下，仅轻罚
        if below_ma20:
            bonus -= 0.05
        # 回踩 50% 黄金位且上升趋势中 -> 加分（强回调买点）；命中 0.382/0.618 次级加分
        if fib.get("at_50"):
            bonus += 0.08
        elif fib.get("at_382") or fib.get("at_618"):
            bonus += 0.04
        score = round(strat_score + bonus, 4)

        # 硬约束（kline 可判定部分）
        if filters.get("max_rsi") is not None and rsi > filters["max_rsi"]:
            return None
        if filters.get("max_pos_52w") is not None and pos52 > filters["max_pos_52w"]:
            return None
        # 可选硬约束：仅保留回踩 50% 黄金分割位的标的
        if filters.get("require_fib_50") and not fib.get("at_50"):
            return None
        # 趋势硬过滤（修复B）：跌破20日线 且 5日动量明显为负 -> 直接淘汰逆势/背离票。
        # 例外：回踩黄金50%支撑位的强回调买点不淘汰（属顺势中的正常回调）。
        if filters.get("require_uptrend", REQUIRE_UPTREND):
            if below_ma20 and mom5 < MIN_MOM5 and not fib.get("at_50"):
                return None

        # K线迷你（前端卡片）
        kt = df.tail(60)
        kline_mini = {
            "dates": kt["date"].tolist(),
            "candles": [[round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                        for o, c, l, h in zip(kt["open"], kt["close"], kt["low"], kt["high"])],
        }

        tags = list(buy_strats[:3])
        if ma_bull:
            tags.append("均线多头")
        if 1.2 <= vol_ratio <= 3:
            tags.append("温和放量")
        if rsi < 35:
            tags.append("超跌")
        if fib.get("at_50"):
            tags.append("回踩黄金50%")
        elif fib.get("at_382"):
            tags.append("回踩黄金38.2%")
        elif fib.get("at_618"):
            tags.append("回踩黄金61.8%")

        factors = {
            "rsi": rsi,
            "pos_52w": pos52,
            "momentum_5d": mom5,
            "ma_bull": ma_bull,
            "volume_ratio": vol_ratio,
            "buy_signal_count": len(buy_strats),
            "buy_strategies": buy_strats,
            "strat_score": round(strat_score, 4),
            "bonus": round(bonus, 4),
            "fib": fib,  # 斐波那契黄金分割回撤明细
            "main_inflow_positive": None,  # enrich 阶段回填
        }
        # 所属热门板块（动态候选池标注），用于主线加分与剔除脱离主线者
        hot = self._sector_map.get(symbol)
        if hot:
            factors["hot_sector"] = hot.get("sector")
            factors["hot_sector_rank"] = hot.get("sector_rank")
            factors["hot_sector_week_pct"] = hot.get("week_pct")
            tags.append(f"主线·{hot.get('sector')}")

        return {
            "symbol": symbol,
            "score": score,
            "entry_price": round(float(last["close"]), 2),
            "rsi": rsi,
            "pos_52w": pos52,
            "tags": tags,
            "buy_reasons": buy_reasons,
            "factors": factors,
            "kline_mini": kline_mini,
            "hot_sector": hot,
        }

    # ------------------------------------------------------------------
    # 富化（仅对入选候选）：名称/板块/资金流
    # ------------------------------------------------------------------
    def _enrich(self, item: dict) -> dict:
        symbol = item["symbol"]
        item["name"] = resolve_name(self.fetcher, symbol)
        info = self.fetcher.get_basic_info(symbol)
        item["industry"] = str(info.get("行业") or info.get("所处行业")
                               or POOL_INDUSTRY.get(symbol) or "—")
        total_mv = info.get("总市值")
        try:
            item["total_mv"] = f"{float(total_mv)/1e8:.1f}亿" if total_mv else "—"
        except Exception:
            item["total_mv"] = "—"

        flow = self.fetcher.get_fund_flow(symbol)
        main_net = flow.get("主力净流入-净额") or flow.get("主力净流入")
        big_net = flow.get("大单净流入-净额") or flow.get("超大单净流入-净额")
        item["flow_date"] = flow.get("日期", "")

        def _fmt(v):
            try:
                v = float(v)
                sign = "+" if v >= 0 else ""
                return f"{sign}{v/1e8:.2f}亿" if abs(v) >= 1e8 else f"{sign}{v/1e4:.0f}万"
            except Exception:
                return "—"

        inflow_positive = None
        main_net_val = None
        try:
            if main_net is not None:
                main_net_val = float(main_net)
                inflow_positive = main_net_val >= 0
        except Exception:
            inflow_positive = None
        item["main_net_inflow"] = _fmt(main_net) if main_net is not None else "—"
        item["big_order_net"] = _fmt(big_net) if big_net is not None else "—"
        item["factors"]["main_inflow_positive"] = inflow_positive
        item["factors"]["main_net_value"] = main_net_val  # 主力净额（元），供资金流加分
        return item

    def _multifactor(self, item: dict, sentiment: Optional[dict] = None) -> dict:
        """阶段二多维评分：把市场情绪(最高权重)/基本面/业绩/北向/龙虎榜/利好叠加到综合评分。"""
        from recommend.fundamentals import score_multifactor
        mf = score_multifactor(self.fetcher, item["symbol"], kline_factors=item["factors"],
                               industry=item.get("industry"), sentiment=sentiment, item=item)
        item["fundamentals"] = mf
        item["factors"]["multi_bonus"] = mf["multi_bonus"]
        item["factors"]["fundamentals"] = mf  # 持久化，供只读接口展示
        item["factors"]["sentiment_bonus"] = mf.get("sentiment", {}).get("score")

        # 主线板块加分：踩中近一周最热板块的个股顺势加分（板块排名越靠前加分越多）
        hot = item.get("hot_sector") or {}
        rank = hot.get("sector_rank")
        sector_bonus = 0.0
        if rank:
            # rank1 +0.18，rank2 +0.14 … 递减，体现"越是主线越优先"
            sector_bonus = round(max(0.20 - (rank - 1) * 0.04, 0.04), 4)
            wk = hot.get("week_pct")
            if wk is not None and wk > 0:
                sector_bonus += round(min(wk * 0.006, 0.06), 4)  # 周涨幅越大额外加成
        item["factors"]["sector_bonus"] = round(sector_bonus, 4)
        mf["leading_sector"] = {"sector": hot.get("sector"), "rank": rank,
                                "week_pct": hot.get("week_pct"),
                                "score": round(sector_bonus, 3)}

        # 资金流向加分（第二权重）：主力净流入为正加分、净流出减分。
        # 加分幅度随净额规模递增（封顶），强化"资金认可"的票、压制"主力出逃"的票。
        inflow_bonus = 0.0
        main_val = item["factors"].get("main_net_value")
        if main_val is not None:
            yi = main_val / 1e8  # 净额（亿元）
            if yi > 0:
                inflow_bonus = round(min(0.06 + yi * 0.02, 0.16), 4)   # 净流入：+0.06 起，按亿递增封顶 +0.16
            elif yi < 0:
                inflow_bonus = round(max(-0.05 + yi * 0.02, -0.16), 4)  # 净流出：减分，封顶 -0.16
        item["factors"]["inflow_bonus"] = inflow_bonus
        if inflow_bonus > 0:
            item.setdefault("tags", []).append("主力净流入")

        # 综合分 = 技术面分 + 多维加分（含最高权重的市场情绪）+ 主线板块加分 + 资金流向加分
        item["score"] = round(item.get("tech_score", item["score"])
                              + mf["multi_bonus"] + sector_bonus + inflow_bonus, 4)
        # 富化标签
        cat = mf.get("catalyst", {})
        if cat.get("highlights"):
            for h in cat["highlights"][:2]:
                if h.startswith("利好:"):
                    item.setdefault("tags", []).append(h[3:])
        lhb = mf.get("lhb", {})
        if lhb.get("times") and lhb.get("net_buy", 0) > 0:
            item.setdefault("tags", []).append("龙虎榜净买")
        nb = mf.get("northbound", {})
        if (nb.get("trend_5d") or 0) > 0.05:
            item.setdefault("tags", []).append("北向增持")
        return item

    def _gen_reason(self, item: dict) -> str:
        parts = []
        if item.get("buy_reasons"):
            parts.append("触发买点：" + "；".join(item["buy_reasons"][:2]))
        feats = [t for t in item.get("tags", []) if t in ("均线多头", "温和放量", "超跌")]
        if feats:
            parts.append("技术：" + "、".join(feats))
        # 斐波那契黄金分割回踩
        fib = (item.get("factors") or {}).get("fib") or {}
        if fib.get("at_50"):
            parts.append(f"回踩黄金50%支撑位（{fib.get('fib_50')}，前期涨幅{fib.get('up_pct')}%回调{fib.get('retrace_pct')}%）")
        elif fib.get("at_382"):
            parts.append(f"回踩黄金38.2%位（{(fib.get('levels') or {}).get('0.382')}）")
        elif fib.get("at_618"):
            parts.append(f"回踩黄金61.8%位（{(fib.get('levels') or {}).get('0.618')}）")
        f = item["factors"]
        # 多维亮点（市场情绪权重最高，置于最前）
        mf = item.get("fundamentals", {})
        sent_note = (mf.get("sentiment") or {}).get("note")
        if sent_note:
            parts.append("市场情绪：" + sent_note)
        # 资金流向（第二权重）
        ib = f.get("inflow_bonus") or 0
        if item.get("main_net_inflow") and item.get("main_net_inflow") != "—":
            tag = "主力净流入" if ib > 0 else ("主力净流出" if ib < 0 else "主力资金")
            parts.append(f"资金：{tag} {item['main_net_inflow']}")
        # 主线板块归属（与市场资金主线一致性）
        hot = item.get("hot_sector") or {}
        if hot.get("sector"):
            wk = hot.get("week_pct")
            wk_txt = f"，周{wk:+.1f}%" if isinstance(wk, (int, float)) else ""
            parts.append(f"踩中市场主线【{hot.get('sector')}】(热度榜第{hot.get('sector_rank','-')}{wk_txt})")
        highs = []
        for key in ("performance", "catalyst", "industry", "valuation", "northbound", "lhb"):
            note = (mf.get(key) or {}).get("note")
            if note:
                highs.append(note)
        if highs:
            parts.append("多维：" + "｜".join(highs[:4]))
        parts.append(f"综合分 {item['score']}（技术{item.get('tech_score', '-')}＋多维{f.get('multi_bonus', 0):+}），"
                     f"RSI={f['rsi']:.0f}、52周位={f['pos_52w']:.0f}%")
        return "；".join(parts) + "。"

    # ------------------------------------------------------------------
    # 反思接入
    # ------------------------------------------------------------------
    def _maybe_reflect(self, base_date: str) -> tuple[dict, dict, Optional[dict]]:
        """返回 (weights, filters, reflection)。胜率不达标时执行反思并调整。"""
        prev = self.db.latest_weights(on_or_before=base_date)
        weights = (prev or {}).get("weights") or default_weights()
        filters = (prev or {}).get("filters") or default_filters()
        # 确保权重覆盖全部当前策略
        for n in STRATEGY_REGISTRY:
            weights.setdefault(n, 1.0)

        prev_wr = self.db.latest_winrate(before=base_date)
        if not prev_wr:
            return weights, filters, None  # 无历史，按默认/上次权重

        # 已对该批次反思过则不重复
        if self.db.get_reflection(base_date):
            return weights, filters, self.db.get_reflection(base_date)

        if prev_wr["win_rate"] >= settings.winrate_threshold:
            return weights, filters, None  # 达标，无需反思

        # —— 触发反思 ——
        from agents.llm import get_llm
        llm = get_llm()
        ref = reflect(
            db=self.db,
            based_on_date=prev_wr["base_date"],
            reflect_date=base_date,
            prev_win_rate=prev_wr["win_rate"],
            threshold=settings.winrate_threshold,
            prev_weights=weights,
            prev_filters=filters,
            fetcher=self.fetcher,
            llm=llm,
        )
        adj = ref["adjustments"]
        self.db.save_weights(base_date, adj["weights"], adj["filters"],
                             source="reflection",
                             note=f"基于{prev_wr['base_date']}胜率{prev_wr['win_rate']*100:.0f}%反思")
        self.db.save_reflection(
            reflect_date=base_date,
            based_on_date=prev_wr["base_date"],
            prev_win_rate=prev_wr["win_rate"],
            threshold=settings.winrate_threshold,
            dimensions=ref["dimensions"],
            conclusion=ref["conclusion"],
            adjustments=adj,
            llm_used=ref["llm_used"],
        )
        return adj["weights"], adj["filters"], self.db.get_reflection(base_date)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, as_of: Optional[str] = None, count: Optional[int] = None,
            pool: Optional[list[str]] = None,
            extra_filters: Optional[dict] = None) -> dict:
        count = count or settings.recommend_count

        print("[荐股] 1/7 结算历史推荐...")
        settle_all_pending(self.db, self.fetcher)

        print("[荐股] 2/7 定位交易日...")
        base_date = self._resolve_base_date(as_of)
        if not base_date:
            return {"error": "无法获取交易日数据"}

        print(f"[荐股] 3/7 反思与权重（base_date={base_date}）...")
        weights, filters, reflection = self._maybe_reflect(base_date)
        # 合并用户临时约束（如 require_fib_50：仅回踩黄金50%）
        if extra_filters:
            filters = {**filters, **{k: v for k, v in extra_filters.items() if v is not None}}

        print("[荐股] 4/7 构建动态候选池...")
        from recommend.universe import build_dynamic_universe
        try:
            dyn_pool, sector_map, hot_week = build_dynamic_universe(
                self.fetcher, base_pool=(pool or RECOMMEND_POOL),
                as_of=base_date)  # 历史日期回退固定池，保证时间一致性
        except Exception:
            dyn_pool, sector_map, hot_week = [], {}, []
        if not dyn_pool:  # 兜底：动态池失败回退固定池
            dyn_pool = list(pool or RECOMMEND_POOL)
        pool = [s for s in dyn_pool if eligible(s)]
        scan_max = int(os.getenv("REC_SCAN_MAX", "30"))
        if len(pool) > scan_max:
            pool = pool[:scan_max]
        self._sector_map = sector_map  # 供 _score_stock / _multifactor 读取板块归属

        print(f"[荐股] 候选池数量：{len(pool)}")
        print("[荐股] 5/7 计算市场情绪...")
        from recommend.sentiment import compute_market_sentiment
        try:
            sentiment = compute_market_sentiment(self.fetcher, base_date, hot_sectors=hot_week)
        except Exception:
            sentiment = {}

        # 大盘择时 + 情绪门槛：弱市/低迷情绪抬高入选门槛
        market_note = ""
        min_score = float(filters.get("min_score") or 0.0)
        if filters.get("market_timing"):
            idx = self.fetcher.get_index_kline("sh000001", days=120)
            if idx is not None and not idx.empty:
                idx = idx[idx["date"] <= base_date]
                if len(idx) >= 20:
                    ma20 = idx["close"].tail(20).mean()
                    if float(idx.iloc[-1]["close"]) < ma20:
                        min_score += 0.10
                        market_note = "大盘处于20日线下方（弱市），已抬高入选门槛、收紧推荐。"
        # 情绪冰点/低迷时整体提高门槛（情绪是最高权重，直接影响是否值得参与）
        sent_score = float(sentiment.get("score", 50.0)) if sentiment else 50.0
        if sent_score < 30:
            min_score += 0.12
            market_note = (market_note + " " if market_note else "") + \
                f"市场情绪{sent_score:.0f}分（冰点），大幅抬高门槛、从严推荐。"
        elif sent_score < 45:
            min_score += 0.05
            market_note = (market_note + " " if market_note else "") + \
                f"市场情绪{sent_score:.0f}分（低迷），适度抬高入选门槛。"

        # 主线一致性：情绪不冷且存在明确主线时，优先/限定主线板块内选股，
        # 剔除与近期市场情绪脱节（不属任何热门板块）的标的。
        leading = (sentiment or {}).get("leading_sectors") or hot_week or []
        enforce_mainline = bool(leading) and sent_score >= 45
        if enforce_mainline:
            tops = "、".join(s.get("name", "") for s in leading[:4] if s.get("name"))
            market_note = (market_note + " " if market_note else "") + \
                f"市场主线聚焦【{tops}】，已优先从主线板块内选股、剔除脱离主线标的。"

        print("[荐股] 6/7 技术面初筛...")
        scored = []
        for sym in pool:
            try:
                r = self._score_stock(sym, base_date, weights, filters)
                if r and r["score"] >= min_score:
                    r["tech_score"] = r["score"]
                    r["factors"]["tech_score"] = r["score"]
                    scored.append(r)
            except Exception:
                continue
        scored.sort(key=lambda x: x["score"], reverse=True)
        print(f"[荐股] 初筛入围：{len(scored)}")

        print("[荐股] 7/7 多因子富化与落库...")
        # 阶段二候选：强制主线时，主线股优先纳入 shortlist（避免被高技术分蓝筹挤占），
        # 再用非主线股补足，保证富化与最终名单聚焦市场主线。
        cap = max(count * 4, 16)
        if enforce_mainline:
            on = [x for x in scored if (x.get("hot_sector") or {}).get("sector")]
            off = [x for x in scored if not (x.get("hot_sector") or {}).get("sector")]
            shortlist = (on + off)[:cap]
        else:
            shortlist = scored[:cap]
        for item in shortlist:
            try:
                self._enrich(item)
            except Exception:
                item.setdefault("name", resolve_name(self.fetcher, item["symbol"]))
                item.setdefault("industry", "—")
            try:
                self._multifactor(item, sentiment=sentiment)
            except Exception:
                item.setdefault("fundamentals", {})
            item["reason"] = self._gen_reason(item)

        # LLM 后置复核：对规则筛完的候选做风险/机会评估，加减分叠加到最终分
        llm_enabled = False
        try:
            from agents.llm import get_llm
            llm_enabled = get_llm().available
        except Exception:
            pass
        if llm_enabled and shortlist:
            try:
                reviews = llm_review_candidates(
                    shortlist, sentiment, hot_week, base_date,
                )
                if reviews:
                    review_map = {r["symbol"]: r for r in reviews}
                    for item in shortlist:
                        rv = review_map.get(item["symbol"])
                        if rv:
                            item["ai_score"] = rv["ai_score"]
                            item["ai_flags"] = rv["ai_flags"]
                            item["ai_reason"] = rv["ai_reason"]
                            item["score"] = round(
                                item["score"] + rv["ai_score"], 4
                            )
                    print(f"[荐股] LLM复核完成，{len(reviews)}只票获得AI加减分")
            except Exception as e:
                print(f"[荐股] LLM复核跳过（{e}）")

        shortlist.sort(key=lambda x: x["score"], reverse=True)

        # 6. 约束筛选（资金流入 / 板块上限 / 主线一致性），填满 count
        # 资金流硬筛：filters 配置 或 .env(REC_REQUIRE_INFLOW) 任一开启即生效
        require_inflow = bool(filters.get("require_fund_inflow")) or REQUIRE_INFLOW
        require_fund = bool(filters.get("require_positive_fundamental"))
        max_sector = filters.get("max_per_sector")
        picks: list[dict] = []
        sector_count: dict[str, int] = {}

        def _is_uptrend(item) -> bool:
            """顺势判定（修复C）：拒绝"底部阴跌/弱势震荡"票。

            真正顺势需满足任一：
            - 多头排列 且 5日动量非负（确认上行，排除底部阴跌的假多头）
            - 5日动量为正（启动/上涨中）
            - 回踩黄金50%支撑（顺势中的健康回调买点）
            """
            fac = item.get("factors") or {}
            mom = fac.get("momentum_5d") or 0
            if fac.get("ma_bull") and mom >= 0:
                return True
            if mom > 0:
                return True
            # 黄金50%回踩需是"上升趋势中的健康回调（已接近企稳）"，而非持续阴跌恰好
            # 路过该位：要求回踩段在上升结构(on_uptrend)、近5日基本企稳(mom>=-1)、
            # 且非空头排列下的弱势。借此剔除"空头阴跌正好压到50%位"的假买点。
            fib = fac.get("fib") or {}
            if fib.get("at_50") and fib.get("on_uptrend") and mom >= -1:
                return True
            return False

        def _passes(item) -> bool:
            if require_inflow and item["factors"].get("main_inflow_positive") is False:
                return False
            if require_fund and (item["factors"].get("multi_bonus") or 0) < 0:
                return False
            # 宁缺毋滥：补位/收录时也要求顺势，杜绝逆势背离票（修复C）
            if STRICT_NO_FILL and not _is_uptrend(item):
                return False
            if max_sector:
                ind = item.get("industry", "—")
                if sector_count.get(ind, 0) >= max_sector:
                    return False
            return True

        def _take(item):
            if max_sector:
                ind = item.get("industry", "—")
                sector_count[ind] = sector_count.get(ind, 0) + 1
            picks.append(item)

        # 第一遍：仅收"踩中主线板块"的标的（确保名单与市场主线高度一致）
        for item in shortlist:
            if len(picks) >= count:
                break
            on_mainline = bool((item.get("hot_sector") or {}).get("sector"))
            if not on_mainline:
                continue
            if _passes(item):
                _take(item)

        # 第二遍：主线股不足 count 时补齐。
        #   强制主线(enforce_mainline)时：只补"踩中主线板块"的票，绝不混入非主线标的，
        #   确保最终名单与市场资金主线 100% 一致（核心要求：不背离市场情绪）。
        #   非强制主线时：可用非主线顺势标的补齐。
        if len(picks) < count:
            chosen = {p["symbol"] for p in picks}
            for item in shortlist:
                if len(picks) >= count:
                    break
                if item["symbol"] in chosen:
                    continue
                if enforce_mainline and not (item.get("hot_sector") or {}).get("sector"):
                    continue  # 强制主线：跳过非主线票
                if _passes(item):
                    _take(item)
                    chosen.add(item["symbol"])

        # 兜底：若约束过严导致不足——
        #   STRICT_NO_FILL=True（默认）：宁缺毋滥，不塞逆势票，宁可少于 count；
        #   STRICT_NO_FILL=False：放宽其他约束按综合分补齐（但仍排除逆势背离票）。
        if len(picks) < count and not STRICT_NO_FILL:
            chosen = {p["symbol"] for p in picks}
            for item in shortlist:
                if len(picks) >= count:
                    break
                if item["symbol"] in chosen:
                    continue
                if enforce_mainline and not (item.get("hot_sector") or {}).get("sector"):
                    continue  # 强制主线：放宽约束也不混入非主线票
                if _is_uptrend(item):  # 即便放宽约束，仍拒绝逆势票
                    picks.append(item)
                    chosen.add(item["symbol"])

        if len(picks) < count:
            print(f"[荐股] 顺势主线优质标的仅 {len(picks)} 只（宁缺毋滥，未凑满 {count}）")
        print(f"[荐股] 最终推荐：{len(picks)}，写入数据库...")
        self.db.save_recommendations(base_date, picks)

        from agents.llm import get_llm
        prev_wr = self.db.latest_winrate(before=base_date)
        spot_source = getattr(self.fetcher, "last_market_spot_source", "未知")
        kline_source = getattr(self.fetcher, "last_kline_source", "未知")
        data_source = f"行情快照：{spot_source}；K线：{kline_source}"
        return {
            "base_date": base_date,
            "count": len(picks),
            "picks": picks,
            "data_source": data_source,
            "data_source_tip": "K线优先Tushare；行情快照：盘中优先东财实时行情，收盘后优先Tushare日级快照。" + data_source,
            "weights": weights,
            "filters": filters,
            "market_note": market_note,
            "sentiment": sentiment,
            "prev_winrate": prev_wr,
            "reflection": reflection,
            "threshold": settings.winrate_threshold,
            "llm_enabled": get_llm().available,
        }

    # ------------------------------------------------------------------
    # 历史回填（用于离线演示反思迭代）
    # ------------------------------------------------------------------
    def backfill(self, n_days: int = 6, count: Optional[int] = None) -> list[dict]:
        """对最近 n_days 个交易日依次生成推荐并结算，构建历史以触发反思迭代。"""
        dates = self._trading_dates()
        if not dates:
            return []
        targets = dates[-n_days:]
        out = []
        for d in targets:
            res = self.run(as_of=d, count=count)
            settle_all_pending(self.db, self.fetcher)
            wr = self.db.latest_winrate()
            out.append({"base_date": d, "picks": res.get("count", 0),
                        "winrate": wr})
        return out


def run_recommend(as_of: Optional[str] = None, count: Optional[int] = None) -> dict:
    return RecommendEngine().run(as_of=as_of, count=count)
