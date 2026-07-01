"""轻量级本地文件缓存（pickle），降低对数据源的请求频率。

缓存文件命名规则：
- 原始 key → MD5(key) → {hash}.pkl
- 并行维护 _cache_index.json 映射 key→hash，使 invalidate_pattern(key_prefix)
  能正确按 key 前缀批量清除（旧实现错误地用前缀匹配 MD5 哈希文件名，从不命中）。
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import threading
import time
from typing import Any, Callable, Optional

from core.config import settings

_index_lock = threading.Lock()


def _index_path() -> str:
    return os.path.join(settings.cache_dir, "_cache_index.json")


def _load_index() -> dict[str, str]:
    """加载 key→hash 映射表。"""
    p = _index_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(idx: dict[str, str]) -> None:
    """持久化 key→hash 映射表（原子写入）。"""
    os.makedirs(settings.cache_dir, exist_ok=True)
    tmp = _index_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False)
        os.replace(tmp, _index_path())
    except Exception:
        pass


def _index_add(key: str, h: str) -> None:
    with _index_lock:
        idx = _load_index()
        idx[key] = h
        _save_index(idx)


def _index_remove(key: str) -> None:
    with _index_lock:
        idx = _load_index()
        idx.pop(key, None)
        _save_index(idx)


def _key_to_path(key: str) -> str:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return os.path.join(settings.cache_dir, f"{h}.pkl")


def get_cached(key: str, ttl: Optional[int] = None, allow_expired: bool = False) -> Optional[Any]:
    """读取缓存；过期或不存在返回 None。

    allow_expired=True 时忽略 TTL，用于实时数据源临时失效时返回最近一次成功值。
    """
    ttl = settings.cache_ttl if ttl is None else ttl
    path = _key_to_path(key)
    if not os.path.exists(path):
        return None
    if not allow_expired and ttl > 0 and (time.time() - os.path.getmtime(path)) > ttl:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def get_cache_age(key: str) -> Optional[float]:
    """返回缓存年龄（秒）；不存在返回 None。"""
    path = _key_to_path(key)
    if not os.path.exists(path):
        return None
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except Exception:
        return None


def set_cached(key: str, value: Any) -> None:
    try:
        p = _key_to_path(key)
        with open(p, "wb") as f:
            pickle.dump(value, f)
        _index_add(key, os.path.basename(p))
    except Exception:
        pass


def is_cacheable_value(value: Any) -> bool:
    """判断结果是否值得写入缓存，避免空结果污染后续调用。"""
    if value is None:
        return False
    try:
        import pandas as pd  # type: ignore
        if isinstance(value, pd.DataFrame):
            return not value.empty
    except Exception:
        pass
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def cached_call(key: str, func: Callable[[], Any], ttl: Optional[int] = None) -> Any:
    """带缓存的函数调用包装。"""
    val = get_cached(key, ttl)
    if val is not None:
        return val
    val = func()
    if is_cacheable_value(val):
        set_cached(key, val)
    return val


def cached_call_with_stale(
    key: str,
    func: Callable[[], Any],
    ttl: Optional[int] = None,
    stale_ttl: Optional[int] = None,
) -> tuple[Any, bool, Optional[float]]:
    """带过期兜底的缓存调用。

    返回 (value, is_stale, cache_age_seconds)。正常命中或拉取成功时 is_stale=False；
    拉取失败但存在最近一次成功缓存时 is_stale=True。
    """
    val = get_cached(key, ttl)
    if val is not None:
        return val, False, get_cache_age(key)

    val = func()
    if is_cacheable_value(val):
        set_cached(key, val)
        return val, False, 0.0

    stale = get_cached(key, stale_ttl or 0, allow_expired=True)
    if stale is not None:
        age = get_cache_age(key)
        if stale_ttl is None or stale_ttl <= 0 or age is None or age <= stale_ttl:
            return stale, True, age
    return val, False, None


def invalidate_cache(key: str) -> bool:
    """删除指定 key 的缓存文件，强制下次调用重新拉取。返回是否成功删除。"""
    path = _key_to_path(key)
    _index_remove(key)
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    return False


def invalidate_pattern(prefix: str = "") -> int:
    """批量删除匹配 key 前缀的缓存（prefix 为空则清全部）。

    通过 _cache_index.json 映射表从原始 key 反查文件，解决旧实
    现用前缀匹配 MD5 哈希文件名永远不命中的 bug。
    """
    d = settings.cache_dir
    if not os.path.isdir(d):
        return 0

    count = 0
    if prefix == "":
        # 清空全部：删所有 .pkl + 清索引
        for f in os.listdir(d):
            if f.endswith(".pkl"):
                fp = os.path.join(d, f)
                try:
                    os.remove(fp)
                    count += 1
                except Exception:
                    pass
        # 清空索引文件
        idx_path = _index_path()
        if os.path.exists(idx_path):
            try:
                os.remove(idx_path)
            except Exception:
                pass
        return count

    # 按前缀匹配 → 通过索引反查文件名
    with _index_lock:
        idx = _load_index()
        to_remove: list[str] = [k for k in idx if k.startswith(prefix)]

    for key in to_remove:
        path = _key_to_path(key)
        if os.path.exists(path):
            try:
                os.remove(path)
                count += 1
            except Exception:
                pass

    # 清理索引中已删除的条目
    if count > 0:
        with _index_lock:
            idx = _load_index()
            for key in to_remove:
                idx.pop(key, None)
            _save_index(idx)

    return count
