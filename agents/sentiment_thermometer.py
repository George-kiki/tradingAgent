"""个股情绪温度计 —— 六维加权评分系统。

该指数作为情绪温度计用于判断个股偏冷、正常还是过热，而非直接买卖信号。

六维加权计算：
  1. 主力资金 (权重20%) — 大单/超大单流向，持续流入加分，流出减分
  2. 相对强弱 (权重20%) — 个股涨幅 vs 基准指数涨幅，跑赢则更强
  3. 参与热度 (权重15%) — 量价配合，放量上涨加分，放量下跌不计分
  4. 趋势     (权重20%) — 站上20日均线且MA20>MA60时趋势更好
  5. 回撤修复 (权重15%) — 距阶段高点越近、修复越充分则分越高
  6. 下行风险 (权重10%) — 下跌波动大及负收益多时拉低总分

输出：0-100分 + 状态标签（冰冷/偏冷/正常/偏热/过热）
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional


# ── 权重配置 ──
W_MAIN_FORCE = 0.20    # 主力资金
W_REL_STRENGTH = 0.20  # 相对强弱
W_HEAT = 0.15          # 参与热度
W_TREND = 0.20         # 趋势
W_RECOVERY = 0.15      # 回撤修复
W_DOWNSIDE = 0.10      # 下行风险


def _clip(v: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, v))


def compute_sentiment_thermometer(
    fetcher,
    symbol: str,
    fund_flow: Optional[dict] = None,
    index_pct: Optional[float] = None,
) -> dict:
    """计算个股情绪温度计。

    Args:
        fetcher: data.fetcher 实例
        symbol: 股票代码
        fund_flow: 主力资金流数据（可选，从 get_fund_flow 获取）
        index_pct: 基准指数当日涨跌幅（可选，默认用上证指数）

    Returns:
        {
            "score": 0-100,
            "label": "冰冷/偏冷/正常/偏热/过热",
            "emoji": "❄️/🧊/🌡️/🔥/🌋",
            "dimensions": {
                "main_force": {"score": float, "weight": 0.20, "detail": str},
                "rel_strength": {"score": float, "weight": 0.20, "detail": str},
                "heat": {"score": float, "weight": 0.15, "detail": str},
                "trend": {"score": float, "weight": 0.20, "detail": str},
                "recovery": {"score": float, "weight": 0.15, "detail": str},
                "downside": {"score": float, "weight": 0.10, "detail": str},
            },
            "summary": str,
        }
    """
    import pandas as pd
    import numpy as np

    # ── 获取K线数据 ──
    df = fetcher.get_kline(symbol, days=120)
    if df is None or df.empty or len(df) < 20:
        return _fallback_result("K线数据不足20根")

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")

    closes = df["close"].dropna()
    vols = df["volume"].dropna()
    highs = df["high"].dropna()
    lows = df["low"].dropna()
    opens = df["open"].dropna()

    if len(closes) < 20:
        return _fallback_result("有效K线不足20根")

    # 最新价（用K线最后收盘，或外部传入的实时价）
    latest_close = float(closes.iloc[-1])
    latest_pct = ((float(closes.iloc[-1]) - float(closes.iloc[-2])) / float(closes.iloc[-2]) * 100) \
        if len(closes) >= 2 else 0.0

    # ── 1. 主力资金 (权重20%) ──
    # 兼容两种 fund_flow 格式：
    #   A) 原始中文格式 {"主力净流入-净额": ..., "超大单净流入-净额": ..., "大单净流入-净额": ...}
    #      （来自 fetcher.get_fund_flow() 直接返回）
    #   B) 前端英文格式 {"main_net_yi": ..., "super_net_yi": ..., "big_net_yi": ...}
    #      （来自 /api/realtime 预处理后的 dict，单位为亿）
    if fund_flow is None or not fund_flow:
        try:
            fund_flow = fetcher.get_fund_flow(symbol) or {}
        except Exception:
            fund_flow = {}
    if not fund_flow:
        # 所有接口都拿不到资金流数据，给默认50分
        mf_score = 50
        main_net = 0.0
        super_net = 0.0
        big_net = 0.0
        main_ratio = 0.0
        mf_detail = "主力资金数据暂缺（非交易时段或接口不可用），默认50分"
    else:
        # 兼容两种键名格式
        is_en_format = "main_net_yi" in fund_flow
        if is_en_format:
            # 前端英文格式，单位是亿，转成元用于后续计算
            main_net = float(fund_flow.get("main_net_yi", 0) or 0) * 1e8
            super_net = float(fund_flow.get("super_net_yi", 0) or 0) * 1e8
            big_net = float(fund_flow.get("big_net_yi", 0) or 0) * 1e8
        else:
            # 原始中文格式，单位是元
            main_net = float(fund_flow.get("主力净流入-净额", 0) or 0)
            super_net = float(fund_flow.get("超大单净流入-净额", 0) or 0)
            big_net = float(fund_flow.get("大单净流入-净额", 0) or 0)

        # 主力净额归一化：以成交额为基准
        total_net = main_net
        # 用近5日成交额均值估算"正常"资金流规模
        # volume 单位为"手"（1手=100股），成交额需 ×100
        recent_amt = float(vols.tail(5).mean() * latest_close * 100) if len(vols) >= 5 else 0
        if recent_amt > 0:
            main_ratio = total_net / recent_amt
        else:
            main_ratio = 0

        # 持续流入加分：看近5日资金方向一致性
        # （简化：用当日主力净额占比映射到0-100）
        if main_ratio > 0.15:
            mf_score = 90
        elif main_ratio > 0.08:
            mf_score = 75
        elif main_ratio > 0.03:
            mf_score = 60
        elif main_ratio > -0.03:
            mf_score = 50
        elif main_ratio > -0.08:
            mf_score = 35
        elif main_ratio > -0.15:
            mf_score = 20
        else:
            mf_score = 10

        mf_detail = (f"主力净流入{main_net/1e8:+.2f}亿"
                     f"（超大单{super_net/1e8:+.2f}亿+大单{big_net/1e8:+.2f}亿），"
                     f"占5日均成交额{main_ratio*100:+.1f}%")

    # ── 2. 相对强弱 (权重20%) ──
    # 获取基准指数涨跌幅
    if index_pct is None:
        try:
            idx_spot = fetcher.get_index_spot()
            for idx in (idx_spot or []):
                if idx.get("name") in ("上证指数", "上证综指"):
                    index_pct = float(idx.get("pct", 0))
                    break
        except Exception:
            pass
    if index_pct is None:
        index_pct = 0.0

    # 近5日个股涨幅 vs 指数涨幅
    if len(closes) >= 6:
        stock_5d = (float(closes.iloc[-1]) / float(closes.iloc[-6]) - 1) * 100
    else:
        stock_5d = latest_pct

    # 也获取指数5日涨幅（简化：用当日涨幅近似）
    alpha = latest_pct - index_pct  # 超额收益
    alpha_5d = stock_5d - index_pct * 5  # 粗略5日超额

    if alpha_5d > 10:
        rs_score = 90
    elif alpha_5d > 5:
        rs_score = 75
    elif alpha_5d > 0:
        rs_score = 60
    elif alpha_5d > -5:
        rs_score = 40
    elif alpha_5d > -10:
        rs_score = 25
    else:
        rs_score = 10

    rs_detail = f"个股5日{stock_5d:+.1f}% vs 指数约{index_pct*5:+.1f}%，超额{alpha_5d:+.1f}%"

    # ── 3. 参与热度 (权重15%) ──
    # 量价配合：放量上涨=高热度，放量下跌=不计分
    if len(vols) >= 20:
        vol_ma20 = float(vols.tail(20).mean())
        vol_today = float(vols.iloc[-1])
        vol_ratio = vol_today / vol_ma20 if vol_ma20 > 0 else 1.0
    else:
        vol_ratio = 1.0

    is_up = latest_pct > 0
    is_vol_up = vol_ratio > 1.2  # 放量

    if is_up and is_vol_up:
        # 放量上涨：热度高
        if vol_ratio > 2.0:
            heat_score = 90
        elif vol_ratio > 1.5:
            heat_score = 75
        else:
            heat_score = 65
    elif is_up and not is_vol_up:
        # 缩量上涨：温和
        heat_score = 55
    elif not is_up and is_vol_up:
        # 放量下跌：不计分（给低分）
        heat_score = 20
    else:
        # 缩量下跌：冷清
        heat_score = 35

    heat_detail = f"量比{vol_ratio:.2f}，{'放量' if is_vol_up else '缩量'}{'上涨' if is_up else '下跌'}"

    # ── 4. 趋势 (权重20%) ──
    ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else latest_close
    ma60 = float(closes.tail(60).mean()) if len(closes) >= 60 else ma20

    above_ma20 = latest_close > ma20
    ma20_above_ma60 = ma20 > ma60  # 均线多头

    if above_ma20 and ma20_above_ma60:
        # 完美多头
        gap_ma20 = (latest_close / ma20 - 1) * 100
        tr_score = _clip(70 + gap_ma20 * 2, 70, 95)
    elif above_ma20 and not ma20_above_ma60:
        # 站上MA20但MA20<MA60（反弹中）
        tr_score = 55
    elif not above_ma20 and ma20_above_ma60:
        # 跌破MA20但均线仍多头（回踩）
        tr_score = 45
    else:
        # 空头排列
        tr_score = 20

    tr_detail = (f"现价{latest_close:.2f} {'站上' if above_ma20 else '跌破'}MA20({ma20:.2f})，"
                 f"MA20{'>' if ma20_above_ma60 else '<'}MA60({ma60:.2f})")

    # ── 5. 回撤修复 (权重15%) ──
    # 距阶段高点的距离
    if len(highs) >= 60:
        high_60 = float(highs.tail(60).max())
    elif len(highs) > 0:
        high_60 = float(highs.max())
    else:
        high_60 = latest_close

    drawdown_pct = (latest_close / high_60 - 1) * 100 if high_60 > 0 else 0

    if drawdown_pct >= -2:
        rec_score = 90  # 接近高点，修复充分
    elif drawdown_pct >= -5:
        rec_score = 75
    elif drawdown_pct >= -10:
        rec_score = 60
    elif drawdown_pct >= -15:
        rec_score = 45
    elif drawdown_pct >= -20:
        rec_score = 30
    else:
        rec_score = 15

    rec_detail = f"距60日高点{high_60:.2f}回撤{drawdown_pct:+.1f}%"

    # ── 6. 下行风险 (权重10%) ──
    # 近20日下跌波动率 + 负收益占比
    if len(closes) >= 21:
        rets = closes.pct_change().dropna().tail(20)
        neg_ret = rets[rets < 0]
        neg_ratio = len(neg_ret) / len(rets) if len(rets) > 0 else 0
        # 下跌日平均跌幅
        avg_neg = float(neg_ret.mean()) if len(neg_ret) > 0 else 0
        # 下行波动率（仅下跌日的标准差）
        downside_vol = float(neg_ret.std()) if len(neg_ret) > 1 else 0

        # 下行风险分：neg_ratio越高、跌幅越大 → 分越低
        risk_penalty = neg_ratio * 50 + abs(avg_neg) * 100 + downside_vol * 200
        ds_score = _clip(80 - risk_penalty, 10, 80)
    else:
        ds_score = 50
        neg_ratio = 0
        avg_neg = 0

    ds_detail = (f"近20日下跌{int(neg_ratio*20)}天({neg_ratio*100:.0f}%)，"
                 f"均跌{avg_neg*100:.2f}%，下行波动{downside_vol*100:.2f}%"
                 if len(closes) >= 21 else "数据不足")

    # ── 加权汇总 ──
    total = (
        mf_score * W_MAIN_FORCE +
        rs_score * W_REL_STRENGTH +
        heat_score * W_HEAT +
        tr_score * W_TREND +
        rec_score * W_RECOVERY +
        ds_score * W_DOWNSIDE
    )
    total = round(_clip(total), 1)

    # 状态标签
    if total >= 80:
        label, emoji = "过热", "🌋"
    elif total >= 65:
        label, emoji = "偏热", "🔥"
    elif total >= 45:
        label, emoji = "正常", "🌡️"
    elif total >= 30:
        label, emoji = "偏冷", "🧊"
    else:
        label, emoji = "冰冷", "❄️"

    dimensions = {
        "main_force": {"score": round(mf_score, 1), "weight": W_MAIN_FORCE, "detail": mf_detail, "label": "主力资金"},
        "rel_strength": {"score": round(rs_score, 1), "weight": W_REL_STRENGTH, "detail": rs_detail, "label": "相对强弱"},
        "heat": {"score": round(heat_score, 1), "weight": W_HEAT, "detail": heat_detail, "label": "参与热度"},
        "trend": {"score": round(tr_score, 1), "weight": W_TREND, "detail": tr_detail, "label": "趋势"},
        "recovery": {"score": round(rec_score, 1), "weight": W_RECOVERY, "detail": rec_detail, "label": "回撤修复"},
        "downside": {"score": round(ds_score, 1), "weight": W_DOWNSIDE, "detail": ds_detail, "label": "下行风险"},
    }

    summary_parts = [f"{d['label']}{d['score']:.0f}" for d in dimensions.values()]
    summary = f"{emoji} 情绪温度{total}分（{label}）｜" + " · ".join(summary_parts)

    return {
        "score": total,
        "label": label,
        "emoji": emoji,
        "dimensions": dimensions,
        "summary": summary,
    }


def _fallback_result(reason: str) -> dict:
    return {
        "score": 50,
        "label": "正常",
        "emoji": "🌡️",
        "dimensions": {},
        "summary": f"数据不足（{reason}），默认50分",
        "error": reason,
    }
