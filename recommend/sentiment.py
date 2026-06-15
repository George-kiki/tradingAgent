"""市场情绪指标：衡量当下A股整体情绪温度，作为每日荐股的最高权重因子。

与个股基本面/技术面不同，市场情绪是**全局变量**——同一交易日对所有候选股一致，
反映"大盘是否值得参与、当前是高潮还是冰点"。在荐股逻辑中其权重最高，因为：
逆市场情绪择股，再好的个股也容易"看对做错"。

== 情绪六维（合成 0-100 情绪分）==
1. 涨跌家数比     —— 上涨家数/总数，衡量赚钱效应广度（最核心）
2. 涨停/跌停对比  —— 涨停数 vs 跌停数，衡量资金做多意愿与情绪冰点
3. 大盘趋势       —— 上证指数相对 20/60 日均线位置 + 近5日动量
4. 量能           —— 大盘成交量相对近期均量（放量上涨=情绪升温）
5. 北向资金       —— 近5日北向净流入方向（外资态度，可选）
6. 板块热度       —— 领涨板块涨幅与赚钱效应扩散度

输出：
- score: 0-100 综合情绪分
- temperature: 冰点/低迷/中性/活跃/亢奋 五档
- bonus_base: 映射到 [-0.25, +0.30] 的全局加分（情绪好整体加分，差则压分）
- breadth / limits / trend / volume / sectors 等分项明细，供前端看板展示

所有数据 best-effort：单维取不到按中性处理，不影响整体情绪判定。
"""
from __future__ import annotations

import os
from typing import Optional

import pandas as pd


def _w(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


# 市场情绪在荐股中的总权重（最高，可经环境变量覆盖）
SENTIMENT_WEIGHT = _w("REC_W_SENTIMENT", 3.0)


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _temperature(score: float) -> tuple[str, str]:
    """情绪分 -> (温度标签, emoji)。"""
    if score >= 80:
        return "亢奋", "🔥"
    if score >= 62:
        return "活跃", "☀️"
    if score >= 45:
        return "中性", "⛅"
    if score >= 30:
        return "低迷", "🌧️"
    return "冰点", "❄️"


def _score_breadth(breadth: dict) -> tuple[float, str]:
    """涨跌家数广度 -> 0-100 子分。上涨占比是赚钱效应最直接的衡量。"""
    total = breadth.get("total") or 0
    if not total:
        return 50.0, ""
    up = breadth.get("up", 0)
    ratio = up / total
    # 上涨占比 0.2~0.8 线性映射到 15~90
    sub = _clip((ratio - 0.2) / 0.6 * 75 + 15, 5, 95)
    return sub, f"上涨{up}/{total}（{ratio*100:.0f}%）"


def _score_limits(breadth: dict) -> tuple[float, str]:
    """涨停 vs 跌停 -> 0-100 子分。情绪冰点常伴跌停潮，亢奋常涨停潮。"""
    lu = breadth.get("limit_up", 0)
    ld = breadth.get("limit_down", 0)
    if lu == 0 and ld == 0:
        return 50.0, ""
    diff = lu - ld
    # diff 从 -40 ~ +60 映射到 10 ~ 92
    sub = _clip((diff + 40) / 100 * 82 + 10, 5, 95)
    return sub, f"涨停{lu}/跌停{ld}"


def _score_trend(fetcher, base_date: Optional[str]) -> tuple[float, str]:
    """大盘趋势：上证指数相对 20/60 日均线 + 近5日动量 -> 0-100 子分。"""
    try:
        df = fetcher.get_index_kline("sh000001", days=120)
        if df is None or df.empty:
            return 50.0, ""
        if base_date:
            df = df[df["date"] <= base_date]
        if len(df) < 60:
            return 50.0, ""
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        last = float(close.iloc[-1])
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean())
        mom5 = (last / float(close.iloc[-6]) - 1) * 100 if len(close) > 6 else 0.0

        sub = 50.0
        if last > ma20:
            sub += 14
        else:
            sub -= 14
        if last > ma60:
            sub += 10
        else:
            sub -= 10
        if ma20 > ma60:          # 均线多头
            sub += 6
        sub += _clip(mom5 * 2.5, -16, 16)  # 近5日动量
        note = f"上证{'站上' if last > ma20 else '跌破'}20日线、近5日{mom5:+.1f}%"
        return _clip(sub, 5, 95), note
    except Exception:
        return 50.0, ""


def _score_volume(fetcher, base_date: Optional[str]) -> tuple[float, str]:
    """大盘量能：当日成交量 / 近20日均量 -> 0-100 子分（放量配合上涨更健康）。"""
    try:
        df = fetcher.get_index_kline("sh000001", days=60)
        if df is None or df.empty or "volume" not in df.columns:
            return 50.0, ""
        if base_date:
            df = df[df["date"] <= base_date]
        vol = pd.to_numeric(df["volume"], errors="coerce").dropna()
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(vol) < 21:
            return 50.0, ""
        vr = float(vol.iloc[-1]) / (float(vol.tail(20).mean()) or 1)
        rising = float(close.iloc[-1]) >= float(close.iloc[-2]) if len(close) >= 2 else True
        # 放量上涨加分、放量下跌减分、缩量中性
        if vr >= 1.15:
            sub = 70 if rising else 36
        elif vr <= 0.8:
            sub = 44
        else:
            sub = 55 if rising else 48
        return _clip(sub, 5, 95), f"量能{vr:.2f}倍{'(放量)' if vr >= 1.15 else '(缩量)' if vr <= 0.8 else ''}"
    except Exception:
        return 50.0, ""


def _score_sectors(fetcher, hot_week: Optional[list] = None) -> tuple[float, str]:
    """板块热度：近一周主线板块累计涨幅 + 当日红盘占比 -> 0-100 子分。

    优先使用近一周热度榜（hot_week，反映持续主线）；缺失时回退到单日涨幅榜。
    """
    try:
        if hot_week:
            wk = [s.get("week_pct") for s in hot_week if s.get("week_pct") is not None]
            top_name = hot_week[0].get("name", "") if hot_week else ""
            top_week = wk[0] if wk else 0.0
            up_ratio = (sum(1 for x in wk if x > 0) / len(wk)) if wk else 0.5
            sub = _clip(up_ratio * 55 + _clip(top_week * 3.5, -12, 30) + 18, 5, 95)
            return sub, f"主线{top_name} 周{top_week:+.1f}%"
        sectors = fetcher.get_hot_sectors(limit=60)
        if not sectors:
            return 50.0, ""
        pcts = [s.get("pct", 0.0) for s in sectors]
        up_ratio = sum(1 for p in pcts if p > 0) / len(pcts)
        top = pcts[0] if pcts else 0.0
        sub = _clip(up_ratio * 60 + _clip(top * 6, -10, 25) + 15, 5, 95)
        lead = sectors[0]
        return sub, f"领涨{lead.get('name','')} {top:+.1f}%（{up_ratio*100:.0f}%板块红盘）"
    except Exception:
        return 50.0, ""


def _score_northbound(fetcher, base_date: Optional[str]) -> tuple[Optional[float], str]:
    """北向资金近5日净流入方向 -> 0-100 子分。取不到返回 None（不参与，权重重分配）。"""
    # 北向逐日资金接口在 akshare 中常变更/限流，这里 best-effort，失败即跳过
    try:
        import akshare as ak
        df = None
        for caller in (
            lambda: ak.stock_hsgt_hist_em(symbol="北向资金"),
            lambda: ak.stock_hsgt_north_net_flow_in_em(symbol="北上"),
        ):
            try:
                df = caller()
                if df is not None and not df.empty:
                    break
            except Exception:
                continue
        if df is None or df.empty:
            return None, ""
        val_col = next((c for c in df.columns if "净流入" in str(c) or "净买" in str(c)
                        or "当日资金流入" in str(c)), None)
        date_col = next((c for c in df.columns if "日期" in str(c)), None)
        if not val_col:
            return None, ""
        if date_col:
            df = df.sort_values(date_col)
            if base_date:
                df = df[df[date_col].astype(str) <= base_date]
        vals = pd.to_numeric(df[val_col], errors="coerce").dropna()
        if vals.empty:
            return None, ""
        net5 = float(vals.tail(5).sum())  # 单位通常为亿元
        # net5 从 -200亿 ~ +200亿 映射到 20 ~ 85
        sub = _clip(net5 / 200 * 32.5 + 50, 10, 90)
        return sub, f"北向近5日净{'流入' if net5 >= 0 else '流出'}{abs(net5):.0f}亿"
    except Exception:
        return None, ""


def compute_market_sentiment(fetcher, base_date: Optional[str] = None,
                             hot_sectors: Optional[list] = None) -> dict:
    """计算当下市场情绪（全局），返回 0-100 情绪分 + 温度 + 分项明细 + 全局加分基数 + 主线板块。

    weights 为各子维内部权重（涨跌广度与趋势权重更高）。北向取不到时其权重按比例
    重分配给其余维度，保证情绪分始终可计算。
    hot_sectors 为近一周板块热度榜（识别市场主线），若未传入则自行拉取。
    """
    breadth = {}
    try:
        breadth = fetcher.get_market_breadth() or {}
    except Exception:
        breadth = {}

    # 近一周主线板块（用于板块热度维度 + 输出给选股/看板）
    if hot_sectors is None:
        try:
            hot_sectors = fetcher.get_hot_sectors_week(top=12) or []
        except Exception:
            hot_sectors = []

    s_breadth, n_breadth = _score_breadth(breadth)
    s_limits, n_limits = _score_limits(breadth)
    s_trend, n_trend = _score_trend(fetcher, base_date)
    s_volume, n_volume = _score_volume(fetcher, base_date)
    s_sectors, n_sectors = _score_sectors(fetcher, hot_sectors)
    s_north, n_north = _score_northbound(fetcher, base_date)

    # 子维内部权重（广度/趋势最重）
    parts = [
        ("breadth", s_breadth, 0.26, n_breadth),
        ("limits", s_limits, 0.16, n_limits),
        ("trend", s_trend, 0.24, n_trend),
        ("volume", s_volume, 0.12, n_volume),
        ("sectors", s_sectors, 0.12, n_sectors),
        ("northbound", s_north, 0.10, n_north),
    ]
    # 北向缺失则剔除并把权重按比例分给其余维度
    active = [(k, v, w, note) for (k, v, w, note) in parts if v is not None]
    total_w = sum(w for _, _, w, _ in active) or 1.0
    score = sum(v * (w / total_w) for _, v, w, _ in active)
    score = round(_clip(score, 0, 100), 1)

    temp, emoji = _temperature(score)
    # 情绪分 0-100 -> 全局加分 [-0.25, +0.30]（50 分中性=0）
    bonus_base = round(_clip((score - 50) / 50, -1, 1) * (0.30 if score >= 50 else 0.25), 4)

    details = {k: {"score": round(v, 1), "weight": round(w / total_w, 3), "note": note}
               for (k, v, w, note) in active}

    # 主线板块精简列表（供选股加分与前端看板）
    leading = [{"name": s.get("name"), "type": s.get("type"),
                "week_pct": s.get("week_pct"), "day_pct": s.get("day_pct"),
                "leader": s.get("leader", "")}
               for s in (hot_sectors or [])[:8]]

    return {
        "score": score,
        "temperature": temp,
        "emoji": emoji,
        "bonus_base": bonus_base,
        "weight": SENTIMENT_WEIGHT,
        "breadth": breadth,
        "details": details,
        "leading_sectors": leading,
        "summary": f"{emoji} 市场情绪{score}分（{temp}）：" + "；".join(
            d["note"] for d in details.values() if d["note"]),
        "base_date": base_date or "",
    }


def sentiment_stock_bonus(sentiment: dict, item: dict) -> tuple[float, str]:
    """把全局情绪映射为对单只候选股的加分（最高权重维度）。

    在全局 bonus_base 基础上，做轻度个股增强：
    - 情绪升温(>55分)时，温和放量且均线多头的个股顺势加成；
    - 情绪低迷(<40分)时，整体压分，避免逆势追高。
    返回 (加权后的情绪加分, 文案)。
    """
    base = float(sentiment.get("bonus_base", 0.0))
    score = float(sentiment.get("score", 50.0))
    factors = item.get("factors", {})
    vol_ratio = factors.get("volume_ratio")
    ma_bull = factors.get("ma_bull")

    align = 0.0
    if score >= 55:  # 情绪偏暖：奖励顺势个股
        if ma_bull:
            align += 0.03
        if vol_ratio and 1.2 <= vol_ratio <= 3:
            align += 0.02
    elif score < 40:  # 情绪偏冷：弱势个股额外压分
        if vol_ratio and vol_ratio > 4:   # 冰点放量易是出货
            align -= 0.03

    raw = base + align
    weighted = round(raw * SENTIMENT_WEIGHT, 4)
    note = f"{sentiment.get('emoji','')}情绪{score:.0f}分({sentiment.get('temperature','')})"
    return weighted, note
