"""个股深度分析 HTML 报告（UZI 风格：大号评分 + 评审团灯阵 + 多空分歧 + DCF 热力图 + 三情景）。

输出自包含、可离线打开/分享的暗色 HTML 文档；并支持本地持久化（供历史查看/导出）。

免责声明：本报告由 AI 基于公开数据自动生成，所有评分、评委评语、估值测算均为算法输出，
不代表任何真实投资者观点，不构成投资建议。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid

STORE = os.path.join("data_store", "analysis_reports.json")
_MAX = 200

_LIGHT = {"bull": ("🟢", "#3fb950", "看多"),
          "bear": ("🔴", "#f85149", "看空"),
          "neutral": ("⚪", "#8b949e", "中性")}


# ---------------- 存储 ----------------
def _load() -> list[dict]:
    if not os.path.exists(STORE):
        return []
    try:
        with open(STORE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(items[:_MAX], fp, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def save_report(symbol: str, name: str, overall: float, verdict: dict, html: str) -> dict:
    items = _load()
    rec = {
        "id": uuid.uuid4().hex[:12],
        "symbol": symbol, "name": name,
        "overall": overall,
        "verdict": (verdict or {}).get("label"),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "html": html,
    }
    items.insert(0, rec)
    _save(items)
    return rec


def list_reports() -> list[dict]:
    return [{k: r.get(k) for k in ("id", "symbol", "name", "overall", "verdict", "created_at")}
            for r in _load()]


def get_report(rid: str) -> dict | None:
    for r in _load():
        if r.get("id") == rid:
            return r
    return None


def delete_report(rid: str) -> bool:
    items = _load()
    new = [r for r in items if r.get("id") != rid]
    if len(new) == len(items):
        return False
    _save(new)
    return True


# ---------------- HTML 片段构建 ----------------
def _cls(v) -> str:
    try:
        return "up" if float(v) >= 0 else "down"
    except Exception:
        return ""


def _verdict_color(tone: str) -> str:
    return {"buy": "#3fb950", "sell": "#f85149", "hold": "#d29922"}.get(tone, "#58a6ff")


def _build_hero(r: dict) -> str:
    name, symbol = r.get("name", ""), r.get("symbol", "")
    overall = r.get("overall_score", 0)
    v = r.get("verdict") or {}
    vc = _verdict_color(v.get("tone"))
    snap = r.get("snapshot") or {}
    chg = snap.get("change_pct", 0)
    stats = (r.get("jury") or {}).get("stats", {})
    return f"""
<div class="hero">
  <div class="hero-left">
    <div class="hero-name">{name} <span class="hero-code">{symbol}</span></div>
    <div class="hero-price">{snap.get('close','-')} <span class="{_cls(chg)}">{'▲' if (chg or 0)>=0 else '▼'} {chg}%</span></div>
  </div>
  <div class="hero-score">
    <div class="score-ring" style="--c:{vc}">
      <div class="score-num" style="color:{vc}">{overall}</div>
      <div class="score-lbl">综合评分</div>
    </div>
    <div class="verdict-badge" style="background:{vc}22;color:{vc};border-color:{vc}55">
      {v.get('emoji','')} {v.get('label','-')}
    </div>
  </div>
  <div class="hero-right">
    <div class="mini"><b style="color:#3fb950">{stats.get('bull',0)}</b><span>看多</span></div>
    <div class="mini"><b style="color:#8b949e">{stats.get('neutral',0)}</b><span>中性</span></div>
    <div class="mini"><b style="color:#f85149">{stats.get('bear',0)}</b><span>看空</span></div>
    <div class="mini"><b>{r.get('fund_score','-')}</b><span>基本面</span></div>
    <div class="mini"><b>{r.get('consensus_score','-')}</b><span>评委共识</span></div>
  </div>
</div>
<div class="formula">综合评分 = 基本面 {r.get('fund_score','-')} × 0.6 + 评委共识 {r.get('consensus_score','-')} × 0.4 = <b>{overall}</b></div>
"""


def _build_jury(r: dict) -> str:
    jury = r.get("jury") or {}
    judges = jury.get("judges") or []
    if not judges:
        return ""
    cards = ""
    for j in judges:
        emoji, color, txt = _LIGHT.get(j.get("light"), _LIGHT["neutral"])
        hits = "".join(f"<li>{h}</li>" for h in (j.get("hits") or []))
        cards += f"""
        <div class="judge-card" style="border-left:3px solid {color}">
          <div class="judge-head">
            <span class="judge-light">{emoji}</span>
            <span class="judge-name">{j.get('name')}</span>
            <span class="judge-school">{j.get('school')}</span>
            <span class="judge-score" style="color:{color}">{j.get('score')}</span>
          </div>
          <div class="judge-note">{j.get('note')}</div>
          {f'<ul class="judge-hits">{hits}</ul>' if hits else ''}
        </div>"""
    return f"""
<h2>🏛️ 投资大佬评审团</h2>
<div class="hint2">{jury.get('stats',{}).get('active',0)} 位评委 · 共识分 {jury.get('consensus','-')}（看多+0.6×中性占比）。评语为算法基于规则的模拟输出，不代表真实观点。</div>
<div class="judge-grid">{cards}</div>
"""


def _build_divide(r: dict) -> str:
    d = (r.get("jury") or {}).get("divide")
    if not d:
        return ""
    b, s = d.get("bull") or {}, d.get("bear") or {}
    bhits = "".join(f"<li>{h}</li>" for h in (b.get("hits") or []))
    shits = "".join(f"<li>{h}</li>" for h in (s.get("hits") or []))
    return f"""
<h2>⚔️ 多空大分歧 · THE GREAT DIVIDE</h2>
<div class="divide">
  <div class="divide-side bull">
    <div class="divide-head">🐂 最看多 · {b.get('name')} <span>{b.get('score')}分</span></div>
    <div class="divide-note">{b.get('note')}</div>
    <ul>{bhits}</ul>
  </div>
  <div class="divide-vs">VS</div>
  <div class="divide-side bear">
    <div class="divide-head">🐻 最看空 · {s.get('name')} <span>{s.get('score')}分</span></div>
    <div class="divide-note">{s.get('note')}</div>
    <ul>{shits}</ul>
  </div>
</div>
"""


def _build_valuation(r: dict) -> str:
    val = r.get("valuation") or {}
    if not val.get("available"):
        return f'<h2>💰 DCF 估值</h2><div class="card2">{val.get("note","估值数据不足")}</div>'
    a = val.get("assumptions", {})
    hm = val.get("heatmap", {})
    waccs = hm.get("waccs", [])
    term_gs = hm.get("term_gs", [])
    vals = hm.get("values", [])
    price = val.get("price")

    # 热力图：颜色按相对当前价的高低估（绿=低估/上涨空间，红=高估）
    flat = [x for row in vals for x in row if x is not None]
    vmin, vmax = (min(flat), max(flat)) if flat else (0, 1)

    def _cell_color(v):
        if v is None or vmax == vmin:
            return "#21262d"
        # 越高于当前价越绿（低估），越低越红（高估）
        up = (v / price - 1) if price else 0
        if up >= 0.3:
            return "#1a7f37"
        if up >= 0.1:
            return "#2ea043"
        if up >= -0.1:
            return "#9e6a03"
        if up >= -0.3:
            return "#bd561d"
        return "#b62324"

    head = "<tr><th>WACC＼终值g</th>" + "".join(f"<th>{g}%</th>" for g in term_gs) + "</tr>"
    body = ""
    for i, w in enumerate(waccs):
        row = f"<tr><td>{w}%</td>"
        for j, g in enumerate(term_gs):
            v = vals[i][j] if i < len(vals) and j < len(vals[i]) else None
            row += f'<td style="background:{_cell_color(v)};color:#fff">{v if v is not None else "-"}</td>'
        row += "</tr>"
        body += row

    scs = ""
    for sc in val.get("scenarios", []):
        up = sc.get("upside")
        scs += f"""<div class="scenario">
          <div class="sc-label">{sc.get('label')} <span class="hint2">概率 {int(sc.get('prob',0)*100)}%</span></div>
          <div class="sc-val">{sc.get('value','-')} <span class="{_cls(up)}">{('+' if (up or 0)>=0 else '')}{up}%</span></div>
          <div class="hint2">增长 {sc.get('growth')}% · WACC {sc.get('wacc')}%</div>
        </div>"""

    iv = val.get("intrinsic_value")
    up = val.get("upside")
    return f"""
<h2>💰 DCF 估值与三情景</h2>
<div class="val-top">
  <div class="val-box"><div class="vb-num">{iv}</div><div class="vb-lbl">内在价值/股</div></div>
  <div class="val-box"><div class="vb-num {_cls(up)}">{('+' if (up or 0)>=0 else '')}{up}%</div><div class="vb-lbl">较现价空间</div></div>
  <div class="val-box"><div class="vb-num">{val.get('verdict','-')}</div><div class="vb-lbl">估值判断</div></div>
  <div class="val-box"><div class="vb-num">{val.get('weighted_target','-')}</div><div class="vb-lbl">概率加权目标</div></div>
</div>
<div class="hint2">假设：rf {a.get('rf')}% · ERP {a.get('erp')}% · β {a.get('beta')} · WACC {a.get('wacc')}% · 永续 {a.get('terminal_g')}% · 高增长 {a.get('base_growth')}%（{a.get('high_years')}年）</div>
<h3>5×5 敏感性热力图（内在价值/股）</h3>
<table class="heatmap">{head}{body}</table>
<h3>IC 投委会三情景</h3>
<div class="scenarios">{scs}</div>
<div class="hint2">{val.get('note','')}</div>
"""


def _build_scorecard(r: dict) -> str:
    sc = r.get("scorecard") or {}
    if not sc.get("available"):
        return ""
    lvc = {"good": "#3fb950", "mid": "#d29922", "bad": "#f85149", "na": "#8b949e"}
    lvt = {"good": "优", "mid": "中", "bad": "差", "na": "—"}
    cats = ""
    for c in sc.get("categories", []):
        rows = "".join(
            f'<tr><td>{row["metric"]}</td><td style="text-align:right;font-weight:600">{row["value"]}</td>'
            f'<td style="text-align:right"><span style="color:{lvc.get(row["level"])}">{lvt.get(row["level"])}</span></td></tr>'
            for row in c.get("rows", []))
        cats += f'<div class="sc-cat"><h4>{c["name"]}</h4><table>{rows}</table></div>'
    return f"""
<h2>🏦 基本面评分卡</h2>
<div class="hint2">{sc.get('rating')} · {sc.get('good_count')}优/{sc.get('bad_count')}差（共{sc.get('total')}项）{(' · 报告期 '+sc.get('report_date')) if sc.get('report_date') else ''}</div>
<div class="sc-grid">{cats}</div>
"""


def _build_earnings(r: dict) -> str:
    e = r.get("earnings_forecast") or {}
    if not e.get("available"):
        return ""
    sf = e.get("self_forecast") or {}
    latest = sf.get("latest") or {}
    tr = e.get("trend") or {}
    ch = e.get("chain") or {}
    cards = ""
    # 官方预告
    if latest.get("type"):
        pc = ""
        if latest.get("p_change_min") is not None and latest.get("p_change_max") is not None:
            pc = f"净利同比 {latest['p_change_min']}%~{latest['p_change_max']}%"
        cards += (f'<div class="agent-card"><div class="agent-role">📋 官方业绩预告 '
                  f'{latest.get("end_date","")[:6]}</div>'
                  f'<div style="font-weight:700">{latest.get("type","")} {pc}</div>'
                  f'<div class="hint2">{(latest.get("change_reason") or "")[:100]}</div></div>')
    # 产业链景气
    if ch.get("available"):
        cards += (f'<div class="agent-card"><div class="agent-role">🔗 产业链/同业景气</div>'
                  f'<div style="font-weight:700">{ch.get("tone","")}</div>'
                  f'<div class="hint2">同业 {ch.get("up",0)} 家向好 / {ch.get("down",0)} 家承压（板块：{ch.get("sector","-")}）</div></div>')
    # 历史趋势
    if tr.get("direction") and tr.get("direction") != "数据不足":
        cards += (f'<div class="agent-card"><div class="agent-role">📈 历史季报趋势</div>'
                  f'<div style="font-weight:700">{tr.get("direction")}</div>'
                  f'<div class="hint2">{tr.get("note","")}</div></div>')
    llm = ""
    if e.get("llm_view") and not str(e["llm_view"]).startswith("["):
        llm = f'<div class="agent-card"><div class="agent-role">🤖 AI 业绩前瞻推演</div><div>{e["llm_view"]}</div></div>'
    return (f'<h2>🔮 业绩前瞻（官方预告 + 产业链景气 + 趋势外推）</h2>'
            f'<div class="hint2">无真实订单数据，为基于公开信息的概率性推演。</div>'
            f'{cards}{llm}')


def _build_strategies(r: dict) -> str:
    sigs = r.get("strategy_signals") or []
    q = r.get("quant_consensus") or {}
    if not sigs:
        return ""
    rows = "".join(
        f'<tr><td>{s.get("strategy")}</td><td>{s.get("signal")}</td><td>{s.get("score")}</td>'
        f'<td class="hint2">{s.get("reason")}</td></tr>' for s in sigs)
    return f"""
<h2>🔢 量化策略信号</h2>
<div class="hint2">综合评分 {q.get('avg_score')} · {q.get('buy_signals')}买 / {q.get('sell_signals')}卖（共{q.get('total_strategies')}策略）</div>
<table><tr><th>策略</th><th>信号</th><th>评分</th><th>理由</th></tr>{rows}</table>
"""


def _build_agents(r: dict) -> str:
    out = ""
    reports = r.get("analyst_reports")
    if reports:
        cards = "".join(f'<div class="agent-card"><div class="agent-role">{k}</div><div>{v}</div></div>'
                        for k, v in reports.items())
        out += f'<h2>👨‍💼 分析师团队</h2>{cards}'
    dec = r.get("decision") or {}
    if dec and dec.get("mode") == "multi_agent":
        out += f"""<h2>🎯 交易决策（AI）</h2>
        <div class="card2">
          <p><b>结论：</b>{dec.get('action','-')} · 信心 {dec.get('confidence','-')}</p>
          <p>目标价 {dec.get('target_price','-')} ｜ 止损 {dec.get('stop_loss','-')} ｜ 仓位 {dec.get('position','-')} ｜ 周期 {dec.get('horizon','-')}</p>
          <p>{dec.get('summary','')}</p>
        </div>"""
    if r.get("risk_assessment"):
        out += f'<h2>🛡️ 风险评估</h2><div class="card2">{r.get("risk_assessment")}</div>'
    return out


# ---------------- 主入口 ----------------
def build_analysis_html(r: dict) -> str:
    """根据 orchestrator.analyze 的结果构建完整自包含 HTML 报告。"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    body = (_build_hero(r) + _build_jury(r) + _build_divide(r)
            + _build_valuation(r) + _build_earnings(r) + _build_scorecard(r)
            + _build_strategies(r) + _build_agents(r))
    return _SKELETON.replace("{{TITLE}}", f"{r.get('name','')}（{r.get('symbol','')}）深度分析") \
                    .replace("{{NOW}}", now).replace("{{BODY}}", body)


def generate_and_store(symbol: str) -> dict:
    """编排分析 -> 生成 HTML 报告 -> 落库，返回 {result, html, record}。"""
    from agents.orchestrator import AgentOrchestrator
    r = AgentOrchestrator().analyze(symbol)
    html = build_analysis_html(r)
    rec = save_report(symbol, r.get("name", ""), r.get("overall_score", 0),
                      r.get("verdict", {}), html)
    return {"result": r, "html": html,
            "record": {"id": rec["id"], "created_at": rec["created_at"]}}


_SKELETON = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}}</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#0d1117;color:#c9d1d9;line-height:1.7;padding:20px;max-width:980px;margin:0 auto}
h2{font-size:19px;color:#79c0ff;border-left:4px solid #58a6ff;padding-left:12px;margin:30px 0 12px}
h3{font-size:15px;color:#d2a8ff;margin:18px 0 8px}
h4{font-size:13px;color:#8b949e;margin-bottom:6px}
.up{color:#f85149}.down{color:#3fb950}
.hint2{color:#8b949e;font-size:12.5px;margin-bottom:10px}
table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}
th{background:#21262d;color:#8b949e;padding:8px 10px;text-align:left;border-bottom:1px solid #30363d}
td{padding:8px 10px;border-bottom:1px solid #21262d}
.card2{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px 18px;margin:10px 0}
/* Hero */
.hero{display:flex;align-items:center;gap:24px;flex-wrap:wrap;background:linear-gradient(135deg,#1a2230,#141b25);border:1px solid #30363d;border-radius:16px;padding:22px 26px;margin-bottom:8px}
.hero-name{font-size:22px;font-weight:800;color:#f0f6fc}
.hero-code{font-size:13px;color:#8b949e;font-weight:400;margin-left:6px}
.hero-price{font-size:18px;font-weight:700;margin-top:6px}
.hero-score{display:flex;flex-direction:column;align-items:center;gap:8px;margin:0 auto}
.score-ring{width:120px;height:120px;border-radius:50%;border:6px solid var(--c);display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 0 24px -6px var(--c)}
.score-num{font-size:42px;font-weight:900;line-height:1}
.score-lbl{font-size:11px;color:#8b949e}
.verdict-badge{font-size:15px;font-weight:800;padding:6px 18px;border-radius:20px;border:1px solid}
.hero-right{display:flex;gap:14px;flex-wrap:wrap}
.mini{display:flex;flex-direction:column;align-items:center;min-width:52px}
.mini b{font-size:20px;font-weight:800}
.mini span{font-size:11px;color:#8b949e}
.formula{text-align:center;color:#8b949e;font-size:13px;margin:6px 0 4px}
.formula b{color:#f0b429;font-size:15px}
/* 评审团 */
.judge-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:10px}
.judge-card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px}
.judge-head{display:flex;align-items:center;gap:8px}
.judge-light{font-size:14px}
.judge-name{font-weight:700;color:#f0f6fc}
.judge-school{font-size:10.5px;color:#8b949e;background:rgba(255,255,255,.05);padding:1px 7px;border-radius:8px}
.judge-score{margin-left:auto;font-size:18px;font-weight:800}
.judge-note{font-size:12.5px;margin:6px 0 4px}
.judge-hits{padding-left:16px;font-size:11.5px;color:#8b949e}
/* 多空分歧 */
.divide{display:grid;grid-template-columns:1fr auto 1fr;gap:14px;align-items:center}
@media(max-width:680px){.divide{grid-template-columns:1fr}}
.divide-side{border-radius:12px;padding:14px 16px;border:1px solid #30363d}
.divide-side.bull{background:rgba(63,185,80,.07);border-color:rgba(63,185,80,.3)}
.divide-side.bear{background:rgba(248,81,73,.07);border-color:rgba(248,81,73,.3)}
.divide-head{font-weight:800;font-size:15px;margin-bottom:6px}
.divide-head span{float:right;color:#8b949e}
.divide-side ul{padding-left:16px;font-size:12px;color:#8b949e;margin-top:6px}
.divide-vs{font-size:22px;font-weight:900;color:#d29922}
/* 估值 */
.val-top{display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 10px}
.val-box{flex:1;min-width:130px;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;text-align:center}
.vb-num{font-size:22px;font-weight:800}
.vb-lbl{font-size:11.5px;color:#8b949e;margin-top:2px}
.heatmap td,.heatmap th{text-align:center;font-size:12px}
.scenarios{display:flex;gap:12px;flex-wrap:wrap}
.scenario{flex:1;min-width:150px;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px}
.sc-label{font-weight:700;margin-bottom:4px}
.sc-val{font-size:20px;font-weight:800}
.sc-grid,.score-grid{display:grid}
.sc-grid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.sc-cat{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px}
.agent-card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 14px;margin:8px 0}
.agent-role{font-weight:700;color:#79c0ff;margin-bottom:4px}
.foot{text-align:center;color:#484f58;font-size:11px;margin-top:36px;padding-top:16px;border-top:1px solid #21262d}
</style></head><body>
<h1 style="font-size:22px;color:#58a6ff;text-align:center;margin-bottom:18px">📊 个股深度分析报告</h1>
{{BODY}}
<div class="foot">生成时间：{{NOW}} ｜ 本报告由 AI 基于公开数据自动生成，所有评分/评委评语/估值测算均为算法模拟输出，<b>不代表任何真实投资者观点，不构成投资建议</b>。市场有风险，投资需谨慎。</div>
</body></html>
"""
