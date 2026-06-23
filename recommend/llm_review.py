"""LLM 后置复核：对规则筛选后的候选股做风险/机会评估，给出加减分。

设计原则：
- 后置而非前置：只复核规则筛完的 5-10 只候选，不做海量预筛
- 加减分而非否决：ai_score 叠加到 final_score，不单独淘汰
- 降级容错：LLM 不可用/超时/异常时自动跳过，不影响规则选股
- 结果可审计：每只票的 ai_score、ai_flags、ai_reason 写入 DB
"""

from __future__ import annotations

from typing import Optional


def _build_review_prompt(
    candidates: list[dict],
    market_sentiment: dict,
    hot_sectors: list[dict],
    base_date: str,
) -> str:
    """构建 LLM 复核 prompt。"""

    # 市场情绪摘要
    ms_score = market_sentiment.get("score", 50)
    ms_temp = market_sentiment.get("temperature", "中性")
    ms_summary = market_sentiment.get("summary", "")

    # 主线板块摘要
    sector_lines = []
    for s in hot_sectors[:5]:
        name = s.get("name") or s.get("sector", "")
        pct = s.get("pct") or s.get("day_pct") or 0
        sector_lines.append(f"  - {name}: {pct:+.1f}%")

    # 候选股摘要
    candidate_lines = []
    for i, c in enumerate(candidates[:10], 1):
        sym = c["symbol"]
        name = c.get("name", sym)
        industry = c.get("industry", "—")
        entry = c.get("entry_price", "—")
        score = c.get("score", 0)
        factors = c.get("factors", {})
        fundamentals = c.get("fundamentals", {})

        # 技术指标
        rsi = factors.get("rsi", "—")
        mom_5d = factors.get("momentum_5d", "—")
        ma_bull = "多头" if factors.get("ma_bull") else "—"
        buy_count = factors.get("buy_signal_count", 0)
        tags = c.get("tags", [])[:5]

        # 基本面
        pe = fundamentals.get("pe", "—")
        roe = fundamentals.get("roe", "—")
        rev_growth = fundamentals.get("rev_growth", "—")

        # 主线板块
        hot = c.get("hot_sector") or {}
        hot_name = hot.get("sector", "") if hot else ""

        line = (
            f"#{i} {name}({sym}) | 行业:{industry}"
            f" | 入场价:{entry} | 规则评分:{score:.2f}"
        )
        line += f"\n   技术: RSI={rsi} 动量={mom_5d}% 均线={ma_bull} 信号数={buy_count}"
        line += f"\n   标签: {'、'.join(str(t) for t in tags) if tags else '无'}"
        if pe != "—" or roe != "—":
            line += f"\n   基本面: PE={pe} ROE={roe}% 营收增长={rev_growth}%"
        if hot_name:
            rank = hot.get("sector_rank", "?")
            line += f"\n   主线: #{rank} {hot_name}"
        candidate_lines.append(line)

    prompt = f"""你是A股短线交易风控分析师。我的系统在 {base_date} 收盘后，用多因子规则筛选出以下候选股，策略是**尾盘买入、次日卖出**（持仓1天）。

## 市场环境
- 情绪评分: {ms_score}/100（{ms_temp}）
- 综述: {ms_summary}

## 今日主线板块
{chr(10).join(sector_lines) if sector_lines else '  无数据'}

## 规则筛选候选（已排序）
{chr(10).join(candidate_lines)}

## 你的任务
逐只审查每只候选股，从短线隔夜风险角度判断：

**加分项（+0.01 ~ +0.05）：**
- 与当日主线板块高度共振，板块龙头或跟风最强
- 技术面出现经典买点形态（如回踩均线放量起稳）
- 基本面优质且无雷（PE合理、ROE稳定）

**减分项（-0.01 ~ -0.08）：**
- 追高风险：近5日涨幅过大（>15%），短期透支
- 板块退潮：所属板块当日冲高回落或尾盘炸板
- 假突破：放量但无板块共振，疑似诱多
- 利空隐患：新闻面有减持/监管/业绩预警
- 流动性差：成交额偏低、换手不足

**输出格式**：严格返回 JSON 数组，每个元素：
```json
[
  {{"symbol":"600xxx","score":0.03,"flags":["板块共振"],"reason":"与半导体主线共振，技术面回踩20日线后放量企稳"}},
  ...
]
```
score 范围 -0.08 ~ +0.05，0 表示无需调整。flags 为关键标签，reason 一句话（20字内）。
只返回 JSON 数组，不要任何其他文字。"""
    return prompt


def llm_review_candidates(
    candidates: list[dict],
    market_sentiment: dict,
    hot_sectors: list[dict],
    base_date: str,
) -> Optional[list[dict]]:
    """LLM 复核候选股，返回 [{symbol, ai_score, ai_flags, ai_reason}]。

    失败/不可用时返回 None，调用方正常跳过。

    返回的 ai_score 会叠加到 final_score 中：
        final = tech + multi + sector + inflow + (ai_score)

    历史回填时不调用（避免大量 LLM 成本），仅当日实时运行时调用。
    """
    import json

    try:
        from agents.llm import get_llm
    except Exception:
        return None

    llm = get_llm()
    if not llm.available:
        return None

    prompt = _build_review_prompt(candidates, market_sentiment, hot_sectors, base_date)

    try:
        resp = llm.chat(
            system="你是A股短线风控分析师，只输出JSON，不作解释。",
            user=prompt,
            temperature=0.15,
            max_tokens=800,
            json_mode=True,
        )
    except Exception:
        return None

    # 解析 JSON
    try:
        # 清理可能的 markdown 包裹
        resp = resp.strip()
        if resp.startswith("```"):
            resp = resp.split("\n", 1)[-1]
            if resp.endswith("```"):
                resp = resp[:-3]
            resp = resp.strip()
        reviews = json.loads(resp)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(reviews, list):
        return None

    # 标准化输出
    out = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        out.append({
            "symbol": str(r.get("symbol", "")).strip(),
            "ai_score": round(float(r.get("score", 0) or 0), 4),
            "ai_flags": r.get("flags", []) if isinstance(r.get("flags"), list) else [],
            "ai_reason": str(r.get("reason", "")),
        })

    # 校验分数范围
    for o in out:
        o["ai_score"] = max(-0.10, min(0.08, o["ai_score"]))

    return out if out else None
