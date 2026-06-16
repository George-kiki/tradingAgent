"""盘后复盘记录持久化（本地 JSON）。

保存每次生成的复盘报告（完整 HTML + 结构化指标），支持：
- 历史列表 / 按 id 查看详情 / 删除
- 按日期范围取（多日多维对比用）
- 导出独立 HTML 文件

每条记录结构：
    {
      "id": 12位hex,
      "date": "YYYY-MM-DD",        # 复盘对应的交易日（同日重复生成则覆盖更新）
      "created_at": "YYYY-MM-DD HH:MM:SS",
      "title": "每日盘后复盘",
      "html": "<完整HTML文档>",
      "metrics": {                  # 结构化指标，供多日对比
          "indices": [...],         # 主要指数点位/涨跌幅
          "breadth": {...},         # 涨跌家数/涨停跌停/平均涨幅
          "global": [...],          # 外盘
          "top_sectors": [...],     # 领涨板块 TOP
          "limit_up_count": int,
          "limit_down_count": int,
      }
    }
"""
from __future__ import annotations

import json
import os
import time
import uuid

STORE = os.path.join("data_store", "reviews.json")
_MAX = 200


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)


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
    _ensure_dir()
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(items[:_MAX], fp, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def save_review(date: str, title: str, html: str, metrics: dict | None = None) -> dict:
    """保存（同日覆盖）一份复盘报告，返回带 id 的记录。"""
    items = _load()
    # 同一交易日只保留一份（重复生成则更新）
    items = [r for r in items if r.get("date") != date]
    rec = {
        "id": uuid.uuid4().hex[:12],
        "date": date,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "title": title or "每日盘后复盘",
        "html": html or "",
        "metrics": metrics or {},
    }
    items.insert(0, rec)
    # 按日期倒序
    items.sort(key=lambda r: r.get("date", ""), reverse=True)
    _save(items)
    return rec


def list_reviews() -> list[dict]:
    """历史摘要列表（不含完整 HTML，含关键指标用于列表展示）。"""
    out = []
    for r in _load():
        m = r.get("metrics") or {}
        b = m.get("breadth") or {}
        idx = m.get("indices") or []
        sh = next((i for i in idx if i.get("name") == "上证指数"), None)
        out.append({
            "id": r.get("id"),
            "date": r.get("date"),
            "created_at": r.get("created_at"),
            "title": r.get("title"),
            "sh_index": (sh or {}).get("price"),
            "sh_pct": (sh or {}).get("pct"),
            "limit_up": b.get("limit_up"),
            "limit_down": b.get("limit_down"),
            "up": b.get("up"),
            "down": b.get("down"),
            "top_sector": (m.get("top_sectors") or [{}])[0].get("name") if m.get("top_sectors") else None,
        })
    return out


def get_review(rid: str) -> dict | None:
    for r in _load():
        if r.get("id") == rid:
            return r
    return None


def get_review_by_date(date: str) -> dict | None:
    for r in _load():
        if r.get("date") == date:
            return r
    return None


def delete_review(rid: str) -> bool:
    items = _load()
    new = [r for r in items if r.get("id") != rid]
    if len(new) == len(items):
        return False
    _save(new)
    return True


def all_dates() -> list[str]:
    """所有已存复盘的日期（倒序）。"""
    return sorted({r.get("date") for r in _load() if r.get("date")}, reverse=True)


def reviews_by_dates(dates: list[str]) -> list[dict]:
    """按给定日期列表取复盘记录（含 metrics，供多日对比），按日期升序返回。"""
    want = set(dates)
    out = [r for r in _load() if r.get("date") in want]
    out.sort(key=lambda r: r.get("date", ""))
    return out
