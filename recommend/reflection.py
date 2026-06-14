"""反思机制：胜率不达标时的多维诊断 + 策略/约束自动调整。

触发条件：昨日荐股批次胜率 < winrate_threshold（默认 0.7）。

== 反思维度（5 维） ==
1. 选股指标/策略失效  —— 统计各触发策略对应个股的次日收益，定位"拖后腿"的策略，
   按表现自动调降其权重（表现好的调升），实现策略权重的自适应迭代。
2. 追高风险          —— 亏损股是否普遍 RSI 偏高 / 处于 52 周高位，若是则加入
   "RSI 上限 / 价格位置上限"约束，规避追高。
3. 资金流背离        —— 亏损股是否主力资金净流出却被选中，若是则加入
   "要求主力资金净流入"约束。
4. 板块集中度        —— 推荐是否过度集中于某走弱板块，若是则加入"单板块数量上限"约束。
5. 市场情绪误判      —— 对比推荐次日大盘走势，区分"系统性下跌(择时问题)"与"个股选择问题"；
   若系统性下跌则加入"大盘择时"约束（弱市提高入选门槛）。

== 策略自动调整机制 ==
- 策略权重：new_w = clip(old_w * (1 + clip(avg_next_pct/REF, -0.5, 0.5)), 0.2, 2.0)，
  下次选股按新权重加权综合评分。
- 约束条件(filters)：根据上述维度动态收紧（max_rsi / max_pos_52w / require_fund_inflow /
  max_per_sector / market_timing），作为下次选股的硬/软过滤。
- 反思结论(conclusion)：量化诊断汇总；若配置 LLM，则由 DeepSeek 进一步凝练为
  自然语言"约束指令"，连同结构化调整一并落库，融入今日分析逻辑。
"""
from __future__ import annotations

from typing import Optional

from strategies.library import STRATEGY_REGISTRY

# 策略表现 -> 权重调整的参考收益（次日 %），用于归一化调整幅度
_REF_PCT = 3.0


def default_weights() -> dict:
    return {name: 1.0 for name in STRATEGY_REGISTRY}


def default_filters() -> dict:
    return {
        "min_score": 0.0,
        "max_rsi": None,
        "max_pos_52w": None,
        "require_fund_inflow": False,
        "max_per_sector": None,
        "market_timing": False,
    }


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


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

    # ---------- 维度1：选股指标/策略失效 → 调权 ----------
    strat_perf: dict[str, list[float]] = {}
    for p in picks:
        for s in (_factor(p, "buy_strategies", []) or []):
            strat_perf.setdefault(s, []).append(p.get("next_pct", 0.0))

    weight_changes = {}
    for name in weights:
        perf = strat_perf.get(name)
        if not perf:
            continue
        avg = _avg(perf)
        factor = 1 + _clip(avg / _REF_PCT, -0.5, 0.5)
        new_w = round(_clip(weights[name] * factor, 0.2, 2.0), 3)
        if abs(new_w - weights[name]) >= 0.01:
            weight_changes[name] = {"old": weights[name], "new": new_w,
                                    "avg_next_pct": round(avg, 2)}
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

    # ---------- 结论 ----------
    quant_conclusion = _compose_conclusion(prev_win_rate, threshold, dimensions,
                                           weight_changes, cn)
    conclusion = quant_conclusion
    llm_used = 0
    if llm is not None and getattr(llm, "available", False):
        try:
            conclusion = _llm_conclusion(llm, based_on_date, prev_win_rate, threshold,
                                         picks, dimensions) or quant_conclusion
            llm_used = 1
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


def _llm_conclusion(llm, based_on_date, prev_wr, threshold, picks, dimensions) -> str:
    rows = []
    for p in picks:
        rows.append(
            f"- {p.get('name','')}({p.get('symbol')}) 次日{p.get('next_pct',0):+.2f}% "
            f"{'赢' if p.get('is_win') else '输'}｜板块:{p.get('industry','-')}｜"
            f"触发策略:{'、'.join((p.get('factors') or {}).get('buy_strategies', []))}"
        )
    diag = "\n".join(f"- [{d['dimension']}|{d['severity']}] {d['finding']} → {d['action']}"
                     for d in dimensions)
    system = (
        "你是一名严谨的量化交易复盘官。基于昨日荐股的实际结果与量化诊断，"
        "输出一段精炼（150字内）的反思结论，作为今日选股的明确约束指令。"
        "要求：定位失败主因、给出今日须遵守的具体约束，不要空话套话。"
    )
    user = (
        f"昨日批次日期：{based_on_date}\n"
        f"胜率：{prev_wr*100:.0f}%（达标线 {threshold*100:.0f}%）\n\n"
        f"逐只结果：\n" + "\n".join(rows) + "\n\n"
        f"量化诊断：\n" + diag + "\n\n"
        "请给出今日选股的反思结论与约束指令："
    )
    return llm.chat(system, user, max_tokens=400)
