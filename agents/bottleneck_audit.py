"""卡点审计师 —— 严格执行「三问」研究框架。

三问：
  Q1: 需求来源 → 下游真实需求 vs 政策补贴驱动？持续期？
  Q2: 周期长短 → 库存/产能/技术周期所处阶段？
  Q3: 核心瓶颈 → 是否卡住产业链关键环节？替代难度？

输出业务匹配度评分 (0-100) 及证据等级标注。

证据等级来源：
  A: 财报/公告/客户披露实锤
  B: 行业权威数据
  C: 媒体报道交叉验证
  D: 公开搜索推断
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional


# 业务匹配度维度权重
_W_CARD = 0.35       # 卡位稀缺性
_W_DEMAND = 0.25     # 需求持续性
_W_MOAT = 0.20       # 竞争壁垒
_W_CAPACITY = 0.20   # 产能弹性


def audit_bottleneck(
    fetcher,
    symbol: str,
    name: str = "",
    channel_research: Optional[dict] = None,
    llm=None,
) -> dict:
    """执行「三问」卡点审计，输出业务匹配度评分。

    Args:
        fetcher: data.fetcher 实例
        symbol: 股票代码
        name: 股票名称
        channel_research: channel_research 模块输出（可选，提供产业链线索）
        llm: LLM 客户端（可选，调用后获取更丰富的定性判断）

    Returns:
        {
            "available": bool,
            "demand_source": {...},    # Q1
            "cycle_assessment": {...}, # Q2
            "bottleneck_position": {...}, # Q3
            "match_score": 0-100,
            "evidence_level": "B",
            "verified_at": str,
        }
    """
    now_str = _dt.datetime.now().isoformat()
    base = {
        "available": False,
        "demand_source": {},
        "cycle_assessment": {},
        "bottleneck_position": {},
        "match_score": 50,
        "evidence_level": "D",
        "source": "规则推断(无LLM)",
        "verified_at": now_str,
    }

    # 尝试用 LLM 做三问分析
    llm_analysis = None
    if llm and llm.available:
        llm_analysis = _run_llm_audit(llm, name, symbol, channel_research)

    if llm_analysis:
        base.update(llm_analysis)
        base["available"] = True
    else:
        # LLM 不可用 → 基于 channel_research 关键词做基础评分
        base = _fallback_audit(base, channel_research, name, symbol)

    return base


def _run_llm_audit(llm, name: str, symbol: str, channel_research: Optional[dict]) -> Optional[dict]:
    """调用 LLM 执行三问分析 + 业务匹配度评分。"""
    system = (
        "你是专业产业链研究员。请按「三问」框架分析个股的产业链卡位价值。"
        "只输出 JSON，不要任何其他文字。"
    )

    chan_summary = ""
    if channel_research and channel_research.get("available"):
        cr_data = channel_research.get("data") or channel_research
        if isinstance(cr_data, dict):
            lines = []
            for k, v in cr_data.items():
                if isinstance(v, str) and len(v) < 200:
                    lines.append(f"{k}: {v}")
            chan_summary = "\n".join(lines[:20])

    prompt = f"""分析股票：{name}({symbol})

已有产业链线索：
{chan_summary or "无额外线索"}

请按以下 JSON 格式输出你的分析（严格 JSON，不要 markdown 围栏）：

{{
  "demand_source": {{
    "type": "真实需求驱动 / 政策补贴驱动 / 混合型",
    "confidence": 0.5,
    "evidence": "依据（50字以内）",
    "evidence_level": "A/B/C/D"
  }},
  "cycle_assessment": {{
    "type": "库存周期 / 产能周期 / 技术周期",
    "stage": "上行初期 / 上行中期 / 顶峰 / 下行 / 底部",
    "confidence": 0.5,
    "evidence": "依据（50字以内）",
    "evidence_level": "A/B/C/D"
  }},
  "bottleneck_position": {{
    "position": "关键供应商 / 一般供应商 / 外围配套",
    "substitutability": "高 / 中 / 低",
    "rationale": "理由（80字以内）",
    "evidence_level": "A/B/C/D"
  }},
  "match_score": 50,
  "summary": "一句话总结（30字以内）"
}}

评分指南（match_score 0-100）：
- 关键供应商+低替代+真实需求 → 85-95
- 关键供应商+中等替代 → 70-85
- 一般供应商+产能周期上行 → 55-70
- 外围配套/补贴驱动 → 35-55
- 无卡位价值 → 0-35

只输出 JSON。"""
    try:
        resp = llm.chat(system, prompt, temperature=0.3, max_tokens=2000, json_mode=True)
    except Exception:
        return None

    if not resp or resp.startswith("[LLM"):
        return None

    # 解析 LLM JSON
    import json
    for strategy in ["direct", "extract"]:
        try:
            if strategy == "direct":
                data = json.loads(resp)
            else:
                s = resp.find("{")
                e = resp.rfind("}")
                if s >= 0 and e > s:
                    data = json.loads(resp[s:e + 1])
                else:
                    continue
            return {
                "available": True,
                "demand_source": data.get("demand_source", {}),
                "cycle_assessment": data.get("cycle_assessment", {}),
                "bottleneck_position": data.get("bottleneck_position", {}),
                "match_score": int(data.get("match_score", 50)),
                "summary": data.get("summary", ""),
                "evidence_level": _best_evidence(data),
                "source": "LLM分析(基于公开数据)",
                "verified_at": _dt.datetime.now().isoformat(),
            }
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _best_evidence(data: dict) -> str:
    """从 LLM 输出提取最高证据等级。"""
    levels = {"A": 4, "B": 3, "C": 2, "D": 1}
    best = "D"
    best_v = 1
    for section in ["demand_source", "cycle_assessment", "bottleneck_position"]:
        lv = data.get(section, {}).get("evidence_level", "D")
        if lv in levels and levels[lv] > best_v:
            best, best_v = lv, levels[lv]
    return best


def _fallback_audit(base: dict, channel_research, name: str, symbol: str) -> dict:
    """LLM 不可用时的规则兜底：基于关键词+已有数据做基础评分。"""
    # 从 channel_research 中提取线索
    clues = ""
    if channel_research and channel_research.get("available"):
        payload = channel_research.get("data") or channel_research
        if isinstance(payload, dict):
            for k in ("supply_chain_position", "industry_position",
                      "chain_position_summary", "competition", "summary"):
                v = payload.get(k)
                if isinstance(v, str) and len(v) > 10:
                    clues += v[:300] + "\n"
        elif isinstance(payload, str):
            clues = payload[:500]

    # 关键词评分
    keywords = clues.lower() if clues else ""

    card_score = 0
    if any(kw in keywords for kw in ["唯一", "独家", "寡头", "垄断", "核心供应商"]):
        card_score = 85
    elif any(kw in keywords for kw in ["龙头", "头部", "主要供应商", "关键"]):
        card_score = 65
    elif any(kw in keywords for kw in ["供应商", "认证", "客户合作"]):
        card_score = 45
    else:
        card_score = 30

    demand_score = 0
    if any(kw in keywords for kw in ["扩产", "满产", "供不应求", "缺货", "排队"]):
        demand_score = 80
    elif any(kw in keywords for kw in ["增长", "放量", "起量"]):
        demand_score = 60
    elif any(kw in keywords for kw in ["补贴", "政策", "规划"]):
        demand_score = 40  # 政策驱动，需求确定性低
    else:
        demand_score = 50

    moat_score = 0
    if any(kw in keywords for kw in ["专利", "认证", "护城河", "壁垒", "唯一"]):
        moat_score = 80
    elif any(kw in keywords for kw in ["技术", "研发", "领先"]):
        moat_score = 60
    else:
        moat_score = 40

    capacity_score = 0
    if any(kw in keywords for kw in ["扩产", "新产能", "在建", "投产", "产能爬坡"]):
        capacity_score = 75
    elif any(kw in keywords for kw in ["产能", "产线"]):
        capacity_score = 55
    else:
        capacity_score = 50

    match = round(
        card_score * _W_CARD +
        demand_score * _W_DEMAND +
        moat_score * _W_MOAT +
        capacity_score * _W_CAPACITY
    )

    base["available"] = True
    base["demand_source"] = {
        "type": "规则推断",
        "score": demand_score,
        "evidence_level": "D",
    }
    base["cycle_assessment"] = {
        "type": "规则推断",
        "score": capacity_score,
        "evidence_level": "D",
    }
    base["bottleneck_position"] = {
        "position": "待LLM确认",
        "substitutability": "未知",
        "score": card_score,
        "evidence_level": "D",
    }
    base["match_score"] = match
    base["moat_score"] = moat_score
    base["source"] = "规则推断(无LLM)"
    base["evidence_level"] = "D"
    return base
