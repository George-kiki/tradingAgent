"""轻量级本地文件缓存（pickle），降低对数据源的请求频率。"""
from __future__ import annotations

import hashlib
import os
import pickle
import time
from typing import Any, Callable, Optional

from core.config import settings


def _key_to_path(key: str) -> str:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return os.path.join(settings.cache_dir, f"{h}.pkl")


def get_cached(key: str, ttl: Optional[int] = None) -> Optional[Any]:
    """读取缓存；过期或不存在返回 None。"""
    ttl = settings.cache_ttl if ttl is None else ttl
    path = _key_to_path(key)
    if not os.path.exists(path):
        return None
    if ttl > 0 and (time.time() - os.path.getmtime(path)) > ttl:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def set_cached(key: str, value: Any) -> None:
    try:
        with open(_key_to_path(key), "wb") as f:
            pickle.dump(value, f)
    except Exception:
        pass


def cached_call(key: str, func: Callable[[], Any], ttl: Optional[int] = None) -> Any:
    """带缓存的函数调用包装。"""
    val = get_cached(key, ttl)
    if val is not None:
        return val
    val = func()
    if val is not None:
        set_cached(key, val)
    return val


def invalidate_cache(key: str) -> bool:
    """删除指定 key 的缓存文件，强制下次调用重新拉取。返回是否成功删除。"""
    path = _key_to_path(key)
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except Exception:
            return False
    return False


def invalidate_pattern(prefix: str = "") -> int:
    """批量删除匹配前缀的缓存（prefix 为空则清全部）。返回删除数量。"""
    d = settings.cache_dir
    if not os.path.isdir(d):
        return 0
    count = 0
    for f in os.listdir(d):
        if prefix and not f.startswith(prefix):
            continue
        fp = os.path.join(d, f)
        try:
            os.remove(fp)
            count += 1
        except Exception:
            pass
    return count
