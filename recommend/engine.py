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
from typing import Optional

import pandas as pd

from core.config import settings
from core.indicators import add_all_indicators
from data.fetcher import get_fetcher
from recommend.database import RecommendDB
from recommend.reflection import default_filters, default_weights, reflect
from recommend.winrate import settle_all_pending
from strategies.library import STRATEGY_REGISTRY


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
        if mom5 > 18:
            bonus -= 0.08
        score = round(strat_score + bonus, 4)

        # 硬约束（kline 可判定部分）
        if filters.get("max_rsi") is not None and rsi > filters["max_rsi"]:
            return None
        if filters.get("max_pos_52w") is not None and pos52 > filters["max_pos_52w"]:
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
            "main_inflow_positive": None,  # enrich 阶段回填
        }
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
        try:
            if main_net is not None:
                inflow_positive = float(main_net) >= 0
        except Exception:
            inflow_positive = None
        item["main_net_inflow"] = _fmt(main_net) if main_net is not None else "—"
        item["big_order_net"] = _fmt(big_net) if big_net is not None else "—"
        item["factors"]["main_inflow_positive"] = inflow_positive
        return item

    def _multifactor(self, item: dict) -> dict:
        """阶段二多维评分：把基本面/业绩/北向/龙虎榜/利好的加分叠加到综合评分。"""
        from recommend.fundamentals import score_multifactor
        mf = score_multifactor(self.fetcher, item["symbol"], kline_factors=item["factors"],
                               industry=item.get("industry"))
        item["fundamentals"] = mf
        item["factors"]["multi_bonus"] = mf["multi_bonus"]
        item["factors"]["fundamentals"] = mf  # 持久化，供只读接口展示
        # 综合分 = 技术面分 + 多维加分
        item["score"] = round(item.get("tech_score", item["score"]) + mf["multi_bonus"], 4)
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
        f = item["factors"]
        # 多维亮点
        mf = item.get("fundamentals", {})
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
            pool: Optional[list[str]] = None) -> dict:
        count = count or settings.recommend_count
        pool = [s for s in (pool or RECOMMEND_POOL) if eligible(s)]

        # 1. 结算历史推荐 → 更新胜率
        settle_all_pending(self.db, self.fetcher)

        # 2. 定位 base_date
        base_date = self._resolve_base_date(as_of)
        if not base_date:
            return {"error": "无法获取交易日数据"}

        # 3+4. 反思（含胜率达标判定 + 权重/约束调整）
        weights, filters, reflection = self._maybe_reflect(base_date)

        # 大盘择时：弱市抬高门槛
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

        # 5. 阶段一：技术面快速初筛全池
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

        # 阶段二：对靠前候选做多维富化（基本面/业绩/北向/龙虎榜/利好），重排综合分
        shortlist = scored[: max(count * 3, 12)]
        for item in shortlist:
            try:
                self._enrich(item)
            except Exception:
                item.setdefault("name", resolve_name(self.fetcher, item["symbol"]))
                item.setdefault("industry", "—")
            try:
                self._multifactor(item)
            except Exception:
                item.setdefault("fundamentals", {})
            item["reason"] = self._gen_reason(item)
        shortlist.sort(key=lambda x: x["score"], reverse=True)

        # 6. 约束筛选（资金流入 / 板块上限），填满 count
        require_inflow = bool(filters.get("require_fund_inflow"))
        require_fund = bool(filters.get("require_positive_fundamental"))
        max_sector = filters.get("max_per_sector")
        picks: list[dict] = []
        sector_count: dict[str, int] = {}
        for item in shortlist:
            if len(picks) >= count:
                break
            if require_inflow and item["factors"].get("main_inflow_positive") is False:
                continue
            if require_fund and (item["factors"].get("multi_bonus") or 0) < 0:
                continue
            if max_sector:
                ind = item.get("industry", "—")
                if sector_count.get(ind, 0) >= max_sector:
                    continue
                sector_count[ind] = sector_count.get(ind, 0) + 1
            picks.append(item)

        # 兜底：若约束过严导致不足，放宽约束按综合分补齐
        if len(picks) < count:
            chosen = {p["symbol"] for p in picks}
            for item in shortlist:
                if len(picks) >= count:
                    break
                if item["symbol"] not in chosen:
                    picks.append(item)

        # 7. 落库
        self.db.save_recommendations(base_date, picks)

        from agents.llm import get_llm
        prev_wr = self.db.latest_winrate(before=base_date)
        return {
            "base_date": base_date,
            "count": len(picks),
            "picks": picks,
            "weights": weights,
            "filters": filters,
            "market_note": market_note,
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
