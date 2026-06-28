"""LLM 两阶段智能选股系统。

=== 架构 ===

Stage 1: 市场主线提取
  ┌──────────────────────────────────────────┐
  │ LLM 分析：全市场数据（指数、广度、涨停、 │
  │ 板块热度、龙虎榜、大票动向）              │
  │          ↓                               │
  │ 输出 JSON：                              │
  │  - main_themes: 主线板块列表（含理由）    │
  │  - constraints: 选股硬约束               │
  │  - risk_warnings: 风险警示                │
  └──────────────────────────────────────────┘

Stage 2: 候选偏离度校验
  ┌──────────────────────────────────────────┐
  │ 规则筛出候选池 → LLM 逐只校验：           │
  │  1. 是否契合 Stage 1 提取的主线？         │
  │  2. 是否存在偏离主线的虚假信号？           │
  │  3. 综合考虑技术面/资金面/板块面给加减分   │
  │          ↓                               │
  │ 输出 JSON：                              │
  │  - on_theme: 契合主线的标的               │
  │  - off_theme: 偏离主线的标的（过滤）       │
  └──────────────────────────────────────────┘

=== 容错策略 ===
- 任一阶段 LLM 不可用时自动降级到纯规则选股
- Stage1 失败 → 用量化板块热度替代
- Stage2 失败 → 按原有规则评分评出（不过滤）
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import threading
from typing import Optional

# ---------- Pipeline 进度状态（供前端轮询）----------
_pipeline_status_lock = threading.Lock()
_pipeline_status: dict = {"running": False, "stage": "idle", "progress": 0,
                           "message": "", "started_at": None, "elapsed_s": 0}


def get_pipeline_status() -> dict:
    """返回当前 LLM Pipeline 的进度状态（线程安全，所有值 JSON 可序列化）。"""
    with _pipeline_status_lock:
        s = dict(_pipeline_status)
        if s.get("started_at") and isinstance(s["started_at"], _dt.datetime):
            s["elapsed_s"] = round((_dt.datetime.now() - s["started_at"]).total_seconds(), 1)
            s["started_at"] = s["started_at"].isoformat()
        else:
            s["started_at"] = None
        return s


def _update_pipeline_status(stage: str, progress: int, message: str):
    """内部使用：更新 pipeline 进度。"""
    with _pipeline_status_lock:
        _pipeline_status["running"] = True
        _pipeline_status["stage"] = stage
        _pipeline_status["progress"] = progress
        _pipeline_status["message"] = message
        if not _pipeline_status["started_at"]:
            _pipeline_status["started_at"] = _dt.datetime.now()


def _finish_pipeline_status():
    """标记 pipeline 完成。"""
    with _pipeline_status_lock:
        _pipeline_status["running"] = False
        _pipeline_status["stage"] = "done"
        _pipeline_status["progress"] = 100
        _pipeline_status["message"] = "完成"
        _pipeline_status["started_at"] = None


def _safe_json_parse(raw: str) -> Optional[dict]:
    """鲁棒 JSON 解析，支持对象和数组格式。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    for strategy in ["direct", "extract_obj", "extract_arr", "fix_comma"]:
        try:
            if strategy == "direct":
                return json.loads(text)
            elif strategy == "extract_obj":
                s, e = text.find("{"), text.rfind("}")
                if s >= 0 and e > s:
                    return json.loads(text[s:e + 1])
            elif strategy == "extract_arr":
                s, e = text.find("["), text.rfind("]")
                if s >= 0 and e > s:
                    return json.loads(text[s:e + 1])
            elif strategy == "fix_comma":
                cleaned = re.sub(r",\s*([}\]])", r"\1", text)
                return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# ============================================================
# Stage 1: LLM 市场主线提取
# ============================================================

_STAGE1_SYSTEM = (
    "你是一位顶级A股策略分析师。你的任务是根据真实市场数据，精准识别当前市场的主线板块，"
    "并制定严格的选股约束条件。你必须以JSON格式输出，不要添加任何其他文字。"
)


def build_stage1_prompt(
    indices: list[dict],
    breadth: dict,
    hot_sectors: list[dict],
    limit_up: list[dict],
    limit_down: list[dict],
    lhb_top: list[dict],
    core_large_caps: list[dict],
    base_date: str,
) -> str:
    """构建 Stage1 市场分析 prompt（精简版，控制 token 消耗）。"""
    parts = [f"日期：{base_date} A股收盘数据"]

    # 指数（仅3个核心）
    if indices:
        core_idx = [i for i in indices if i.get('name') in ('上证指数','深证成指','科创50')][:3]
        if not core_idx:
            core_idx = indices[:3]
        idx_s = "；".join(f"{i['name']}{i.get('pct',0):+.1f}%" for i in core_idx)
        parts.append(f"指数: {idx_s}")

    # 广度（精简）
    if breadth:
        parts.append(
            f"广度: {breadth.get('up','?')}涨/{breadth.get('down','?')}跌 "
            f"涨停{breadth.get('limit_up','?')} 跌停{breadth.get('limit_down','?')} "
            f"均涨{breadth.get('avg_pct','?')}%")

    # 板块（TOP8）
    if hot_sectors:
        sec_s = "；".join(f"{s['name']}+{s.get('pct',s.get('day_pct',0)):.1f}%" for s in hot_sectors[:8])
        parts.append(f"板块TOP8: {sec_s}")

    # 涨停代表（TOP8）
    if limit_up:
        lu_s = " ".join(f"{p['name']}+{p['pct']}%" for p in limit_up[:8])
        parts.append(f"涨停代表: {lu_s}")

    # 跌停代表（TOP5）
    if limit_down:
        ld_s = " ".join(f"{p['name']}{p['pct']}%" for p in limit_down[:5])
        parts.append(f"跌停: {ld_s}")

    # 龙虎榜净买TOP6
    if lhb_top:
        lhb_s = " ".join(f"{x['name']}净{x['net_buy']:.1f}亿" for x in lhb_top[:6])
        parts.append(f"龙虎榜: {lhb_s}")

    # 核心大票（TOP6）
    if core_large_caps:
        cap_s = " ".join(f"{c['name']}+{c['pct']:.1f}%" for c in core_large_caps[:6])
        parts.append(f"大票动向: {cap_s}")

    parts.append("""
识别2-3个主线板块，输出JSON:
{
  "verdict":"一句话定性",
  "main_themes":[{"name":"板块","rank":1,"confidence":0.8,"reason":"依据","play_style":"趋势持有"}],
  "constraints":{"only_mainline":true,"max_per_sector":3,"min_score":0.15}
}
只输出JSON，不要markdown。""")
    return "\n".join(parts)


def parse_stage1_result(raw: str) -> Optional[dict]:
    """解析 Stage1 LLM 输出 → main_themes + constraints。兼容新旧字段名。"""
    data = _safe_json_parse(raw)
    if not data:
        return None
    return {
        "market_verdict": str(data.get("verdict") or data.get("market_verdict", "")),
        "main_themes": data.get("main_themes") or [],
        "hard_constraints": data.get("constraints") or data.get("hard_constraints") or {},
        "risk_warnings": data.get("risk_warnings") or data.get("warnings") or [],
    }


def run_stage1(
    fetcher,
    base_date: str,
    hot_sectors: list[dict],
) -> Optional[dict]:
    """Stage 1: LLM 分析市场，提取主线板块和选股约束。

    Returns: {market_verdict, main_themes, hard_constraints, risk_warnings}
    失败返回 None，调用方降级到纯规则模式。
    """
    try:
        from agents.llm import get_llm
    except Exception:
        return None

    llm = get_llm()
    if not llm.available:
        print("[Stage1] LLM不可用，降级量化模式")
        return None

    # 收集数据
    indices = fetcher.get_index_spot() or []
    breadth = fetcher.get_market_breadth() or {}
    lhb = fetcher.get_lhb_map() or {}

    # 涨停/跌停从快照提取
    limit_up, limit_down = [], []
    try:
        spot = fetcher.get_market_spot()
        if spot is not None and not spot.empty:
            pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
            code_col = next((c for c in spot.columns if c in ("代码", "symbol")), None)
            name_col = next((c for c in spot.columns if c in ("名称", "name")), None)
            if pct_col and code_col:
                spot["_pct"] = spot[pct_col].astype(float)
                ups = spot[spot["_pct"] >= 9.5].sort_values("_pct", ascending=False).head(15)
                downs = spot[spot["_pct"] <= -9.5].sort_values("_pct").head(10)
                for _, r in ups.iterrows():
                    limit_up.append({
                        "code": str(r[code_col]), "name": str(r.get(name_col, "")),
                        "pct": float(r["_pct"])
                    })
                for _, r in downs.iterrows():
                    limit_down.append({
                        "code": str(r[code_col]), "name": str(r.get(name_col, "")),
                        "pct": float(r["_pct"])
                    })
    except Exception:
        pass

    # 核心大票
    core_large_caps = []
    try:
        spot = fetcher.get_market_spot()
        if spot is not None and not spot.empty:
            pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
            code_col = next((c for c in spot.columns if c in ("代码", "symbol")), None)
            name_col = next((c for c in spot.columns if c in ("名称", "name")), None)
            mcap_col = next((c for c in spot.columns if "市值" in c), None)
            turn_col = next((c for c in spot.columns if "换手" in c), None)
            vr_col = next((c for c in spot.columns if "量比" in c), None)
            if pct_col and code_col:
                import pandas as pd
                caps = spot.copy()
                caps["_pct"] = caps[pct_col].astype(float)
                if mcap_col:
                    caps["_mcap"] = pd.to_numeric(caps[mcap_col], errors="coerce") / 1e8
                    caps = caps[caps["_mcap"] >= 300]
                caps = caps[caps["_pct"].abs() >= 3].sort_values("_pct", ascending=False).head(10)
                for _, r in caps.iterrows():
                    core_large_caps.append({
                        "code": str(r[code_col]),
                        "name": str(r.get(name_col, "")),
                        "pct": round(float(r["_pct"]), 2),
                        "mcap": round(float(r.get("_mcap", 0)), 0),
                        "turnover": round(float(r.get(turn_col, 0)), 2) if turn_col else None,
                        "vol_ratio": round(float(r.get(vr_col, 0)), 2) if vr_col else None,
                    })
    except Exception:
        pass

    # 龙虎榜TOP30
    lhb_top = sorted(
        [{"code": k, "name": "", "times": v.get("times", 0),
          "net_buy": (v.get("net_buy", 0) or 0) / 1e8}
         for k, v in lhb.items()],
        key=lambda x: x["net_buy"], reverse=True)[:10]
    for item in lhb_top:
        try:
            item["name"] = fetcher.get_name(item["code"]) or item["code"]
        except Exception:
            item["name"] = item["code"]

    prompt = build_stage1_prompt(
        indices, breadth, hot_sectors, limit_up, limit_down,
        lhb_top, core_large_caps, base_date)

    print(f"[Stage1] 开始分析市场主线（{len(hot_sectors)}个板块）...")
    try:
        resp = llm.chat(
            system=_STAGE1_SYSTEM,
            user=prompt,
            temperature=0.2,
            max_tokens=3000,
            json_mode=True,
        )
    except Exception as e:
        print(f"[Stage1] LLM调用失败: {e}")
        return None

    result = parse_stage1_result(resp or "")
    if result and result.get("main_themes"):
        themes = result["main_themes"]
        print(f"[Stage1] ✅ 识别 {len(themes)} 个主线: "
              f"{', '.join(t.get('name','')[:8] for t in themes[:4])}")
        print(f"[Stage1] 约束: {json.dumps(result.get('hard_constraints',{}), ensure_ascii=False)[:200]}")
        return result

    print(f"[Stage1] ❌ 解析失败, raw前200: {(resp or '')[:200]}")
    return None


# ============================================================
# Stage 2: 候选偏离度校验
# ============================================================

_STAGE2_SYSTEM = (
    "你是一位A股选股风控官。你的任务是严格审核候选股票是否契合当前市场主线，"
    "识别偏离主线的虚假信号并过滤。你必须以JSON格式输出，不要任何其他文字。"
)


def build_stage2_prompt(
    candidates: list[dict],
    main_themes: list[dict],
    constraints: dict,
    base_date: str,
) -> str:
    """构建 Stage2 候选校验 prompt。"""
    theme_lines = []
    for t in main_themes:
        theme_lines.append(
            f"  #{t.get('rank','?')} {t.get('name','')} "
            f"(置信度:{t.get('confidence',0):.0%}) "
            f"- {t.get('reason','')[:80]}")

    cand_lines = []
    for i, c in enumerate(candidates[:15], 1):
        factors = c.get("factors", {})
        hot = c.get("hot_sector") or {}
        funds = c.get("fundamentals") or {}
        cand_lines.append(
            f"#{i} {c.get('name','')}({c['symbol']}) "
            f"| 板块:{c.get('industry','—')}/{hot.get('sector','—')} "
            f"| 评分:{c.get('score',0):.3f} "
            f"| RSI:{factors.get('rsi','?')} "
            f"| 动量:{factors.get('momentum_5d','?')}% "
            f"| 标签:{', '.join(str(t) for t in (c.get('tags',[]) or [])[:4])}"
        )

    return f"""日期: {base_date}

## 市场主线（Stage1 识别）
{chr(10).join(theme_lines)}

## 强制约束
{json.dumps(constraints, ensure_ascii=False, indent=2)}

## 候选股（规则筛选后）
{chr(10).join(cand_lines)}

## 你的任务
逐只审查以上候选股，判断是否与市场主线契合：

**评分维度**（三选一）：
- `on_theme`  = 契合主线板块，可入选
- `fringe`    = 勉强相关但缺乏板块效应，不推荐
- `off_theme` = 完全偏离当前主线，必须过滤

**额外加减分**（叠加到最终评分）：
- `ai_score`: -0.08 ~ +0.05（与主线质量、技术买点、风险相关）

**输出格式**（严格JSON）：
```json
{{
  "decisions": [
    {{
      "symbol": "000xxx",
      "verdict": "on_theme",
      "ai_score": 0.03,
      "reason": "属于半导体主线，回踩20日线后放量企稳",
      "risk_flags": ["高开勿追"]
    }}
  ],
  "summary": "一句话总结本次校验"
}}
```

只输出 JSON，不要 markdown。"""


def parse_stage2_result(raw: str) -> Optional[dict]:
    """解析 Stage2 LLM 输出。"""
    data = _safe_json_parse(raw)
    if not data:
        return None
    decisions = data.get("decisions") or []
    if isinstance(decisions, dict):
        decisions = list(decisions.values())
    return {
        "decisions": decisions,
        "summary": str(data.get("summary", "")),
    }


def run_stage2(
    candidates: list[dict],
    main_themes: list[dict],
    constraints: dict,
    base_date: str,
) -> Optional[dict]:
    """Stage 2: LLM 校验候选股是否偏离主线，过滤离题标的。

    Returns: {
        decisions: [{symbol, verdict, ai_score, reason, risk_flags}],
        summary: str
    }
    失败返回 None，不过滤任何标的。
    """
    try:
        from agents.llm import get_llm
    except Exception:
        return None

    if not candidates or not main_themes:
        return None

    llm = get_llm()
    if not llm.available:
        print("[Stage2] LLM不可用，跳过偏离度校验")
        return None

    prompt = build_stage2_prompt(candidates, main_themes, constraints, base_date)
    print(f"[Stage2] 开始校验 {len(candidates)} 只候选偏离度...")

    try:
        resp = llm.chat(
            system=_STAGE2_SYSTEM,
            user=prompt,
            temperature=0.15,
            max_tokens=3000,
            json_mode=True,
        )
    except Exception as e:
        print(f"[Stage2] LLM调用失败: {e}")
        return None

    result = parse_stage2_result(resp or "")
    if result:
        decisions = result.get("decisions") or []
        on = sum(1 for d in decisions if d.get("verdict") == "on_theme")
        off = sum(1 for d in decisions if d.get("verdict") == "off_theme")
        fringe = sum(1 for d in decisions if d.get("verdict") == "fringe")
        print(f"[Stage2] ✅ 校验完成: 契合{on} 边缘{fringe} 偏离{off}")
        return result

    print(f"[Stage2] ❌ 解析失败")
    return None


def apply_stage2_filter(
    scored: list[dict],
    decisions: list[dict],
    strict_mode: bool = False,
) -> list[dict]:
    """应用 Stage2 校验结果。

    Args:
        scored: 规则评分后的候选列表 [{symbol, score, ...}]
        decisions: Stage2 LLM 返回的 [{symbol, verdict, ai_score, ...}]
        strict_mode: True=仅保留 on_theme；False=fringe 保留但标记

    Returns:
        过滤和加减分后的候选列表
    """
    # 建立 symbol → decision 索引
    dec_map: dict[str, dict] = {}
    for d in decisions:
        sym = str(d.get("symbol", "")).strip().zfill(6)
        if sym:
            dec_map[sym] = d

    out = []
    for c in scored:
        sym = str(c.get("symbol", "")).strip().zfill(6)
        dec = dec_map.get(sym)
        if dec:
            verdict = dec.get("verdict", "on_theme")
            # 过滤偏离标的
            if verdict == "off_theme":
                print(f"[Stage2] 🚫 过滤 {c.get('name',sym)}({sym}): {dec.get('reason','')}")
                continue
            if strict_mode and verdict == "fringe":
                print(f"[Stage2] ⚠️ 过滤边缘标的 {c.get('name',sym)}({sym})")
                continue
            # 叠加 AI 加减分
            ai_score = dec.get("ai_score", 0) or 0
            c["score"] = round(c.get("score", 0) + ai_score, 4)
            c["ai_score"] = round(ai_score, 4)
            c["ai_verdict"] = verdict
            c["ai_reason"] = str(dec.get("reason", ""))
            # 记录风险标记
            flags = dec.get("risk_flags") or []
            if isinstance(flags, str):
                flags = [flags]
            c["ai_flags"] = list(flags)
        else:
            # LLM 未覆盖的标的：保留但标记
            c["ai_verdict"] = "unchecked"
        out.append(c)

    return out


# ============================================================
# 完整两阶段编排
# ============================================================

def run_two_stage_pipeline(
    fetcher,
    engine,  # RecommendEngine 实例
    base_date: str,
    pool: list[str],
    mode: str = "daily",
) -> dict:
    """运行完整的 LLM 两阶段选股流程。

    Returns:
        {
            stage1: Optional[dict],   # 主线分析结果
            stage2: Optional[dict],   # 校验结果
            filtered: list[dict],     # 过滤后的最终候选
            pipeline_used: bool,      # 是否真正用了 LLM pipeline
        }
    """
    pipeline_used = False
    import time as _time

    _update_pipeline_status("fetching", 5, "拉取全市场快照...")
    _t_pre = _time.time()

    # ---- 预拉取全市场快照（会被后续 get_hot_sectors / engine.run 共享缓存）----
    try:
        spot = fetcher.get_market_spot()
    except Exception:
        spot = None

    # ---- Stage 1: 市场主线提取 ----
    _update_pipeline_status("sectors", 15, "聚合热点板块...")
    # 优先用快速自聚合板块（秒级），get_hot_sectors 走 东财→Tushare→AkShare 链可能 ~75s
    hot_sectors = []
    try:
        # 首次尝试：用缓存中已拉取的 spot 自聚合（复盘哥同款逻辑，秒级）
        from recommend.fugupan_stock import _aggregate_sectors_from_spot
        hot_sectors = _aggregate_sectors_from_spot(spot) or []
    except Exception:
        pass

    # 自聚合为空时才走慢路径（get_hot_sectors 内部有 30min TTL 缓存，二次调用也是秒级）
    if not hot_sectors:
        try:
            hot_sectors = fetcher.get_hot_sectors(limit=12) or []
        except Exception:
            pass

    print(f"[Pipeline] 板块聚合完成（{len(hot_sectors)}个热点，耗时{_time.time()-_t_pre:.0f}s）")

    _update_pipeline_status("stage1", 30, f"LLM 分析 {len(hot_sectors)} 个热点板块识别主线...")
    stage1 = run_stage1(fetcher, base_date, hot_sectors)

    extra_filters = None
    if stage1 and stage1.get("hard_constraints"):
        constraints = stage1["hard_constraints"]
        extra_filters = {
            "only_mainline": constraints.get("only_mainline"),
            "max_per_sector": constraints.get("max_per_sector"),
            "exclude_sectors": constraints.get("exclude_sectors"),
            "min_score": constraints.get("min_score"),
            "max_rsi": constraints.get("max_rsi"),
            "require_uptrend": constraints.get("require_uptrend"),
            "require_inflow": constraints.get("require_inflow"),
        }
        pipeline_used = True
        print(f"[Pipeline] Stage1 完成，使用 LLM 主线约束")

    # 运行规则选股（Stage1 约束作为 extra_filters 传入）
    _update_pipeline_status("scoring", 60, "多因子初筛与评分（候选池扫描+技术面+资金面）...")
    result = engine.run(
        as_of=base_date,
        pool=pool,
        extra_filters=extra_filters,
        mode=mode,
        skip_llm_review=True,  # 跳过原有 LLM review，用 Stage2 替代
    )

    if result.get("error"):
        _finish_pipeline_status()
        return {"stage1": stage1, "stage2": None, "filtered": [],
                "pipeline_used": pipeline_used, "error": result["error"]}

    picks = result.get("picks") or []
    # 标记 Stage1 信息
    result["stage1"] = stage1

    # ---- Stage 2: 候选偏离度校验 ----
    if not picks or not stage1 or not stage1.get("main_themes"):
        result["stage2"] = None
        result["pipeline_used"] = pipeline_used
        _finish_pipeline_status()
        return result

    main_themes = stage1["main_themes"]
    constraints = stage1.get("hard_constraints") or {}

    # 用 Stage2 LLM 校验
    _update_pipeline_status("stage2", 85, f"LLM 校验 {len(picks)} 只候选偏离度...")
    stage2 = run_stage2(picks, main_themes, constraints, base_date)

    if stage2 and stage2.get("decisions"):
        decisions = stage2["decisions"]
        # 应用过滤
        filtered = apply_stage2_filter(picks, decisions, strict_mode=False)
        result["picks"] = filtered
        result["stage2"] = stage2
        pipeline_used = True
        print(f"[Pipeline] Stage2 完成，{len(filtered)}只通过校验")
    else:
        result["stage2"] = None
        print("[Pipeline] Stage2 跳过（LLM不可用或解析失败）")

    result["pipeline_used"] = pipeline_used
    _finish_pipeline_status()
    return result
