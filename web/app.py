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
    """生成今日复盘并自动落库（返回 HTML + 落库记录信息）。"""
    from review.html_report import generate_and_store
    try:
        res = generate_and_store(store=True)
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


def _recommend_view(db, base_date: str) -> dict:
    """读取某一批次的荐股视图（picks + 结果 + 胜率 + 反思），latest/by-date 共用。"""
    from core.config import settings
    from data.fetcher import get_fetcher
    from recommend.engine import resolve_name
    from recommend.sentiment import compute_market_sentiment

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


def _compute_backtest(db, days: int = 60) -> dict:
    """聚合历史荐股回测指标。"""
    from data.fetcher import get_fetcher, normalize_symbol
    dates = db.all_recommendation_dates()
    if not dates:
        return {"empty": True}

    all_picks = []
    batch_stats = []
    stock_map: dict[str, dict] = {}  # symbol -> {name, appearances, returns, wins}

    for d in sorted(dates)[-days:]:
        picks = db.get_recommendations(d)
        if not picks:
            continue
        results = {r["symbol"]: r for r in db.get_results(d)}
        batch_pcts = []
        for p in picks:
            sym = p["symbol"]
            r = results.get(sym)
            nxt = r["next_pct"] if r else None
            is_win = r["is_win"] if r else None
            all_picks.append({"date": d, "symbol": sym, "name": p.get("name", sym),
                              "next_pct": nxt, "is_win": is_win})
            if nxt is not None:
                batch_pcts.append(nxt)
                stock_map.setdefault(sym, {"name": p.get("name", sym),
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

    # 累计收益曲线
    cum = 0.0
    cum_series = []
    for b in batch_stats:
        cum += b["avg_return"]
        cum_series.append({"date": b["date"], "value": round(cum, 2)})

    # 个股排行
    top_stocks = sorted(stock_map.values(), key=lambda x: sum(x["returns"]) / len(x["returns"]), reverse=True)
    for s in top_stocks:
        s["avg_return"] = round(sum(s["returns"]) / len(s["returns"]), 2)
        s["win_rate"] = round(s["wins"] / s["appearances"], 4) if s["appearances"] else 0
        del s["returns"]

    return {
        "summary": {
            "total_batches": len(batch_stats),
            "total_picks": len(all_picks),
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
    }


@app.post("/api/recommend/backtest")
def api_recommend_backtest(payload: dict = Body(...)):
    """触发每日荐股历史回测（回填 + 结算 + 聚合统计）。"""
    days = min(int(payload.get("days", 30)), 60)
    from recommend.engine import RecommendEngine
    from recommend.database import RecommendDB
    from recommend.winrate import settle_all_pending
    from data.fetcher import get_fetcher

    try:
        eng = RecommendEngine()
        db = RecommendDB()
        fetcher = get_fetcher()

        # 回填未生成的历史日期
        eng.backfill(n_days=days)
        # 确保全部结算
        settle_all_pending(db, fetcher)
        # 聚合
        result = _compute_backtest(db, days)
        result["mode"] = "每日荐股"
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/tail-recommend/backtest")
def api_tail_recommend_backtest(payload: dict = Body(...)):
    """触发尾盘荐股历史回测（与每日荐股共用引擎和结算逻辑）。"""
    days = min(int(payload.get("days", 30)), 60)
    from recommend.engine import RecommendEngine
    from recommend.database import RecommendDB
    from recommend.winrate import settle_all_pending
    from data.fetcher import get_fetcher

    try:
        eng = RecommendEngine()
        db = RecommendDB()
        fetcher = get_fetcher()

        eng.backfill(n_days=days)
        settle_all_pending(db, fetcher)
        result = _compute_backtest(db, days)
        result["mode"] = "尾盘荐股"
        result["note"] = "尾盘荐股与每日荐股共用引擎，入场价为当日收盘价、验证为次日收盘价。实际尾盘操作可在14:30后参照入场价买入。"
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


@app.get("/api/tail-recommend")
def api_tail_recommend(count: int = Query(5), force: bool = Query(False)):
    """尾盘荐股：14:30 盘中推荐，今日尾盘买入、次日验证。

    首次生成较慢（需结算历史+动态池+评分+富化），落库后下次秒开。
    force=True 可跳过时间门控，force 模式下不走缓存直接重算。
    """
    status = _tail_recommend_status()
    if not status["can_regenerate"] and not force:
        return JSONResponse({"error": status["reason"], **status}, status_code=403)

    from recommend.engine import RecommendEngine
    from recommend.database import RecommendDB
    try:
        db = RecommendDB()
        today_str = dt.date.today().strftime("%Y-%m-%d")

        # 今天已生成过且非强制重算 → 直接返回缓存（秒开）
        if not force and db.latest_recommendation_date() == today_str and db.get_recommendations(today_str):
            res = _recommend_view(db, today_str)
            res["cached"] = True
            res["tailpick_mode"] = True
            return JSONResponse(res)

        # 首次生成：复用每日荐股的缓存逻辑，避免重复重算
        eng = RecommendEngine()
        res = eng.run(count=count)
        if res.get("error"):
            return JSONResponse(res, status_code=400)
        return JSONResponse({**res, "tailpick_mode": True})
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
