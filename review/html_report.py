"""HTML 复盘报告生成器（暗色卡片风格，模板同款）。

产出四大板块：① 市场面 ② 板块面 ③ 个股面 ④ 次日计划（不含自身交易）。
正文由 LLM 基于当日真实市场数据生成；LLM 不可用时降级为纯数据渲染。
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

# ---------------- HTML 骨架（CSS 取自复盘模板）----------------
HTML_SKELETON = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}} · {{DATE}}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; background: #0d1117; color: #c9d1d9; line-height: 1.7; padding: 20px; }
.container { max-width: 900px; margin: 0 auto; }
h1 { text-align: center; font-size: 28px; color: #f0f6fc; margin-bottom: 8px; letter-spacing: 2px; }
.subtitle { text-align: center; color: #8b949e; font-size: 14px; margin-bottom: 30px; }
h2 { font-size: 20px; color: #58a6ff; border-left: 4px solid #58a6ff; padding-left: 12px; margin: 28px 0 16px; }
h3 { font-size: 16px; color: #f0f6fc; margin: 16px 0 10px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; margin-bottom: 16px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }
.metric { text-align: center; padding: 14px 8px; background: #21262d; border-radius: 8px; border: 1px solid #30363d; }
.metric .label { font-size: 12px; color: #8b949e; margin-bottom: 4px; }
.metric .value { font-size: 22px; font-weight: 700; }
.metric .change { font-size: 12px; margin-top: 2px; }
.red { color: #f85149; }
.green { color: #3fb950; }
.yellow { color: #d29922; }
.blue { color: #58a6ff; }
table { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 14px; }
th { background: #21262d; color: #8b949e; font-weight: 600; text-align: left; padding: 10px 12px; border-bottom: 2px solid #30363d; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
td { padding: 10px 12px; border-bottom: 1px solid #21262d; }
tr:hover td { background: #1c2128; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }
.tag-red { background: rgba(248,81,73,0.15); color: #f85149; }
.tag-green { background: rgba(63,185,80,0.15); color: #3fb950; }
.tag-yellow { background: rgba(210,153,34,0.15); color: #d29922; }
.tag-blue { background: rgba(88,166,255,0.15); color: #58a6ff; }
.summary-box { background: linear-gradient(135deg, #1a1f29, #161b22); border: 1px solid #30363d; border-radius: 10px; padding: 18px 20px; margin: 12px 0; }
.summary-box .title { color: #d29922; font-weight: 700; font-size: 15px; margin-bottom: 8px; }
ul { padding-left: 20px; }
li { margin-bottom: 6px; font-size: 14px; }
.section-icon { margin-right: 6px; }
.divider { border: none; border-top: 1px solid #21262d; margin: 24px 0; }
.disclaimer { text-align: center; color: #484f58; font-size: 12px; margin-top: 30px; padding: 16px; border-top: 1px solid #21262d; }
.emoji-big { font-size: 24px; }
</style>
</head>
<body>
<div class="container">

<h1>📊 {{TITLE}}</h1>
<div class="subtitle">{{SUBTITLE}}</div>

{{BODY}}

<div class="disclaimer">
  ⚠️ 本报告由 AI 系统基于公开市场数据自动生成，仅供参考学习，不构成任何投资建议。<br>
  市场有风险，投资需谨慎。请根据自身风险承受能力独立决策。
</div>

</div>
</body>
</html>
"""


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
    data: dict = {"indices": [], "breadth": {}, "sectors": [], "limit_up": [], "lhb_top": []}
    try:
        data["indices"] = fetcher.get_index_spot()
    except Exception:
        pass
    try:
        data["breadth"] = fetcher.get_market_breadth()
    except Exception:
        pass
    try:
        data["sectors"] = fetcher.get_hot_sectors(limit=12)
    except Exception:
        pass
    try:
        data["limit_up"] = _top_limit_up(fetcher.get_market_spot(), n=15)
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
    if data.get("sectors"):
        sec_txt = "；".join(f"{s['name']} {s['pct']}%（领涨 {s.get('leader','-')}）" for s in data["sectors"])
        lines.append(f"【行业板块涨幅榜】{sec_txt}")
    if data.get("limit_up"):
        lu_txt = "、".join(f"{p['name']}({p['code']},+{p['pct']}%)" for p in data["limit_up"])
        lines.append(f"【今日涨停/领涨个股】{lu_txt}")
    else:
        lines.append("【今日涨停个股】数据暂缺，请基于板块表现做定性分析")
    if data.get("lhb_top"):
        lhb_txt = "；".join(f"{x['name']}({x['code']}) 近一月上榜{x['times']}次/净买{x['net_buy']}亿" for x in data["lhb_top"])
        lines.append(f"【龙虎榜资金（近一月统计）】{lhb_txt}")
    return "\n".join(lines) if lines else "（当日市场数据暂缺）"


# ---------------- LLM 生成正文 ----------------
_SYSTEM = (
    "你是一位资深的A股首席策略师，负责撰写每日盘后复盘报告。"
    "你将收到今日真实市场数据，请据此撰写专业、客观、可执行的复盘正文。"
    "必须完整输出全部四大板块，内容务必精炼，切勿因篇幅过长导致中途截断。"
    "仅供研究，不构成投资建议。"
)


def _build_user_prompt(facts: str, custom: str) -> str:
    focus = (custom or "").strip()
    focus_block = f"\n\n【分析侧重点（用户偏好）】\n{focus}\n" if focus else ""
    return f"""今日真实市场数据如下：
{facts}
{focus_block}
请基于以上真实数据，生成一份盘后复盘报告的【正文 HTML 片段】，严格遵守以下要求：

== 结构（必须且仅有这四大板块，板块之间用 <hr class="divider"> 分隔）==
1. <h2><span class="section-icon">📈</span>一、市场面</h2>
   - 用 <div class="grid-3"> 放三大指数 metric（label/value/change）
   - 用 <div class="card"> 内含 <table> 呈现市场情绪（上涨/下跌家数、涨停/跌停数、平均涨幅、多空比）
   - 用 <div class="summary-box"> + <div class="title">💡 核心判断</div> + <ul> 给出 3-4 条核心判断
2. <h2><span class="section-icon">🔥</span>二、板块面</h2>
   - <div class="grid-3"> 标出领涨/领跌代表板块 metric
   - <div class="card"> + <table> 列「涨幅板块TOP5」（板块/涨幅/催化剂/持续性）
   - <div class="card"> + <table> 列「跌幅板块TOP5」（板块/跌幅/原因/性质）
   - <div class="summary-box"> 给出当日最强主线判断（龙头与梯队是否完整、次日强弱预判）
3. <h2><span class="section-icon">🎯</span>三、个股面</h2>
   - <div class="card"> + <table> 列今日涨停核心标的（个股/代码/涨幅/板块/涨停逻辑与封板质量）
   - 如有龙虎榜数据，再用 <div class="card"> + <table> 呈现主流资金动向
4. <h2><span class="section-icon">📋</span>四、次日计划</h2>
   - <div class="card"> + <table> 给出交易计划（优先级/标的/策略/触发条件/止损位）
   - <div class="card"> + <h3>🚫 次日禁忌</h3> + <ul> 列纪律
   - <div class="summary-box"> + <div class="title">📌 关键观察点</div> + <ul> 列次日重点观察点

== 样式规则 ==
- 只能使用这些 CSS 类：card, grid-3, metric, label, value, change, summary-box, title, table/th/td, tag, tag-red, tag-green, tag-yellow, tag-blue, red, green, yellow, blue, section-icon, divider
- A股习惯「红涨绿跌」：上涨/利好用 class="red"，下跌/利空用 class="green"；箭头用 ▲（涨）/▼（跌）
- 严禁输出 <html>、<head>、<body>、<style> 标签，严禁使用 markdown 代码围栏（```），直接输出 HTML 片段
- 优先使用上面提供的真实数据；数据缺失的维度（如部分涨停逻辑、催化剂）可基于板块特征做合理定性分析，但不要编造精确数字
- 不要包含任何「自身交易/我的持仓/个人操作记录」相关内容
"""


def _clean_llm_html(text: str) -> str:
    """去掉可能的 markdown 围栏与多余包裹标签。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    # 万一模型输出了完整文档，截取 body 内容
    m = re.search(r"<body[^>]*>(.*)</body>", t, re.S | re.I)
    if m:
        t = m.group(1)
    return t.strip()


def _build_body_with_llm(llm, facts: str, custom: str) -> str:
    content = llm.chat(_SYSTEM, _build_user_prompt(facts, custom), max_tokens=8000)
    if not content or content.startswith("[LLM"):
        raise RuntimeError(content or "LLM 返回为空")
    return _clean_llm_html(content)


# ---------------- 降级：纯数据渲染 ----------------
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


def _build_body_fallback(data: dict) -> str:
    parts: list[str] = []

    # ① 市场面
    parts.append('<h2><span class="section-icon">📈</span>一、市场面</h2>')
    idx = data.get("indices") or []
    if idx:
        cells = ""
        for i in idx[:3]:
            c = _cls(i.get("pct"))
            cells += (f'<div class="metric"><div class="label">{i["name"]}</div>'
                      f'<div class="value {c}">{i.get("price","-")}</div>'
                      f'<div class="change {c}">{_arrow(i.get("pct"))} {i.get("pct")}%</div></div>')
        parts.append(f'<div class="grid-3">{cells}</div>')
    b = data.get("breadth") or {}
    if b:
        ratio = (round(b.get("down", 0) / b.get("up"), 2) if b.get("up") else "-")
        parts.append(
            '<div class="card"><h3>市场情绪</h3><table>'
            '<tr><th>指标</th><th>数值</th></tr>'
            f'<tr><td>上涨家数</td><td class="red">{b.get("up")}</td></tr>'
            f'<tr><td>下跌家数</td><td class="green">{b.get("down")}</td></tr>'
            f'<tr><td>涨停</td><td class="red">{b.get("limit_up")} 只</td></tr>'
            f'<tr><td>跌停</td><td class="green">{b.get("limit_down")} 只</td></tr>'
            f'<tr><td>平均涨幅</td><td>{b.get("avg_pct")}%</td></tr>'
            f'<tr><td>多空比</td><td>1 : {ratio}</td></tr>'
            '</table></div>'
        )
        sentiment = "偏强" if b.get("up", 0) > b.get("down", 0) else "偏弱"
        parts.append(
            '<div class="summary-box"><div class="title">💡 核心判断</div><ul>'
            f'<li>全市场上涨 {b.get("up")} 家 / 下跌 {b.get("down")} 家，赚钱效应{sentiment}</li>'
            f'<li>涨停 {b.get("limit_up")} 家 / 跌停 {b.get("limit_down")} 家，平均涨幅 {b.get("avg_pct")}%</li>'
            '</ul></div>'
        )

    parts.append('<hr class="divider">')

    # ② 板块面
    parts.append('<h2><span class="section-icon">🔥</span>二、板块面</h2>')
    sectors = data.get("sectors") or []
    if sectors:
        ups = [s for s in sectors if (s.get("pct") or 0) >= 0][:5]
        downs = sorted(sectors, key=lambda s: s.get("pct") or 0)[:5]
        rows = "".join(
            f'<tr><td>{s["name"]}</td><td class="red">+{s["pct"]}%</td><td>{s.get("leader","-")}</td></tr>'
            for s in ups)
        parts.append(f'<div class="card"><h3>🟢 涨幅板块TOP5</h3><table>'
                     f'<tr><th>板块</th><th>涨幅</th><th>领涨股</th></tr>{rows}</table></div>')
        drows = "".join(
            f'<tr><td>{s["name"]}</td><td class="green">{s["pct"]}%</td><td>{s.get("leader","-")}</td></tr>'
            for s in downs if (s.get("pct") or 0) < 0)
        if drows:
            parts.append(f'<div class="card"><h3>🔴 跌幅板块TOP5</h3><table>'
                         f'<tr><th>板块</th><th>跌幅</th><th>领跌股</th></tr>{drows}</table></div>')
        if ups:
            parts.append(f'<div class="summary-box"><div class="title">🔥 主线判断</div>'
                         f'<ul><li>今日领涨板块为 <strong>{ups[0]["name"]}</strong>（+{ups[0]["pct"]}%），'
                         f'可重点跟踪其龙头与梯队的次日延续性。</li></ul></div>')
    else:
        parts.append('<div class="card">（板块数据暂缺）</div>')

    parts.append('<hr class="divider">')

    # ③ 个股面
    parts.append('<h2><span class="section-icon">🎯</span>三、个股面</h2>')
    lu = data.get("limit_up") or []
    if lu:
        rows = "".join(
            f'<tr><td>{p["name"]}</td><td>{p["code"]}</td><td class="red">+{p["pct"]}%</td>'
            f'<td>{("换手 " + str(p["turnover"]) + "%") if p.get("turnover") is not None else "-"}</td></tr>'
            for p in lu)
        parts.append(f'<div class="card"><h3>今日涨停/领涨核心标的</h3><table>'
                     f'<tr><th>个股</th><th>代码</th><th>涨幅</th><th>换手</th></tr>{rows}</table></div>')
    else:
        parts.append('<div class="card">（涨停个股数据暂缺）</div>')
    lhb = data.get("lhb_top") or []
    if lhb:
        rows = "".join(
            f'<tr><td>{x["name"]}</td><td>{x["code"]}</td><td>{x["times"]}</td><td>{x["net_buy"]} 亿</td></tr>'
            for x in lhb)
        parts.append(f'<div class="card"><h3>龙虎榜主流资金（近一月统计）</h3><table>'
                     f'<tr><th>个股</th><th>代码</th><th>上榜次数</th><th>净买额</th></tr>{rows}</table></div>')

    parts.append('<hr class="divider">')

    # ④ 次日计划
    parts.append('<h2><span class="section-icon">📋</span>四、次日计划</h2>')
    parts.append(
        '<div class="card"><h3>🚫 次日纪律</h3><ul>'
        '<li>❌ 不追涨停板次日高开，宁可错过不可做错</li>'
        '<li>❌ 不抄底趋势转弱板块，不接飞刀</li>'
        '<li>✅ 重点跟踪当日最强主线的龙头与梯队延续性</li>'
        '<li>✅ 严格执行计划，不符合条件坚决不做</li>'
        '</ul></div>'
    )
    parts.append('<div class="summary-box"><div class="title">📌 关键观察点</div><ul>'
                 '<li>🔹 当日最强主线明日能否延续（高开分歧 or 一致涨停）</li>'
                 '<li>🔹 大盘指数关键支撑/压力与量能变化</li>'
                 '<li>🔹 隔夜外盘走势对相关板块的情绪传导</li>'
                 '</ul></div>')
    parts.append('<p style="color:#8b949e;font-size:13px;margin-top:8px;">'
                 '💡 配置有效的 DEEPSEEK_API_KEY 后，本报告将由 AI 首席策略师生成更深度的逻辑分析与个股级次日计划。</p>')

    return "\n".join(parts)


# ---------------- 对外编排 ----------------
def generate_html_review(config: dict | None = None) -> str:
    """生成完整的 HTML 复盘报告字符串。"""
    config = config or get_review_config()
    fetcher = get_fetcher()
    llm = get_llm()

    data = _collect_data(fetcher)
    custom = (config.get("mode_options", {}) or {}).get("ai_summary", {}).get("custom_prompt", "")

    if llm and llm.available:
        try:
            body = _build_body_with_llm(llm, _data_to_facts(data), custom)
        except Exception:
            body = _build_body_fallback(data)
    else:
        body = _build_body_fallback(data)

    now = dt.datetime.now()
    title = config.get("title", "每日盘后复盘")
    date_cn = now.strftime("%Y年%m月%d日")
    subtitle = f"{date_cn} · {_WEEKDAYS[now.weekday()]} · 数据截至收盘 · 数据源：{fetcher.active_source}"

    return (HTML_SKELETON
            .replace("{{TITLE}}", title)
            .replace("{{DATE}}", date_cn)
            .replace("{{SUBTITLE}}", subtitle)
            .replace("{{BODY}}", body))


def save_html_review(html: str, out_dir: str = "reports") -> str:
    """保存 HTML 复盘报告，文件名形如 reports/2026-06-15-daily-review.html。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, dt.datetime.now().strftime("%Y-%m-%d") + "-daily-review.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
