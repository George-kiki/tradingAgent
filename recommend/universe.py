"""动态候选池：让荐股紧贴市场主线与板块轮动。

== 为什么需要它 ==
原候选池是固定的 45 只大盘蓝筹，**结构性地排除了科技/半导体等成长主线**——无论权重
怎么调，都选不出市场主线标的。本模块从**近一周最热板块**动态拉取成分股组成候选池，
确保科技主导期能覆盖半导体、元件、AI 等热门板块的个股。

== 构建逻辑 ==
1. 取近一周板块热度榜（多日累计涨幅，识别持续主线）；
2. 从最热的若干板块各取成分股（按当日涨幅/市值排序取头部，控制单板块数量）；
3. 叠加核心蓝筹基础池（保证大票基本盘 + 数据兜底名称），去重得到动态候选池；
4. 输出 symbol->所属热门板块 的映射，供引擎给"踩中主线"的个股加分、剔除脱离主线者。

== 容错 ==
东财板块接口限流期：回退到内置「主线板块 -> 代表成分股」静态映射（覆盖科技/半导体/
AI/新能源/医药等常见主线），保证限流时仍能给出贴近主线的候选池，不退化为纯蓝筹。
"""
from __future__ import annotations

import os
from typing import Optional

# 核心蓝筹基础池（始终纳入，保证大盘基本盘与名称兜底）。与 engine.POOL_NAMES 互补。
BLUECHIP_BASE = [
    "600519", "600036", "601318", "600900", "601012", "600887", "601888",
    "600030", "601899", "000858", "000333", "002594", "002415", "002230",
]

# 主线板块 -> 代表成分股（静态兜底，限流时使用；覆盖科技成长主线）。
# 选取各板块流动性好、辨识度高的中大盘代表股，确保限流期候选池仍贴近主线。
FALLBACK_THEME_STOCKS: dict[str, list[str]] = {
    "半导体": ["688981", "603501", "002371", "603986", "688012", "002049", "600460", "688082"],
    "元件": ["002475", "300782", "002241", "603160", "600183", "002138"],
    "消费电子": ["002475", "002241", "300433", "002938", "601138", "002916"],
    "光模块/CPO": ["300308", "300502", "002281", "300394"],
    "人工智能": ["002230", "300024", "688256", "002415", "300033", "600588"],
    "软件开发": ["600588", "300033", "002230", "300454", "688111", "688561"],
    "通信设备": ["000063", "300308", "002281", "600522", "002902"],
    "算力/服务器": ["000977", "603019", "300474", "002308"],
    "新能源车": ["002594", "300750", "002460", "300014", "002074"],
    "光伏设备": ["601012", "300274", "002129", "688599", "688223"],
    "电池": ["300750", "002460", "300014", "002074", "688567"],
    "医药/创新药": ["600276", "603259", "300760", "688180", "002821"],
    "证券": ["600030", "300059", "601995", "600999", "000776"],
    "军工": ["600760", "002179", "600893", "000768", "300034"],
    "机器人": ["300124", "002527", "688017", "300161", "002472"],
}


def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default


# 可调参数
TOP_SECTORS = _i("REC_UNIVERSE_TOP_SECTORS", 6)        # 取近一周最热的几个板块
PER_SECTOR = _i("REC_UNIVERSE_PER_SECTOR", 12)         # 每个热门板块取多少成分股
MAX_UNIVERSE = _i("REC_UNIVERSE_MAX", 120)             # 候选池上限（控制扫描耗时）


def _eligible(symbol: str) -> bool:
    """与 engine.eligible 一致：剔除科创/创业/北交/B股，仅留沪深主板。"""
    s = (symbol or "").strip()
    if len(s) != 6 or not s.isdigit():
        return False
    if s.startswith("688") or s.startswith(("300", "301")):
        return False
    if s.startswith(("8", "4", "9")):
        return False
    return s.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))


def build_dynamic_universe(fetcher, base_pool: Optional[list[str]] = None
                           ) -> tuple[list[str], dict[str, dict], list[dict]]:
    """构建动态候选池。

    返回 (候选代码列表, symbol->{sector,sector_type,sector_rank,week_pct} 映射, 主线板块列表)。
    主线板块列表即近一周热度榜（已排序），供情绪看板与选股加分使用。
    """
    base_pool = base_pool or []
    sector_map: dict[str, dict] = {}      # symbol -> 所属最热板块信息
    hot_sectors: list[dict] = []

    # 1) 近一周热度榜（识别主线）
    try:
        hot_sectors = fetcher.get_hot_sectors_week(top=TOP_SECTORS * 2) or []
    except Exception:
        hot_sectors = []

    universe: list[str] = []
    seen: set[str] = set()

    def _add(sym: str, sector: Optional[dict] = None, rank: Optional[int] = None):
        sym = (sym or "").strip()
        if not _eligible(sym) or sym in seen:
            # 即使重复，也补登记板块归属（取排名更靠前的板块）
            if sym and sector and sym in sector_map and rank is not None:
                if rank < sector_map[sym].get("sector_rank", 999):
                    sector_map[sym] = {"sector": sector["name"], "sector_type": sector.get("type", ""),
                                       "sector_rank": rank, "week_pct": sector.get("week_pct")}
            return
        seen.add(sym)
        universe.append(sym)
        if sector and rank is not None:
            sector_map[sym] = {"sector": sector["name"], "sector_type": sector.get("type", ""),
                               "sector_rank": rank, "week_pct": sector.get("week_pct")}

    used_fallback = False
    if hot_sectors:
        # 2) 从最热板块拉成分股
        got_any_cons = False
        for rank, sec in enumerate(hot_sectors[:TOP_SECTORS], 1):
            try:
                cons = fetcher.get_board_cons(sec["name"], sec.get("type", "行业")) or []
            except Exception:
                cons = []
            if not cons:
                continue
            got_any_cons = True
            # 板块内按当日涨幅取头部（活跃），控制单板块数量
            cons = [c for c in cons if _eligible(c.get("代码", ""))]
            cons.sort(key=lambda c: (c.get("涨跌幅") if c.get("涨跌幅") is not None else -999),
                      reverse=True)
            for c in cons[:PER_SECTOR]:
                _add(c["代码"], sec, rank)
            if len(universe) >= MAX_UNIVERSE:
                break
        if not got_any_cons:
            used_fallback = True
    else:
        used_fallback = True

    # 3) 限流兜底：用静态主线板块映射
    if used_fallback:
        for rank, (theme, syms) in enumerate(FALLBACK_THEME_STOCKS.items(), 1):
            sec = {"name": theme, "type": "主线(兜底)", "week_pct": None}
            for s in syms:
                _add(s, sec, rank)
            if len(universe) >= MAX_UNIVERSE:
                break
        if not hot_sectors:
            # 给情绪看板一个兜底主线列表
            hot_sectors = [{"name": t, "type": "主线(兜底)", "week_pct": None, "day_pct": None,
                            "leader": ""} for t in list(FALLBACK_THEME_STOCKS)[:TOP_SECTORS]]

    # 4) 叠加蓝筹基础池（保证大盘基本盘，不抢占主线名额）
    for s in BLUECHIP_BASE:
        _add(s)
    for s in base_pool:
        _add(s)

    return universe[:MAX_UNIVERSE], sector_map, hot_sectors
