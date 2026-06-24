"""HTML 复盘报告生成器（暗色卡片风格，对标 stock_review 模板）。

按九大板块产出完整盘后复盘（提示词遵循「George 复盘法」）：
 一、大盘总览        二、涨停跌停趋势(近一月折线图)   三、外盘与宏观
 四、板块题材梳理    五、涨停股深度分析(连板梯队)     六、跌停股分析
 七、龙虎榜与资金    八、交易复盘框架(赵老哥/刺客)    九、次日作战计划

正文优先由 LLM 基于当日真实市场数据生成；LLM 不可用时降级为纯数据渲染。
涨停跌停近一月趋势以内联 SVG 折线图呈现（不依赖外部图片）。
"""
from __future__ import annotations

import datetime as dt
import os
import re

import pandas as pd

from agents.llm import get_llm
from config import get_review_config
from data.fetcher import get_fetcher

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# ---------------- HTML 骨架（CSS 取自 stock_review 模板）----------------
HTML_SKELETON = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}} · {{DATE}}</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; background: #0d1117; color: #c9d1d9; line-height: 1.7; padding: 20px; max-width: 900px; margin: 0 auto; }
  h1 { text-align: center; font-size: 24px; color: #58a6ff; margin: 20px 0 8px; letter-spacing: 2px; }
  .subtitle { text-align: center; color: #8b949e; font-size: 13px; margin-bottom: 24px; }
  h2 { font-size: 18px; color: #79c0ff; border-left: 4px solid #58a6ff; padding-left: 12px; margin: 28px 0 14px; }
  h3 { font-size: 15px; color: #d2a8ff; margin: 18px 0 10px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px 20px; margin: 12px 0; }
  .card-gold { border-left: 3px solid #f0b429; }
  .card-red { border-left: 3px solid #f85149; }
  .card-green { border-left: 3px solid #3fb950; }
  .card-blue { border-left: 3px solid #58a6ff; }
  .card-purple { border-left: 3px solid #d2a8ff; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; margin-right: 6px; }
  .tag-red { background: #f8514922; color: #f85149; }
  .tag-green { background: #3fb95022; color: #3fb950; }
  .tag-blue { background: #58a6ff22; color: #58a6ff; }
  .tag-gold { background: #f0b42922; color: #f0b429; }
  .tag-purple { background: #d2a8ff22; color: #d2a8ff; }
  table { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px; }
  th { background: #21262d; color: #8b949e; padding: 8px 12px; text-align: left; font-weight: 600; border-bottom: 1px solid #30363d; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; }
  tr:hover td { background: #161b22; }
  .red { color: #f85149; }
  .green { color: #3fb950; }
  .gold { color: #f0b429; }
  .blue { color: #58a6ff; }
  .dim { color: #8b949e; }
  .big-num { font-size: 28px; font-weight: 700; }
  .stat-row { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
  .stat-box { flex: 1; min-width: 120px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; text-align: center; }
  .stat-box .label { font-size: 12px; color: #8b949e; margin-bottom: 4px; }
  .stat-box .value { font-size: 22px; font-weight: 700; }
  ul, ol { padding-left: 20px; margin: 8px 0; }
  li { margin: 4px 0; font-size: 14px; }
  .divider { border: none; border-top: 1px solid #21262d; margin: 24px 0; }
  .highlight-box { background: linear-gradient(135deg, #1a1f2e 0%, #161b22 100%); border: 1px solid #f0b42944; border-radius: 10px; padding: 16px 20px; margin: 14px 0; }
  .warn-box { background: #f851490d; border: 1px solid #f8514933; border-radius: 10px; padding: 16px 20px; margin: 14px 0; }
  .chart-wrap { background: #0d1117; border-radius: 8px; padding: 10px; }
  .footer { text-align: center; color: #484f58; font-size: 11px; margin-top: 36px; padding-top: 16px; border-top: 1px solid #21262d; }
  .plan-table td:first-child { font-weight: 600; color: #79c0ff; }
  .plan-table td:first-child { font-weight: 600; color: #79c0ff; }
  .nope { text-decoration: line-through; color: #484f58; }
  .mainline-section { margin-top: 30px; padding: 18px; border: 1px solid #f0b42955; border-radius: 14px; background: radial-gradient(circle at top left, #2b211033, transparent 38%), linear-gradient(135deg, #161b22 0%, #10151d 100%); box-shadow: 0 0 0 1px rgba(240,180,41,.08), 0 12px 40px rgba(0,0,0,.18); }
  .mainline-section h2 { margin-top: 4px; color: #f0b429; border-left-color: #f0b429; }
  .mainline-section h3 { color: #79c0ff; margin-top: 20px; }
  .mainline-section blockquote { margin: 10px 0; padding: 12px 14px; border-left: 3px solid #f0b429; background: rgba(240,180,41,.08); border-radius: 8px; color: #d7dde6; }
  .mainline-section table { overflow: hidden; border-radius: 10px; border: 1px solid #30363d; }
  .mainline-section th { background: #2a2116; color: #f0b429; }
  .mainline-section td:first-child { color: #79c0ff; font-weight: 700; }
  .mainline-section p { margin: 10px 0; font-size: 14px; }
  .mainline-section strong { color: #f0b429; }
  .final-summary-section { margin-top: 30px; padding: 24px; border: 2px solid #58a6ff55; border-radius: 16px; background: linear-gradient(135deg, #0d2137 0%, #0d1117 50%, #10151d 100%); box-shadow: 0 0 0 1px rgba(88,166,255,.12), 0 16px 48px rgba(0,0,0,.24); }
  .final-summary-section h2 { color: #58a6ff; border-left-color: #58a6ff; margin-top: 0; }
  .final-summary-section h3 { color: #79c0ff; }
  .final-summary-section blockquote { margin: 10px 0; padding: 12px 14px; border-left: 3px solid #58a6ff; background: rgba(88,166,255,.08); border-radius: 8px; color: #d7dde6; }
  .final-summary-section li { margin: 6px 0; }
  .final-summary-section strong { color: #58a6ff; }
  /* 强度徽章：分段进度条 + 文字（自适应，不再拥挤/重叠）*/
  .strength-bar { display: inline-flex; align-items: center; gap: 8px; padding: 4px 10px;
    border-radius: 20px; background: rgba(255,255,255,0.04); border: 1px solid #30363d;
    font-size: 12px; font-weight: 600; line-height: 1; white-space: nowrap; vertical-align: middle; }
  .strength-bar::before { content: ""; flex-shrink: 0; width: 44px; height: 6px; border-radius: 3px;
    background: linear-gradient(90deg, var(--sb-color, #f0b429) var(--sb-fill, 60%), rgba(255,255,255,0.10) var(--sb-fill, 60%)); }
  .s5 { color: #58a6ff; --sb-color: #58a6ff; --sb-fill: 100%; }
  .s4 { color: #3fb950; --sb-color: #3fb950; --sb-fill: 80%; }
  .s3 { color: #f0b429; --sb-color: #f0b429; --sb-fill: 60%; }
  .s2 { color: #f08229; --sb-color: #f08229; --sb-fill: 40%; }
  .s1 { color: #f85149; --sb-color: #f85149; --sb-fill: 20%; }
  /* 表格列宽优化：避免强度/质量列过窄导致拥挤 */
  td .strength-bar { margin: 0; }
</style>
</head>
<body>

<h1>📊 {{TITLE}}</h1>
<p class="subtitle">{{SUBTITLE}}</p>

{{BODY}}

<div class="warn-box" style="text-align:center;">
  ⚠️ <strong>风险提示</strong>：本报告由 AI 系统基于公开市场数据自动生成，仅供研究参考，不构成任何投资建议。股市有风险，入市需谨慎。
</div>

<div class="footer">
  复盘时间：{{NOW}} ｜ 数据来源：{{SOURCE}}<br>
  AI 盘后复盘 · George 复盘法
</div>

</body>
</html>
"""


# ---------------- 涨停跌停趋势 SVG 折线图 ----------------
def _limit_trend_svg(history: list[dict]) -> str:
    """把近一月涨停/跌停家数渲染成内联 SVG 双折线图（红=涨停、绿=跌停）。"""
    if not history or len(history) < 2:
        return ""
    W, H = 840, 280
    pad_l, pad_r, pad_t, pad_b = 40, 16, 20, 40
    iw, ih = W - pad_l - pad_r, H - pad_t - pad_b
    ups = [int(h.get("limit_up", 0) or 0) for h in history]
    downs = [int(h.get("limit_down", 0) or 0) for h in history]
    dates = [h.get("date", "") for h in history]
    vmax = max(max(ups), max(downs), 10)
    vmax = int(vmax * 1.15) + 1
    n = len(history)

    def _x(i: int) -> float:
        return pad_l + (iw * i / (n - 1) if n > 1 else 0)

    def _y(v: int) -> float:
        return pad_t + ih - (ih * v / vmax)

    def _poly(vals: list[int]) -> str:
        return " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(vals))

    # 网格 + Y 轴刻度
    grid = ""
    for g in range(5):
        gv = round(vmax * g / 4)
        gy = _y(gv)
        grid += (f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{W - pad_r}" y2="{gy:.1f}" '
                 f'stroke="#21262d" stroke-width="1"/>'
                 f'<text x="{pad_l - 6}" y="{gy + 4:.1f}" fill="#484f58" font-size="10" '
                 f'text-anchor="end">{gv}</text>')

    # X 轴标签（最多 8 个，避免拥挤）
    step = max(1, n // 8)
    xlabels = ""
    for i in range(0, n, step):
        xlabels += (f'<text x="{_x(i):.1f}" y="{H - pad_b + 16}" fill="#8b949e" '
                    f'font-size="10" text-anchor="middle">{dates[i]}</text>')

    # 数据点
    pts = ""
    for i, v in enumerate(ups):
        pts += f'<circle cx="{_x(i):.1f}" cy="{_y(v):.1f}" r="2.5" fill="#f85149"/>'
    for i, v in enumerate(downs):
        pts += f'<circle cx="{_x(i):.1f}" cy="{_y(v):.1f}" r="2.5" fill="#3fb950"/>'

    return f"""<div class="chart-wrap"><svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">
  {grid}
  <polyline points="{_poly(ups)}" fill="none" stroke="#f85149" stroke-width="2"/>
  <polyline points="{_poly(downs)}" fill="none" stroke="#3fb950" stroke-width="2"/>
  {pts}{xlabels}
  <rect x="{W - 150}" y="{pad_t}" width="10" height="10" fill="#f85149"/>
  <text x="{W - 136}" y="{pad_t + 9}" fill="#c9d1d9" font-size="11">涨停</text>
  <rect x="{W - 90}" y="{pad_t}" width="10" height="10" fill="#3fb950"/>
  <text x="{W - 76}" y="{pad_t + 9}" fill="#c9d1d9" font-size="11">跌停</text>
</svg></div>"""


# ---------------- 数据收集 ----------------
def _top_limit_up(spot: pd.DataFrame, n: int = 15) -> list[dict]:
    """从全市场快照中提取当日涨停/接近涨停个股。"""
    if spot is None or spot.empty:
        return []
    code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
    name_col = next((c for c in spot.columns if c in ("名称", "name")), None)
    pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
    turn_col = next((c for c in spot.columns if "换手" in c), None)
    if not (code_col and pct_col):
        return []
    df = spot.copy()
    df["_pct"] = pd.to_numeric(df[pct_col], errors="coerce")
    ups = df[df["_pct"] >= 9.8].sort_values("_pct", ascending=False).head(n)
    out = []
    for _, row in ups.iterrows():
        out.append({
            "code": str(row[code_col]),
            "name": str(row[name_col]) if name_col else "",
            "pct": round(float(row["_pct"]), 2),
            "turnover": round(float(pd.to_numeric(row.get(turn_col), errors="coerce") or 0), 2) if turn_col else None,
        })
    return out


def _collect_data(fetcher) -> dict:
    """尽力收集复盘所需的当日真实数据，单项失败不影响其余。"""
    data: dict = {"indices": [], "breadth": {}, "sectors": [], "limit_up": [],
                  "limit_down": [], "lhb_top": [], "global": [], "limit_history": [],
                  "core_large_caps": []}
    try:
        data["indices"] = fetcher.get_index_spot()
    except Exception:
        pass
    try:
        data["breadth"] = fetcher.get_market_breadth()
    except Exception:
        pass
    try:
        data["sectors"] = fetcher.get_hot_sectors(limit=14)
    except Exception:
        pass
    try:
        data["limit_up"] = _top_limit_up(fetcher.get_market_spot(), n=15)
    except Exception:
        pass
    try:
        data["limit_down"] = fetcher.get_limit_down_stocks(n=12)
    except Exception:
        pass
    try:
        data["global"] = fetcher.get_global_indices()
    except Exception:
        pass
    try:
        data["limit_history"] = fetcher.get_limit_history(days=15)
    except Exception:
        pass
    try:
        lhb = fetcher.get_lhb_map()
        top = sorted(lhb.items(), key=lambda kv: kv[1].get("net_buy", 0), reverse=True)[:8]
        out = []
        for code, info in top:
            try:
                nm = fetcher.get_name(code)
            except Exception:
                nm = ""
            out.append({"code": code, "name": nm, "times": info.get("times", 0),
                        "net_buy": round(info.get("net_buy", 0) / 1e8, 2), "last_date": info.get("last_date", "")})
        data["lhb_top"] = out
    except Exception:
        pass
    try:
        spot = fetcher.get_market_spot()
        if spot is not None and not spot.empty:
            code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
            name_col = next((c for c in spot.columns if c in ("名称", "name")), None)
            pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
            turn_col = next((c for c in spot.columns if "换手" in c), None)
            mcap_col = next((c for c in spot.columns if "总市值" in c or "市值" in c), None)
            vol_col = next((c for c in spot.columns if "量比" in c), None)
            if code_col and pct_col and name_col:
                df = spot.copy()
                df["_pct"] = pd.to_numeric(df[pct_col], errors="coerce")
                if mcap_col:
                    df["_mcap"] = pd.to_numeric(df[mcap_col], errors="coerce") / 1e8
                else:
                    df["_mcap"] = 0
                if turn_col:
                    df["_turn"] = pd.to_numeric(df[turn_col], errors="coerce")
                else:
                    df["_turn"] = 0
                if vol_col:
                    df["_vol"] = pd.to_numeric(df[vol_col], errors="coerce")
                else:
                    df["_vol"] = 1.0
                # 筛选：涨跌幅>3% 且 市值>300亿
                caps = df[(df["_pct"].abs() >= 3) & (df["_mcap"] >= 300)].copy()
                if not caps.empty:
                    caps = caps.sort_values("_mcap", ascending=False).head(16)
                out = []
                for _, row in caps.iterrows():
                    out.append({
                        "code": str(row[code_col]),
                        "name": str(row[name_col]),
                        "pct": round(float(row["_pct"]), 2),
                        "mcap": round(float(row["_mcap"]), 0),
                        "turnover": round(float(row["_turn"]), 2),
                        "vol_ratio": round(float(row["_vol"]), 2),
                    })
                data["core_large_caps"] = out
    except Exception:
        pass
    return data


def _data_to_facts(data: dict) -> str:
    """把结构化数据整理成喂给 LLM 的事实文本。"""
    lines: list[str] = []
    if data.get("indices"):
        idx_txt = "；".join(f"{i['name']} {i.get('price','-')}（{i.get('pct')}%）" for i in data["indices"])
        lines.append(f"【主要指数】{idx_txt}")
    b = data.get("breadth") or {}
    if b:
        lines.append(
            f"【市场广度】上涨{b.get('up')}家/下跌{b.get('down')}家/平盘{b.get('flat')}家；"
            f"涨停{b.get('limit_up')}家/跌停{b.get('limit_down')}家；"
            f"全市场平均涨幅{b.get('avg_pct')}%（中位数{b.get('median_pct')}%）"
        )
    if data.get("limit_history"):
        h = data["limit_history"]
        recent = h[-6:]
        trend = "、".join(f"{x['date']}(涨{x['limit_up']}/跌{x['limit_down']})" for x in recent)
        lines.append(f"【涨停跌停近期趋势】{trend}")
    if data.get("global"):
        g_txt = "；".join(f"{x['name']} {x.get('pct')}%" for x in data["global"])
        lines.append(f"【外盘指数】{g_txt}")
    if data.get("sectors"):
        sec_txt = "；".join(f"{s['name']} {s['pct']}%（领涨 {s.get('leader','-')}）" for s in data["sectors"])
        lines.append(f"【行业板块涨幅榜】{sec_txt}")
    if data.get("limit_up"):
        lu_txt = "、".join(f"{p['name']}({p['code']},+{p['pct']}%)" for p in data["limit_up"])
        lines.append(f"【今日涨停/领涨个股】{lu_txt}")
    else:
        lines.append("【今日涨停个股】数据暂缺，请基于板块表现做定性分析")
    if data.get("limit_down"):
        ld_txt = "、".join(f"{p['name']}({p['code']},{p['pct']}%)" for p in data["limit_down"])
        lines.append(f"【今日跌停/领跌个股】{ld_txt}")
    if data.get("lhb_top"):
        lhb_txt = "；".join(f"{x['name']}({x['code']}) 近一月上榜{x['times']}次/净买{x['net_buy']}亿" for x in data["lhb_top"])
        lines.append(f"【龙虎榜资金（近一月统计）】{lhb_txt}")
    if data.get("core_large_caps"):
        caps_txt = "；".join(
            f"{c['name']}({c['code']}) {c['pct']}% 市值{c['mcap']:.0f}亿 换手{c['turnover']}% 量比{c['vol_ratio']}"
            for c in data["core_large_caps"])
        lines.append(f"【全市场核心大票（涨幅>3%且市值>300亿）】{caps_txt}")
    return "\n".join(lines) if lines else "（当日市场数据暂缺）"


# ---------------- LLM 生成正文 ----------------
_SYSTEM = (
    "你是一位实战派A股游资策略师，擅长情绪周期、连板梯队与主线轮动分析，深谙赵老哥、"
    "刺客等顶尖游资的交易哲学。你将收到今日真实市场数据，请撰写一份专业、犀利、可执行的"
    "盘后复盘报告。必须完整输出全部十一大板块，内容务必精炼有干货，切勿因篇幅过长导致中途截断。"
    "仅供研究，不构成投资建议。"
)


def _build_user_prompt(facts: str, custom: str, has_chart: bool) -> str:
    focus = (custom or "").strip()
    focus_block = f"\n\n【复盘侧重点（用户偏好）】\n{focus}\n" if focus else ""
    chart_note = (
        '注意：第二板块「涨停跌停趋势」的近一月折线图会由系统自动插入到 {{TREND_CHART}} 占位符处，'
        '你只需在该板块写一段对趋势的文字解读（放在 <div class="warn-box"> 或 <div class="card"> 中），'
        '并在需要插图的位置原样输出 {{TREND_CHART}} 这一占位符。'
        if has_chart else
        '注意：第二板块「涨停跌停趋势」暂无历史图表数据，请用文字描述近期涨停跌停数量变化趋势即可。'
    )
    return f"""今日真实市场数据如下：
{facts}
{focus_block}
请基于以上真实数据，生成一份盘后复盘报告的【正文 HTML 片段】，严格遵守以下要求。

{chart_note}

== 结构（必须且仅有这十一大板块，板块之间用 <hr class="divider"> 分隔）==

一、<h2>一、大盘总览</h2>
   - 用 <div class="stat-row"> 放多个 <div class="stat-box">（label/value/涨跌），含三大指数、成交额、涨停数、跌停数、涨跌比
   - 用 <div class="card card-gold"> 给出情绪周期定位（如冰点/修复/分歧/退潮）与一句话定调

二、<h2>二、涨停跌停趋势</h2>
   - 输出 {{TREND_CHART}} 占位符（系统会替换为近一月折线图）
   - 用 <div class="warn-box"> 或 <div class="card card-blue"> 解读涨停/跌停数量变化（情绪升温还是退潮、是否大面）

三、<h2>三、外盘与宏观</h2>
   - 用 <div class="stat-row"> + <div class="stat-box"> 列外盘指数（纳指/费半/恒生科技/KOSPI 等）
   - 用若干 <div class="card card-xxx"> + <span class="tag tag-xxx"> 列 2-4 条关键宏观/消息面事件（地缘/政策/数据/产业）

四、<h2>四、板块题材梳理</h2>
   - <h3>✅ 强势板块</h3> + <table>（板块/强度/核心逻辑/代表个股），把涨停股按板块归类
   - <h3>❌ 重挫板块</h3> + <table>（板块/触发因素/跌停代表）

五、<h2>五、涨停股深度分析</h2>
   - <h3>🔥 连板梯队</h3> + <table>（高度/个股/板块/封板质量/逻辑），梳理龙头与梯队是否完整
   - 「高度」列用 <span class="tag tag-gold">3板</span> 这种徽章
   - <h3>涨停驱动分类</h3>：用 <div class="card card-gold/blue/green"> 分「涨价驱动/政策驱动/避险驱动」并点评持续性

六、<h2>六、跌停股分析</h2>
   - <table>（类型<span class="tag tag-red">/代表/核心原因），分类如高位补跌/利空冲击/板块退潮/概念证伪
   - 用 <div class="warn-box"> 点评杀跌性质与连板晋级率

七、<h2>七、龙虎榜与资金动向</h2>
   - 用 <div class="card card-gold"> + <ul> 列主流资金动向（主力净流入板块、机构/游资席位、北向、是否机构缺席需警惕）

八、<h2>八、交易复盘框架</h2>
   - <h3>✅ 做对的事（可复制）</h3> + <div class="card card-green"><ol>
   - <h3>❌ 要戒的习惯</h3> + <div class="card card-red"><ol>
   - <h3>📖 赵老哥/刺客交易哲学提炼</h3> + <div class="highlight-box">：提炼「只做最强主线/买分歧卖一致/不接退潮飞刀」等可学习的买卖逻辑
   - 注意：这是方法论框架与纪律提炼，不要编造「我的持仓/我的具体买卖记录」

九、<h2>九、次日作战计划</h2>
   - <h3>🎯 重点板块排序</h3> + <div class="card card-gold"><ol>
   - <h3>📋 预选标的与买点</h3> + <table class="plan-table">（标的/方向/买点/止损位）
   - <h3>⛔ 不做清单</h3> + <div class="card card-red"><ul>，用 <li class="nope"> 划掉，结尾强调「不符合计划坚决不做」
   - <h3>📐 仓位管理</h3> + <div class="card card-blue"><ul>

十、<h2>十、主线深度复盘</h2>
   - 用 <section class="mainline-section"> 包裹本板块全部内容
   - 开头用 <blockquote> 说明：本模块不从每日荐股池取样，而是从全市场快照中筛选市值大、涨幅强、换手/量比有资金痕迹的核心大票，判断真正驱动盘面的主线
   - 列出市场广度数据：上涨/下跌家数、涨停/跌停家数
   - <h3>🔥 全市场核心大票热度</h3> + <table>（核心股/主线归属/当日涨幅/市值(亿)/换手/量比/量价信号）
   - 量价信号列：量比>1.5 且涨幅>5% 标 🔥 放量上攻，量比<0.9 标 ➖ 平淡换手
   - <h3>🔍 主线甄别深度复盘</h3>：对各主线分别从「资金/逻辑/标杆/次日信号」四维度做深度分析，给出盘面结构一句话概括
   - <h3>次日生死信号</h3>：列出 3-5 条决定性观察点（如某标杆不破位/某板块必须表态/量能总量门槛）、不做清单

十一、<h2>十一、全日复盘总结</h2>
   - 用 <section class="final-summary-section"> 包裹本板块全部内容
   - 这是全文压轴板块，必须由大模型基于以上所有分析做一次全面、深刻的「全日复盘总结」
   - 内容需涵盖：今日盘面核心特征、主线地位确认、风险点、明日关键观察、策略总纲
   - 用 <blockquote> 给出 2-3 条警句格言式的交易箴言
   - 最后用 <p style="font-size:13px;color:#8b949e;"> 强调：以上内容仅供研究交流，不构成任何投资建议

== 样式规则 ==
- 只能使用骨架已定义的 CSS 类：card, card-gold, card-red, card-green, card-blue, card-purple, tag, tag-red, tag-green, tag-blue, tag-gold, tag-purple, stat-row, stat-box, label, value, table/th/td, red, green, gold, blue, dim, big-num, highlight-box, warn-box, divider, plan-table, nope, strength-bar, s1~s5, mainline-section, final-summary-section
- 强度/封板质量必须写成「徽章」：<span class="strength-bar s5">极强</span>，文字放在 span 内部作为标签。等级映射固定为：s5=极强、s4=强、s3=中等、s2=偏弱、s1=弱。严禁让文字溢出或在 span 外另写文字。
- A股习惯「红涨绿跌」：上涨/涨停/利好用 class="red"，下跌/跌停/利空用 class="green"
- 严禁输出 <html>、<head>、<body>、<style> 标签，严禁使用 markdown 代码围栏（```），直接输出 HTML 片段
- 优先使用上面提供的真实数据；数据缺失维度（如部分涨停逻辑、催化剂、外盘）可基于板块特征与常识做合理定性分析，但不要编造精确数字
- 必须输出完整十一大板块，宁可每段精炼也要写全，禁止中途截断
"""


def _clean_llm_html(text: str) -> str:
    """去掉可能的 markdown 围栏与多余包裹标签。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"<body[^>]*>(.*)</body>", t, re.S | re.I)
    if m:
        t = m.group(1)
    return t.strip()


def _build_body_with_llm(llm, facts: str, custom: str, chart_svg: str) -> str:
    content = llm.chat(_SYSTEM, _build_user_prompt(facts, custom, bool(chart_svg)), max_tokens=12000)
    if not content or content.startswith("[LLM"):
        raise RuntimeError(content or "LLM 返回为空")
    body = _clean_llm_html(content)
    # 替换趋势图占位符；若模型漏写占位符则在第二板块后补插
    if chart_svg:
        if "{{TREND_CHART}}" in body:
            body = body.replace("{{TREND_CHART}}", chart_svg)
        else:
            body = re.sub(r"(<h2>二、[^<]*</h2>)", r"\1" + chart_svg, body, count=1)
    else:
        body = body.replace("{{TREND_CHART}}", "")
    return body


# ---------------- 降级：纯数据渲染（九大板块）----------------
def _cls(pct) -> str:
    try:
        return "red" if float(pct) >= 0 else "green"
    except Exception:
        return ""


def _arrow(pct) -> str:
    try:
        return "▲" if float(pct) >= 0 else "▼"
    except Exception:
        return ""


def _build_body_fallback(data: dict, chart_svg: str) -> str:
    parts: list[str] = []
    b = data.get("breadth") or {}

    # 一、大盘总览
    parts.append('<h2>一、大盘总览</h2>')
    idx = data.get("indices") or []
    if idx:
        cells = ""
        for i in idx[:4]:
            c = _cls(i.get("pct"))
            cells += (f'<div class="stat-box"><div class="label">{i["name"]}</div>'
                      f'<div class="value {c}">{i.get("price","-")}</div>'
                      f'<div class="{c}">{_arrow(i.get("pct"))} {i.get("pct")}%</div></div>')
        parts.append(f'<div class="stat-row">{cells}</div>')
    if b:
        ratio = f'{b.get("up","-")}:{b.get("down","-")}'
        parts.append(
            '<div class="stat-row">'
            f'<div class="stat-box"><div class="label">涨停</div><div class="value red">{b.get("limit_up","-")}</div></div>'
            f'<div class="stat-box"><div class="label">跌停</div><div class="value green">{b.get("limit_down","-")}</div></div>'
            f'<div class="stat-box"><div class="label">涨跌家数</div><div class="value dim">{ratio}</div></div>'
            f'<div class="stat-box"><div class="label">平均涨幅</div><div class="value {_cls(b.get("avg_pct"))}">{b.get("avg_pct","-")}%</div></div>'
            '</div>'
        )
        mood = "偏强、赚钱效应回暖" if b.get("up", 0) > b.get("down", 0) else "偏弱、亏钱效应明显"
        parts.append(f'<div class="card card-gold"><strong class="gold">📅 情绪定位</strong><br>'
                     f'全市场上涨 {b.get("up")} 家 / 下跌 {b.get("down")} 家，涨停 {b.get("limit_up")} / 跌停 '
                     f'{b.get("limit_down")}，当日情绪{mood}。</div>')

    parts.append('<hr class="divider">')

    # 二、涨停跌停趋势
    parts.append('<h2>二、涨停跌停趋势</h2>')
    if chart_svg:
        parts.append(f'<div class="card card-blue">{chart_svg}'
                     '<p class="dim" style="text-align:center;font-size:12px;">近一个月涨停/跌停数量走势</p></div>')
        h = data.get("limit_history") or []
        if len(h) >= 2:
            d0, d1 = h[-2], h[-1]
            up_d = d1["limit_up"] - d0["limit_up"]
            dn_d = d1["limit_down"] - d0["limit_down"]
            parts.append(f'<div class="warn-box">最新一日涨停 {d1["limit_up"]} 家（环比{up_d:+d}）、'
                         f'跌停 {d1["limit_down"]} 家（环比{dn_d:+d}）。'
                         f'{"涨停回落、跌停增多，情绪退潮需防大面。" if up_d < 0 or dn_d > 0 else "涨停回升、情绪修复，可适度参与。"}</div>')
    else:
        parts.append('<div class="card">（涨停跌停历史数据暂缺，接口恢复后将自动生成近一月趋势图）</div>')

    parts.append('<hr class="divider">')

    # 三、外盘与宏观
    parts.append('<h2>三、外盘与宏观</h2>')
    g = data.get("global") or []
    if g:
        cells = "".join(
            f'<div class="stat-box"><div class="label">{x["name"]}</div>'
            f'<div class="value {_cls(x.get("pct"))}">{_arrow(x.get("pct"))} {x.get("pct")}%</div></div>'
            for x in g[:8])
        parts.append(f'<div class="stat-row">{cells}</div>')
    else:
        parts.append('<div class="card">（外盘指数数据暂缺）</div>')

    parts.append('<hr class="divider">')

    # 四、板块题材梳理
    parts.append('<h2>四、板块题材梳理</h2>')
    sectors = data.get("sectors") or []
    if sectors:
        ups = [s for s in sectors if (s.get("pct") or 0) >= 0][:5]
        downs = sorted(sectors, key=lambda s: s.get("pct") or 0)[:5]
        bars = [("s5", "极强"), ("s4", "强"), ("s3", "中等"), ("s2", "偏弱"), ("s1", "弱")]
        rows = "".join(
            f'<tr><td><strong class="gold">{s["name"]}</strong></td>'
            f'<td><span class="strength-bar {bars[min(i,4)][0]}">{bars[min(i,4)][1]}</span></td>'
            f'<td class="red">+{s["pct"]}%</td><td>{s.get("leader","-")}</td></tr>'
            for i, s in enumerate(ups))
        if rows:
            parts.append('<h3>✅ 强势板块</h3><table>'
                         '<tr><th>板块</th><th>强度</th><th>涨幅</th><th>领涨股</th></tr>'
                         f'{rows}</table>')
        drows = "".join(
            f'<tr><td class="red"><strong>{s["name"]}</strong></td><td>板块退潮/补跌</td>'
            f'<td class="green">{s["pct"]}%（领跌 {s.get("leader","-")}）</td></tr>'
            for s in downs if (s.get("pct") or 0) < 0)
        if drows:
            parts.append('<h3>❌ 重挫板块</h3><table>'
                         '<tr><th>板块</th><th>触发因素</th><th>跌幅代表</th></tr>'
                         f'{drows}</table>')
    else:
        parts.append('<div class="card">（板块数据暂缺）</div>')

    parts.append('<hr class="divider">')

    # 五、涨停股深度分析
    parts.append('<h2>五、涨停股深度分析</h2>')
    lu = data.get("limit_up") or []
    if lu:
        rows = "".join(
            f'<tr><td>{p["name"]}</td><td>{p["code"]}</td><td class="red">+{p["pct"]}%</td>'
            f'<td>{("换手 " + str(p["turnover"]) + "%") if p.get("turnover") is not None else "-"}</td></tr>'
            for p in lu)
        parts.append('<h3>🔥 今日涨停/领涨核心标的</h3><table>'
                     '<tr><th>个股</th><th>代码</th><th>涨幅</th><th>封板/换手</th></tr>'
                     f'{rows}</table>')
        parts.append('<div class="card card-gold"><span class="tag tag-gold">提示</span>'
                     '配置 LLM 后将自动梳理连板梯队高度、封板质量与涨价/政策/避险驱动分类。</div>')
    else:
        parts.append('<div class="card">（涨停个股数据暂缺）</div>')

    parts.append('<hr class="divider">')

    # 六、跌停股分析
    parts.append('<h2>六、跌停股分析</h2>')
    ld = data.get("limit_down") or []
    if ld:
        rows = "".join(
            f'<tr><td><span class="tag tag-red">跌停</span></td><td>{p["name"]}（{p["code"]}）</td>'
            f'<td class="green">{p["pct"]}%</td></tr>' for p in ld)
        parts.append('<table><tr><th>类型</th><th>个股</th><th>跌幅</th></tr>' + rows + '</table>')
        if b:
            parts.append(f'<div class="warn-box">⚠️ 今日跌停 {b.get("limit_down")} 家。'
                         '跌停集中通常意味着跟风盘被清洗、情绪退潮，需警惕连板晋级率走低、不接退潮飞刀。</div>')
    else:
        parts.append('<div class="card">（无跌停个股或数据暂缺）</div>')

    parts.append('<hr class="divider">')

    # 七、龙虎榜与资金动向
    parts.append('<h2>七、龙虎榜与资金动向</h2>')
    lhb = data.get("lhb_top") or []
    if lhb:
        rows = "".join(
            f'<tr><td>{x["name"]}</td><td>{x["code"]}</td><td>{x["times"]}</td><td>{x["net_buy"]} 亿</td></tr>'
            for x in lhb)
        parts.append('<div class="card card-gold"><h3>龙虎榜主流资金（近一月统计）</h3><table>'
                     '<tr><th>个股</th><th>代码</th><th>上榜次数</th><th>净买额</th></tr>'
                     f'{rows}</table></div>')
    else:
        parts.append('<div class="card">（龙虎榜数据暂缺，通常收盘后晚间披露）</div>')

    parts.append('<hr class="divider">')

    # 八、交易复盘框架
    parts.append('<h2>八、交易复盘框架</h2>')
    parts.append('<h3>✅ 做对的事（可复制）</h3>'
                 '<div class="card card-green"><ol>'
                 '<li>只做当日最强主线的前排龙头，弱水三千只取一瓢</li>'
                 '<li>不追高位连板、不跟风杂毛首板</li>'
                 '<li>按主线逻辑（涨价/政策双驱动）选股，胜率高于纯情绪博弈</li>'
                 '</ol></div>')
    parts.append('<h3>❌ 要戒的习惯</h3>'
                 '<div class="card card-red"><ol>'
                 '<li>退潮日满仓追涨、逆势硬扛</li>'
                 '<li>主线不明时频繁交易、临时起意</li>'
                 '<li>跌停板上接飞刀、抱侥幸心理</li>'
                 '</ol></div>')
    parts.append('<h3>📖 赵老哥/刺客交易哲学提炼</h3>'
                 '<div class="highlight-box">'
                 '<p><strong class="gold">赵老哥</strong>：只做市场最强的那一条线；主线不明时空仓等，主线确立后满仓干前排龙头。</p>'
                 '<p><strong class="gold">刺客</strong>：买入分歧、卖出一致；分歧日低吸前排龙头，一致高潮日减仓兑现，核心是不在退潮时接飞刀。</p>'
                 '</div>')

    parts.append('<hr class="divider">')

    # 九、次日作战计划
    parts.append('<h2>九、次日作战计划</h2>')
    top_sec = ""
    if sectors:
        ups = [s for s in sectors if (s.get("pct") or 0) >= 0][:3]
        top_sec = "".join(f'<li><strong class="gold">{s["name"]}</strong>（关注龙头与梯队延续）</li>' for s in ups)
    parts.append('<h3>🎯 重点板块排序</h3><div class="card card-gold"><ol>'
                 + (top_sec or '<li>跟踪当日最强主线的次日延续性</li>') + '</ol></div>')
    parts.append('<h3>⛔ 不做清单</h3><div class="card card-red"><ul>'
                 '<li class="nope">❌ 高位连板（晋级率走低风险大）</li>'
                 '<li class="nope">❌ 跟风杂毛首板</li>'
                 '<li class="nope">❌ 趋势走坏的退潮板块</li>'
                 '<li class="nope">❌ 不符合计划临时起意的票 → <strong class="gold">坚决不做</strong></li>'
                 '</ul></div>')
    parts.append('<h3>📐 仓位管理</h3><div class="card card-blue"><ul>'
                 '<li>分歧/退潮期控仓 <strong>3-5 成</strong>，情绪修复期再加</li>'
                 '<li>单票不超过 <strong>2 成</strong>，分批建仓不一把梭</li>'
                 '<li><strong class="gold">不急跌不买、不深水不吸</strong></li>'
                 '</ul></div>')
    parts.append('<p style="color:#8b949e;font-size:13px;margin-top:8px;">'
                 '💡 配置有效的 LLM API Key 后，本报告将由 AI 游资策略师生成连板梯队、涨停逻辑、'
                 '个股级预选标的与买点止损的深度分析。</p>')

    return "\n".join(parts)


# ---------------- 结构化指标提取（供多日对比）----------------
def _extract_metrics(data: dict) -> dict:
    """从收集的数据中抽取可对比的结构化指标。"""
    sectors = data.get("sectors") or []
    ups = sorted([s for s in sectors if (s.get("pct") or 0) >= 0],
                 key=lambda s: s.get("pct") or 0, reverse=True)[:5]
    b = data.get("breadth") or {}
    return {
        "indices": data.get("indices") or [],
        "breadth": b,
        "global": data.get("global") or [],
        "top_sectors": [{"name": s.get("name"), "pct": s.get("pct"),
                         "leader": s.get("leader")} for s in ups],
        "limit_up_count": b.get("limit_up"),
        "limit_down_count": b.get("limit_down"),
        "avg_pct": b.get("avg_pct"),
    }


def _render(data: dict, config: dict, fetcher, llm) -> str:
    """组装完整 HTML 文档（不落库）。"""
    chart_svg = _limit_trend_svg(data.get("limit_history") or [])
    custom = (config.get("mode_options", {}) or {}).get("ai_summary", {}).get("custom_prompt", "")

    if llm and llm.available:
        try:
            body = _build_body_with_llm(llm, _data_to_facts(data), custom, chart_svg)
        except Exception:
            body = _build_body_fallback(data, chart_svg)
    else:
        body = _build_body_fallback(data, chart_svg)

    now = dt.datetime.now()
    title = config.get("title", "每日盘后复盘")
    date_cn = now.strftime("%Y年%m月%d日")
    subtitle = f"{date_cn} · {_WEEKDAYS[now.weekday()]} · 数据截至收盘 · 数据源：{fetcher.active_source}"

    return (HTML_SKELETON
            .replace("{{TITLE}}", title)
            .replace("{{DATE}}", date_cn)
            .replace("{{SUBTITLE}}", subtitle)
            .replace("{{NOW}}", now.strftime("%Y年%m月%d日 %H:%M"))
            .replace("{{SOURCE}}", fetcher.active_source)
            .replace("{{BODY}}", body))


# ---------------- 对外编排 ----------------
def generate_html_review(config: dict | None = None) -> str:
    """生成完整的 HTML 复盘报告字符串（兼容旧调用，不落库）。"""
    config = config or get_review_config()
    fetcher = get_fetcher()
    llm = get_llm()
    data = _collect_data(fetcher)
    return _render(data, config, fetcher, llm)


def generate_and_store(config: dict | None = None, store: bool = True) -> dict:
    """生成复盘报告并落库，返回 {html, metrics, record?}。

    供 Web 接口使用：一次采集数据 → 渲染 HTML + 抽取结构化指标 → 持久化（同日覆盖）。
    """
    config = config or get_review_config()
    fetcher = get_fetcher()
    llm = get_llm()
    data = _collect_data(fetcher)
    html = _render(data, config, fetcher, llm)
    metrics = _extract_metrics(data)
    title = config.get("title", "每日盘后复盘")
    date = dt.datetime.now().strftime("%Y-%m-%d")

    out = {"html": html, "metrics": metrics, "date": date, "title": title}
    if store:
        try:
            from review.store import save_review
            rec = save_review(date, title, html, metrics)
            out["record"] = {"id": rec["id"], "date": rec["date"],
                             "created_at": rec["created_at"]}
        except Exception:
            pass
    return out


def save_html_review(html: str, out_dir: str = "reports") -> str:
    """保存 HTML 复盘报告，文件名形如 reports/2026-06-15-daily-review.html。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, dt.datetime.now().strftime("%Y-%m-%d") + "-daily-review.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
