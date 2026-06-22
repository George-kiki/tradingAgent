"""A-Stock 直连数据源：通达信(mootdx) + 腾讯财经。

设计理念：
- mootdx（通达信 TCP 7709 协议）：K线、盘口，实测不封 IP，优先级最高
- 腾讯财经 HTTP：实时行情、PE/PB/市值、指数，不封 IP
- 东财系列：仅用于独有数据（龙虎榜、资金流、板块成分），内置限流防封

所有方法均为 best-effort：失败返回空/None，由上层 DataFetcher 自动降级到 AkShare/Tushare。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any, Optional

import pandas as pd
import requests

from data.cache import cached_call

# ============================================================
# mootdx（通达信）—— 可选依赖，未安装时自动降级
# ============================================================
try:
    from mootdx.quotes import Quotes
    _MOOTDX_AVAILABLE = True
except Exception:
    _MOOTDX_AVAILABLE = False


def _mootdx_client():
    """获取通达信行情客户端，自动探测可用服务器。"""
    if not _MOOTDX_AVAILABLE:
        return None
    try:
        # 直接创建客户端（Quotes.factory 自动连接最快服务器）
        c = Quotes.factory(market='std', timeout=10)
        # 快速探测可用性
        try:
            _ = c.bars(symbol='600519', frequency=9, start=0, offset=1)
            return c
        except Exception as e:
            print(f"[通达信] 探测失败: {e}，尝试备用服务器...")
            return None
    except Exception as e:
        print(f"[通达信] 初始化失败: {e}")
        return None


# ============================================================
# 腾讯财经 —— HTTP 直连，不封 IP
# ============================================================
_TENCENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _tencent_get(url: str, params: dict = None) -> Optional[requests.Response]:
    """腾讯财经 HTTP 请求（简单重试）。"""
    sess = requests.Session()
    sess.headers.update({"User-Agent": _TENCENT_UA})
    for attempt in range(3):
        try:
            resp = sess.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp
        except Exception:
            if attempt < 2:
                time.sleep(0.5)
    return None


def _tencent_market_snapshot(codes: list[str]) -> Optional[list[dict]]:
    """腾讯实时行情快照（批量）。返回 [{代码,名称,...}] 列表。"""
    tcodes = []
    for c in codes:
        c = str(c).strip().zfill(6)
        if c.startswith("6"):
            tcodes.append(f"sh{c}")
        else:
            tcodes.append(f"sz{c}")
    if not tcodes:
        return None
    resp = _tencent_get(
        "https://qt.gtimg.cn/",
        params={"q": ",".join(tcodes), "fmt": "json"}
    )
    if not resp:
        return None
    return _parse_tencent_json(resp.text)


def _tencent_all_market() -> Optional[pd.DataFrame]:
    """腾讯全市场行情快照（分页批量拉取沪深主板）。"""
    all_rows = []
    prefixes = [
        ("sh", "600"), ("sh", "601"), ("sh", "603"), ("sh", "605"),
        ("sz", "000"), ("sz", "001"), ("sz", "002"), ("sz", "003"),
    ]
    for market, prefix in prefixes:
        codes = [f"{market}{prefix}{i:03d}" for i in range(1000)]
        for batch_start in range(0, len(codes), 50):
            batch = codes[batch_start:batch_start + 50]
            rows = _tencent_market_snapshot(batch)  # 现在直接返回列表
            if rows:
                all_rows.extend(rows)
            time.sleep(0.1)
        if len(all_rows) >= 4000:
            break
    if not all_rows:
        return None
    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset=["代码"]).reset_index(drop=True)
    print(f"[腾讯] 全市场快照获取 {len(df)} 只")
    return df


def _parse_tencent_json(raw: str) -> list[dict]:
    """解析腾讯 JSON 格式快照 → [{代码,名称,最新价,涨跌幅,...}]。

    腾讯 API 返回: {"sh600519":["1","name","600519","1241.41",...]}
    字段索引: 1=名称, 3=最新价, 32=涨跌幅, 38=换手率, 39=PE, 45=总市值(亿), 46=PB
    """
    rows = []
    try:
        data = json.loads(raw)
    except Exception:
        return rows
    for code_key, vals in data.items():
        if not isinstance(vals, list) or len(vals) < 40:
            continue
        code = re.sub(r'^[shz]+', '', code_key).zfill(6)
        try:
            amount_val = _safe_float(vals[37])
            amount = amount_val * 10000 if amount_val else None  # 万→元
            mv_val = _safe_float(vals[45])
            rows.append({
                "代码": code,
                "名称": str(vals[1]) if len(vals) > 1 else code,
                "最新价": _safe_float(vals[3]) if len(vals) > 3 else None,
                "涨跌幅": _safe_float(vals[32]) if len(vals) > 32 else None,
                "成交额": amount,
                "换手率": _safe_float(vals[38]) if len(vals) > 38 else None,
                "市盈率-动态": _safe_float(vals[39]) if len(vals) > 39 else None,
                "量比": _safe_float(vals[49]) if len(vals) > 49 else None,
                "总市值": mv_val * 1e8 if mv_val else None,  # 亿→元
                "市净率": _safe_float(vals[46]) if len(vals) > 46 else None,
            })
        except (IndexError, ValueError):
            continue
    return rows


def _parse_tencent_snapshot(raw: str) -> list[dict]:
    """解析腾讯行情快照文本 → [{代码,名称,最新价,涨跌幅,...}]。

    兼容两种格式:
    - 旧: v_sh600519="1~茅台~2000.00~1.23~..."
    - 新: {"sh600519":["1","茅台",...]}
    """
    raw_strip = raw.strip()
    if raw_strip.startswith("{"):
        return _parse_tencent_json(raw_strip)
    # 旧格式
    rows = []
    for line in raw_strip.split("\n"):
        m = re.search(r'v_(s[hz]\d+)="(.+)"', line)
        if not m:
            continue
        code = re.sub(r'^[shz]+', '', m.group(1)).zfill(6)
        fields = m.group(2).split("~")
        if len(fields) < 40:
            continue
        try:
            rows.append({
                "代码": code,
                "名称": fields[1],
                "最新价": _safe_float(fields[3]),
                "涨跌幅": _safe_float(fields[32]),
                "成交额": _safe_float(fields[37]) * 10000 if _safe_float(fields[37]) else None,
                "换手率": _safe_float(fields[38]),
                "市盈率-动态": _safe_float(fields[39]),
                "量比": _safe_float(fields[49]) if len(fields) > 49 else None,
                "总市值": _safe_float(fields[45]) * 1e8 if _safe_float(fields[45]) else None,
                "市净率": _safe_float(fields[46]),
            })
        except (IndexError, ValueError):
            continue
    return rows


def _tencent_single_snapshot(symbol: str) -> Optional[dict]:
    """单只股票腾讯快照。"""
    symbol = str(symbol).strip().zfill(6)
    market = "sh" if symbol.startswith("6") else "sz"
    rows = _tencent_market_snapshot([symbol])  # 现在直接返回列表
    return rows[0] if rows else None


def _safe_float(v) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return round(float(v), 4)
    except (ValueError, TypeError):
        return None


# ============================================================
# 东财直连 —— 用于独有数据（板块、资金流、龙虎榜等），内置限流
# ============================================================
_EM_SESSION = None
_EM_LAST_CALL = 0.0
_EM_MIN_INTERVAL = 1.2  # 秒


def _em_session():
    global _EM_SESSION
    if _EM_SESSION is None:
        _EM_SESSION = requests.Session()
        _EM_SESSION.headers.update({
            "User-Agent": _TENCENT_UA,
            "Referer": "https://quote.eastmoney.com/",
        })
    return _EM_SESSION


def _em_get(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    """东财 HTTP GET，内置串行限流防封。"""
    global _EM_LAST_CALL
    elapsed = time.time() - _EM_LAST_CALL
    if elapsed < _EM_MIN_INTERVAL:
        time.sleep(_EM_MIN_INTERVAL - elapsed + (elapsed % 0.3))
    try:
        resp = _em_session().get(url, params=params, timeout=timeout)
        _EM_LAST_CALL = time.time()
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        _EM_LAST_CALL = time.time()
    return None


# ============================================================
# ASDataSource 主类
# ============================================================
class ASDataSource:
    """A-Stock 直连数据源：通达信 + 腾讯 + 东财独有端点。

    每个方法都返回结果字典或 DataFrame，字段与现有 DataFetcher 对齐。
    """

    def __init__(self):
        self._tdx_client = None
        self._tdx_available = _MOOTDX_AVAILABLE

    # ---------- K 线（通达信 > 腾讯 > 百度）----------
    def get_kline(self, symbol: str, days: int = 250) -> Optional[pd.DataFrame]:
        """日K线（前复权）。优先 mootdx，失败降级腾讯/百度。"""
        symbol = str(symbol).strip().zfill(6)
        key = f"as_kline:{symbol}:{days}"

        def _fetch():
            # 主源：通达信 mootdx
            df = self._kline_mootdx(symbol, days)
            if df is not None:
                return df

            # 备选：腾讯
            df = self._kline_tencent(symbol, days)
            if df is not None:
                return df

            return None

        return cached_call(key, _fetch, ttl=1800)

    def _kline_mootdx(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        """通达信日K线（前复权）。"""
        if not _MOOTDX_AVAILABLE:
            return None
        try:
            if self._tdx_client is None:
                self._tdx_client = _mootdx_client()
            if self._tdx_client is None:
                return None  # 不永久禁用，下次重试

            # 取足够多的K线
            df = self._tdx_client.bars(
                symbol=symbol,
                frequency=9,
                start=0,
                offset=min(days + 100, 800),
            )
            if df is None or df.empty:
                return None

            # mootdx 返回的 DataFrame 以 datetime 为索引，同时有一列 datetime
            # reset_index 会冲突，需要特殊处理
            df = df.reset_index(drop=True)  # 丢弃旧索引
            if "datetime" in df.columns:
                df["date"] = pd.to_datetime(df["datetime"]).dt.strftime("%Y-%m-%d")
            elif df.index.name == "datetime" or hasattr(df.index, "name") and "datetime" in str(df.index.name):
                df["date"] = df.index.astype(str)
            elif "date" not in df.columns:
                df["date"] = df.index.astype(str)

            # mootdx 同时返回 'vol' 和 'volume'，取其一，避免重复列名
            if "vol" in df.columns and "volume" in df.columns:
                df = df.drop(columns=["vol"])
            elif "vol" in df.columns:
                df = df.rename(columns={"vol": "volume"})

            df = df.sort_values("date").reset_index(drop=True)
            keep = [c for c in ["date", "open", "close", "high", "low",
                                "volume", "amount"] if c in df.columns]
            df = df[keep].tail(days).reset_index(drop=True)

            for c in ["open", "close", "high", "low", "volume", "amount"]:
                if c in df.columns:
                    df[c] = df[c].astype(float)
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            if not df.empty:
                df.attrs["source"] = "通达信K线"
                print(f"[通达信] {symbol} K线获取成功，{len(df)}条")
                return df
        except Exception as e:
            print(f"[通达信] {symbol} K线失败: {e}")
        return None

    def _kline_tencent(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        """腾讯财经日K线。"""
        symbol = str(symbol).strip().zfill(6)
        market = "sh" if symbol.startswith("6") else "sz"
        code = f"{market}{symbol}"
        params = {
            "kl": "101",  # 日K线
            "qn": "1",
            "code": code,
        }
        resp = _tencent_get("https://ifzq.gtimg.cn/appstock/app/fqkline/get", params=params)
        if not resp:
            return None
        try:
            data = resp.json()
            # 结构: data[code]["qfqday"] 或 data[code]["day"]
            stock_data = data.get("data", {}).get(code, {})
            klist = stock_data.get("qfqday") or stock_data.get("day") or []
            if not klist:
                return None
            rows = []
            for item in klist:
                # 格式: ["2024-01-15", "100.00", "102.00", "98.00", "101.50", "1234567.00"]
                parts = item if isinstance(item, list) else item.strip().split()
                if len(parts) < 2:
                    continue
                rows.append({
                    "date": str(parts[0]),
                    "open": float(parts[1]) if len(parts) > 1 else None,
                    "close": float(parts[2]) if len(parts) > 2 else None,
                    "high": float(parts[3]) if len(parts) > 3 else None,
                    "low": float(parts[4]) if len(parts) > 4 else None,
                    "volume": float(parts[5]) if len(parts) > 5 else None,
                })
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").tail(days).reset_index(drop=True)
            for c in ["open", "close", "high", "low", "volume"]:
                if c in df.columns:
                    df[c] = df[c].astype(float)
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            if not df.empty:
                df.attrs["source"] = "腾讯K线"
                print(f"[腾讯] {symbol} K线获取成功，{len(df)}条")
                return df
        except Exception as e:
            print(f"[腾讯] {symbol} K线解析失败: {e}")
        return None

    # ---------- 全市场快照（腾讯财经，不封IP）----------
    def get_market_spot(self) -> Optional[pd.DataFrame]:
        """全市场实时行情快照（优先腾讯，因不封IP）。"""

        def _fetch():
            df = _tencent_all_market()
            if df is not None and not df.empty:
                df.attrs["source"] = "腾讯财经实时快照"
                return df
            return None

        df = cached_call("as_spot_all", _fetch, ttl=300)
        if df is not None and not df.empty:
            return df
        return None

    # ---------- 单股基本信息 ----------
    def get_basic_info(self, symbol: str) -> dict:
        """个股基本信息：名称、行业、市值、PE/PB。"""
        snap = _tencent_single_snapshot(symbol)
        if not snap:
            return {}
        return {
            "股票简称": str(snap.get("名称", "")),
            "总市值": snap.get("总市值"),
            "动态市盈率": snap.get("市盈率-动态"),
            "市净率": snap.get("市净率"),
        }

    def get_name(self, symbol: str) -> Optional[str]:
        """仅获取股票名称。"""
        snap = _tencent_single_snapshot(symbol)
        return str(snap.get("名称", "")) if snap else None

    # ---------- 指数行情 ----------
    def get_index_spot(self) -> list[dict]:
        """主要指数腾讯实时行情。"""
        index_codes = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
            "sh000300": "沪深300",
            "sh000688": "科创50",
        }
        rows = _tencent_market_snapshot(list(index_codes.keys()))  # 直接返回列表
        if not rows:
            return []
        out = []
        for r in rows:
            name = index_codes.get(f"sh{r['代码']}", index_codes.get(f"sz{r['代码']}", ""))
            if name:
                out.append({
                    "name": name,
                    "price": r.get("最新价"),
                    "pct": r.get("涨跌幅"),
                })
        return out

    def get_index_kline(self, index_code: str = "sh000001",
                        days: int = 120) -> Optional[pd.DataFrame]:
        """大盘指数K线（腾讯）。"""
        params = {
            "kl": "101",
            "qn": "1",
            "code": index_code,
        }
        resp = _tencent_get("https://ifzq.gtimg.cn/appstock/app/fqkline/get", params=params)
        if not resp:
            return None
        try:
            data = resp.json()
            stock_data = data.get("data", {}).get(index_code, {})
            klist = stock_data.get("day") or []
            if not klist:
                return None
            rows = []
            for item in klist:
                parts = item if isinstance(item, list) else item.strip().split()
                if len(parts) < 5:
                    continue
                rows.append({
                    "date": str(parts[0]),
                    "close": float(parts[2]) if len(parts) > 2 else None,
                    "volume": float(parts[5]) if len(parts) > 5 else None,
                })
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").tail(days).reset_index(drop=True)
            for c in ["close", "volume"]:
                if c in df.columns:
                    df[c] = df[c].astype(float)
            return df
        except Exception:
            return None

    # ---------- 自聚合：从全市场快照推算板块热度（绕开东财限流）----------
    @staticmethod
    def _build_industry_map() -> dict[str, str]:
        """构建 symbol → industry 映射（从已知数据源静态聚合）。"""
        m: dict[str, str] = {}
        # 来源1: recommend/engine.py 的 POOL_INDUSTRY（45只蓝筹）
        try:
            from recommend.engine import POOL_INDUSTRY
            m.update(POOL_INDUSTRY)
        except Exception:
            pass
        # 来源2: recommend/universe.py 的 FALLBACK_THEME_STOCKS（16个主线板块）
        try:
            from recommend.universe import FALLBACK_THEME_STOCKS
            for theme, syms in FALLBACK_THEME_STOCKS.items():
                for s in syms:
                    s = str(s).strip().zfill(6)
                    if s not in m:
                        m[s] = theme
        except Exception:
            pass
        return m

    def get_hot_sectors_from_snapshot(self, limit: int = 10) -> list[dict]:
        """基于全市场快照自聚合板块涨幅（不依赖东财板块接口）。

        流程：
        1. 取腾讯全市场快照（已缓存，4000+只）
        2. 用已知行业映射(symbol→industry)给每只股标行业
        3. 按行业聚合：平均涨幅、领涨股、样本数
        4. 返回涨幅榜 Top N
        """

        def _fetch():
            spot = self.get_market_spot()
            if spot is None or spot.empty:
                return None

            ind_map = self._build_industry_map()
            if not ind_map:
                return None

            # 取涨幅/名称/代码列
            code_col = next((c for c in spot.columns if "代码" in c), None)
            name_col = next((c for c in spot.columns if "名称" in c), None)
            pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
            if not code_col or not pct_col:
                return None

            # 按行业聚合
            sector_data: dict[str, list[float]] = {}
            sector_leaders: dict[str, tuple[str, str, float]] = {}  # ind -> (code, name, pct)
            for _, row in spot.iterrows():
                code = str(row.get(code_col, "")).strip().zfill(6)
                industry = ind_map.get(code)
                if not industry:
                    continue
                try:
                    pct = float(row.get(pct_col, 0) or 0)
                except (ValueError, TypeError):
                    continue
                name = str(row.get(name_col, "")) if name_col else code

                sector_data.setdefault(industry, []).append(pct)
                cur_leader = sector_leaders.get(industry)
                if cur_leader is None or pct > cur_leader[2]:
                    sector_leaders[industry] = (code, name, pct)

            if not sector_data:
                return None

            out = []
            for ind, pcts in sector_data.items():
                avg = round(sum(pcts) / len(pcts), 2)
                leader = sector_leaders[ind]
                out.append({
                    "name": ind,
                    "pct": avg,
                    "leader": f"{leader[1]}({leader[2]:+.1f}%)",
                    "count": len(pcts),
                })
            out.sort(key=lambda x: x["pct"], reverse=True)
            return out[:limit] or None

        return cached_call("as_sectors_snapshot", _fetch, ttl=300) or []

    # ---------- 板块/热点（优先东财，失败自聚合）----------
    def get_hot_sectors(self, limit: int = 10) -> list[dict]:
        """行业板块涨幅榜：东财直连 → 自聚合兜底。"""

        def _fetch():
            # 优先东财（字段全：涨幅/领涨股/板块名）
            try:
                params = {
                    "pn": 1, "pz": limit,
                    "po": 1, "np": 1,
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": 2, "invt": 2,
                    "fid": "f3", "fs": "m:90+t:2",
                    "fields": "f12,f14,f3,f128,f4",
                }
                js = _em_get("https://push2.eastmoney.com/api/qt/clist/get", params=params)
                if js:
                    items = (js.get("data") or {}).get("diff") or []
                    if isinstance(items, dict):
                        items = list(items.values())
                    if items:
                        out = []
                        for d in items:
                            out.append({
                                "name": str(d.get("f14", "")),
                                "pct": _safe_float(d.get("f3")),
                                "leader": str(d.get("f128", "")),
                            })
                        return out
            except Exception:
                pass
            # 兜底：全市场快照自聚合
            return self.get_hot_sectors_from_snapshot(limit=limit) or None

        out = cached_call(f"as_sectors:{limit}", _fetch, ttl=600)
        return out or []

    def get_board_cons(self, board_name: str, board_type: str = "行业") -> list[dict]:
        """板块成分股（东财直连）。board_type: '行业' | '概念'。"""
        key = f"as_board_cons:{board_type}:{board_name}"

        def _fetch():
            # 先搜板块代码
            search_params = {
                "pn": 1, "pz": 5,
                "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2,
                "fid": "f3",
                "fs": "m:90+t:2" if board_type == "行业" else "m:90+t:3",
                "fields": "f12,f14",
                "f4": board_name,
            }
            # 尝试用名称搜索
            js = _em_get("https://push2.eastmoney.com/api/qt/clist/get", params=search_params)
            if not js:
                return None
            items = (js.get("data") or {}).get("diff") or []
            if isinstance(items, dict):
                items = list(items.values())
            if not items:
                return None
            bk_code = str(items[0].get("f12", ""))
            if not bk_code:
                return None

            # 取成分股
            cons_params = {
                "pn": 1, "pz": 200,
                "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2,
                "fid": "f3",
                "fs": f"b:{bk_code}+f:!200",
                "fields": "f12,f14,f3,f20",
            }
            js2 = _em_get("https://push2.eastmoney.com/api/qt/clist/get", params=cons_params)
            if not js2:
                return None
            cons_items = (js2.get("data") or {}).get("diff") or []
            if isinstance(cons_items, dict):
                cons_items = list(cons_items.values())
            out = []
            for d in cons_items:
                out.append({
                    "代码": str(d.get("f12", "")).zfill(6),
                    "名称": str(d.get("f14", "")),
                    "涨跌幅": _safe_float(d.get("f3")),
                    "总市值": _safe_float(d.get("f20")),
                })
            return out or None

        return cached_call(key, _fetch, ttl=21600) or []

    # ---------- 资金流向 ----------
    def get_fund_flow(self, symbol: str) -> dict:
        """个股资金流向（东财页面接口，与 akshare 同等逻辑但直连）。"""
        symbol = str(symbol).strip().zfill(6)
        market = 1 if symbol.startswith("6") else 0
        key = f"as_fund:{symbol}"

        def _fetch():
            params = {
                "lmt": 0, "klt": "1",
                "secid": f"{market}.{symbol}",
                "fields1": "f1,f2,f3,f7",
                "fields2": ("f51,f52,f53,f54,f55,f56,f57,f58,"
                            "f59,f60,f61,f62,f63,f64,f65"),
            }
            js = _em_get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                         params=params)
            if not js:
                return {}
            klines = (js.get("data") or {}).get("klines") or []
            if not klines:
                return {}
            # 取最近一条
            last = klines[-1]
            parts = last.split(",")
            if len(parts) < 7:
                return {}
            # f51=日期,f52=主力,f53=小单,f54=中单,f55=大单,f56=超大单
            # f57=主力净占比,f62=主力净流入
            def _p(i):
                return _safe_float(parts[i]) if i < len(parts) else 0.0
            main_net = _p(5) + _p(6) if len(parts) > 6 else _p(1)  # 大单+超大单
            return {
                "日期": parts[0],
                "主力净流入-净额": main_net,
                "大单净流入-净额": _p(5) if len(parts) > 5 else 0.0,
                "超大单净流入-净额": _p(6) if len(parts) > 6 else 0.0,
            }

        return cached_call(key, _fetch, ttl=3600) or {}

    # ---------- 龙虎榜 ----------
    def get_lhb_map(self) -> dict:
        """近一月龙虎榜统计 {代码: {次数, 净买额}}。"""

        def _fetch():
            params = {
                "pn": 1, "pz": 500,
                "po": 1, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2,
                "fid": "f184", "fs": "m:0+t:4",
                "fields": "f12,f61,f184,f66",
            }
            js = _em_get("https://push2.eastmoney.com/api/qt/clist/get", params=params)
            if not js:
                return None
            items = (js.get("data") or {}).get("diff") or []
            if isinstance(items, dict):
                items = list(items.values())
            out = {}
            for d in items:
                code = str(d.get("f12", "")).zfill(6)
                out[code] = {
                    "times": int(_safe_float(d.get("f61")) or 0),
                    "net_buy": _safe_float(d.get("f184")) or 0.0,
                    "last_date": str(d.get("f66", "")),
                }
            return out or None

        return cached_call("as_lhb_map", _fetch, ttl=3600) or {}

    # ---------- 新闻 ----------
    def get_news(self, symbol: str, limit: int = 8) -> list[dict]:
        """个股新闻（东财直连）。"""
        symbol = str(symbol).strip().zfill(6)
        key = f"as_news:{symbol}:{limit}"

        def _fetch():
            params = {
                "cb": "jQuery",
                "code": ("SH" if symbol.startswith("6") else "SZ") + symbol,
                "pageIndex": 1,
                "pageSize": limit,
                "rt": str(int(time.time() * 1000)),
            }
            try:
                resp = _em_session().get(
                    "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
                    params=params, timeout=10)
                if resp.status_code != 200:
                    return []
                # 返回是 JSONP 格式
                text = resp.text
                m = re.search(r'\((\{.*\})\)', text, re.DOTALL)
                if not m:
                    return []
                data = json.loads(m.group(1))
                items = data.get("data", {}).get("list", []) or data.get("data", {}).get("fastNewsList", []) or []
                out = []
                for item in items:
                    title = item.get("title", "") or item.get("Title", "")
                    if not title:
                        continue
                    out.append({
                        "title": title,
                        "time": str(item.get("showTime", "") or item.get("ShowTime", "")),
                    })
                return out[:limit]
            except Exception:
                return []

        return cached_call(key, _fetch, ttl=1800) or []

    # ---------- 北向资金持股 ----------
    def get_north_hold(self, symbol: str) -> dict:
        """个股北向资金持股（东财直连）。"""
        symbol = str(symbol).strip().zfill(6)
        market = 1 if symbol.startswith("6") else 0
        key = f"as_north:{symbol}"

        def _fetch():
            params = {
                "lmt": 10,
                "klt": "1",
                "secid": f"{market}.{symbol}",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52",
            }
            # 使用港股通持股接口
            js = _em_get(
                "https://push2his.eastmoney.com/api/qt/stock/hkhold/chgday/get",
                params=params)
            if not js:
                return {}
            klines = (js.get("data") or {}).get("klines") or []
            if not klines:
                return {}
            # 最新一条
            last = klines[-1].split(",") if isinstance(klines[-1], str) else klines[-1]
            if len(last) < 2:
                return {}
            ratio = _safe_float(last[1])  # 持股占比
            trend = None
            if len(klines) >= 6:
                prev = klines[-6].split(",") if isinstance(klines[-6], str) else klines[-6]
                if len(prev) >= 2:
                    prev_ratio = _safe_float(prev[1])
                    if prev_ratio is not None and ratio is not None:
                        trend = round(ratio - prev_ratio, 3)
            return {
                "ratio": ratio,
                "market_cap": None,
                "trend_5d": trend,
            }

        return cached_call(key, _fetch, ttl=86400) or {}

    # ---------- 板块热度榜（近一周）----------
    def get_hot_sectors_week(self, top: int = 12) -> list[dict]:
        """近一周板块热度榜：东财直连 → 自聚合兜底。

        兜底时 week_pct 用当日涨幅近似（腾讯无历史板块数据）。
        """

        def _fetch():
            # 优先东财（有近一周历史累计涨幅）
            try:
                rows = []
                for fs_type in ["m:90+t:2", "m:90+t:3"]:
                    board_type = "行业" if "t:2" in fs_type else "概念"
                    params = {
                        "pn": 1, "pz": 30, "po": 1, "np": 1,
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                        "fltt": 2, "invt": 2,
                        "fid": "f3", "fs": fs_type,
                        "fields": "f12,f14,f3,f128",
                    }
                    js = _em_get("https://push2.eastmoney.com/api/qt/clist/get", params=params)
                    if not js:
                        continue
                    items = (js.get("data") or {}).get("diff") or []
                    if isinstance(items, dict):
                        items = list(items.values())
                    for d in items:
                        day_pct = _safe_float(d.get("f3"))
                        week_pct = None
                        try:
                            bk_code = str(d.get("f12", ""))
                            if bk_code:
                                hist_js = _em_get(
                                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                                    params={"lmt": 10, "klt": "101",
                                            "secid": f"90.{bk_code}",
                                            "fields1": "f1,f2,f3,f7",
                                            "fields2": "f51,f52,f53,f54,f55,f56,f57"})
                                if hist_js:
                                    klines = (hist_js.get("data") or {}).get("klines") or []
                                    if len(klines) >= 2:
                                        p0 = klines[-min(6, len(klines))].split(",")
                                        p1 = klines[-1].split(",")
                                        if len(p0) > 2 and len(p1) > 2:
                                            c0, c1 = _safe_float(p0[2]), _safe_float(p1[2])
                                            if c0 and c1:
                                                week_pct = round((c1 / c0 - 1) * 100, 2)
                        except Exception:
                            pass
                        rows.append({
                            "name": str(d.get("f14", "")),
                            "type": board_type,
                            "day_pct": day_pct,
                            "week_pct": week_pct if week_pct is not None else day_pct,
                            "leader": str(d.get("f128", "")),
                            "fund_flow": "",
                        })
                if rows:
                    rows.sort(key=lambda x: (x["week_pct"] if x["week_pct"] is not None else -999),
                              reverse=True)
                    seen, uniq = set(), []
                    for r in rows:
                        if r["name"] not in seen:
                            seen.add(r["name"]); uniq.append(r)
                    return uniq[:top]
            except Exception:
                pass

            # 兜底：全市场快照自聚合（week_pct ≈ day_pct）
            spot_sectors = self.get_hot_sectors_from_snapshot(limit=top * 2)  # expanded to capture concepts
            if spot_sectors:
                result = []
                for s in spot_sectors:
                    result.append({
                        "name": s["name"],
                        "type": "行业",
                        "day_pct": s["pct"],
                        "week_pct": s["pct"],  # 无历史，当日近似
                        "leader": s.get("leader", ""),
                        "fund_flow": "",
                    })
                return result[:top]
            return None

        out = cached_call(f"as_sectors_week:{top}", _fetch, ttl=3600)
        return out or []


# ============================================================
# 单例工厂
# ============================================================
_as_source = None


def get_as_source() -> ASDataSource:
    global _as_source
    if _as_source is None:
        _as_source = ASDataSource()
    return _as_source
