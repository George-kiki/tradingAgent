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
    for_json_mode: bool = True,
) -> str:
    """构建 LLM 复核 prompt。

    for_json_mode=True 时输出 JSON 对象格式（兼容 response_format=json_object），
    for_json_mode=False 时输出纯 JSON 数组格式（降级重试用）。
    """

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

    # 根据是否 json_mode 使用不同的输出格式
    if for_json_mode:
        output_format = """**输出格式**：严格返回 JSON 对象，reviews 字段为复核结果数组：
```json
{
  "reviews": [
    {"symbol":"600xxx","score":0.03,"flags":["板块共振"],"reason":"与半导体主线共振，技术面回踩20日线后放量企稳"},
    {"symbol":"000xxx","score":-0.05,"flags":["追高风险"],"reason":"近5日涨幅超15%，短线透支"}
  ]
}
```
score 范围 -0.08 ~ +0.05，0 表示无需调整。只返回 JSON 对象，不要 markdown 包裹或其他文字。"""
    else:
        output_format = """**输出格式**：严格返回纯 JSON 数组，每个元素：
```json
[
  {"symbol":"600xxx","score":0.03,"flags":["板块共振"],"reason":"与半导体主线共振，技术面回踩20日线后放量企稳"},
  {"symbol":"000xxx","score":-0.05,"flags":["追高风险"],"reason":"近5日涨幅超15%，短线透支"}
]
```
score 范围 -0.08 ~ +0.05，0 表示无需调整。只返回 JSON 数组，不要任何 markdown 包裹或其他文字。"""

    prompt = f"""你是A股短线交易风控分析师。系统在 {base_date} 收盘后，用多因子规则筛选出以下候选股用于**尾盘买入、次日卖出**（持仓1天）。

## 市场环境
- 情绪评分: {ms_score}/100（{ms_temp}）
- 综述: {ms_summary}

## 今日主线板块
{chr(10).join(sector_lines) if sector_lines else '  无数据'}

## 规则筛选候选（已排序）
{chr(10).join(candidate_lines)}

## 你的任务
逐只审查每只候选股，只关注与次日短线隔夜风险强相关的信息：
- 追高风险（近5日涨幅>15%则减分）
- 板块共振（与主线板块一致则加分）
- 技术买点（回踩均线放量企稳则加分）
- 流动性（换手/成交额偏低则减分）
- 利空隐患（如有明显利空则减分）

加分范围 +0.01 ~ +0.05，减分范围 -0.01 ~ -0.08，0 表示无需调整。

{output_format}"""
    return prompt


def _parse_llm_response(resp: str) -> Optional[list[dict]]:
    """解析 LLM 返回的 JSON 响应，支持对象和数组两种格式。

    返回解析后的 reviews 列表，失败返回 None。
    """
    import json
    import re

    if not resp or not resp.strip():
        return None

    resp = resp.strip()

    # 尝试多种解析方式
    for attempt, strategy in enumerate([
        "direct_json",     # 直接 json.loads
        "extract_object",  # 提取 {...} 再解析
        "extract_array",   # 提取 [...] 再解析
        "fix_trailing",    # 修复常见 JSON 错误后解析
    ]):
        try:
            if strategy == "direct_json":
                data = json.loads(resp)

            elif strategy == "extract_object":
                start = resp.find("{")
                end = resp.rfind("}")
                if start < 0 or end <= start:
                    continue
                data = json.loads(resp[start:end + 1])

            elif strategy == "extract_array":
                start = resp.find("[")
                end = resp.rfind("]")
                if start < 0 or end <= start:
                    continue
                data = json.loads(resp[start:end + 1])

            elif strategy == "fix_trailing":
                # 修复尾部多余逗号等常见格式错误
                cleaned = re.sub(r',\s*([}\]])', r'\1', resp)
                data = json.loads(cleaned)

            # 统一提取 reviews 列表
            reviews = None
            if isinstance(data, dict):
                if "reviews" in data:
                    reviews = data["reviews"]
                elif "review" in data:
                    reviews = data["review"]
                else:
                    # 可能只有一个 review 对象
                    for v in data.values():
                        if isinstance(v, list):
                            reviews = v
                            break
                    if reviews is None:
                        # 尝试把 dict 的 values 直接当 review
                        if "symbol" in data and "score" in data:
                            reviews = [data]
            elif isinstance(data, list):
                reviews = data

            if not isinstance(reviews, list):
                if attempt < 3:
                    continue
                return None

            # 标准化
            out = []
            for r in reviews:
                if not isinstance(r, dict):
                    continue
                sym = str(r.get("symbol", "")).strip()
                if not sym:
                    continue
                out.append({
                    "symbol": sym,
                    "ai_score": round(float(r.get("score", 0) or 0), 4),
                    "ai_flags": (r.get("flags") if isinstance(r.get("flags"), list)
                                 else ([r["flags"]] if isinstance(r.get("flags"), str) else [])),
                    "ai_reason": str(r.get("reason", "")),
                })

            if not out:
                continue
            return out

        except (json.JSONDecodeError, ValueError, KeyError):
            if attempt >= 3:
                print(f"[LLM复核] 所有解析策略失败, raw(前300): {resp[:300]}")
                return None
            continue

    return None


def llm_review_candidates(
    candidates: list[dict],
    market_sentiment: dict,
    hot_sectors: list[dict],
    base_date: str,
) -> Optional[list[dict]]:
    """LLM 复核候选股，返回 [{symbol, ai_score, ai_flags, ai_reason}]。

    失败/不可用时返回 None，调用方正常跳过。

    返回的 ai_score 会叠加到 final_score 中：
        final = tech + multi + sector + inflow + ai_score

    历史回填时不调用（避免大量 LLM 成本），仅当日实时运行时调用。
    """
    try:
        from agents.llm import get_llm
    except Exception:
        return None

    llm = get_llm()
    if not llm.available:
        print("[LLM复核] LLM不可用，跳过")
        return None

    if not candidates:
        return None

    # 第一次调用：json_mode=True，用 JSON 对象格式的 prompt
    prompt_json_obj = _build_review_prompt(candidates, market_sentiment, hot_sectors, base_date,
                                           for_json_mode=True)
    print(f"[LLM复核] 开始复核 {len(candidates[:10])}只候选（json_mode=True）...")
    try:
        resp = llm.chat(
            system="你是A股短线风控分析师。你输出一个JSON对象，包含reviews数组。不要输出任何非JSON内容。",
            user=prompt_json_obj,
            temperature=0.15,
            max_tokens=2000,
            json_mode=True,
        )
    except Exception as e:
        print(f"[LLM复核] json_mode调用异常: {e}")
        resp = None

    # 尝试解析
    result = _parse_llm_response(resp or "")
    if result:
        print(f"[LLM复核] json_mode解析成功，{len(result)}只票")
        # 校验分数范围
        for o in result:
            o["ai_score"] = max(-0.10, min(0.08, o["ai_score"]))
        return result

    # json_mode 失败，降级：json_mode=False，用纯数组格式 prompt
    print(f"[LLM复核] json_mode失败（resp前100: {(resp or '')[:100]}），降级重试...")
    prompt_arr = _build_review_prompt(candidates, market_sentiment, hot_sectors, base_date,
                                      for_json_mode=False)
    try:
        resp = llm.chat(
            system="你是A股短线风控分析师。严格只输出纯JSON数组，不要任何markdown代码块，不要任何解释文字。",
            user=prompt_arr,
            temperature=0.15,
            max_tokens=2000,
            json_mode=False,
        )
    except Exception as e:
        print(f"[LLM复核] 降级调用异常: {e}")
        return None

    result = _parse_llm_response(resp or "")
    if result:
        print(f"[LLM复核] 降级解析成功，{len(result)}只票")
        for o in result:
            o["ai_score"] = max(-0.10, min(0.08, o["ai_score"]))
        return result

    # 彻底失败
    print(f"[LLM复核] 降级也失败，原始响应(前400): {(resp or '')[:400]}")
    return None
