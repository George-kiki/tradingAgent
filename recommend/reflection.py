"""反思机制：胜率不达标时的多维诊断 + 策略/约束自动调整。

触发条件：昨日荐股批次胜率 < winrate_threshold（默认 0.7）。

== 策略分组与市场环境自适应（P0-1）==
策略按逻辑分组，不同市场环境自动调整各组基础权重：
- trend（趋势组）：均线金叉/MACD/多头趋势/高点突破/放量突破 → 强趋势市增益
- mean_rev（回归组）：KDJ/布林带/RSI/网格 → 震荡市增益
- pattern（形态组）：缠论分型 → 中性
- 市场环境由上证20日均线斜率 + 量能 + 涨跌比三维判定

== 反思维度（6维）==
1. 选股指标/策略失效  —— 统计各触发策略对应个股的次日收益，定位"拖后腿"的策略，
   按表现自动调降其权重（表现好的调升），实现策略权重的自适应迭代。
2. 追高风险          —— 亏损股是否普遍 RSI 偏高 / 处于 52 周高位
3. 资金流背离        —— 亏损股是否主力资金净流出却被选中
4. 板块集中度        —— 推荐是否过度集中于某走弱板块
5. 市场情绪误判      —— 区分"系统性下跌"与"个股选择问题"
6. 基本面/估值误判   —— 亏损股基本面是否偏弱

== 策略自动调整机制 ==
- 策略权重：new_w = clip(old_w * (1 + clip(avg_next_pct/REF, -0.5, 0.5)), 0.2, 2.0)，
  下次选股按新权重加权综合评分。
- 约束条件(filters)：根据维度动态收紧/放松（约束自动过期机制，3天TTL）
- 反思结论(conclusion)：量化诊断汇总；若配置 LLM，则由 DeepSeek 进一步凝练为
  自然语言"约束指令"，连同结构化调整一并落库，融入今日分析逻辑。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from data.fetcher import get_fetcher
from strategies.library import STRATEGY_REGISTRY

# ---------- 策略分组 ----------
STRATEGY_GROUPS = {
    "trend":    ["ma_cross", "macd", "trend", "breakout", "vol_break"],
    "mean_rev": ["kdj", "boll", "rsi", "grid"],
    "pattern":  ["chan"],
}

# 策略->所属组的反向索引
_STRATEGY_TO_GROUP = {}
for _g, _names in STRATEGY_GROUPS.items():
    for _n in _names:
        _STRATEGY_TO_GROUP[_n] = _g


def _is_weak_in_trend(regime: str, group: str) -> bool:
    """判断某策略组在当前环境下是否被削弱。"""
    if regime == "strong_trend":
        return group == "mean_rev"
    if regime == "sideways":
        return group == "trend"
    if regime == "weak":
        return group in ("trend", "pattern")
    return False


def _is_strong_in_trend(regime: str, group: str) -> bool:
    """判断某策略组在当前环境下是否被增强。"""
    if regime == "strong_trend":
        return group == "trend"
    if regime == "sideways":
        return group == "mean_rev"
    return False


# ---------- 市场环境判定 ----------
_REGIME_CACHE: dict[str, str] = {}  # date → regime
_REGIME_CACHE_DATE = ""


def detect_market_regime(base_date: str = "") -> str:
    """三维判定当前市场环境：strong_trend / sideways / weak。

    维度1：上证20日均线斜率 → 趋势方向
    维度2：近5日量能变化   → 放量/缩量
    维度3：涨跌比         → 赚钱效应

    缓存一天不变。
    """
    global _REGIME_CACHE, _REGIME_CACHE_DATE
    today = _dt.date.today().strftime("%Y-%m-%d")
    if _REGIME_CACHE_DATE == today:
        return _REGIME_CACHE.get(base_date or today, "sideways")

    _REGIME_CACHE = {}
    _REGIME_CACHE_DATE = today
    try:
        fetcher = get_fetcher()
        df = fetcher.get_index_kline("sh000001", days=120)
        if df is None or df.empty or "close" not in df.columns:
            return "sideways"

        df = df.tail(60).reset_index(drop=True)
        closes = df["close"].astype(float)

        # 维度1：20日均线斜率（近5日变化率）
        ma20 = closes.rolling(20).mean()
        if len(ma20) >= 25:
            slope = (float(ma20.iloc[-1]) - float(ma20.iloc[-6])) / float(ma20.iloc[-6])
        else:
            slope = 0.0

        # 维度2：量能（近5日均量 vs 近20日均量）
        if "volume" in df.columns:
            vol = df["volume"].astype(float)
            vol5 = vol.tail(5).mean()
            vol20 = vol.tail(20).mean()
            vol_ratio = float(vol5 / vol20) if vol20 > 0 else 1.0
        else:
            vol_ratio = 1.0

        # 维度3：涨跌比（从全市场广度取）
        try:
            breadth = fetcher.get_market_breadth()
            up_ratio = (breadth.get("up", 0) / max(breadth.get("up", 0) + breadth.get("down", 0), 1)) if breadth else 0.5
        except Exception:
            up_ratio = 0.5

        # 综合判定
        if slope > 0.005 and vol_ratio > 1.1:         # 均线上行 + 放量
            regime = "strong_trend"
        elif slope > 0.002 and up_ratio > 0.45:        # 温和上行 + 赚钱效应尚可
            regime = "strong_trend"
        elif slope < -0.005 and vol_ratio < 0.9:       # 均线下行 + 缩量
            regime = "weak"
        elif slope < -0.002 and up_ratio < 0.35:       # 弱势
            regime = "weak"
        else:
            regime = "sideways"

        _REGIME_CACHE[today] = regime
        return regime
    except Exception:
        return "sideways"


def default_weights(regime: str = "") -> dict:
    """生成策略初始权重，根据市场环境自动分组调整。

    强趋势: 趋势组×1.4 / 回归组×0.6
    震荡:   回归组×1.4 / 趋势组×0.6
    弱势:   趋势组×0.7 / 回归组×0.8 / 整体控仓
    """
    if not regime:
        regime = detect_market_regime()
    w = {}
    for name in STRATEGY_REGISTRY:
        group = _STRATEGY_TO_GROUP.get(name, "pattern")
        if _is_strong_in_trend(regime, group):
            w[name] = 1.4
        elif _is_weak_in_trend(regime, group):
            w[name] = 0.6
        else:
            w[name] = 1.0
    # 弱势环境整体打7折
    if regime == "weak":
        w = {k: round(v * 0.7, 2) for k, v in w.items()}
    return w


def default_filters(regime: str = "") -> dict:
    """生成默认约束，根据市场环境初始化。

    弱势环境：提高最低分门槛、要求上升趋势。
    """
    f = {
        "min_score": 0.0,
        "max_rsi": None,
        "max_pos_52w": None,
        "require_fund_inflow": False,
        "max_per_sector": None,
        "market_timing": False,
        "regime": regime or "sideways",
    }
    if not regime:
        regime = detect_market_regime()
    if regime == "weak":
        f["min_score"] = 0.10
        f["market_timing"] = True
    elif regime == "strong_trend":
        f["min_score"] = 0.0        # 趋势市放宽门槛
    return f


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# 策略表现 -> 权重调整的参考收益（次日 %），用于归一化调整幅度
_REF_PCT = 3.0


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _index_next_pct(fetcher, base_date: str) -> Optional[float]:
    """大盘（上证指数）在 base_date 之后下一交易日的涨跌幅 %。"""
    try:
        df = fetcher.get_index_kline("sh000001", days=400)
        if df is None or df.empty or "date" not in df.columns:
            return None
        dates = df["date"].tolist()
        after = [i for i, d in enumerate(dates) if d > base_date]
        if not after:
            return None
        i = after[0]
        if i == 0:
            return None
        prev_close = float(df.iloc[i - 1]["close"])
        cur_close = float(df.iloc[i]["close"])
        if prev_close:
            return round((cur_close / prev_close - 1) * 100, 2)
    except Exception:
        pass
    return None


def reflect(
    db,
    based_on_date: str,
    reflect_date: str,
    prev_win_rate: float,
    threshold: float,
    prev_weights: dict,
    prev_filters: dict,
    fetcher,
    llm=None,
) -> dict:
    """对昨日批次做多维反思，产出调整后的 weights/filters + 结论。"""
    picks = db.get_picks_with_results(based_on_date)
    weights = dict(prev_weights or default_weights())
    filters = dict(prev_filters or default_filters())
    dimensions: list[dict] = []

    losers = [p for p in picks if not p.get("is_win")]
    winners = [p for p in picks if p.get("is_win")]

    def _factor(p, key, default=None):
        f = p.get("factors") or {}
        return f.get(key, default)

    # ---------- 维度1：选股指标/策略失效 → 调权（EMA平滑 + 多周期验证）----------
    # 加权综合评分替代单一"次日收益率"：
    #   composite = 0.4 * avg_next_pct + 0.35 * avg_3d_pct + 0.25 * win_ratio
    # 通过多周期交叉验证，降低单日噪声对权重调整的误导
    _EMA_ALPHA = 0.3       # 平滑系数
    _W_NEXT1D = 0.4        # 次日收益权重
    _W_NEXT3D = 0.35       # 3日收益权重
    _W_WIN = 0.25          # 胜率权重

    strat_perf: dict[str, list[float]] = {}
    strat_3d: dict[str, list[float]] = {}
    strat_wins: dict[str, tuple[int, int]] = {}  # (wins, total)
    for p in picks:
        nxt = p.get("next_pct", 0.0)
        n3d = p.get("pct_3d")
        is_win = p.get("is_win")
        for s in (_factor(p, "buy_strategies", []) or []):
            strat_perf.setdefault(s, []).append(nxt)
            if n3d is not None:
                strat_3d.setdefault(s, []).append(n3d)
            if is_win is not None:
                w, t = strat_wins.get(s, (0, 0))
                strat_wins[s] = (w + (1 if is_win else 0), t + 1)

    weight_changes = {}
    for name in weights:
        perf = strat_perf.get(name)
        old_w = weights[name]
        if not perf:
            continue

        # 多周期综合评分
        next1d = _avg(perf)
        next3d = _avg(strat_3d.get(name, [])) if strat_3d.get(name) else next1d  # fallback
        wr = strat_wins.get(name)
        win_ratio = wr[0] / wr[1] if wr and wr[1] > 0 else 0.5
        # 胜率映射到 [-3, 3] 区间与收益率对齐
        win_score = (win_ratio - 0.5) * 6.0

        composite = (_W_NEXT1D * next1d + _W_NEXT3D * next3d + _W_WIN * win_score)
        factor = 1 + _clip(composite / _REF_PCT, -0.5, 0.5)
        candidate_w = _clip(old_w * factor, 0.2, 2.0)
        # EMA平滑
        new_w = round(old_w * (1 - _EMA_ALPHA) + candidate_w * _EMA_ALPHA, 3)
        new_w = _clip(new_w, 0.2, 2.0)
        if abs(new_w - old_w) >= 0.005:
            weight_changes[name] = {"old": old_w, "new": new_w,
                                    "composite": round(composite, 2),
                                    "next1d": round(next1d, 2),
                                    "win_ratio": round(win_ratio, 2) if wr else None}
        weights[name] = new_w

    bad_strats = sorted(
        [(n, _avg(v)) for n, v in strat_perf.items() if _avg(v) < 0],
        key=lambda x: x[1],
    )
    cn = {k: v.cn_name for k, v in STRATEGY_REGISTRY.items()}
    if bad_strats:
        dimensions.append({
            "dimension": "选股指标/策略失效",
            "severity": "high" if bad_strats[0][1] < -2 else "medium",
            "finding": "拖累胜率的策略：" + "、".join(
                f"{cn.get(n, n)}(均次日{p:+.2f}%)" for n, p in bad_strats[:3]),
            "action": "已自动调降上述策略权重，调升表现好的策略权重。",
        })
    else:
        dimensions.append({
            "dimension": "选股指标/策略失效",
            "severity": "low",
            "finding": "未发现明显失效策略，权重微调。",
            "action": "按各策略次日表现微调权重。",
        })

    # ---------- 维度2：追高风险 ----------
    loser_rsi = _avg([float(_factor(p, "rsi") or 50) for p in losers])
    loser_pos = _avg([float(_factor(p, "pos_52w") or 50) for p in losers])
    if losers and (loser_rsi > 68 or loser_pos > 82):
        filters["max_rsi"] = 70
        filters["max_pos_52w"] = 85
        dimensions.append({
            "dimension": "追高风险",
            "severity": "high",
            "finding": f"亏损股平均 RSI={loser_rsi:.1f}、52周位={loser_pos:.0f}%，存在追高迹象。",
            "action": "加入约束：RSI≤70 且 52周价格位置≤85%，规避高位股。",
        })
    else:
        dimensions.append({
            "dimension": "追高风险",
            "severity": "low",
            "finding": f"亏损股平均 RSI={loser_rsi:.1f}、52周位={loser_pos:.0f}%，无明显追高。",
            "action": "维持不变。",
        })

    # ---------- 维度3：资金流背离 ----------
    if losers:
        outflow = [p for p in losers if _factor(p, "main_inflow_positive") is False]
        ratio = len(outflow) / len(losers)
        if ratio >= 0.5:
            filters["require_fund_inflow"] = True
            dimensions.append({
                "dimension": "资金流背离",
                "severity": "high",
                "finding": f"{len(outflow)}/{len(losers)} 只亏损股主力资金净流出却被选中。",
                "action": "加入约束：仅推荐主力资金净流入（或中性）的个股。",
            })
        else:
            dimensions.append({
                "dimension": "资金流背离",
                "severity": "low",
                "finding": "亏损股资金流向无系统性背离。",
                "action": "维持不变。",
            })

    # ---------- 维度4：板块集中度 ----------
    sector_perf: dict[str, list[float]] = {}
    for p in picks:
        ind = p.get("industry") or "未知"
        sector_perf.setdefault(ind, []).append(p.get("next_pct", 0.0))
    if sector_perf:
        biggest = max(sector_perf.items(), key=lambda kv: len(kv[1]))
        ind, perfs = biggest
        if len(perfs) >= 3 and _avg(perfs) < 0:
            filters["max_per_sector"] = 2
            dimensions.append({
                "dimension": "板块集中度",
                "severity": "medium",
                "finding": f"过度集中于「{ind}」({len(perfs)}只，均次日{_avg(perfs):+.2f}%)且走弱。",
                "action": "加入约束：单一板块推荐不超过 2 只，分散风险。",
            })
        else:
            dimensions.append({
                "dimension": "板块集中度",
                "severity": "low",
                "finding": "板块分布合理，无集中性拖累。",
                "action": "维持不变。",
            })

    # ---------- 维度5：市场情绪误判（系统性 vs 个股）----------
    idx_pct = _index_next_pct(fetcher, based_on_date)
    avg_ret = _avg([p.get("next_pct", 0.0) for p in picks])
    if idx_pct is not None and idx_pct < -0.8:
        filters["market_timing"] = True
        dimensions.append({
            "dimension": "市场情绪误判",
            "severity": "high",
            "finding": f"次日大盘下跌 {idx_pct:.2f}%，属系统性回调，组合均收益 {avg_ret:+.2f}%。",
            "action": "加入约束：弱市（指数跌破20日线）提高入选门槛、减少持仓暴露。",
        })
    elif idx_pct is not None and idx_pct >= 0 and avg_ret < 0:
        dimensions.append({
            "dimension": "市场情绪误判",
            "severity": "high",
            "finding": f"大盘次日上涨 {idx_pct:.2f}% 但组合均收益 {avg_ret:+.2f}%，跑输大盘，属个股选择问题。",
            "action": "归因为选股而非择时，已通过策略调权与约束收紧应对。",
        })
    else:
        dimensions.append({
            "dimension": "市场情绪误判",
            "severity": "low",
            "finding": f"大盘次日 {('+' if (idx_pct or 0)>=0 else '')}{idx_pct}% ，与组合表现基本一致。",
            "action": "维持不变。",
        })

    # ---------- 维度6：基本面/估值误判 ----------
    def _mf(p, key, sub="score"):
        fd = (p.get("factors") or {}).get("fundamentals") or {}
        return (fd.get(key) or {}).get(sub)

    if losers:
        val_scores = [v for v in (_mf(p, "valuation") for p in losers) if v is not None]
        perf_scores = [v for v in (_mf(p, "performance") for p in losers) if v is not None]
        fund_avg = _avg(val_scores + perf_scores)
        if (val_scores or perf_scores) and fund_avg < 0:
            filters["require_positive_fundamental"] = True
            dimensions.append({
                "dimension": "基本面/估值误判",
                "severity": "medium",
                "finding": f"亏损股基本面偏弱（估值/业绩平均评分 {fund_avg:+.3f}），存在高估或业绩下滑。",
                "action": "加入约束：优先选择基本面综合加分为正（估值合理+业绩稳健）的个股。",
            })
        else:
            dimensions.append({
                "dimension": "基本面/估值误判",
                "severity": "low",
                "finding": "亏损股基本面无系统性偏弱。",
                "action": "维持不变。",
            })

    # ---------- 结论（LLM闭环：结构化输出→直接修改权重）----------
    quant_conclusion = _compose_conclusion(prev_win_rate, threshold, dimensions,
                                           weight_changes, cn)
    conclusion = quant_conclusion
    llm_used = 0
    if llm is not None and getattr(llm, "available", False):
        try:
            text, llm_weights, llm_filters = _llm_conclusion(
                llm, based_on_date, prev_win_rate, threshold, picks, dimensions,
                dict(weights), dict(filters))
            if text:
                conclusion = text
            llm_used = 1
            # LLM 输出的权重直接覆盖量化调整（LLM 优先级更高）
            if llm_weights:
                for name, w in llm_weights.items():
                    if name in weights:
                        old = weights[name]
                        weights[name] = w
                        weight_changes[name] = {"old": old, "new": w, "source": "llm"}
                print(f"[反思] LLM 调权: {len(llm_weights)}个策略")
            # LLM 约束合并
            if llm_filters:
                filters.update(llm_filters)
        except Exception:
            conclusion = quant_conclusion

    adjustments = {
        "weights": weights,
        "filters": filters,
        "weight_changes": weight_changes,
    }
    return {
        "dimensions": dimensions,
        "conclusion": conclusion,
        "adjustments": adjustments,
        "llm_used": llm_used,
    }


def _compose_conclusion(prev_wr, threshold, dimensions, weight_changes, cn) -> str:
    parts = [
        f"昨日荐股胜率 {prev_wr*100:.0f}% 低于达标线 {threshold*100:.0f}%，触发反思迭代。"
    ]
    highs = [d for d in dimensions if d["severity"] == "high"]
    if highs:
        parts.append("主要问题：" + "；".join(d["finding"] for d in highs))
    if weight_changes:
        downs = [f"{cn.get(n,n)}↓" for n, c in weight_changes.items() if c["new"] < c["old"]]
        ups = [f"{cn.get(n,n)}↑" for n, c in weight_changes.items() if c["new"] > c["old"]]
        adj = []
        if downs:
            adj.append("调降 " + "、".join(downs))
        if ups:
            adj.append("调升 " + "、".join(ups))
        if adj:
            parts.append("策略权重：" + "；".join(adj) + "。")
    parts.append("今日将在上述约束下重新选股，以迭代提升胜率。")
    return " ".join(parts)


def _llm_conclusion(llm, based_on_date, prev_wr, threshold, picks, dimensions,
                    current_weights: dict, current_filters: dict) -> tuple[str, dict, dict]:
    """LLM 反思分析 + 结构化权重/约束输出（闭环）。

    Returns: (text_conclusion, weight_adjustments, filter_adjustments)
    """
    import json
    rows = []
    for p in picks:
        rows.append(
            f"- {p.get('name','')}({p.get('symbol')}) 次日{p.get('next_pct',0):+.2f}% "
            f"{'赢' if p.get('is_win') else '输'}｜板块:{p.get('industry','-')}｜"
            f"触发策略:{'、'.join((p.get('factors') or {}).get('buy_strategies', []))}"
        )
    diag = "\n".join(f"- [{d['dimension']}|{d['severity']}] {d['finding']} → {d['action']}"
                     for d in dimensions)

    w_report = [f"  {k}: {v:.2f}" for k, v in (current_weights or {}).items() if v != 1.0]
    cn = {k: v.cn_name for k, v in STRATEGY_REGISTRY.items()}

    system = (
        "你是量化交易复盘官。基于昨日荐股结果，输出反思结论+策略权重调整建议。"
        "权重范围0.2~2.0，调整理由必须引用数据。输出JSON格式。"
    )
    user = (
        f"日期：{based_on_date} | 胜率：{prev_wr*100:.0f}%（线{threshold*100:.0f}%）\n\n"
        f"逐只结果：\n" + "\n".join(rows) + "\n\n"
        f"量化诊断：\n" + diag + "\n\n"
        f"当前非默认权重：\n" + ("\n".join(w_report) if w_report else " 全为1.0(默认)") + "\n\n"
        "输出JSON：\n"
        "{\n"
        '  "conclusion": "一句话反思结论（50字内）",\n'
        '  "adjustments": [\n'
        '    {"strategy":"ma_cross","weight":0.7,"reason":"趋势策略在震荡市信号噪声大"}\n'
        "  ],\n"
        '  "今日约束": "今日选股须遵守的核心约束"\n'
        "}"
    )
    resp = llm.chat(system, user, max_tokens=600)

    # 解析 LLM 的结构化输出
    try:
        data = json.loads(resp or "{}")
    except Exception:
        # 尝试提取 JSON
        import re
        m = re.search(r'\{.*\}', resp or "", re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except Exception:
                data = {}
        else:
            data = {}

    text = data.get("conclusion", "") or (resp or "")[:200]
    weight_adj = {}
    for adj in (data.get("adjustments") or []):
        name = adj.get("strategy", "")
        if name in current_weights:
            w = float(adj.get("weight", current_weights[name]))
            weight_adj[name] = _clip(w, 0.2, 2.0)

    constraint_text = data.get("今日约束", "")
    return text, weight_adj, {"llm_constraint": constraint_text} if constraint_text else {}
