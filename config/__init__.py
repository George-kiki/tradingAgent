"""用户配置文件加载（watchlist / review）。"""
from __future__ import annotations

import json
import os
from functools import lru_cache

_DIR = os.path.dirname(__file__)


def _load_json(filename: str, default: dict) -> dict:
    path = os.path.join(_DIR, filename)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


@lru_cache(maxsize=1)
def get_watchlist_config() -> dict:
    return _load_json("watchlist.json", {"watchlist": [], "holdings": []})


@lru_cache(maxsize=1)
def get_review_config() -> dict:
    return _load_json("review.json", {
        "title": "每日盘后复盘",
        "enabled_modes": ["market", "breadth", "hotspot"],
        "use_llm_for": [],
        "mode_options": {},
    })


def get_watchlist() -> list[str]:
    return get_watchlist_config().get("watchlist", [])


def get_holdings() -> list[dict]:
    return get_watchlist_config().get("holdings", [])
