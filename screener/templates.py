"""自定义选股模板的本地持久化（JSON 文件，可命名保存/加载/删除）。

模板结构（conditions）：
{
  "pct_change":   {"min":3, "max":5},     # 当日涨幅 %（None 不限）
  "vol_ratio":    {"min":1, "max":null},  # 量比
  "turnover":     {"min":5, "max":10},    # 换手率 %
  "market_cap":   {"min":50,"max":200},   # 总市值（亿元）
  "fund_flow_stable": true,               # 资金流向稳定（剔除主力净流出/不稳）
  "ma_golden_cross":  {"fast":5,"slow":30,"within":5,"slow_rising":true},  # 5日金叉30日且30日向上
  "new_high_at_close":{"window":20},      # 尾盘（收盘）创 N 日新高
  "exclude_boards": true                  # 剔除科创板688/创业板300·301/北交所
}
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Optional

_PATH = os.path.join("data_store", "screener_templates.json")


# 默认模板（对应用户给定的经典条件）
DEFAULT_TEMPLATE = {
    "name": "默认模板（涨幅3-5%·金叉新高）",
    "conditions": {
        "pct_change": {"min": 3, "max": 5},
        "vol_ratio": {"min": 1, "max": None},
        "turnover": {"min": 5, "max": 10},
        "market_cap": {"min": 50, "max": 200},
        "fund_flow_stable": True,
        "ma_golden_cross": {"fast": 5, "slow": 30, "within": 5, "slow_rising": True},
        "new_high_at_close": {"window": 20},
        "exclude_boards": True,
    },
    "builtin": True,
}


def _load_raw() -> list[dict]:
    if not os.path.exists(_PATH):
        return []
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_raw(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(_PATH)), exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def list_templates() -> list[dict]:
    """返回所有模板：内置默认模板 + 用户自定义模板。"""
    user = _load_raw()
    # 默认模板始终置顶（若用户未用同名覆盖）
    names = {t.get("name") for t in user}
    out = []
    if DEFAULT_TEMPLATE["name"] not in names:
        out.append(DEFAULT_TEMPLATE)
    out.extend(user)
    return out


def save_template(name: str, conditions: dict) -> dict:
    """保存（新增或按名称覆盖）一个模板。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    items = _load_raw()
    items = [t for t in items if t.get("name") != name]  # 同名覆盖
    item = {"name": name, "conditions": conditions,
            "builtin": False, "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    items.append(item)
    _save_raw(items)
    return item


def delete_template(name: str) -> bool:
    items = _load_raw()
    new = [t for t in items if t.get("name") != name]
    if len(new) == len(items):
        return False
    _save_raw(new)
    return True
