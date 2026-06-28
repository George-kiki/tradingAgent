"""热点驱动编排器：本地持久化 + 7日清理 + 异步预分析。

数据流：
  定时任务(每12h)
      │
      ▼
  fetch_all_news() ──→ 写入 news_raw 表（按日期分区）
      │
      ▼
  异步触发预分析 ──→ filter + analysts ──→ 写入 news_analysis 表
      │
      ▼
  用户点击"分析" ──→ 直接读 news_analysis 最新预存结果（秒级响应）

清理策略：
  每7天执行一次，删除 >4天前的所有 raw + analysis 记录
  （如第7天清理第1-4天数据，保留第5-7天）
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import threading
from typing import Optional


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "news_driven.db")

# 保留天数（>此天数的记录将被清理）
RETENTION_DAYS = 4
# 清理周期（每N天执行一次清理）
CLEANUP_INTERVAL_DAYS = 7


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_raw (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetch_date TEXT NOT NULL,
            fetch_time TEXT NOT NULL,
            title TEXT,
            source TEXT,
            url TEXT,
            summary TEXT,
            published TEXT,
            region TEXT,
            keywords_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_time TEXT NOT NULL,
            news_count INTEGER,
            filtered_count INTEGER,
            sectors_json TEXT,
            filtered_news_json TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_raw_date ON news_raw(fetch_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_analysis_time ON news_analysis(scan_time)
    """)
    conn.commit()
    return conn


# ────────────────── 数据写入 ──────────────────

def save_raw_news(news_list: list[dict]) -> int:
    """将抓取的新闻写入 news_raw 表。"""
    conn = _get_db()
    today = _dt.date.today().isoformat()
    now = _dt.datetime.now().isoformat()
    count = 0
    for n in news_list:
        try:
            conn.execute(
                "INSERT INTO news_raw (fetch_date, fetch_time, title, source, url, summary, "
                "published, region, keywords_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (today, now, n.get("title", ""), n.get("source", ""), n.get("url", ""),
                 n.get("summary", ""), n.get("published", ""), n.get("region", ""),
                 json.dumps(n.get("keywords", []), ensure_ascii=False))
            )
            count += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return count


def save_analysis_result(result: dict) -> int:
    """将预分析结果写入 news_analysis 表。"""
    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO news_analysis (scan_time, news_count, filtered_count, "
            "sectors_json, filtered_news_json) VALUES (?,?,?,?,?)",
            (result.get("scan_time", _dt.datetime.now().isoformat()),
             result.get("news_count", 0), result.get("filtered_count", 0),
             json.dumps(result, ensure_ascii=False),
             json.dumps(result.get("filtered_news", []), ensure_ascii=False))
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


# ────────────────── 数据读取 ──────────────────

def get_latest_analysis() -> Optional[dict]:
    """获取最近一次预分析结果（用户点击"分析"时调用，秒级响应）。"""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM news_analysis ORDER BY scan_time DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return json.loads(row["sectors_json"])
    except Exception:
        return None
    finally:
        conn.close()


def get_latest_raw_news(limit: int = 50) -> list[dict]:
    """获取最近抓取的原始新闻。"""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM news_raw ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "title": r["title"],
                "source": r["source"],
                "url": r["url"],
                "summary": r["summary"],
                "published": r["published"],
                "region": r["region"],
                "keywords": json.loads(r["keywords_json"] or "[]"),
                "fetch_date": r["fetch_date"],
            })
        return results
    finally:
        conn.close()


def get_analysis_history(limit: int = 10) -> list[dict]:
    """获取预分析历史列表。"""
    conn = _get_db()
    try:
        rows = conn.execute(
            "SELECT id, scan_time, news_count, filtered_count, created_at "
            "FROM news_analysis ORDER BY scan_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def get_analysis_by_id(scan_id: int) -> Optional[dict]:
    """按ID获取某次预分析详情。"""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT * FROM news_analysis WHERE id=?", (scan_id,)
        ).fetchone()
        if not row:
            return None
        return json.loads(row["sectors_json"])
    except Exception:
        return None
    finally:
        conn.close()


# ────────────────── 数据清理 ──────────────────

def cleanup_old_data(days: int = RETENTION_DAYS) -> dict:
    """清理过期数据：删除 >days 天前的 raw + analysis 记录。

    策略：每7天执行一次，删除>4天前的数据。
    例如第7天执行时，删除第1-3天数据，保留第4-7天。
    """
    conn = _get_db()
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    result = {"cutoff_date": cutoff, "raw_deleted": 0, "analysis_deleted": 0}

    try:
        # 清理 raw 新闻
        cur = conn.execute(
            "DELETE FROM news_raw WHERE fetch_date < ?", (cutoff,)
        )
        result["raw_deleted"] = cur.rowcount

        # 清理分析结果（按 scan_time 前缀匹配日期）
        cur = conn.execute(
            "DELETE FROM news_analysis WHERE scan_time < ?", (cutoff,)
        )
        result["analysis_deleted"] = cur.rowcount

        conn.commit()
        print(f"[数据清理] 清理 {cutoff} 之前数据: raw={result['raw_deleted']}, "
              f"analysis={result['analysis_deleted']}")
    except Exception as e:
        result["error"] = str(e)
    finally:
        conn.close()

    return result


def should_run_cleanup() -> bool:
    """判断是否应该执行清理（每7天一次）。

    通过检查 news_raw 表中最早的 fetch_date 来判断。
    如果最早记录>7天前，则需要清理。
    """
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT MIN(fetch_date) as earliest FROM news_raw"
        ).fetchone()
        if not row or not row["earliest"]:
            return False
        earliest = _dt.date.fromisoformat(row["earliest"])
        age = (_dt.date.today() - earliest).days
        return age >= CLEANUP_INTERVAL_DAYS
    except Exception:
        return False
    finally:
        conn.close()


# ────────────────── 完整扫描流程 ──────────────────

def run_full_scan(llm=None, skip_fetch: bool = False) -> dict:
    """执行完整的热点驱动扫描流程（抓取→存储→预分析→存储结果）。

    Args:
        llm: LLM 客户端（可选）
        skip_fetch: True=跳过抓取，直接用本地已有新闻分析

    Returns:
        预分析结果 dict
    """
    from agents.news_fetcher import fetch_all_news
    from agents.news_driven import filter_news, run_analysts

    scan_time = _dt.datetime.now().isoformat()

    # Step 1: 抓取新闻（或读取本地）
    if skip_fetch:
        print("[热点驱动] 跳过抓取，使用本地已有新闻")
        all_news = get_latest_raw_news(limit=100)
    else:
        print("[热点驱动] 1/5 抓取全球热点新闻（多渠道）...")
        all_news = fetch_all_news()
        print(f"[热点驱动]   → 抓取 {len(all_news)} 条")

        # Step 2: 本地持久化
        if all_news:
            print("[热点驱动] 2/5 写入本地存储...")
            saved = save_raw_news(all_news)
            print(f"[热点驱动]   → 持久化 {saved} 条")

    # Step 3: 过滤
    print("[热点驱动] 3/5 过滤Agent筛选有价值资讯...")
    filtered = filter_news(all_news, llm=llm)
    print(f"[热点驱动]   → 过滤后保留 {len(filtered)} 条")

    # Step 4: 多分析师分析
    print("[热点驱动] 4/5 多分析师Agent协作分析...")
    sectors = run_analysts(filtered, llm=llm)
    print(f"[热点驱动]   → 识别 {len(sectors)} 个利好板块")

    # Step 5: 预存分析结果
    result = {
        "scan_time": scan_time,
        "news_count": len(all_news),
        "filtered_count": len(filtered),
        "sectors": sectors,
        "filtered_news": filtered[:20],
    }
    print("[热点驱动] 5/5 预存分析结果...")
    save_analysis_result(result)
    print(f"[热点驱动] ✅ 预分析完成，结果已存储")

    # 检查是否需要清理
    if should_run_cleanup():
        print("[热点驱动] 触发7日数据清理...")
        cleanup_old_data()

    return result


def run_async_preanalysis(llm=None):
    """异步触发预分析（不阻塞调用方）。

    在定时任务抓取完新闻后立即调用，将结果预存本地。
    用户点击"分析"时直接读取预存结果，无需等待。
    """
    def _bg():
        try:
            run_full_scan(llm=llm)
        except Exception as e:
            print(f"[热点驱动] 异步预分析失败: {e}")

    thread = threading.Thread(target=_bg, daemon=True)
    thread.start()
    return thread


# ────────────────── 多日对比分析 ──────────────────

def get_recent_dates(days: int = 3) -> list[dict]:
    """获取可用日期列表（按日去重，每天取最新一次扫描）。

    Args:
        days: 3=近3日, 0=全部历史（受7天清理限制）
    """
    conn = _get_db()
    try:
        if days:
            cutoff = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
            rows = conn.execute(
                "SELECT id, scan_time, news_count, filtered_count FROM news_analysis "
                "WHERE scan_time >= ? ORDER BY scan_time DESC",
                (cutoff,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, scan_time, news_count, filtered_count FROM news_analysis "
                "ORDER BY scan_time DESC LIMIT 20"
            ).fetchall()

        # 按日期去重：每天只保留最新的一次扫描
        seen_dates = set()
        result = []
        for r in rows:
            st = r["scan_time"]
            date_str = st[:10] if len(st) >= 10 else st
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)
            display_date = date_str[5:] if date_str.startswith("202") else date_str
            sector_count = _count_sectors_fast(r["id"])
            result.append({
                "id": r["id"],
                "date": display_date,
                "full_date": date_str,
                "scan_time": st,
                "news_count": r["news_count"] or 0,
                "sector_count": sector_count,
            })
        return result
    finally:
        conn.close()


def _count_sectors_fast(scan_id: int) -> int:
    """快速获取某次分析的板块数量（不解析完整JSON）。"""
    try:
        analysis = get_analysis_by_id(scan_id)
        if analysis:
            return len(analysis.get("sectors", []))
    except Exception:
        pass
    return 0


def compare_analyses(ids: list[int]) -> dict:
    """对多批预分析结果进行交叉比对（纯规则引擎，秒级）。

    Args:
        ids: 分析记录 ID 列表（2-7个）

    Returns:
        {days_analyzed, id_range, days, trends, heatmap, comparison, summary}
    """
    # 1. 读取各日数据
    analyses: list[dict] = []
    for i in ids:
        a = get_analysis_by_id(i)
        if a:
            analyses.append(a)

    if len(analyses) < 2:
        return {"error": "至少需要2天有效数据"}

    analyses.sort(key=lambda x: x.get("scan_time", ""))

    # 2. 构建日概览
    days_info = []
    day_labels = []
    for a in analyses:
        st = a.get("scan_time", "")[:16]
        sectors = a.get("sectors", [])
        scores = [s.get("bullish_score", 0) for s in sectors if s.get("bullish_score")]
        day_labels.append(st[:10] if len(st) >= 10 else st)
        days_info.append({
            "id": ids[analyses.index(a)] if analyses.index(a) < len(ids) else None,
            "scan_time": st,
            "news_count": a.get("news_count", 0),
            "filtered_count": a.get("filtered_count", 0),
            "sector_count": len(sectors),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
            "top_sectors": sorted(sectors, key=lambda x: x.get("bullish_score", 0), reverse=True)[:3],
        })

    # 3. 构建板块评分矩阵
    all_sectors_raw: dict[str, list] = {}
    for ai, a in enumerate(analyses):
        for s in a.get("sectors", []):
            name = s["sector"]
            if name not in all_sectors_raw:
                all_sectors_raw[name] = {
                    "scores": [0] * len(analyses),
                    "details": [None] * len(analyses),
                }
            all_sectors_raw[name]["scores"][ai] = s.get("bullish_score", 0)
            all_sectors_raw[name]["details"][ai] = {
                "catalyst": s.get("catalyst", ""),
                "benefit_type": s.get("benefit_type", ""),
                "stocks": s.get("stocks", [])[:3],
            }

    # 4. 分类：共性 / 特异
    common_items = []
    unique_per_day: dict[str, list] = {dl: [] for dl in day_labels}
    hot_common = []
    cooling_common = []

    for name, info in all_sectors_raw.items():
        scores = info["scores"]
        appearances = sum(1 for v in scores if v > 0)

        if appearances >= 2:
            avg = sum(s for s in scores if s > 0) / appearances
            trend = "→平稳"
            valid = [s for s in scores if s > 0]
            if len(valid) >= 2:
                if valid[-1] > valid[0] + 5:
                    trend = "↑连续升温"
                elif valid[-1] < valid[0] - 5:
                    trend = "↓持续降温"
                elif max(valid) - min(valid) > 8:
                    trend = "↗震荡走强"
            item = {
                "name": name, "appearances": appearances, "avg_score": round(avg, 1),
                "trend": trend, "scores": scores,
            }
            common_items.append(item)
            if trend in ("↑连续升温", "↗震荡走强"):
                hot_common.append(item)
            elif trend in ("↓持续降温",):
                cooling_common.append(item)
        else:
            # 特异：仅出现在1天
            for di, s in enumerate(scores):
                if s > 0:
                    unique_per_day[day_labels[di]].append({"name": name, "score": s})

    common_items.sort(key=lambda x: (-x["appearances"], -x["avg_score"]))
    for v in unique_per_day.values():
        v.sort(key=lambda x: -x["score"])

    # 5. 趋势数据（折线图用，取前8个共性板块）
    trends_sectors = []
    for item in common_items[:8]:
        trends_sectors.append({
            "name": item["name"],
            "scores": item["scores"],
            "dates": day_labels,
            "trend": item["trend"],
        })

    trends_metrics = {
        "dates": day_labels,
        "total_news": [d["news_count"] for d in days_info],
        "sector_counts": [d["sector_count"] for d in days_info],
        "avg_score": [d["avg_score"] for d in days_info],
    }

    # 6. 热力图数据
    heatmap = _build_heatmap_matrix(analyses, day_labels)

    # 7. 共性关键词
    all_kws = []
    for a in analyses:
        for n in a.get("filtered_news", [])[:10]:
            kws = n.get("keywords", [])
            if isinstance(kws, list):
                all_kws.extend(kws[:3])
            elif isinstance(kws, str):
                all_kws.extend(kws.split(",")[:3])

    from collections import Counter
    kw_counter = Counter(all_kws)
    common_kws = [k for k, c in kw_counter.most_common(8) if c >= 2]
    single_kws = [k for k, c in kw_counter.most_common(8) if c == 1][:5]

    # 8. 生成总结
    summary = _generate_comparison_summary(
        trends_sectors, common_items, unique_per_day, day_labels,
        common_kws, single_kws, days_info)

    return {
        "days_analyzed": len(analyses),
        "id_range": ids,
        "days": days_info,
        "trends": {
            "sectors": trends_sectors,
            "metrics": trends_metrics,
        },
        "heatmap": heatmap,
        "comparison": {
            "common_sectors": common_items,
            "hot_common": hot_common,
            "cooling_common": cooling_common,
            "unique_per_day": unique_per_day,
            "common_keywords": common_kws,
            "divergence_keywords": single_kws,
        },
        "summary": summary,
    }


def _build_heatmap_matrix(analyses: list, day_labels: list) -> dict:
    """构建热力图数据矩阵。"""
    all_names = set()
    for a in analyses:
        for s in a.get("sectors", []):
            all_names.add(s["sector"])

    # 按最高评分降序，取前12个
    name_max = {}
    for nm in all_names:
        mx = 0
        for a in analyses:
            for s in a.get("sectors", []):
                if s["sector"] == nm:
                    mx = max(mx, s.get("bullish_score", 0))
        name_max[nm] = mx

    sectors_sorted = sorted(all_names, key=lambda n: -name_max.get(n, 0))[:12]

    data = []
    for a in analyses:
        score_map = {s["sector"]: s.get("bullish_score", 0) for s in a.get("sectors", [])}
        data.append([score_map.get(nm, 0) for nm in sectors_sorted])

    return {
        "dates": day_labels,
        "sectors": sectors_sorted,
        "data": data,
    }


def _generate_comparison_summary(trends_sectors, common_items, unique_per_day,
                                  day_labels, common_kws, single_kws, days_info) -> dict:
    """规则引擎自动生成多日总结。"""
    n_days = len(day_labels)
    highlights = []
    risks = []

    # 热度攀升
    rising = [t for t in trends_sectors if t["trend"] in ("↑连续升温",)]
    if rising:
        highlights.append(f"{'、'.join(t['name'] for t in rising[:3])}评分逐日攀升")

    # 全勤板块
    full = [c for c in common_items if c["appearances"] >= n_days]
    if len(full) >= 3:
        highlights.append(f"{'、'.join(c['name'] for c in full[:3])}连续{n_days}日上榜")

    # 均值趋势
    first_avg = days_info[0]["avg_score"]
    last_avg = days_info[-1]["avg_score"]
    if last_avg > first_avg + 3:
        highlights.append(f"整体热度上升（均分 {first_avg}→{last_avg}）")
    elif last_avg < first_avg - 3:
        highlights.append(f"整体热度下降（均分 {first_avg}→{last_avg}）")

    # 共性关键词总结
    if common_kws:
        highlights.append(f"共性关键词：{'、'.join(common_kws[:4])}")

    # 降温板块
    cooling = [c for c in common_items if c["trend"] == "↓持续降温"]
    if cooling:
        risks.append(f"{'、'.join(c['name'] for c in cooling[:3])}热度持续降温，需关注")

    # 特异板块风险
    for day, sectors in unique_per_day.items():
        names = [s["name"] for s in sectors]
        if names:
            risks.append(f"{day}特异板块({'、'.join(names[:3])})，持续性存疑")

    # 拼合正文
    lines = [f"近{n_days}日热点扫描覆盖 {days_info[-1]['news_count']} 条新闻。"]

    if common_items:
        top3 = common_items[:3]
        lines.append(
            f"主线方向：{'、'.join(c['name'] + '(' + ('↑' if c['trend'].startswith('↑') else '→') + ')' for c in top3)}。"
        )

    if highlights:
        lines.append("亮点：" + "；".join(highlights[:4]) + "。")

    if risks:
        lines.append("⚠️ 风险关注：" + "；".join(risks[:3]) + "。")

    text = "".join(lines)

    return {
        "text": text,
        "highlights": highlights[:5],
        "risk_notes": risks[:5],
        "generated_by": "rule_engine",
    }


def _llm_enhance_summary(trends_sectors: list, comparison: dict, days_info: list) -> dict:
    """LLM 增强总结。失败时返回规则引擎结果。"""
    try:
        from agents.llm import get_llm

        llm = get_llm()
        if not llm.available:
            return {"error": "LLM 不可用", "generated_by": "rule_engine_fallback"}

        # 构建输入
        sectors_text = "\n".join(
            f"- {t['name']}: 评分 {t['scores']}, 趋势 {t['trend']}"
            for t in trends_sectors[:8]
        )
        common_items = comparison.get("common_sectors", [])[:10]
        common_text = "\n".join(
            f"- {c['name']}: 出现{c['appearances']}天, 均分{c['avg_score']}"
            for c in common_items
        )
        unique_text = ""
        for day, items in comparison.get("unique_per_day", {}).items():
            names = [i["name"] for i in items[:3]]
            if names:
                unique_text += f"\n{day}特异: {', '.join(names)}"

        days_text = "\n".join(
            f"- {d['scan_time']}: {d['news_count']}条, {d['sector_count']}板块, 均分{d['avg_score']}"
            for d in days_info
        )

        prompt = f"""你是资深宏观策略分析师。基于以下多日热点扫描数据生成200字以内的总结。

数据概览:
{days_text}

板块趋势:
{sectors_text}

共性板块:
{common_text}

特异板块:
{unique_text}

要求：
1. 概括主线演变趋势（哪条主线在强化/弱化）
2. 指出关键变化（新出现/退出的板块）
3. 风险提示（单日特异板块、评分骤降板块）
4. 输出纯文本（不要markdown），自然段落"""

        response = llm.chat(prompt, temperature=0.3, max_tokens=300)
        text = response.strip() if response else ""

        if text:
            return {
                "text": text,
                "highlights": [],
                "risk_notes": [],
                "generated_by": "llm",
            }
        return {"error": "LLM 返回空", "generated_by": "rule_engine_fallback"}

    except Exception as e:
        return {"error": str(e), "generated_by": "rule_engine_fallback"}
