"""FastAPI Web 服务：提供分析、回测、选股的接口与页面。"""
from __future__ import annotations

import datetime as dt
import os

from fastapi import Body, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from strategies.library import list_strategies

app = FastAPI(title="AI-Agent A股智能分析系统", version="1.0.0")

_BASE = os.path.dirname(__file__)
_STATIC = os.path.join(_BASE, "static")
os.makedirs(_STATIC, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    path = os.path.join(_STATIC, "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/strategies")
def api_strategies():
    return {"strategies": list_strategies()}


@app.get("/api/news-driven")
def api_news_driven(force: bool = Query(False, description="强制重新扫描")):
    """热点驱动板块分析：读取预存结果（秒级）或触发重新扫描。"""
    from agents.news_orchestrator import get_latest_analysis, run_full_scan
    from agents.llm import get_llm
    try:
        if not force:
            # 优先读预存结果（极速响应）
            latest = get_latest_analysis()
            if latest:
                return JSONResponse(latest)

        # force=True 或无预存结果 → 执行完整扫描
        llm = get_llm()
        result = run_full_scan(llm=llm)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/news-driven/history")
def api_news_driven_history(limit: int = Query(10)):
    """热点驱动预分析历史列表。"""
    from agents.news_orchestrator import get_analysis_history
    try:
        return {"items": get_analysis_history(limit=limit)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/news-driven/get")
def api_news_driven_get(id: int = Query(...)):
    """按ID获取某次预分析详情。"""
    from agents.news_orchestrator import get_analysis_by_id
    try:
        result = get_analysis_by_id(id)
        if not result:
            return {"empty": True}
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/news-driven/cleanup")
def api_news_driven_cleanup():
    """手动触发7日数据清理。"""
    from agents.news_orchestrator import cleanup_old_data
    try:
        result = cleanup_old_data()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/news-driven/dates")
def api_news_driven_dates(range: str = Query("3d", description="3d=近三日, all=全部历史")):
    """返回可用日期列表，供前端日期选择器。"""
    from agents.news_orchestrator import get_recent_dates
    try:
        days = 3 if range == "3d" else 0
        dates = get_recent_dates(days=days)
        return JSONResponse({
            "range": range,
            "max_retention_days": 7,
            "dates": dates,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/news-driven/compare")
def api_news_driven_compare(ids: str = Query(..., description="逗号分隔的分析记录ID，2-7个")):
    """多日热点数据交叉分析（纯规则引擎，秒级）。"""
    from agents.news_orchestrator import compare_analyses
    try:
        id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
        if len(id_list) < 2:
            return JSONResponse({"error": "至少选择 2 天数据"}, status_code=400)
        if len(id_list) > 7:
            return JSONResponse({"error": "最多选择 7 天数据"}, status_code=400)
        result = compare_analyses(id_list)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/news-driven/compare-llm")
def api_news_driven_compare_llm(ids: str = Query(..., description="逗号分隔的分析记录ID")):
    """多日对比 + LLM 增强总结。"""
    from agents.news_orchestrator import compare_analyses, _llm_enhance_summary
    try:
        id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
        if len(id_list) < 2:
            return JSONResponse({"error": "至少选择 2 天数据"}, status_code=400)
        result = compare_analyses(id_list)
        llm_summary = _llm_enhance_summary(
            result["trends"]["sectors"],
            result["comparison"],
            result["days"],
        )
        if "error" not in llm_summary:
            result["summary"] = llm_summary
        else:
            result["summary"]["llm_fallback"] = llm_summary.get("error", "LLM 不可用")
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/news-driven/compare-png")
def api_news_driven_compare_png(payload: dict = Body(...)):
    """导出多日对比分析为 PNG 图片。"""
    from review.html_report import _html_to_image
    import tempfile
    try:
        days = payload.get("days", [])
        trends = payload.get("trends", {})
        comparison = payload.get("comparison", {})
        summary = payload.get("summary", {})

        html = _build_compare_html(days, trends, comparison, summary)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                         delete=False, encoding="utf-8") as f:
            f.write(html)
            tmp_html = f.name
        png_path = _html_to_image(tmp_html, os.path.dirname(tmp_html))
        os.unlink(tmp_html)

        if not png_path or not os.path.exists(png_path):
            return JSONResponse({"error": "截图生成失败，请确认已安装 playwright"}, status_code=500)
        fname = f"热点对比分析_{dt.date.today().strftime('%Y%m%d')}.png"
        with open(png_path, "rb") as f:
            img_data = f.read()
        os.unlink(png_path)
        return Response(
            content=img_data,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _build_compare_html(days: list, trends: dict, comparison: dict, summary: dict) -> str:
    """构建多日对比 PNG 导出 HTML 卡片。"""
    days_rows = ""
    for d in days:
        top = (d.get("top_sectors") or [{}])[0]
        days_rows += f"""<tr>
<td style="padding:6px 0;border-bottom:1px solid #21262d;">{d.get('scan_time','')[:10]}</td>
<td style="padding:6px 0;border-bottom:1px solid #21262d;">{d.get('news_count',0)}条</td>
<td style="padding:6px 0;border-bottom:1px solid #21262d;">{d.get('sector_count',0)}板块</td>
<td style="padding:6px 0;border-bottom:1px solid #21262d;">均分{d.get('avg_score',0)}</td>
<td style="padding:6px 0;border-bottom:1px solid #21262d;">TOP: {top.get('name','—')} {top.get('score','')}</td>
</tr>"""

    common_rows = ""
    for c in (comparison.get("common_sectors") or [])[:8]:
        common_rows += f"""<tr>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{c['name']}</td>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{c['appearances']}天</td>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{c['avg_score']}分</td>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{c.get('trend','—')}</td>
</tr>"""

    unique_rows = ""
    for day, items in (comparison.get("unique_per_day") or {}).items():
        for it in items[:3]:
            unique_rows += f"""<tr>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{day}</td>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{it['name']}</td>
<td style="padding:4px 0;border-bottom:1px solid #21262d;">{it['score']}分</td>
</tr>"""

    highlights_html = "<br>".join(f"✅ {h}" for h in summary.get("highlights", [])[:5])
    risks_html = "<br>".join(f"⚠️ {r}" for r in summary.get("risk_notes", [])[:3])
    gen_by = summary.get("generated_by", "rule_engine")
    gen_label = "🧠 AI生成" if gen_by == "llm" else "📋 规则引擎"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,system-ui,sans-serif;padding:24px 28px;width:720px}}
h1{{font-size:22px;background:linear-gradient(135deg,#58a6ff,#3fb950);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}}
.sub{{font-size:12px;color:#8b949e;margin-bottom:18px}}
.section{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 18px;margin-bottom:14px}}
.section h2{{font-size:15px;color:#58a6ff;margin-bottom:10px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#8b949e;font-size:11px;padding:4px 0;border-bottom:1px solid #30363d}}
.footer{{text-align:center;font-size:10px;color:#484f58;margin-top:16px;border-top:1px solid #21262d;padding-top:12px}}
.highlight{{color:#3fb950;font-size:12px;line-height:1.8}}
.risk{{color:#f85149;font-size:12px;line-height:1.8}}
.summary-text{{font-size:13px;line-height:1.7;color:#c9d1d9}}
</style></head><body>
<h1>🌍 热点驱动 · 多日对比分析</h1>
<div class="sub">{len(days)}天数据 · 交叉分析 · {gen_label}</div>

<div class="section">
<h2>📊 日概览</h2>
<table>
<tr><th>日期</th><th>新闻</th><th>板块</th><th>均分</th><th>TOP板块</th></tr>
{days_rows}
</table>
</div>

<div class="section">
<h2>🌐 共性板块</h2>
<table>
<tr><th>板块</th><th>出现</th><th>均分</th><th>趋势</th></tr>
{common_rows or '<tr><td colspan="4" style="color:#8b949e;padding:8px 0;">无共性板块</td></tr>'}
</table>
</div>

<div class="section">
<h2>⚡ 特异板块</h2>
<table>
<tr><th>日期</th><th>板块</th><th>评分</th></tr>
{unique_rows or '<tr><td colspan="3" style="color:#8b949e;padding:8px 0;">无特异板块</td></tr>'}
</table>
</div>

<div class="section">
<h2>📝 总结</h2>
<div class="summary-text">{summary.get('text','暂无总结')}</div>
</div>

{('<div class="section"><div class="highlight">'+highlights_html+'</div></div>') if highlights_html else ''}
{('<div class="section"><div class="risk">'+risks_html+'</div></div>') if risks_html else ''}

<div class="footer">AI-Agent 智能分析系统 · 自动生成</div>
</body></html>"""


@app.get("/api/kline-health")
def api_kline_health(sample: int = Query(100, description="采样数量"), pool: str = Query("", description="板块筛选，如 sh,sz,bj")):
    """K线数据健康扫描：检测无日线数据的股票并自动分类故障原因。"""
    from tools.kline_health_monitor import scan_kline_health
    try:
        if sample < 10 or sample > 5600:
            return JSONResponse({"error": "sample 需在 10-5600 之间"}, status_code=400)
        p = None
        if pool:
            prefixes = [x.strip().lower() for x in pool.split(",")]
            from data.fetcher import get_fetcher
            f = get_fetcher()
            spot = f.get_market_spot()
            code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
            codes = spot[code_col].astype(str).str.strip().str.zfill(6).tolist()
            prefix_map = {"sh": "6", "sz": ("0", "3"), "bj": ("4", "8", "9")}
            allowed = set()
            for pfx in prefixes:
                for k, v in prefix_map.items():
                    if pfx == k:
                        allowed.update(v if isinstance(v, tuple) else (v,))
            if allowed:
                p = [c for c in codes if c[0] in allowed]
        report = scan_kline_health(pool=p, sample_size=sample)
        return JSONResponse(report)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _compute_thermometer(fetcher, symbol: str, fund_flow: dict, index_pct: float = None) -> dict:
    """计算情绪温度计（六维加权）。"""
    try:
        from agents.sentiment_thermometer import compute_sentiment_thermometer
        return compute_sentiment_thermometer(fetcher, symbol, fund_flow=fund_flow, index_pct=index_pct)
    except Exception as e:
        return {"score": 50, "label": "正常", "emoji": "🌡️", "error": str(e),
                "dimensions": {}, "summary": f"温度计计算异常: {e}"}


@app.get("/api/realtime")
def api_realtime(symbol: str = Query(..., description="股票代码")):
    """实时分析：走势图MA破位 + 主力筹码 + 缩量状态。

    返回结构：
    {
      "symbol", "name", "price", "change_pct",
      "ma_signals": {
        "price_vs_vwap": {"below": bool, "gap_pct": float},
        "vs_ma5": {"below": bool, "gap_pct": float, "ma": float},
        "vs_ma10": ...,
        "vs_ma20": ...
      },
      "fund_flow": {"main_net_yi": float, "inflow_ratio": float, "direction": "流入/流出},
      "volume_state": {"status": "极度缩量/温和缩量/正常/放量", "vol_ratio": float, "detail": str},
      "kline_mini": { "dates": [...], "candles": [...], "ma5": [...], "ma10": [...], "ma20": [...] },
    }
    """
    from data.fetcher import get_fetcher
    import datetime as _dt
    try:
        f = get_fetcher()
        code = str(symbol).strip().zfill(6)
        name = f.get_name(code) or code

        # ── 1. 实时快照 ──
        spot = None
        price = None
        change_pct = None
        turnover = None
        vol_ratio = None
        try:
            spot = f.get_market_spot()
            if spot is not None and not spot.empty:
                code_col = next((c for c in spot.columns if c in ("代码","symbol","code")), None)
                price_col = next((c for c in spot.columns if "最新价" in c or "现价" in c), None)
                pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
                turn_col = next((c for c in spot.columns if "换手" in c), None)
                vr_col = next((c for c in spot.columns if "量比" in c), None)
                if code_col:
                    row = spot[spot[code_col].astype(str).str.strip().str.zfill(6) == code]
                    if not row.empty:
                        r = row.iloc[0]
                        price = float(r.get(price_col, 0)) if price_col else None
                        change_pct = float(r.get(pct_col, 0)) if pct_col else None
                        turnover = float(r.get(turn_col, 0)) if turn_col else None
                        vol_ratio = float(r.get(vr_col, 0)) if vr_col else None
        except Exception:
            pass

        # ── 2. K线（均线 + 缩量判断 + 迷你图表数据）──
        df = f.get_kline(code, days=120)
        ma_signals = {"price_vs_vwap": {"below": False, "gap_pct": 0, "note": ""}}
        kline_mini = {"dates": [], "candles": [], "ma5": [], "ma10": [], "ma20": []}
        volume_state = {"status": "未知", "vol_ratio_val": None, "detail": ""}

        if df is not None and not df.empty and len(df) >= 20:
            import pandas as pd
            import numpy as np
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
            df["open"] = pd.to_numeric(df["open"], errors="coerce")
            df["high"] = pd.to_numeric(df["high"], errors="coerce")
            df["low"] = pd.to_numeric(df["low"], errors="coerce")

            cls = df["close"].dropna()
            vols = df["volume"].dropna()
            opens = df["open"].dropna()
            highs = df["high"].dropna()
            lows = df["low"].dropna()

            # 均线
            ma5 = float(cls.tail(5).mean()) if len(cls) >= 5 else None
            ma10 = float(cls.tail(10).mean()) if len(cls) >= 10 else None
            ma20 = float(cls.tail(20).mean()) if len(cls) >= 20 else None

            if price and ma5 and ma10 and ma20:
                for ma_key, ma_val, label in [
                    ("vs_ma5", ma5, "5日"), ("vs_ma10", ma10, "10日"), ("vs_ma20", ma20, "20日")]:
                    gap = round((price - ma_val) / ma_val * 100, 2)
                    ma_signals[ma_key] = {
                        "below": gap < 0,
                        "gap_pct": gap,
                        "ma": round(ma_val, 2),
                        "label": f"{label}均线{ma_val:.2f} ({'跌破' if gap < 0 else '站上'} {abs(gap):.2f}%)",
                    }

                # VWAP 近似：用 (当日开盘+收盘)/2 作为"分时均线"代理
                latest_open = float(opens.iloc[-1]) if len(opens) > 0 else price
                vwap_approx = (latest_open + price) / 2
                vwap_gap = round((price - vwap_approx) / vwap_approx * 100, 2)
                ma_signals["price_vs_vwap"] = {
                    "below": vwap_gap < 0,
                    "gap_pct": vwap_gap,
                    "vwap_approx": round(vwap_approx, 2),
                    "note": f"分时均线≈{vwap_approx:.2f} ({'已跌破' if vwap_gap < 0 else '站上'} {abs(vwap_gap):.2f}%)（无分钟K线，用 (开盘+现价)/2 估算）",
                }

            # 缩量判断：近5日 vs 近20日均量
            if len(vols) >= 20:
                vol5_avg = float(vols.tail(5).mean())
                vol20_avg = float(vols.tail(20).mean())
                vr_val = round(vol5_avg / vol20_avg, 2) if vol20_avg > 0 else 1.0
                volume_state["vol_ratio_val"] = vr_val
                if vr_val < 0.5:
                    volume_state["status"] = "极度缩量"
                    volume_state["detail"] = f"近5日均量仅为近20日的{vr_val*100:.0f}%（<50%），交投极度清淡"
                elif vr_val < 0.7:
                    volume_state["status"] = "温和缩量"
                    volume_state["detail"] = f"近5日均量是近20日的{vr_val*100:.0f}%（<70%），量能明显萎缩"
                elif vr_val < 0.9:
                    volume_state["status"] = "轻微缩量"
                    volume_state["detail"] = f"近5日均量{vr_val*100:.0f}%，略低于20日均量"
                elif vr_val < 1.5:
                    volume_state["status"] = "正常"
                    volume_state["detail"] = f"近5日均量{vr_val*100:.0f}%，量能平稳"
                else:
                    volume_state["status"] = "放量"
                    volume_state["detail"] = f"近5日均量是近20日的{vr_val*100:.0f}%（>150%），明显放量"

            # 迷你K线数据（最近60根）
            recent = df.tail(60)
            dates = recent["date"].tolist()
            kline_mini = {
                "dates": [str(d) for d in dates],
                "candles": [
                    [float(recent.iloc[i]["open"]), float(recent.iloc[i]["close"]),
                     float(recent.iloc[i]["low"]), float(recent.iloc[i]["high"])]
                    for i in range(len(recent))
                ],
                "ma5": [round(float(cls.iloc[max(0,i-4):i+1].mean()), 2) for i in range(len(cls.tail(60)))],
                "ma10": [round(float(cls.iloc[max(0,i-9):i+1].mean()), 2) for i in range(len(cls.tail(60)))],
                "ma20": [round(float(cls.iloc[max(0,i-19):i+1].mean()), 2) for i in range(len(cls.tail(60)))],
            }

        # ── 3. 主力资金流 ──
        # raw_ff 保留原始格式（中文键），传给温度计；fund_flow 为处理后的前端展示格式
        raw_ff = {}  # 原始数据，用于温度计
        fund_flow = {"main_net_yi": 0, "inflow_ratio": 0, "direction": "—",
                     "detail": "资金数据暂缺（非交易时段或接口不可用）",
                     "big_net_yi": 0, "super_net_yi": 0}
        fund_available = False
        try:
            ff = f.get_fund_flow(code)
            if ff:
                raw_ff = ff
                net = float(ff.get("主力净流入-净额", 0) or 0)
                net_yi = round(net / 1e8, 2)
                big_yi = round(float(ff.get("大单净流入-净额", 0) or 0) / 1e8, 2)
                super_yi = round(float(ff.get("超大单净流入-净额", 0) or 0) / 1e8, 2)

                fund_available = True
                if net_yi > 0:
                    direction = "净流入"
                elif net_yi < 0:
                    direction = "净流出"
                else:
                    direction = "平盘"

                ratio = ff.get("主力净流入-净占比", 0) or 0
                fund_flow = {
                    "main_net_yi": net_yi,
                    "big_net_yi": big_yi,
                    "super_net_yi": super_yi,
                    "inflow_ratio": round(float(ratio), 2) if ratio else 0,
                    "direction": direction,
                    "detail": f"主力{direction} {abs(net_yi):.2f}亿（超大单{abs(super_yi):.2f}亿 + 大单{abs(big_yi):.2f}亿）",
                }
        except Exception as e:
            fund_flow["detail"] = f"资金数据获取异常: {e}"

        # 温度计：优先传原始格式 raw_ff（中文键），数据缺失时传空让温度计自取兜底
        thermo_fund_flow = raw_ff if raw_ff else None

        result = {
            "symbol": code,
            "name": name,
            "price": round(price, 2) if price else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "turnover": round(turnover, 2) if turnover else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "ma_signals": ma_signals,
            "fund_flow": fund_flow,
            "volume_state": volume_state,
            "kline_mini": kline_mini,
            "sentiment_thermometer": _compute_thermometer(f, code, thermo_fund_flow, change_pct),
            "as_of": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/analyze")
def api_analyze(symbol: str = Query(..., description="股票代码")):
    from agents.orchestrator import AgentOrchestrator
    try:
        result = AgentOrchestrator().analyze(symbol)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/analyze/report")
def api_analyze_report(symbol: str = Query(..., description="股票代码")):
    """生成个股深度分析（UZI 风格）并自动归档，返回结构化结果 + HTML + 记录。"""
    from report.analysis_report import generate_and_store
    try:
        out = generate_and_store(symbol)
        res = out["result"]
        res["report_html"] = out["html"]
        res["report_record"] = out["record"]
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/analyze/report")
def api_analyze_report_post(payload: dict = Body(...)):
    """生成个股深度分析报告（POST 版）。"""
    from report.analysis_report import generate_and_store
    try:
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            return JSONResponse({"error": "缺少股票代码"}, status_code=400)
        out = generate_and_store(symbol)
        res = out["result"]
        res["report_html"] = out["html"]
        res["report_record"] = out["record"]
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/analyze/history")
def api_analyze_history():
    """历史深度分析报告列表。"""
    from report.analysis_report import list_reports
    try:
        return {"items": list_reports()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/analyze/get")
def api_analyze_get(id: str = Query(...)):
    from report.analysis_report import get_report
    try:
        rec = get_report(id)
        return rec if rec else {"empty": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/analyze/item")
def api_analyze_delete(id: str = Query(...)):
    from report.analysis_report import delete_report
    try:
        return {"deleted": delete_report(id)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/analyze/export")
def api_analyze_export(id: str = Query(...)):
    """导出某条深度分析为独立 HTML 文件（下载）。"""
    from report.analysis_report import get_report
    try:
        rec = get_report(id)
        if not rec:
            return JSONResponse({"error": "报告不存在"}, status_code=404)
        fname = f"analysis-{rec.get('symbol','report')}-{rec.get('created_at','')[:10]}.html"
        return Response(
            content=rec.get("html") or "",
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/analyze/export-png")
def api_analyze_export_png(id: str = Query(...)):
    """导出某条深度分析为 PNG 图片（下载）。"""
    from report.analysis_report import get_report
    from review.html_report import _html_to_image
    import tempfile
    try:
        rec = get_report(id)
        if not rec:
            return JSONResponse({"error": "报告不存在"}, status_code=404)
        html = rec.get("html") or ""
        if not html:
            return JSONResponse({"error": "报告无 HTML 内容"}, status_code=400)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(html)
            tmp_html = f.name
        png_path = _html_to_image(tmp_html, os.path.dirname(tmp_html))
        os.unlink(tmp_html)
        if not png_path or not os.path.exists(png_path):
            return JSONResponse({"error": "截图生成失败，请确认已安装 playwright"}, status_code=500)
        fname = f"analysis-{rec.get('symbol','report')}-{rec.get('created_at','')[:10]}.png"
        with open(png_path, "rb") as f:
            img_data = f.read()
        os.unlink(png_path)
        return Response(
            content=img_data,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/backtest")
def api_backtest(
    symbol: str = Query(...),
    strategy: str = Query("ma_cross"),
    days: int = Query(250),
    all: bool = Query(False),
):
    from backtest.engine import run_backtest
    from data.fetcher import get_fetcher
    from strategies.library import STRATEGY_REGISTRY

    df = get_fetcher().get_kline(symbol, days=days)
    if df.empty:
        return JSONResponse({"error": "无法获取行情数据"}, status_code=400)

    names = list(STRATEGY_REGISTRY) if all else [strategy]
    results = []
    for name in names:
        try:
            results.append(run_backtest(df, name).to_dict())
        except Exception as e:
            results.append({"strategy": name, "error": str(e)})
    return {"symbol": symbol, "results": results}


@app.get("/api/select")
def api_select(top: int = Query(10)):
    from report.selector import select_stocks
    try:
        return {"picks": select_stocks(top_n=top)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/kline")
def api_kline(symbol: str = Query(...), days: int = Query(120)):
    """返回 K线 + 均线 + MACD + 成交量，供 ECharts 绘图。"""
    from core.indicators import add_all_indicators
    from data.fetcher import get_fetcher

    f = get_fetcher()
    df = f.get_kline(symbol, days=days)
    if df.empty:
        return JSONResponse({"error": "无法获取行情数据"}, status_code=400)
    d = add_all_indicators(df).fillna(0)

    def col(name):
        return [round(float(x), 3) for x in d[name].tolist()]

    return {
        "symbol": symbol,
        "name": f.get_name(symbol),
        "dates": d["date"].tolist(),
        # ECharts 蜡烛图顺序：[open, close, low, high]
        "candles": [[round(float(o), 2), round(float(c), 2), round(float(l), 2), round(float(h), 2)]
                    for o, c, l, h in zip(d["open"], d["close"], d["low"], d["high"])],
        "volume": col("volume"),
        "ma5": col("ma5"), "ma10": col("ma10"), "ma20": col("ma20"), "ma60": col("ma60"),
        "dif": col("dif"), "dea": col("dea"), "macd": col("macd"),
    }


@app.get("/api/review")
def api_review(holdings: str = Query(default="", description="可选持仓股，逗号分隔 如 600519,000858")):
    """生成今日复盘并自动落库（返回 HTML + 落库记录信息）。"""
    from review.html_report import generate_and_store
    try:
        hlist = [h.strip().zfill(6) for h in holdings.split(",") if h.strip()] if holdings.strip() else []
        res = generate_and_store(store=True, holdings=hlist)
        return {"html": res["html"], "date": res.get("date"),
                "record": res.get("record")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/review/history")
def api_review_history():
    """历史复盘列表（摘要 + 关键指标）。"""
    from review.store import list_reviews
    try:
        return {"items": list_reviews()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/review/get")
def api_review_get(id: str = Query(..., description="复盘记录 id")):
    """按 id 查看复盘详情（含完整 HTML）。"""
    from review.store import get_review
    try:
        rec = get_review(id)
        if not rec:
            return {"empty": True}
        return rec
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/review/item")
def api_review_delete(id: str = Query(...)):
    from review.store import delete_review
    try:
        return {"deleted": delete_review(id)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/review/export")
def api_review_export(id: str = Query(..., description="复盘记录 id")):
    """将某条复盘导出为独立可读的 HTML 文件（浏览器直接下载）。"""
    from review.store import get_review
    try:
        rec = get_review(id)
        if not rec:
            return JSONResponse({"error": "复盘记录不存在"}, status_code=404)
        html = rec.get("html") or ""
        fname = f"review-{rec.get('date','report')}.html"
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/review/export-png")
def api_review_export_png(id: str = Query(..., description="复盘记录 id")):
    """将某条复盘渲染为 PNG 图片下载。"""
    from review.store import get_review
    from review.html_report import _html_to_image
    import tempfile
    try:
        rec = get_review(id)
        if not rec:
            return JSONResponse({"error": "复盘记录不存在"}, status_code=404)
        html = rec.get("html") or ""
        if not html:
            return JSONResponse({"error": "复盘记录无 HTML 内容"}, status_code=400)
        # 先写临时 HTML，再截图
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
            f.write(html)
            tmp_html = f.name
        png_path = _html_to_image(tmp_html, os.path.dirname(tmp_html))
        os.unlink(tmp_html)
        if not png_path or not os.path.exists(png_path):
            return JSONResponse({"error": "截图生成失败，请确认已安装 playwright"}, status_code=500)
        fname = f"review-{rec.get('date','report')}.png"
        with open(png_path, "rb") as f:
            img_data = f.read()
        os.unlink(png_path)
        return Response(
            content=img_data,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/review/compare")
def api_review_compare(dates: str = Query(..., description="逗号分隔的日期 YYYY-MM-DD")):
    """多日复盘多维度对比分析。

    约束：时间范围最长近一周（最多 7 天跨度），最少须选 2 个相邻工作日。
    返回各日的结构化指标对齐序列 + 维度变化摘要，供前端画对比表/趋势图。
    """
    from review.store import reviews_by_dates
    try:
        sel = [d.strip() for d in (dates or "").split(",") if d.strip()]
        sel = sorted(set(sel))
        if len(sel) < 2:
            return JSONResponse({"error": "至少选择 2 个相邻工作日进行对比"}, status_code=400)
        # 最长近一周：首尾日期跨度不超过 7 个自然日
        d0 = dt.datetime.strptime(sel[0], "%Y-%m-%d").date()
        d1 = dt.datetime.strptime(sel[-1], "%Y-%m-%d").date()
        if (d1 - d0).days > 7:
            return JSONResponse({"error": "时间范围最长为最近一周（不超过 7 天跨度）"}, status_code=400)

        recs = reviews_by_dates(sel)
        found = {r["date"] for r in recs}
        missing = [d for d in sel if d not in found]
        if len(recs) < 2:
            return JSONResponse(
                {"error": f"所选日期中可用复盘不足 2 天（缺少：{', '.join(missing) or '—'}）。"
                          "请先在这些交易日生成并保存复盘。"},
                status_code=400)

        # 对齐序列
        series = {"dates": [], "sh_index": [], "sh_pct": [],
                  "limit_up": [], "limit_down": [], "avg_pct": [],
                  "up": [], "down": [], "top_sector": []}
        rows = []
        for r in recs:
            m = r.get("metrics") or {}
            b = m.get("breadth") or {}
            idx = m.get("indices") or []
            sh = next((i for i in idx if i.get("name") == "上证指数"), {})
            top = (m.get("top_sectors") or [{}])[0]
            series["dates"].append(r["date"])
            series["sh_index"].append(sh.get("price"))
            series["sh_pct"].append(sh.get("pct"))
            series["limit_up"].append(b.get("limit_up"))
            series["limit_down"].append(b.get("limit_down"))
            series["avg_pct"].append(b.get("avg_pct"))
            series["up"].append(b.get("up"))
            series["down"].append(b.get("down"))
            series["top_sector"].append(top.get("name"))
            rows.append({
                "date": r["date"],
                "sh_index": sh.get("price"), "sh_pct": sh.get("pct"),
                "limit_up": b.get("limit_up"), "limit_down": b.get("limit_down"),
                "avg_pct": b.get("avg_pct"), "up": b.get("up"), "down": b.get("down"),
                "top_sector": top.get("name"), "top_sector_pct": top.get("pct"),
            })

        # 维度变化摘要（首日 -> 末日）
        def _delta(key):
            a, z = series[key][0], series[key][-1]
            if isinstance(a, (int, float)) and isinstance(z, (int, float)):
                return round(z - a, 2)
            return None

        summary = {
            "limit_up_delta": _delta("limit_up"),
            "limit_down_delta": _delta("limit_down"),
            "sh_pct_first": series["sh_pct"][0],
            "sh_pct_last": series["sh_pct"][-1],
            "days": len(recs),
            "missing": missing,
        }
        return {"dates": sel, "rows": rows, "series": series, "summary": summary}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _recommend_regen_status() -> dict:
    """每日荐股重新生成开关：18:30 后（以配置为准）才允许，用完整收盘/龙虎榜数据。"""
    from core.config import settings

    hm = settings.recommend_push_time or "18:30"
    try:
        hour, minute = [int(x) for x in hm.split(":", 1)]
    except Exception:
        hour, minute = 18, 30
        hm = "18:30"

    now = dt.datetime.now()
    unlock = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    can = now >= unlock
    next_unlock = unlock if not can else unlock + dt.timedelta(days=1)
    reason = (
        "每日荐股使用今日收盘后的完整行情、资金流与龙虎榜数据；"
        f"龙虎榜通常 18:00 后逐步完整，因此 {hm} 后才开放重新生成。"
    )
    return {
        "can_regenerate": can,
        "unlock_time": unlock.strftime("%Y-%m-%d %H:%M"),
        "next_unlock_time": next_unlock.strftime("%Y-%m-%d %H:%M"),
        "configured_time": hm,
        "reason": reason,
    }


@app.get("/api/recommend/status")
def api_recommend_status():
    """返回荐股重新生成按钮的可用状态。"""
    return _recommend_regen_status()


@app.get("/api/recommend")
def api_recommend(count: int = Query(5), date: str = Query(""),
                  require_fib_50: bool = Query(False), force: bool = Query(False)):
    """生成（或返回最新）每日荐股 + 反思看板。

    require_fib_50=true 时，仅保留回踩斐波那契黄金50%分割位的标的。
    """
    status = _recommend_regen_status()
    if not status["can_regenerate"]:
        return JSONResponse({"error": status["reason"], **status}, status_code=403)

    from recommend.engine import RecommendEngine
    from recommend.database import RecommendDB
    try:
        # 当天已经生成过时默认直接返回缓存，避免重复触发全市场重算导致长时间 Loading。
        # 如确实需要强制重算，可请求 /api/recommend?...&force=1。
        if not force and not date:
            db = RecommendDB()
            latest = db.latest_recommendation_date()
            if latest == dt.date.today().strftime("%Y-%m-%d") and db.get_recommendations(latest):
                res = _recommend_view(db, latest)
                res["cached"] = True
                return JSONResponse(res)

        eng = RecommendEngine()
        extra = {"require_fib_50": True} if require_fib_50 else None
        res = eng.run(as_of=date or None, count=count, extra_filters=extra)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/recommend/llm-pipeline")
def api_recommend_llm_pipeline(count: int = Query(5)):
    """LLM 两阶段智能选股：Stage1 识别主线 → Stage2 偏离度校验过滤。

    与普通 /api/recommend 的区别：
    - Stage1：LLM 分析全市场数据，提取主线板块作为选股硬约束
    - Stage2：LLM 对规则筛出的候选股逐只校验是否偏离主线，过滤离题标的
    - 两阶段均失败时自动降级到纯规则模式
    """
    status = _recommend_regen_status()
    if not status["can_regenerate"]:
        return JSONResponse({"error": status["reason"], **status}, status_code=403)

    from recommend.engine import RecommendEngine, RECOMMEND_POOL
    from recommend.llm_pipeline import run_two_stage_pipeline, _finish_pipeline_status
    try:
        eng = RecommendEngine()
        base_date = dt.date.today().strftime("%Y-%m-%d")
        result = run_two_stage_pipeline(
            fetcher=eng.fetcher,
            engine=eng,
            base_date=base_date,
            pool=list(RECOMMEND_POOL),
            mode="daily",
        )
        return JSONResponse(result)
    except Exception as e:
        _finish_pipeline_status()
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/recommend/llm-status")
def api_recommend_llm_status():
    """LLM Pipeline 实时进度（前端每2秒轮询一次）。"""
    from recommend.llm_pipeline import get_pipeline_status
    return JSONResponse(get_pipeline_status())


def _recommend_view(db, base_date: str, mode: str = "daily") -> dict:
    """读取某一批次的荐股视图（picks + 结果 + 胜率 + 反思），latest/by-date 共用。"""
    from core.config import settings
    from data.fetcher import get_fetcher
    from recommend.engine import resolve_name
    from recommend.sentiment import compute_market_sentiment

    picks = db.get_recommendations(base_date, mode=mode)
    results = {r["symbol"]: r for r in db.get_results(base_date, mode=mode)}
    f = get_fetcher()
    name_fixed = False
    for p in picks:
        if not p.get("name") or p["name"] == p["symbol"]:
            nm = resolve_name(f, p["symbol"])
            if nm and nm != p["symbol"]:
                p["name"] = nm
                name_fixed = True
        r = results.get(p["symbol"])
        if r:
            p["next_pct"] = r["next_pct"]
            p["is_win"] = r["is_win"]
            p["eval_date"] = r.get("eval_date")
            p["eval_price"] = r.get("eval_price")
    if name_fixed:
        try:
            db.save_recommendations(base_date, picks, mode=mode)
        except Exception:
            pass

    # 从 picks 的因子快照还原该批次的全局市场情绪（情绪分对全批次一致）+ 主线板块
    cached_score = None
    cached_temp = None
    cached_weight = None
    sectors_seen: dict = {}
    for p in picks:
        fac = p.get("factors") or {}
        sent = ((fac.get("fundamentals") or {}).get("sentiment")) or {}
        if cached_score is None and sent.get("market_score") is not None:
            cached_score = sent.get("market_score")
            cached_temp = sent.get("temperature")
            cached_weight = sent.get("weight")
        # 还原 pick 的所属主线板块（供卡片标签）
        hs = fac.get("hot_sector")
        if hs:
            p["hot_sector"] = {"sector": hs, "sector_rank": fac.get("hot_sector_rank"),
                               "week_pct": fac.get("hot_sector_week_pct")}
            if hs not in sectors_seen:
                sectors_seen[hs] = {"name": hs, "week_pct": fac.get("hot_sector_week_pct"),
                                    "rank": fac.get("hot_sector_rank")}

    # 实时重算完整情绪数据（含分项明细 details + 带涨跌幅的主线板块），
    # 解决历史快照缺少 details / leading_sectors 涨跌幅导致前端“数据加载中…”与横杠占位。
    sentiment = None
    try:
        sentiment = compute_market_sentiment(f, base_date=base_date)
    except Exception:
        sentiment = None

    if sentiment is not None:
        # 历史批次优先保留当时的情绪分/温度，保证与已落库结果一致
        if cached_score is not None:
            sentiment["score"] = cached_score
            if cached_temp:
                sentiment["temperature"] = cached_temp
            if cached_weight:
                sentiment["weight"] = cached_weight
            sentiment["from_cache"] = True
        # 重算未能拿到主线板块涨跌幅时，用快照里的板块兜底
        if not sentiment.get("leading_sectors") and sectors_seen:
            sentiment["leading_sectors"] = sorted(
                sectors_seen.values(), key=lambda x: (x.get("rank") or 999))
    elif cached_score is not None:
        # 实时计算彻底失败时退回快照（仅有分数+板块名）
        sentiment = {
            "score": cached_score,
            "temperature": cached_temp,
            "weight": cached_weight,
            "from_cache": True,
            "leading_sectors": sorted(
                sectors_seen.values(), key=lambda x: (x.get("rank") or 999)),
        }

    return {
        "base_date": base_date,
        "picks": picks,
        "data_source": "历史/缓存结果",
        "data_source_tip": "当前为已落库荐股记录；原始生成时的数据源未单独存储。新生成时优先级：东方财富直连 → Tushare → AkShare封装东财 → 新浪兜底。",
        "sentiment": sentiment,
        "prev_winrate": db.latest_winrate(before=base_date),
        "winrate": db.get_winrate(base_date),
        "reflection": db.get_reflection(base_date),
        "threshold": settings.winrate_threshold,
    }


@app.get("/api/recommend/latest")
def api_recommend_latest():
    """只读取最新一期已落库的荐股结果（不重新计算，秒开）。"""
    from recommend.database import RecommendDB
    try:
        db = RecommendDB()
        base_date = db.latest_recommendation_date()
        if not base_date:
            return {"empty": True}
        return _recommend_view(db, base_date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/recommend/dates")
def api_recommend_dates():
    """所有历史荐股日期 + 各自胜率（供历史下拉选择）。"""
    from recommend.database import RecommendDB
    try:
        db = RecommendDB()
        dates = db.all_recommendation_dates()
        wmap = {w["base_date"]: w for w in db.winrate_history(limit=1000)}
        out = []
        for d in dates:
            w = wmap.get(d)
            out.append({
                "base_date": d,
                "win_rate": (w or {}).get("win_rate"),
                "avg_return": (w or {}).get("avg_return"),
                "settled": w is not None,
            })
        return {"dates": out}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/recommend/by-date")
def api_recommend_by_date(date: str = Query(..., description="荐股批次日期 YYYY-MM-DD")):
    """按日期查看历史荐股（只读，不重新计算）。"""
    from recommend.database import RecommendDB
    try:
        db = RecommendDB()
        if not db.get_recommendations(date):
            return {"empty": True, "base_date": date}
        return _recommend_view(db, date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/recommend/history")
def api_recommend_history(limit: int = Query(30)):
    """胜率历史 + 近期反思记录。"""
    from recommend.database import RecommendDB
    try:
        db = RecommendDB()
        return {
            "winrate_history": db.winrate_history(limit),
            "reflections": db.recent_reflections(10),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _compute_backtest(db, days: int = 60, fetcher=None) -> dict:
    """聚合历史荐股回测指标，含尾盘买入→次日卖出的交易明细。

    策略：
    - 买入价: base_date 收盘价 ≈ 14:40 尾盘价格
    - 卖出价: 次日早盘判断趋势
      · 次日开盘 > 买入价 × 1.005（高开向好）→ 持有到收盘价卖出
      · 次日开盘 < 买入价 × 0.995（低开走弱）→ 开盘即卖出（止损）
      · 其他情况 → 收盘价卖出
    """
    from data.fetcher import get_fetcher

    dates = db.all_recommendation_dates()
    if not dates:
        return {"empty": True}

    all_picks = []
    batch_stats = []
    trades: list[dict] = []           # 交易明细
    stock_map: dict[str, dict] = {}

    for d in sorted(dates)[-days:]:
        picks = db.get_recommendations(d)
        if not picks:
            continue
        results = {r["symbol"]: r for r in db.get_results(d)}
        batch_pcts = []
        for p in picks:
            sym = p["symbol"]
            r = results.get(sym)
            # entry_price: recommendations表里存的入场参考价
            entry_price = float(p.get("entry_price") or 0)
            if not entry_price and r:
                entry_price = float(r.get("entry_price") or 0)
            nxt = r["next_pct"] if r else None
            is_win = r["is_win"] if r else None

            # 获取次日开盘价（从K线数据）
            next_open = None
            next_close_db = float(r.get("eval_price") or 0) if r else 0
            sell_strategy = "收盘卖出（无开盘数据）"
            try:
                if fetcher and entry_price > 0:
                    kline = fetcher.get_kline(sym, days=60)
                    if kline is not None and not kline.empty and "open" in kline.columns:
                        dates_k = kline["date"].tolist()
                        # 找到base_date的下一个交易日
                        after_idx = None
                        for i, dd in enumerate(dates_k):
                            if dd > d:
                                after_idx = i
                                break
                        if after_idx is not None:
                            next_open = float(kline.iloc[after_idx]["open"])
                            # 如果结果里已有次日收盘价，直接取；否则从K线取
                            if not next_close_db:
                                next_close_db = float(kline.iloc[after_idx]["close"])
            except Exception:
                pass

            # 尾盘策略：根据次日开盘判断卖出时机
            if next_open and entry_price > 0 and next_close_db > 0:
                open_ratio = next_open / entry_price
                if open_ratio >= 1.005:
                    sell_strategy = "高开持有→收盘卖出"
                    sell_price = next_close_db
                elif open_ratio <= 0.995:
                    sell_strategy = "低开走弱→开盘卖出"
                    sell_price = next_open
                else:
                    sell_strategy = "平开→收盘卖出"
                    sell_price = next_close_db

                trade_pct = round((sell_price / entry_price - 1) * 100, 2)
                trades.append({
                    "date": d,
                    "symbol": sym,
                    "name": p.get("name", sym),
                    "buy_price": round(entry_price, 2),
                    "next_open": round(next_open, 2),
                    "open_ratio": round((open_ratio - 1) * 100, 2),
                    "sell_strategy": sell_strategy,
                    "sell_price": round(sell_price, 2),
                    "profit_pct": trade_pct,
                    "is_win": trade_pct > 0,
                    "ai_score": p.get("ai_score"),
                    "ai_flags": p.get("ai_flags", []),
                    "ai_reason": p.get("ai_reason", ""),
                })
                batch_pcts.append(trade_pct)
            elif nxt is not None:
                # 退化为收盘→收盘
                batch_pcts.append(nxt)

            all_picks.append({"date": d, "symbol": sym, "name": p.get("name", sym),
                              "next_pct": nxt, "is_win": is_win})
            if nxt is not None:
                stock_map.setdefault(sym, {"symbol": sym, "name": p.get("name", sym),
                    "appearances": 0, "returns": [], "wins": 0})
                stock_map[sym]["appearances"] += 1
                stock_map[sym]["returns"].append(nxt)
                if is_win:
                    stock_map[sym]["wins"] += 1

        if batch_pcts:
            batch_stats.append({
                "date": d,
                "picks": len(picks),
                "wins": sum(1 for p in picks if results.get(p["symbol"], {}).get("is_win")),
                "win_rate": round(sum(1 for p in picks if results.get(p["symbol"], {}).get("is_win")) / len(picks), 4),
                "avg_return": round(sum(batch_pcts) / len(batch_pcts), 2),
            })

    if not batch_stats:
        return {"empty": True}

    cum = 0.0
    cum_series = []
    for b in batch_stats:
        cum += b["avg_return"]
        cum_series.append({"date": b["date"], "value": round(cum, 2)})

    top_stocks = sorted(stock_map.values(), key=lambda x: sum(x["returns"]) / len(x["returns"]), reverse=True)
    for s in top_stocks:
        s["avg_return"] = round(sum(s["returns"]) / len(s["returns"]), 2)
        s["win_rate"] = round(s["wins"] / s["appearances"], 4) if s["appearances"] else 0
        del s["returns"]

    # 交易明细按日期分组
    trades_by_date: dict[str, list] = {}
    for t in trades:
        trades_by_date.setdefault(t["date"], []).append(t)

    return {
        "summary": {
            "total_batches": len(batch_stats),
            "total_picks": len(all_picks),
            "total_trades": len(trades),
            "win_rate": round(sum(b["wins"] for b in batch_stats) / sum(b["picks"] for b in batch_stats), 4),
            "avg_return": round(sum(b["avg_return"] for b in batch_stats) / len(batch_stats), 2) if batch_stats else 0,
            "max_return": max(b["avg_return"] for b in batch_stats),
            "min_return": min(b["avg_return"] for b in batch_stats),
            "cum_return": round(cum, 2),
        },
        "daily": batch_stats,
        "cumulative": cum_series,
        "top_stocks": top_stocks[:8],
        "worst_stocks": list(reversed(top_stocks[-8:])),
        "trades": trades,              # 交易明细列表
        "strategy": "尾盘买入(≈收盘价) → 次日早盘判断 → 高开持有/低开止损/平开收盘",
    }


@app.post("/api/recommend/backtest")
def api_recommend_backtest(payload: dict = Body(...)):
    """每日荐股历史回测（只读已落库数据，秒出）。需回填数据请用 CLI: python main.py recommend --backfill 30"""
    days = min(int(payload.get("days", 30)), 60)
    from recommend.database import RecommendDB
    from recommend.winrate import settle_all_pending
    from data.fetcher import get_fetcher

    try:
        db = RecommendDB()
        fetcher = get_fetcher()

        # 结算已有的未结算批次
        settle_all_pending(db, fetcher)
        # 聚合（传 fetcher 以获取次日开盘价）
        result = _compute_backtest(db, days, fetcher=fetcher)
        result["mode"] = "每日荐股"
        if result.get("empty") or not result.get("daily"):
            result["message"] = "暂无足够历史数据。请先用 'python main.py recommend --backfill 30' 回填历史荐股。"
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/tail-recommend/backtest")
def api_tail_recommend_backtest(payload: dict = Body(...)):
    """尾盘荐股历史回测（只读已落库数据，秒出）。需回填: python main.py tail-recommend --backfill 30"""
    days = min(int(payload.get("days", 30)), 60)
    from recommend.database import RecommendDB
    from recommend.winrate import settle_all_pending
    from data.fetcher import get_fetcher

    try:
        db = RecommendDB()
        fetcher = get_fetcher()
        settle_all_pending(db, fetcher)
        result = _compute_backtest(db, days, fetcher=fetcher)
        result["mode"] = "尾盘荐股"
        result["note"] = "入场价=当日收盘价 · 验证=次日收盘价 · 14:30后参照入场价尾盘买入"
        if result.get("empty") or not result.get("daily"):
            result["message"] = "暂无足够历史数据。请先用 CLI 回填: python main.py recommend --backfill 30"
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------- 尾盘荐股 ----------------


def _tail_recommend_status() -> dict:
    """尾盘荐股重新生成开关：14:30 开放。"""
    from core.config import settings

    hm = settings.tail_recommend_push_time or "14:30"
    try:
        hour, minute = [int(x) for x in hm.split(":", 1)]
    except Exception:
        hour, minute = 14, 30
        hm = "14:30"

    now = dt.datetime.now()
    unlock = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    can = now >= unlock and now.weekday() < 5
    next_unlock = unlock if not can else unlock + dt.timedelta(days=1)
    reason = (
        "尾盘荐股在 14:30 盘中运行，使用当日实时数据，推荐的票可在 15:00 收盘前买入。"
        "14:30 前数据不够充分，请稍后再试。"
    )
    return {
        "can_regenerate": can,
        "unlock_time": unlock.strftime("%Y-%m-%d %H:%M"),
        "next_unlock_time": next_unlock.strftime("%Y-%m-%d %H:%M"),
        "configured_time": hm,
        "strategy": "14:30 盘中推荐，今日尾盘买入，次日早盘验证",
        "reason": reason,
    }


@app.get("/api/tail-recommend/status")
def api_tail_recommend_status():
    return _tail_recommend_status()


def _is_realtime_window() -> bool:
    """判断当前是否处于尾盘实时数据窗口（交易日 14:30-14:50）。

    窗口内：清缓存、从 A-Stock 直连拉最新实时行情（用于实际买入决策）。
    窗口外（含 15:00 收盘后）：使用缓存数据（与 14:30 决策时点一致，保证验证可复现）。
    """
    now = dt.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt.time(14, 30) <= t <= dt.time(14, 50)


@app.get("/api/tailpick-v2")
def api_tailpick_v2(count: int = Query(5), force: bool = Query(False)):
    """尾盘专属选股引擎 v2 — 午后动量+VWAP+抢筹+回落检测。"""
    from tailpick.tail_engine import TailEngine
    try:
        eng = TailEngine()
        result = eng.run(count=count, force=force)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/tailpick-ai")
def api_tailpick_ai(count: int = Query(5), force: bool = Query(False)):
    """AI协同尾盘荐股 — Layer1 LLM市场感知 + Layer2 ML拉升预测 + Layer3 LLM审查。"""
    from tailpick.ai_tail_engine import AITailEngine
    try:
        eng = AITailEngine()
        result = eng.run(count=count, force=force)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/tail-recommend")
def api_tail_recommend(count: int = Query(5), force: bool = Query(False)):
    """尾盘荐股：14:30 盘中推荐，今日尾盘买入、次日验证。

    数据策略：
    - 14:30-14:50（盘中买入窗口）：清缓存，A-Stock 直连实时行情
    - 15:00 收盘后：用 14:30 的缓存数据（与决策时点一致，保证验证可复现）
    force=True 可跳过时间门控强制重算。
    """
    status = _tail_recommend_status()
    if not status["can_regenerate"] and not force:
        return JSONResponse({"error": status["reason"], **status}, status_code=403)

    from recommend.engine import RecommendEngine
    from recommend.database import RecommendDB
    MODE = "tail"
    try:
        db = RecommendDB()
        today_str = dt.date.today().strftime("%Y-%m-%d")

        # force=False 且今日已有结果 → 直接返回 DB 缓存（秒开）
        if not force and db.latest_recommendation_date(mode=MODE) == today_str \
                and db.get_recommendations(today_str, mode=MODE):
            res = _recommend_view(db, today_str, mode=MODE)
            res["cached"] = True
            res["tailpick_mode"] = True
            return JSONResponse(res)

        # 仅在实时窗口或 force=True 时清除市场级别缓存
        in_window = _is_realtime_window()
        should_clear = in_window or force
        if should_clear:
            from data.cache import invalidate_pattern
            # 清除全市场快照 + 板块 + 指数 + K线缓存（确保拿到当日最新数据）
            cleared = 0
            for prefix in ("spot:all:", "as_spot_", "index_spot", "as_sectors:",
                           "as_lhb_map", "kline:"):
                cleared += invalidate_pattern(prefix)
            label = "实时窗口" if in_window else "强制刷新(force=True)"
            print(f"[尾盘荐股] {label} {dt.datetime.now():%H:%M}，清除 {cleared} 个缓存，A-Stock 实时拉取...")
        else:
            print(f"[尾盘荐股] 非实时窗口 {dt.datetime.now():%H:%M}，使用缓存数据（与 14:30 一致）")

        eng = RecommendEngine()
        # 尾盘模式：as_of=today，_resolve_base_date 会正确返回今天
        # （当K线无当日条形时自动回退到today_str，非退回昨天）
        os.environ["REC_SCAN_MAX"] = str(min(count * 3, 15))
        res = eng.run(count=count, mode=MODE, skip_llm_review=True, as_of=today_str)
        if res.get("error"):
            return JSONResponse(res, status_code=400)
        return JSONResponse({**res, "tailpick_mode": True, "data_realtime": in_window})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _build_fugupan_card_html(picks: list, cycle: dict, summary: dict,
                              weights: dict, as_of: str) -> str:
    """构建复盘哥选股结果导出图片HTML（含代码+名称）。"""
    phase = cycle.get("phase", "?")
    score = cycle.get("score", 0)
    mantra = summary.get("mantra", "")
    sutras = summary.get("heart_sutras", [])[:3]
    warnings = summary.get("warnings", [])

    score_color = "#f0b429" if score >= 75 else "#58a6ff" if score >= 55 else "#8b949e"
    phase_cls = {"高潮期": "#f0b429", "复苏期": "#3fb950",
                 "退潮期": "#f85149", "冰点期": "#8b949e"}.get(phase, "#8b949e")

    picks_html = ""
    for i, p in enumerate(picks, 1):
        ti = p.get("theme_info", {})
        pi = p.get("pos_info", {})
        rank_emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
        picks_html += f"""<div class="pick-row">
<span class="rank">{rank_emoji}</span>
<span class="name">{p.get('name','?')}</span>
<span class="code">{p.get('symbol','?')}</span>
<span class="score">{p.get('score',0):.0f}分</span>
<div class="dims">
<span>天时{p.get('cycle',0):.0f}</span>
<span>人和{p.get('theme',0):.0f}</span>
<span>身位{p.get('position',0):.0f}</span>
<span>形态{p.get('structure',0):.0f}</span>
</div>
<div class="info">📌 {ti.get('sector','其他')} · {ti.get('role','通用')} · 换手{pi.get('turnover','?')}% · 量比{pi.get('vol_ratio','?')}</div>
</div>"""

    warnings_html = ""
    if warnings:
        warnings_html = "<div class=\"warn\">" + "<br>".join(
            f"⚠️ {w}" for w in warnings) + "</div>"

    return f"""<!DOCTYPE html><html><head><meta charset=\"utf-8\">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0d1117;color:#c9d1d9;font-family:-apple-system,system-ui,sans-serif;padding:28px 32px;width:780px}}
.header{{text-align:center;margin-bottom:20px}}
.header h1{{font-size:24px;background:linear-gradient(135deg,#f0b429,#ff8c00);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px}}
.header .sub{{font-size:12px;color:#8b949e}}
.phase-row{{display:flex;gap:14px;justify-content:center;margin-bottom:18px;flex-wrap:wrap}}
.phase-box{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 18px;text-align:center;min-width:120px}}
.phase-box .v{{font-size:22px;font-weight:700;margin-bottom:3px}}
.phase-box .l{{font-size:11px;color:#8b949e}}
.mantra{{text-align:center;font-size:13px;color:{score_color};margin-bottom:14px;font-style:italic}}
.sutras{{text-align:center;font-size:11px;color:#8b949e;margin-bottom:18px;border-top:1px solid #21262d;border-bottom:1px solid #21262d;padding:10px 0}}
.warn{{background:rgba(248,81,73,.08);border:1px solid rgba(248,81,73,.25);border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:11px}}
.picks-title{{font-size:16px;font-weight:700;margin-bottom:10px;color:#58a6ff}}
.pick-row{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 16px;margin-bottom:10px}}
.pick-row .rank{{font-size:18px;margin-right:8px}}
.pick-row .name{{font-size:16px;font-weight:700;color:#79c0ff;margin-right:8px}}
.pick-row .code{{font-size:12px;color:#8b949e;background:#21262d;padding:2px 7px;border-radius:4px;margin-right:12px}}
.pick-row .score{{font-size:20px;font-weight:700;color:{score_color};float:right}}
.dims{{display:flex;gap:12px;margin-top:6px;font-size:11px;color:#8b949e}}
.dims span{{background:#21262d;padding:2px 8px;border-radius:4px}}
.info{{font-size:11px;color:#8b949e;margin-top:4px}}
.footer{{text-align:center;font-size:10px;color:#484f58;margin-top:20px;border-top:1px solid #21262d;padding-top:12px}}
</style></head><body>
<div class="header">
<h1>🧠 复盘哥 · 三维共振选股</h1>
<div class="sub">{as_of} · 情绪周期+龙头战法+养家心法</div>
</div>
<div class="phase-row">
<div class="phase-box"><div class="v" style="color:{phase_cls}">{phase}</div><div class="l">情绪周期</div></div>
<div class="phase-box"><div class="v" style="color:{score_color}">{score}</div><div class="l">周期评分</div></div>
<div class="phase-box"><div class="v">{len(picks)}</div><div class="l">精选标的</div></div>
</div>
<div class="mantra">「{mantra}」</div>
<div class="sutras">{' ｜ '.join(sutras or ['交易你所见，非你所想。'])}</div>
{warnings_html}
<div class="picks-title">🎯 四维评分精选（天时30%+人和25%+身位25%+形态20%）</div>
{picks_html}
<div class="footer">以上内容仅供研究交流，不构成任何投资建议 · 复盘哥选股引擎</div>
</body></html>"""


# ---------------- 复盘哥选股（三维共振）----------------
@app.get("/api/fugupan")
def api_fugupan(count: int = Query(8, description="返回标的数量")):
    """复盘哥选股 — 情绪周期+龙头战法+养家心法三维共振。

    四维评分：天时(30%) + 人和(25%) + 地利·身位(25%) + 地利·形态(20%)
    核心约束：非主流板块拒绝 / 退潮期空仓 / 封板率<70%警示
    """
    from recommend.fugupan_stock import FugupanEngine
    try:
        if count < 1 or count > 20:
            return JSONResponse({"error": "count需在1-20之间"}, status_code=400)
        eng = FugupanEngine()
        result = eng.run(count=count)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/fugupan/export-png")
def api_fugupan_export_png(payload: dict = Body(...)):
    """将复盘哥选股结果导出为PNG图片。

    Body: {"cycle": {...}, "picks": [...], "summary": {...},
           "theme_analysis": {...}, "weights": {...}, "as_of": "..."}
    """
    from review.html_report import _html_to_image
    import tempfile
    try:
        picks = payload.get("picks") or []
        cycle = payload.get("cycle") or {}
        summary = payload.get("summary") or {}
        weights = payload.get("weights") or {}
        as_of = payload.get("as_of", "")

        if not picks:
            return JSONResponse({"error": "无选股结果可导出"}, status_code=400)

        # 构建精美HTML卡片
        html = _build_fugupan_card_html(picks, cycle, summary, weights, as_of)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".html",
                                         delete=False, encoding="utf-8") as f:
            f.write(html)
            tmp_html = f.name

        png_path = _html_to_image(tmp_html, os.path.dirname(tmp_html))
        os.unlink(tmp_html)

        if not png_path or not os.path.exists(png_path):
            return JSONResponse({"error": "截图生成失败，请确认已安装playwright"}, status_code=500)

        fname = f"fugupan-{as_of[:10] if as_of else 'report'}.png"
        with open(png_path, "rb") as f:
            img_data = f.read()
        os.unlink(png_path)
        return Response(
            content=img_data,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------- 尾盘选股 ----------------
@app.get("/api/tailpick/status")
def api_tailpick_status():
    """尾盘选股时间窗口状态。"""
    from tailpick import tailpick_status
    return tailpick_status()


@app.get("/api/tailpick")
def api_tailpick(count: int = Query(5), max_pct: float = Query(6.0),
                 min_amount_yi: float = Query(1.0), min_turnover: float = Query(2.0),
                 only_mainline: bool = Query(False)):
    """执行尾盘选股：13:00-15:00 买入、次日早盘卖出的短线 TopN。

    每次调用均实时重算（市场情绪/资金流/技术形态随行情变化），不做结果级缓存。
    """
    from tailpick import TailPickEngine
    try:
        res = TailPickEngine().run(count=count, max_pct=max_pct, force=True,
                                   min_amount_yi=min_amount_yi,
                                   min_turnover=min_turnover,
                                   only_mainline=only_mainline)
        code = 400 if res.get("error") else 200
        return JSONResponse(res, status_code=code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------- 自定义条件选股 ----------------
@app.get("/api/screener/templates")
def api_screener_templates():
    """列出所有选股模板（内置默认 + 用户自定义）。"""
    from screener.templates import list_templates
    try:
        return {"templates": list_templates()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/screener/templates")
def api_screener_save_template(payload: dict = Body(...)):
    """保存（新增/同名覆盖）选股模板。body: {name, conditions}"""
    from screener.templates import save_template
    try:
        item = save_template(payload.get("name", ""), payload.get("conditions", {}))
        return {"saved": item}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/screener/templates")
def api_screener_delete_template(name: str = Query(...)):
    from screener.templates import delete_template
    try:
        return {"deleted": delete_template(name)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/screener/run")
def api_screener_run(payload: dict = Body(...)):
    """按条件选股。body: {conditions, limit?}"""
    from screener.engine import ScreenerEngine
    try:
        res = ScreenerEngine().run(payload.get("conditions", {}),
                                   limit=int(payload.get("limit", 30)))
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------------- 价值挖掘（五步智能体）----------------
@app.post("/api/value/run")
def api_value_run(payload: dict = Body(...)):
    """五步价值挖掘。body: {target(行业或标的), focus_symbol?}"""
    from value.engine import ValueEngine
    try:
        res = ValueEngine().run(payload.get("target", ""),
                                focus_symbol=payload.get("focus_symbol", ""))
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/value/history")
def api_value_history():
    """历史分析摘要列表。"""
    from value.store import list_analyses
    try:
        return {"items": list_analyses()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/value/get")
def api_value_get(id: str = Query(...)):
    """按 id 查看完整历史分析。"""
    from value.store import get_analysis
    try:
        rec = get_analysis(id)
        return rec if rec else {"empty": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/value/item")
def api_value_delete(id: str = Query(...)):
    from value.store import delete_analysis
    try:
        return {"deleted": delete_analysis(id)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
