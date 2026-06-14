"""价值挖掘分析结果持久化（本地 JSON）。

保存每次五步分析的完整结果，支持历史列表、按 id 查看、删除。
"""
from __future__ import annotations

import json
import os
import time
import uuid

STORE = os.path.join("data_store", "value_analyses.json")
_MAX = 100


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


def save_analysis(result: dict) -> dict:
    """保存一次分析，返回带 id/created_at 的记录。"""
    items = _load()
    rec = dict(result)
    rec["id"] = uuid.uuid4().hex[:12]
    rec["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    items.insert(0, rec)
    _save(items)
    return rec


def list_analyses() -> list[dict]:
    """历史摘要列表（不含完整正文）。"""
    out = []
    for r in _load():
        primary = r.get("primary") or {}
        out.append({
            "id": r.get("id"),
            "created_at": r.get("created_at"),
            "target": r.get("target"),
            "primary_name": primary.get("name"),
            "primary_symbol": primary.get("symbol"),
            "verdict": (r.get("redteam") or {}).get("verdict"),
            "bottleneck_count": len((r.get("bottlenecks") or {}).get("bottlenecks", [])),
        })
    return out


def get_analysis(aid: str) -> dict | None:
    for r in _load():
        if r.get("id") == aid:
            return r
    return None


def delete_analysis(aid: str) -> bool:
    items = _load()
    new = [r for r in items if r.get("id") != aid]
    if len(new) == len(items):
        return False
    _save(new)
    return True
