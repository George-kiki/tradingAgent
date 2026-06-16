"""FastAPI Web 服务：提供分析、回测、选股的接口与页面。"""
from __future__ import annotations

import datetime as dt
import os

from fastapi import Body, FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
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


@app.get("/api/analyze")
def api_analyze(symbol: str = Query(..., description="股票代码")):
    from agents.orchestrator import AgentOrchestrator
    try:
        result = AgentOrchestrator().analyze(symbol)
        return JSONResponse(result)
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
def api_review():
    from review.html_report import generate_html_review
    try:
        return {"html": generate_html_review()}
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
                  require_fib_50: bool = Query(False)):
    """生成（或返回最新）每日荐股 + 反思看板。

    require_fib_50=true 时，仅保留回踩斐波那契黄金50%分割位的标的。
    """
    status = _recommend_regen_status()
    if not status["can_regenerate"]:
        return JSONResponse({"error": status["reason"], **status}, status_code=403)

    from recommend.engine import RecommendEngine
    try:
        eng = RecommendEngine()
        extra = {"require_fib_50": True} if require_fib_50 else None
        res = eng.run(as_of=date or None, count=count, extra_filters=extra)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _recommend_view(db, base_date: str) -> dict:
    """读取某一批次的荐股视图（picks + 结果 + 胜率 + 反思），latest/by-date 共用。"""
    from core.config import settings
    from data.fetcher import get_fetcher
    from recommend.engine import resolve_name

    picks = db.get_recommendations(base_date)
    results = {r["symbol"]: r for r in db.get_results(base_date)}
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
            db.save_recommendations(base_date, picks)
        except Exception:
            pass

    # 从 picks 的因子快照还原该批次的全局市场情绪（情绪分对全批次一致）+ 主线板块
    sentiment = None
    sectors_seen: dict = {}
    for p in picks:
        fac = p.get("factors") or {}
        sent = ((fac.get("fundamentals") or {}).get("sentiment")) or {}
        if sentiment is None and sent.get("market_score") is not None:
            sentiment = {
                "score": sent.get("market_score"),
                "temperature": sent.get("temperature"),
                "weight": sent.get("weight"),
                "from_cache": True,
            }
        # 还原 pick 的所属主线板块（供卡片标签）
        hs = fac.get("hot_sector")
        if hs:
            p["hot_sector"] = {"sector": hs, "sector_rank": fac.get("hot_sector_rank"),
                               "week_pct": fac.get("hot_sector_week_pct")}
            if hs not in sectors_seen:
                sectors_seen[hs] = {"name": hs, "week_pct": fac.get("hot_sector_week_pct"),
                                    "rank": fac.get("hot_sector_rank")}
    if sentiment is not None:
        lead = sorted(sectors_seen.values(), key=lambda x: (x.get("rank") or 999))
        if lead:
            sentiment["leading_sectors"] = lead

    return {
        "base_date": base_date,
        "picks": picks,
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
