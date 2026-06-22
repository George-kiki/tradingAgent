"""A股数据采集器：多源架构。

数据源优先级（不封IP优先）：
1. A-Stock直连（通达信mootdx + 腾讯财经，不封IP，主力源）
2. Tushare（需Token，数据最全，备用）
3. AkShare（免费兜底，东财/新浪封装）
4. 新浪（最底层兜底）

封装常用数据接口并做：
1. 中文列名 -> 英文标准列名
2. 本地缓存
3. 异常降级容错（单点数据失败不影响整体分析）
"""
from __future__ import annotations

import datetime as dt
import time
from functools import lru_cache
from typing import Callable, Optional

import pandas as pd

from data.cache import cached_call

try:
    import akshare as ak
except Exception:  # pragma: no cover
    ak = None


def _retry(func: Callable, retries: int = 3, backoff: float = 1.5):
    """对易受限流/网络抖动影响的数据接口做自动重试 + 退避。

    成功返回结果；全部失败时返回最后一次的异常对象交由上层处理（这里返回 None）。
    """
    last_err = None
    for i in range(retries):
        try:
            return func()
        except Exception as e:  # 网络/限流等临时错误
            last_err = e
            if i < retries - 1:
                time.sleep(backoff * (i + 1))
    if last_err:
        print(f"[数据获取重试{retries}次仍失败] {last_err}")
    return None


# K线中文列 -> 英文列 映射
_HIST_COLS = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "换手率": "turnover",
}


def _require_ak():
    if ak is None:
        raise RuntimeError(
            "未安装 akshare，请先执行: pip install -r requirements.txt"
        )


def normalize_symbol(symbol: str) -> str:
    """标准化股票代码为 6 位数字（去除 sh/sz 前缀等）。"""
    s = symbol.strip().lower().replace("sh", "").replace("sz", "").replace(".", "")
    return s.zfill(6) if s.isdigit() else symbol.strip()


class DataFetcher:
    """统一数据访问入口。

    数据源优先级：A-Stock直连（不封IP）→ Tushare（Token）→ AkShare → 新浪。
    """

    def __init__(self):
        try:
            from data.tushare_source import get_tushare
            self._ts = get_tushare()
        except Exception:
            self._ts = None
        try:
            from data.astock_source import get_as_source
            self._as = get_as_source()
        except Exception:
            self._as = None
        self._last_market_spot_source = ""
        self._last_kline_source = ""

    @property
    def active_source(self) -> str:
        parts = []
        if self._as:
            parts.append("A-Stock直连")
        if self._ts and self._ts.ready:
            parts.append("Tushare")
        if ak is not None:
            parts.append("AkShare")
        return "+".join(parts) if parts else "无可用源"

    @property
    def last_market_spot_source(self) -> str:
        return self._last_market_spot_source or "未知"

    @property
    def last_kline_source(self) -> str:
        return self._last_kline_source or "未知"

    # ---------- 历史 K 线 ----------
    def get_kline(
        self,
        symbol: str,
        days: int = 250,
        adjust: str = "qfq",
        period: str = "daily",
    ) -> pd.DataFrame:
        """获取日 K 线（默认前复权）。返回标准英文列 DataFrame。"""
        symbol = normalize_symbol(symbol)
        end = dt.date.today()
        start = end - dt.timedelta(days=int(days * 1.7) + 30)  # 预留非交易日
        key = f"kline:v2:{symbol}:{period}:{adjust}:{start}:{end}"

        def _fetch():
            # 主源1：A-Stock直连（通达信mootdx > 腾讯，不封IP）
            if self._as and period == "daily":
                try:
                    df_as = self._as.get_kline(symbol, days=days)
                    if df_as is not None and not df_as.empty:
                        df_as.attrs["source"] = df_as.attrs.get("source", "A-Stock直连K线")
                        return df_as
                except Exception:
                    pass

            # 主源2：Tushare 日线（备用）
            if self._ts and self._ts.ready and period == "daily":
                try:
                    df_ts = self._ts.get_kline(symbol, days=days, adjust=adjust)
                    if df_ts is not None and not df_ts.empty:
                        df_ts.attrs["source"] = "Tushare日线"
                        return df_ts
                except Exception:
                    pass

            # 主源3：东方财富 K 线（兜底）
            if ak is not None:
                try:
                    df_em = _retry(lambda: ak.stock_zh_a_hist(
                        symbol=symbol,
                        period=period,
                        start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"),
                        adjust=adjust,
                    ), retries=1)
                    if df_em is not None and not df_em.empty:
                        df_em.attrs["source"] = "东方财富K线兜底"
                        return df_em
                except Exception:
                    pass

            # 备用源：新浪（不同服务器，规避东财限流）
            if ak is not None:
                print("[K线] Tushare/东财 获取失败，切换新浪备用源...")
                try:
                    sina_symbol = ("sh" if symbol.startswith("6") else "sz") + symbol
                    df_sina = _retry(lambda: ak.stock_zh_a_daily(
                        symbol=sina_symbol, adjust=adjust or "qfq"
                    ), retries=1)
                    # 新浪换手率为小数比例（如 0.002），统一换算为百分比口径，与东财一致
                    if df_sina is not None and not df_sina.empty:
                        df_sina = df_sina.copy()
                        if "turnover" in df_sina.columns:
                            df_sina["turnover"] = pd.to_numeric(df_sina["turnover"], errors="coerce") * 100
                        df_sina.attrs["source"] = "新浪K线兜底"
                    return df_sina
                except Exception:
                    return None
            return None

        df = cached_call(key, _fetch, ttl=3600)
        if df is None or df.empty:
            self._last_kline_source = "无可用K线源"
            return pd.DataFrame()
        self._last_kline_source = df.attrs.get("source", "未知K线源")

        df = df.rename(columns=_HIST_COLS)
        keep = [c for c in ["date", "open", "close", "high", "low", "volume",
                            "amount", "pct_change", "turnover"] if c in df.columns]
        df = df[keep].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for c in ["open", "close", "high", "low", "volume", "amount", "pct_change", "turnover"]:
            if c in df.columns:
                df[c] = df[c].astype(float)
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        out = df.tail(days).reset_index(drop=True)
        out.attrs["source"] = self._last_kline_source
        return out

    # ---------- 个股基本信息 ----------
    def get_basic_info(self, symbol: str) -> dict:
        """个股基本信息：名称、行业、总市值、流通市值等。"""
        symbol = normalize_symbol(symbol)
        key = f"basic:{symbol}"

        def _fetch():
            # 优先腾讯（不封IP，快速）
            if self._as:
                try:
                    info = self._as.get_basic_info(symbol)
                    if info:
                        return info
                except Exception:
                    pass
            # 回退东财
            if ak is not None:
                try:
                    df = ak.stock_individual_info_em(symbol=symbol)
                    return dict(zip(df["item"], df["value"]))
                except Exception:
                    return {}
            return {}

        return cached_call(key, _fetch, ttl=86400) or {}

    def get_code_name_map(self) -> dict:
        """全 A 股 代码->名称 映射（轻量静态接口，单次缓存，最稳的名称来源）。"""
        _require_ak()

        def _fetch():
            try:
                df = ak.stock_info_a_code_name()
                if df is None or df.empty:
                    return None  # 返回 None 避免空结果被缓存，便于下次重试
                code_col = next((c for c in df.columns if "code" in c.lower() or "代码" in c), df.columns[0])
                name_col = next((c for c in df.columns if "name" in c.lower() or "名称" in c), df.columns[-1])
                out = {}
                for code, name in zip(df[code_col].astype(str), df[name_col].astype(str)):
                    out[normalize_symbol(code)] = name
                return out or None
            except Exception:
                return None

        return cached_call("code_name_map", _fetch, ttl=86400) or {}

    def get_name(self, symbol: str) -> str:
        symbol = normalize_symbol(symbol)
        # 优先腾讯快照（最快，不封IP）
        if self._as:
            try:
                nm = self._as.get_name(symbol)
                if nm and nm != symbol:
                    return nm
            except Exception:
                pass
        info = self.get_basic_info(symbol)
        name = info.get("股票简称") or info.get("简称")
        if name:
            return str(name)
        # 兜底1：全 A 股 代码->名称 映射（最稳）
        try:
            nm = self.get_code_name_map().get(symbol)
            if nm:
                return str(nm)
        except Exception:
            pass
        # 兜底2：全市场快照取名称
        try:
            spot = self.get_market_spot()
            if spot is not None and not spot.empty:
                code_col = next((c for c in spot.columns if c in ("代码", "股票代码")), None)
                name_col = next((c for c in spot.columns if "名称" in c), None)
                if code_col and name_col:
                    row = spot[spot[code_col].astype(str) == symbol]
                    if not row.empty:
                        return str(row.iloc[0][name_col])
        except Exception:
            pass
        return symbol

    # ---------- 财务摘要 ----------
    def get_financials(self, symbol: str) -> pd.DataFrame:
        """财务指标摘要（近几期）。"""
        _require_ak()
        symbol = normalize_symbol(symbol)
        key = f"fin:{symbol}"

        def _fetch():
            try:
                return ak.stock_financial_abstract(symbol=symbol)
            except Exception:
                return pd.DataFrame()

        df = cached_call(key, _fetch, ttl=86400)
        return df if df is not None else pd.DataFrame()

    # ---------- 估值与财务指标（基本面评分卡用）----------
    def get_valuation_metrics(self, symbol: str) -> dict:
        """聚合估值/盈利/成长/财务健康指标，供基本面评分卡使用。

        多源 best-effort：乐咕乐股(PE/PB/PS) + 东财财务摘要(ROE/利润率/负债率/增长)。
        任一源失败不影响其他指标，缺失项以 None 返回。
        """
        symbol = normalize_symbol(symbol)
        key = f"valmetrics:{symbol}"

        def _fetch():
            m: dict = {}
            # --- 估值：从全市场快照取 PE/PB（一次调用、已缓存）---
            try:
                spot = self.get_market_spot()
                if spot is not None and not spot.empty:
                    code_col = next((c for c in spot.columns if c in ("代码", "股票代码")), None)
                    if code_col:
                        row = spot[spot[code_col].astype(str) == symbol]
                        if not row.empty:
                            row = row.iloc[0]
                            pe_col = next((c for c in spot.columns if "市盈率" in c), None)
                            pb_col = next((c for c in spot.columns if "市净率" in c), None)
                            mv_col = next((c for c in spot.columns if "总市值" in c), None)

                            def _num(col):
                                if not col:
                                    return None
                                try:
                                    v = float(pd.to_numeric(row[col], errors="coerce"))
                                    return round(v, 3) if v == v else None  # NaN 检查
                                except Exception:
                                    return None
                            m["pe"] = _num(pe_col)
                            m["pb"] = _num(pb_col)
                            m["total_mv"] = _num(mv_col)
            except Exception:
                pass

            # --- 盈利/成长/健康：东财财务摘要解析 ---
            fa = self.get_financials(symbol)
            if fa is not None and not fa.empty and "指标" in fa.columns:
                date_cols = sorted([c for c in fa.columns if c not in ("选项", "指标") and str(c).isdigit()],
                                   reverse=True)
                if date_cols:
                    latest = date_cols[0]
                    idx = dict(zip(fa["指标"].astype(str), fa[latest]))

                    def pick(*keywords):
                        for name, val in idx.items():
                            if any(kw in name for kw in keywords):
                                try:
                                    return round(float(val), 3)
                                except Exception:
                                    continue
                        return None

                    m["roe"] = pick("净资产收益率", "ROE")
                    m["gross_margin"] = pick("销售毛利率", "毛利率")
                    m["net_margin"] = pick("销售净利率", "净利率")
                    m["debt_ratio"] = pick("资产负债率")
                    m["report_date"] = latest

                    # 同比增长：用上一年同报告期计算（YoY）
                    year_ago = str(int(latest[:4]) - 1) + latest[4:]
                    if year_ago in fa.columns:
                        prev = dict(zip(fa["指标"].astype(str), fa[year_ago]))

                        def yoy(*keywords):
                            for name in idx:
                                if any(kw in name for kw in keywords):
                                    try:
                                        cur, pre = float(idx[name]), float(prev.get(name))
                                        if pre:
                                            return round((cur / pre - 1) * 100, 2)
                                    except Exception:
                                        continue
                            return None
                        m["revenue_growth"] = yoy("营业总收入", "营业收入")
                        m["profit_growth"] = yoy("归母净利润", "净利润")
            return m

        return cached_call(key, _fetch, ttl=86400) or {}


    # ---------- 资金流 ----------
    def get_stock_industry(self, symbol: str) -> str:
        """个股所属行业（Tushare 同花顺行业）。无 Tushare 时返回空串。"""
        if self._ts and self._ts.ready:
            try:
                return self._ts.get_stock_industry(normalize_symbol(symbol)) or ""
            except Exception:
                return ""
        return ""

    # ---------- 业绩前瞻（官方预告 + 财务趋势）----------
    def get_forecast(self, symbol: str) -> list[dict]:
        """官方业绩预告（Tushare）。无 Tushare 时返回空。"""
        if self._ts and self._ts.ready:
            try:
                return self._ts.get_forecast(normalize_symbol(symbol)) or []
            except Exception:
                return []
        return []

    def get_express(self, symbol: str) -> list[dict]:
        """业绩快报（Tushare）。无 Tushare 时返回空。"""
        if self._ts and self._ts.ready:
            try:
                return self._ts.get_express(normalize_symbol(symbol)) or []
            except Exception:
                return []
        return []

    def get_fina_trend(self, symbol: str, periods: int = 6) -> list[dict]:
        """历史季报财务趋势（Tushare）。无 Tushare 时返回空。"""
        if self._ts and self._ts.ready:
            try:
                return self._ts.get_fina_trend(normalize_symbol(symbol), periods=periods) or []
            except Exception:
                return []
        return []

    def get_fund_flow(self, symbol: str) -> dict:
        """个股近期资金流向（主力净流入等）。A-Stock直连 → Tushare → AkShare。"""
        symbol = normalize_symbol(symbol)
        # 优先 A-Stock直连东财资金流
        if self._as:
            try:
                as_out = self._as.get_fund_flow(symbol)
                if as_out:
                    return as_out
            except Exception:
                pass
        # 备选：Tushare
        if self._ts and self._ts.ready:
            try:
                ts_out = self._ts.get_fund_flow(symbol)
                if ts_out:
                    return ts_out
            except Exception:
                pass
        if ak is None:
            return {}
        market = "sh" if symbol.startswith("6") else "sz"
        key = f"fund:{symbol}"

        def _fetch():
            try:
                df = ak.stock_individual_fund_flow(stock=symbol, market=market)
                if df is None or df.empty:
                    return {}
                last = df.iloc[-1].to_dict()
                return {str(k): str(v) for k, v in last.items()}
            except Exception:
                return {}

        return cached_call(key, _fetch, ttl=3600) or {}

    # ---------- 北向资金（沪深股通持股）----------
    def get_north_hold(self, symbol: str) -> dict:
        """个股北向（陆股通）持股。优先 A-Stock直连，失败降级AkShare。"""
        symbol = normalize_symbol(symbol)
        # 优先 A-Stock直连
        if self._as:
            try:
                as_out = self._as.get_north_hold(symbol)
                if as_out:
                    return as_out
            except Exception:
                pass
        if ak is None:
            return {}
        key = f"north:{symbol}"

        def _fetch():
            df = None
            for caller in (
                lambda: ak.stock_hsgt_individual_em(stock=symbol),
                lambda: ak.stock_hsgt_individual_em(symbol=symbol),
            ):
                try:
                    df = caller()
                    if df is not None and not df.empty:
                        break
                except Exception:
                    continue
            if df is None or df.empty:
                return {}
            try:
                date_col = next((c for c in df.columns if "日期" in c), None)
                ratio_col = next((c for c in df.columns if "占总股本" in c or "占流通" in c or "持股比" in c), None)
                mv_col = next((c for c in df.columns if "市值" in c), None)
                if date_col:
                    df = df.sort_values(date_col)
                last = df.iloc[-1]
                ratio = pd.to_numeric(last.get(ratio_col), errors="coerce") if ratio_col else None
                mv = pd.to_numeric(last.get(mv_col), errors="coerce") if mv_col else None
                trend = None
                if ratio_col and len(df) >= 6:
                    prev = pd.to_numeric(df.iloc[-6].get(ratio_col), errors="coerce")
                    if pd.notna(prev) and pd.notna(ratio):
                        trend = round(float(ratio) - float(prev), 3)
                return {
                    "ratio": round(float(ratio), 3) if pd.notna(ratio) else None,
                    "market_cap": float(mv) if mv is not None and pd.notna(mv) else None,
                    "trend_5d": trend,  # 持股占比近5日变化（正=增持）
                }
            except Exception:
                return {}

        return cached_call(key, _fetch, ttl=86400) or {}

    # ---------- 龙虎榜（近一月统计，一次缓存全市场）----------
    def get_lhb_map(self, period: str = "近一月") -> dict:
        """近一月龙虎榜统计。优先 A-Stock直连，失败降级AkShare。"""
        # 优先 A-Stock直连东财龙虎榜
        if self._as:
            try:
                as_out = self._as.get_lhb_map()
                if as_out:
                    return as_out
            except Exception:
                pass
        if ak is None:
            return {}
        key = f"lhb:{period}"

        def _fetch():
            try:
                df = ak.stock_lhb_stock_statistic_em(symbol=period)
            except Exception:
                return None
            if df is None or df.empty:
                return None
            code_col = next((c for c in df.columns if c in ("代码", "股票代码")), None)
            times_col = next((c for c in df.columns if "上榜次数" in c or "次数" in c), None)
            net_col = next((c for c in df.columns if "净买额" in c or "净买入" in c), None)
            last_col = next((c for c in df.columns if "最近上榜" in c or "上榜日" in c), None)
            if not code_col:
                return None
            out = {}
            for _, row in df.iterrows():
                code = normalize_symbol(str(row[code_col]))
                out[code] = {
                    "times": int(pd.to_numeric(row.get(times_col), errors="coerce") or 0) if times_col else 0,
                    "net_buy": float(pd.to_numeric(row.get(net_col), errors="coerce") or 0) if net_col else 0.0,
                    "last_date": str(row.get(last_col, "")) if last_col else "",
                }
            return out or None

        return cached_call(key, _fetch, ttl=3600) or {}

    # ---------- 新闻舆情 ----------
    def get_news(self, symbol: str, limit: int = 8) -> list[dict]:
        """个股相关新闻标题列表。优先 A-Stock直连，失败降级AkShare。"""
        symbol = normalize_symbol(symbol)
        # 优先 A-Stock直连东财新闻
        if self._as:
            try:
                as_out = self._as.get_news(symbol, limit=limit)
                if as_out:
                    return as_out
            except Exception:
                pass
        if ak is None:
            return []
        key = f"news:{symbol}:{limit}"

        def _fetch():
            try:
                df = ak.stock_news_em(symbol=symbol)
                if df is None or df.empty:
                    return []
                cols = {c: c for c in df.columns}
                title_col = next((c for c in df.columns if "标题" in c), None)
                time_col = next((c for c in df.columns if "时间" in c), None)
                out = []
                for _, row in df.head(limit).iterrows():
                    out.append({
                        "title": str(row.get(title_col, "")) if title_col else "",
                        "time": str(row.get(time_col, "")) if time_col else "",
                    })
                return out
            except Exception:
                return []

        return cached_call(key, _fetch, ttl=1800) or []

    # ---------- 全市场快照（用于选股）----------
    @staticmethod
    def _spot_em_direct() -> Optional[pd.DataFrame]:
        """直连东方财富行情列表接口拉取全市场快照。

        相比 akshare 默认封装，这里自定义 header / ut 参数，并用带自动重试退避的
        Session 按 **代码顺序分页** 拉取，更可控、更抗限流，且自带
        **真实量比 / 换手率 / 总市值 / PE / PB** 等完整字段。

        限流期服务器会主动断连，这里：每页自动重试退避；某页彻底失败则保留已得数据；
        只要拿到足够多（≥全市场 80% 或 ≥800 只）即视为成功并返回（缺口可由 K线/基本信息补）。
        全部失败返回 None。
        """
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except Exception:
            return None

        sess = requests.Session()
        retry = Retry(total=1, connect=1, read=1, backoff_factor=0.3,
                      status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
        sess.mount("https://", HTTPAdapter(max_retries=retry))
        sess.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36"),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "*/*",
            "Connection": "close",  # 规避长连接被服务器中途重置
        })
        # 沪深京 A 股过滤串（与东财官方一致）
        fs = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
        # f12代码 f14名称 f2最新价 f3涨跌幅 f6成交额 f8换手率 f9市盈率 f10量比
        # f20总市值 f23市净率 f62主力净流入 f184主力净占比
        fields = "f12,f14,f2,f3,f6,f8,f9,f10,f20,f23,f62,f184"

        def _g(d, k):
            v = d.get(k)
            return None if v in ("-", "", None) else v

        def _page(host, pn, pz):
            params = {
                "pn": pn, "pz": pz, "po": 0, "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2, "invt": 2, "fid": "f12",  # 按代码升序，保证翻页覆盖全市场
                "fs": fs, "fields": fields,
            }
            resp = sess.get(host + "/api/qt/clist/get", params=params, timeout=20)
            js = resp.json()
            data = (js or {}).get("data") or {}
            diff = data.get("diff")
            items = (list(diff.values()) if isinstance(diff, dict) else list(diff)) if diff else []
            return items, int(data.get("total") or 0)

        hosts = ("https://82.push2.eastmoney.com",
                 "https://push2.eastmoney.com",
                 "https://13.push2.eastmoney.com")
        pz = 500
        seen, rows, total = set(), [], 0
        for host in hosts:
            try:
                pn = 1
                while pn <= 30:
                    try:
                        items, tot = _page(host, pn, pz)
                    except Exception:
                        time.sleep(0.5)
                        try:
                            items, tot = _page(host, pn, pz)  # 该页二次尝试
                        except Exception:
                            break  # 该页彻底失败，跳出用已得数据
                    if tot:
                        total = tot
                    if not items:
                        break
                    for d in items:
                        code = str(_g(d, "f12") or "").zfill(6)
                        if code and code not in seen:
                            seen.add(code)
                            rows.append(d)
                    if total and len(rows) >= total:
                        break
                    pn += 1
                    time.sleep(0.5)
                # 拿到足够多即停（不再换 host）；800 太少会导致沪市整段缺失，至少拉到覆盖沪深两市主代码段
                if rows and (not total or len(rows) >= total * 0.85 or len(rows) >= 3500):
                    break
            except Exception:
                continue

        if not rows:
            return None
        recs = [{
            "代码": str(_g(d, "f12") or "").zfill(6),
            "名称": _g(d, "f14"),
            "最新价": _g(d, "f2"),
            "涨跌幅": _g(d, "f3"),
            "成交额": _g(d, "f6"),
            "换手率": _g(d, "f8"),
            "市盈率-动态": _g(d, "f9"),
            "量比": _g(d, "f10"),
            "总市值": _g(d, "f20"),
            "市净率": _g(d, "f23"),
            "主力净流入": _g(d, "f62"),
            "主力净占比": _g(d, "f184"),
        } for d in rows]
        df = pd.DataFrame(recs)
        cov = f"{len(df)}/{total}" if total else str(len(df))
        print(f"[快照] 直连东财成功，获取 {cov} 只（含真实量比/换手/市值）")
        if not df.empty:
            df.attrs["source"] = "东方财富直连实时快照"
            return df
        return None

    def get_market_spot(self) -> pd.DataFrame:
        """A股全市场实时快照（涨跌幅、市值、换手、量比等）。

        数据源优先级：A-Stock直连(腾讯/东财) → Tushare → AkShare → 新浪。
        """
        import datetime as _dt
        _now = _dt.datetime.now()
        _is_trading_hours = _now.weekday() < 5 and _dt.time(9, 30) <= _now.time() <= _dt.time(15, 5)

        key = "spot:all:v3"

        def _fetch():
            # 主源1：A-Stock直连
            if self._as:
                # 盘中：东财直连实时行情（字段全、实时性好）
                if _is_trading_hours:
                    df = self._spot_em_direct()
                    if df is not None and not df.empty:
                        return df
                # 腾讯财经快照（不封IP，盘中盘后都可用）
                try:
                    df_tencent = self._as.get_market_spot()
                    if df_tencent is not None and not df_tencent.empty:
                        return df_tencent
                except Exception:
                    pass

            # 主源2：Tushare 日级快照
            if self._ts and self._ts.ready:
                try:
                    df = self._ts.get_market_spot_all()
                    if df is not None and not df.empty:
                        df.attrs["source"] = "Tushare日级快照"
                        print(f"[快照] Tushare 全市场快照成功，{len(df)} 只")
                        return df
                except Exception:
                    pass

            # 主源3：东财直连（A-Stock/Tushare不可用）
            if not _is_trading_hours:
                df = self._spot_em_direct()
                if df is not None and not df.empty:
                    return df

            # 兜底：AkShare 封装东财
            if ak is not None:
                try:
                    df = _retry(lambda: ak.stock_zh_a_spot_em(), retries=2)
                    if df is not None and not df.empty:
                        df.attrs["source"] = "东方财富(AkShare封装)实时快照兜底"
                        return df
                except Exception:
                    pass

            # 最终兜底：新浪
            if ak is not None:
                print("[快照] 所有源不可用，切换新浪备用源（量比/换手/市值将降级）...")
                try:
                    s = _retry(lambda: ak.stock_zh_a_spot(), retries=2)
                    if s is not None and not s.empty:
                        s = s.copy()
                        code_col = next((c for c in s.columns if c in ("代码", "symbol", "code")), None)
                        if code_col:
                            s[code_col] = s[code_col].astype(str).str.replace(
                                r"^(sh|sz|bj)", "", regex=True)
                        s.attrs["source"] = "新浪实时快照兜底"
                        return s
                except Exception:
                    pass
            return None

        df = cached_call(key, _fetch, ttl=600)
        if df is not None and not df.empty:
            self._last_market_spot_source = df.attrs.get("source", "未知快照源")
            return df
        self._last_market_spot_source = "无可用快照源"
        return pd.DataFrame()

    # ---------- 市场广度（涨跌家数）----------
    def get_market_breadth(self, as_of: str = "") -> dict:
        """全市场涨跌家数、涨停跌停、平均涨幅等。

        as_of: 指定日期 YYYY-MM-DD，为空或当天则用实时快照（A-Stock优先）。
               传历史日期时走 Tushare daily 保证时间一致性。
        """
        import datetime as _dt
        today_str = _dt.date.today().strftime("%Y-%m-%d")
        as_of_clean = (as_of or "").replace("-", "")

        # 仅当明确指定历史日期（非今天）且 Tushare 可用时，才走历史广度
        if as_of and as_of != today_str and self._ts and self._ts.ready:
            breadth = self._ts.get_breadth_on_date(as_of_clean)
            if breadth:
                return breadth

        # 当天/兜底：全市场快照（A-Stock 腾讯优先）
        spot = self.get_market_spot()
        if spot is None or spot.empty:
            return {}
        pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
        if not pct_col:
            return {}
        pct = pd.to_numeric(spot[pct_col], errors="coerce").dropna()
        return {
            "total": int(len(pct)),
            "up": int((pct > 0).sum()),
            "down": int((pct < 0).sum()),
            "flat": int((pct == 0).sum()),
            "limit_up": int((pct >= 9.8).sum()),
            "limit_down": int((pct <= -9.8).sum()),
            "avg_pct": round(float(pct.mean()), 2),
            "median_pct": round(float(pct.median()), 2),
        }

    # ---------- 板块/热点榜 ----------
    def get_hot_sectors(self, limit: int = 10) -> list[dict]:
        """行业板块涨幅榜（热点）。A-Stock直连 → Tushare → AkShare → 自聚合。"""
        # 优先 A-Stock直连（内部已有东财→自聚合兜底）
        if self._as:
            try:
                as_out = self._as.get_hot_sectors(limit=limit)
                if as_out:
                    return as_out
            except Exception:
                pass
        # 备选：Tushare（同花顺板块）
        if self._ts and self._ts.ready:
            try:
                ts_out = self._ts.get_hot_sectors(limit=limit)
                if ts_out:
                    return ts_out
            except Exception:
                pass
        # 备选：AkShare 东财封装
        if ak is not None:
            key = f"sectors:{limit}"
            def _fetch():
                try:
                    df = _retry(lambda: ak.stock_board_industry_name_em(), retries=3)
                    if df is None or df.empty:
                        return None
                    pct_col = next((c for c in df.columns if "涨跌幅" in c), None)
                    name_col = next((c for c in df.columns if "板块名称" in c or "名称" in c), None)
                    if not pct_col or not name_col:
                        return None
                    df = df.sort_values(pct_col, ascending=False).head(limit)
                    out = []
                    lead_col = next((c for c in df.columns if "领涨" in c), None)
                    for _, row in df.iterrows():
                        out.append({
                            "name": str(row[name_col]),
                            "pct": round(float(pd.to_numeric(row[pct_col], errors="coerce") or 0), 2),
                            "leader": str(row[lead_col]) if lead_col else "",
                        })
                    return out
                except Exception:
                    return None
            ak_out = cached_call(key, _fetch, ttl=1800)
            if ak_out:
                return ak_out

        # 最终兜底：全市场快照自聚合（绕过所有东财依赖）
        if self._as:
            try:
                as_out = self._as.get_hot_sectors_from_snapshot(limit=limit)
                if as_out:
                    return as_out
            except Exception:
                pass
        return []

    # ---------- 近一周板块热度榜（识别市场主线）----------
    def get_hot_sectors_week(self, top: int = 12, days: int = 5) -> list[dict]:
        """近一周（默认5个交易日）板块热度榜。

        数据源优先级：A-Stock直连 → Tushare → AkShare → 兜底。
        """
        # 优先 A-Stock直连东财板块热度
        if self._as:
            try:
                as_out = self._as.get_hot_sectors_week(top=top)
                if as_out:
                    return as_out
            except Exception:
                pass
        # 备选：Tushare（同花顺行业+概念）
        if self._ts and self._ts.ready:
            try:
                ts_out = self._ts.get_hot_sectors_week(top=top)
                if ts_out:
                    return ts_out
            except Exception:
                pass
        if ak is None:
            return []
        key = f"hot_sectors_week:{top}:{days}"

        def _name_pct_list(list_caller, hist_caller, board_type: str) -> list[dict]:
            """通用：取板块榜 -> 对前若干板块取近一周历史累计涨幅。"""
            try:
                df = _retry(list_caller, retries=2)
            except Exception:
                df = None
            if df is None or df.empty:
                return []
            pct_col = next((c for c in df.columns if "涨跌幅" in c), None)
            name_col = next((c for c in df.columns if "板块名称" in c or "名称" in c), None)
            lead_col = next((c for c in df.columns if "领涨" in c and "股" in c), None) \
                or next((c for c in df.columns if "领涨" in c), None)
            flow_col = next((c for c in df.columns if "资金" in c or "主力" in c), None)
            if not name_col or not pct_col:
                return []
            # 先按当日涨幅取榜前 N*2，再用近一周累计涨幅精排
            df = df.sort_values(pct_col, ascending=False).head(top * 2)
            out = []
            end = dt.date.today()
            start = end - dt.timedelta(days=max(days * 2 + 6, 16))
            for _, row in df.iterrows():
                nm = str(row[name_col])
                day_pct = round(float(pd.to_numeric(row[pct_col], errors="coerce") or 0), 2)
                week_pct = None
                try:
                    h = hist_caller(nm, start, end)
                    if h is not None and not h.empty:
                        hp = next((c for c in h.columns if "涨跌幅" in c), None)
                        cl = next((c for c in h.columns if c in ("收盘", "收盘价")), None)
                        if cl and len(h) >= 2:
                            c0 = float(pd.to_numeric(h[cl].iloc[-min(days + 1, len(h))], errors="coerce"))
                            c1 = float(pd.to_numeric(h[cl].iloc[-1], errors="coerce"))
                            if c0:
                                week_pct = round((c1 / c0 - 1) * 100, 2)
                        elif hp:
                            week_pct = round(float(pd.to_numeric(h[hp].tail(days), errors="coerce").sum()), 2)
                except Exception:
                    week_pct = None
                out.append({
                    "name": nm,
                    "type": board_type,
                    "day_pct": day_pct,
                    "week_pct": week_pct if week_pct is not None else day_pct,
                    "leader": str(row[lead_col]) if lead_col else "",
                    "fund_flow": str(row[flow_col]) if flow_col else "",
                })
            return out

        def _ind_hist(nm, start, end):
            return ak.stock_board_industry_hist_em(
                symbol=nm, period="日k",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                adjust="")

        def _con_hist(nm, start, end):
            return ak.stock_board_concept_hist_em(
                symbol=nm, period="daily",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
                adjust="")

        def _sina_fallback() -> list[dict]:
            """东财限流时的备用源：新浪行业板块涨跌幅（不同服务器，规避东财限流）。

            新浪源只有当日板块涨跌幅，无近一周历史，故 week_pct 用 day_pct 兜底，
            保证前端主线板块至少显示真实涨跌幅而非横杠。
            """
            try:
                df = _retry(lambda: ak.stock_sector_spot(indicator="新浪行业"), retries=1)
            except Exception:
                df = None
            if df is None or df.empty:
                return []
            # 新浪行业列：label, 板块, 公司家数, 平均价格, 涨跌额, 涨跌幅, ..., 股票名称
            name_col = next((c for c in df.columns if c == "板块"), None) \
                or next((c for c in df.columns if "板块" in c and "名称" not in c), None)
            # 精确匹配“涨跌幅”，避免误取“涨跌额”
            pct_col = next((c for c in df.columns if c == "涨跌幅"), None) \
                or next((c for c in df.columns if "涨跌幅" in c and "个股" not in c), None)
            lead_col = next((c for c in df.columns if "股票名称" in c), None) \
                or next((c for c in df.columns if "领涨" in c), None)
            if not name_col or not pct_col:
                return []
            df = df.copy()
            df[pct_col] = pd.to_numeric(df[pct_col], errors="coerce")
            df = df.sort_values(pct_col, ascending=False).head(top)
            out = []
            for _, row in df.iterrows():
                dp = round(float(pd.to_numeric(row[pct_col], errors="coerce") or 0), 2)
                out.append({
                    "name": str(row[name_col]),
                    "type": "行业",
                    "day_pct": dp,
                    "week_pct": dp,  # 新浪源无近一周历史，用当日涨幅兜底
                    "leader": str(row[lead_col]) if lead_col else "",
                    "fund_flow": "",
                })
            return out

        def _fetch():
            rows = []
            rows += _name_pct_list(lambda: ak.stock_board_industry_name_em(), _ind_hist, "行业")
            try:
                rows += _name_pct_list(lambda: ak.stock_board_concept_name_em(), _con_hist, "概念")
            except Exception:
                pass
            if not rows:
                # 东财主备均限流，切换新浪行业板块备用源
                print("[板块] 东财限流，切换新浪行业板块备用源...")
                rows = _sina_fallback()
            if not rows:
                return None
            # 按近一周累计涨幅排序，取 top
            rows.sort(key=lambda x: (x.get("week_pct") if x.get("week_pct") is not None else -999),
                      reverse=True)
            # 去重（同名概念/行业取累计更高者）
            seen, uniq = set(), []
            for r in rows:
                k = r["name"]
                if k not in seen:
                    seen.add(k)
                    uniq.append(r)
            return uniq[:top]

        return cached_call(key, _fetch, ttl=7200) or []

    # ---------- 板块成分股（动态候选池来源）----------
    def get_board_cons(self, board_name: str, board_type: str = "行业") -> list[dict]:
        """获取某板块的成分股 [{代码,名称,涨跌幅,总市值}]。

        board_type: '行业' | '概念'。
        降级：A-Stock直连 → Tushare → AkShare。
        """
        # 优先 A-Stock直连东财板块
        if self._as:
            try:
                as_out = self._as.get_board_cons(board_name, board_type)
                if as_out:
                    return as_out
            except Exception:
                pass
        # 备选：Tushare（同花顺成分股）
        if self._ts and self._ts.ready:
            try:
                ts_out = self._ts.get_board_cons(board_name, board_type)
                if ts_out:
                    return ts_out
            except Exception:
                pass
        if ak is None:
            return []
        key = f"board_cons:{board_type}:{board_name}"

        def _fetch():
            caller = (ak.stock_board_concept_cons_em if board_type == "概念"
                      else ak.stock_board_industry_cons_em)
            try:
                df = _retry(lambda: caller(symbol=board_name), retries=2)
            except Exception:
                df = None
            if df is None or df.empty:
                return None
            code_col = next((c for c in df.columns if c in ("代码", "股票代码")), None)
            name_col = next((c for c in df.columns if c in ("名称", "股票名称") or "名称" in c), None)
            pct_col = next((c for c in df.columns if "涨跌幅" in c), None)
            mv_col = next((c for c in df.columns if "总市值" in c), None)
            if not code_col:
                return None
            out = []
            for _, row in df.iterrows():
                out.append({
                    "代码": normalize_symbol(str(row[code_col])),
                    "名称": str(row[name_col]) if name_col else "",
                    "涨跌幅": float(pd.to_numeric(row[pct_col], errors="coerce") or 0) if pct_col else None,
                    "总市值": float(pd.to_numeric(row[mv_col], errors="coerce") or 0) if mv_col else None,
                })
            return out or None

        return cached_call(key, _fetch, ttl=21600) or []

    def get_index_spot(self) -> list[dict]:
        """主要指数实时行情（上证/深成/创业板/沪深300/科创50）。

        数据源优先级：① 腾讯（不封IP） ② 东财 ③ 新浪。
        """
        # 优先腾讯（不封IP，速度快）
        if self._as:
            try:
                as_out = self._as.get_index_spot()
                if as_out:
                    return as_out
            except Exception:
                pass
        if ak is None:
            return []
        wanted = {"上证指数", "深证成指", "创业板指", "沪深300", "科创50"}
        # 各指数点位合理下限（低于此值判为脏数据/错列）
        _floor = {"上证指数": 2500, "深证成指": 7000, "创业板指": 1500,
                  "沪深300": 3000, "科创50": 800}

        def _parse(df) -> list[dict]:
            if df is None or df.empty:
                return []
            name_col = next((c for c in df.columns if c in ("名称", "指数名称")), None) \
                or next((c for c in df.columns if "名称" in c), None)
            pct_col = next((c for c in df.columns if "涨跌幅" in c), None)
            # 价列优先精确匹配“最新价”，再退“最新/收盘”，避免选到“年初至今”等百分比列
            price_col = next((c for c in df.columns if c == "最新价"), None) \
                or next((c for c in df.columns if c in ("最新", "收盘", "收盘价")), None)
            if not name_col:
                return []
            out, seen = [], set()
            for _, row in df.iterrows():
                nm = str(row[name_col]).strip()
                if nm not in wanted or nm in seen:  # 去重（如沪深300在新浪源有两条）
                    continue
                price = float(pd.to_numeric(row[price_col], errors="coerce") or 0) if price_col else None
                # 点位合理性校验：明显偏低视为脏数据，丢弃该源
                if price is not None and price < _floor.get(nm, 0):
                    return []
                seen.add(nm)
                out.append({
                    "name": nm,
                    "price": price,
                    "pct": round(float(pd.to_numeric(row[pct_col], errors="coerce") or 0), 2) if pct_col else None,
                })
            return out

        def _fetch():
            # 主源：东财（两种 symbol 分类各试一次）
            def _em_default():
                return ak.stock_zh_index_spot_em()

            def _em_member():
                return ak.stock_zh_index_spot_em(symbol="指数成份")

            for caller in (_em_member, _em_default):
                try:
                    df = _retry(caller, retries=2)
                    res = _parse(df)
                    if res:
                        return res
                except Exception:
                    pass
            # 备用源：新浪（点位稳定可靠）
            try:
                df = _retry(lambda: ak.stock_zh_index_spot_sina(), retries=2)
                res = _parse(df)
                if res:
                    print("[指数] 东财限流，已切换新浪源获取指数点位")
                    return res
            except Exception:
                pass
            return []

        return cached_call("index_spot", _fetch, ttl=600) or []

    # ---------- 大盘指数 ----------
    def get_index_kline(self, index_code: str = "sh000001", days: int = 120) -> pd.DataFrame:
        """大盘指数日线（默认上证指数）。优先腾讯，失败降级AkShare。"""
        key = f"index:{index_code}:{days}"

        def _fetch():
            # 优先腾讯（不封IP）
            if self._as:
                try:
                    df_as = self._as.get_index_kline(index_code, days=days)
                    if df_as is not None and not df_as.empty:
                        return df_as
                except Exception:
                    pass
            # 回退AkShare
            if ak is not None:
                try:
                    df = ak.stock_zh_index_daily(symbol=index_code)
                    return df
                except Exception:
                    return pd.DataFrame()
            return pd.DataFrame()

        df = cached_call(key, _fetch, ttl=3600)
        if df is None or df.empty:
            return pd.DataFrame()
        if "date" not in df.columns:
            return pd.DataFrame()
        df = df.rename(columns={c: c for c in df.columns})
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        return df.tail(days).reset_index(drop=True)

    # ---------- 外盘指数（隔夜外围走势）----------
    def get_global_indices(self) -> list[dict]:
        """主要外盘指数行情（纳指/标普/道指/费城半导体/恒生/恒生科技/日经/KOSPI）。

        用于复盘「外盘走势」段：隔夜欧美 + 早盘亚太对A股的情绪传导。
        best-effort：东财外盘接口限流时返回空，由上层降级处理。
        """
        _require_ak()

        def _fetch():
            # 关注列表：name -> 别名（东财外盘名称可能不同）
            wanted = {
                "纳斯达克": "🇺🇸 纳斯达克", "标普500": "🇺🇸 标普500", "道琼斯": "🇺🇸 道琼斯",
                "费城半导体": "🇺🇸 费城半导体", "恒生指数": "🇭🇰 恒生指数",
                "恒生科技指数": "🇭🇰 恒生科技", "日经225": "🇯🇵 日经225",
                "韩国KOSPI": "🇰🇷 KOSPI",
            }
            out: list[dict] = []
            try:
                df = _retry(lambda: ak.index_global_spot_em(), retries=2)
            except Exception:
                df = None
            if df is not None and not df.empty:
                name_col = next((c for c in df.columns if "名称" in c), None)
                pct_col = next((c for c in df.columns if "涨跌幅" in c), None)
                price_col = next((c for c in df.columns if c in ("最新价", "最新", "收盘价")), None)
                if name_col and pct_col:
                    for _, row in df.iterrows():
                        nm = str(row[name_col]).strip()
                        for key_nm, disp in wanted.items():
                            if key_nm in nm:
                                out.append({
                                    "name": disp,
                                    "price": float(pd.to_numeric(row[price_col], errors="coerce") or 0) if price_col else None,
                                    "pct": round(float(pd.to_numeric(row[pct_col], errors="coerce") or 0), 2),
                                })
                                break
            # 去重（保留首个）
            seen, uniq = set(), []
            for x in out:
                if x["name"] not in seen:
                    seen.add(x["name"])
                    uniq.append(x)
            return uniq or None

        return cached_call("global_indices", _fetch, ttl=1800) or []

    # ---------- 涨停跌停近一月历史（画趋势图）----------
    def get_limit_history(self, days: int = 22) -> list[dict]:
        """近一个月每个交易日的涨停/跌停家数，用于画趋势线图。

        返回 [{date, limit_up, limit_down}]（按日期升序）。
        优先用东财「涨停股池/跌停股池」按日统计；接口不支持历史日则尽力取近 N 个交易日。
        best-effort：限流/接口缺失时返回空列表。
        """
        _require_ak()
        key = f"limit_history:{days}"

        def _fetch():
            # 取最近 days*2 自然日内的交易日
            end = dt.date.today()
            dates: list[str] = []
            try:
                cal = _retry(lambda: ak.tool_trade_date_hist_sina(), retries=2)
                if cal is not None and not cal.empty:
                    col = cal.columns[0]
                    ser = pd.to_datetime(cal[col]).dt.date
                    ser = ser[ser <= end].sort_values()
                    dates = [d.strftime("%Y%m%d") for d in ser.tail(days)]
            except Exception:
                dates = []
            if not dates:
                # 退化：用日历近 days 个工作日（粗略）
                d = end
                while len(dates) < days:
                    if d.weekday() < 5:
                        dates.append(d.strftime("%Y%m%d"))
                    d -= dt.timedelta(days=1)
                dates = sorted(dates)

            out: list[dict] = []
            for ds in dates:
                lu = ld = None
                try:
                    up_df = ak.stock_zt_pool_em(date=ds)
                    lu = int(len(up_df)) if up_df is not None else None
                except Exception:
                    lu = None
                try:
                    dt_df = ak.stock_zt_pool_dtgc_em(date=ds)
                    ld = int(len(dt_df)) if dt_df is not None else None
                except Exception:
                    ld = None
                if lu is None and ld is None:
                    continue
                out.append({
                    "date": f"{ds[4:6]}.{ds[6:8]}",
                    "limit_up": lu if lu is not None else 0,
                    "limit_down": ld if ld is not None else 0,
                })
            return out or None

        return cached_call(key, _fetch, ttl=7200) or []

    # ---------- 跌停/领跌个股（复盘跌停分析）----------
    def get_limit_down_stocks(self, n: int = 12) -> list[dict]:
        """当日跌停/领跌个股 [{code,name,pct,turnover}]（按跌幅降序）。"""
        spot = self.get_market_spot()
        if spot is None or spot.empty:
            return []
        code_col = next((c for c in spot.columns if c in ("代码", "symbol", "code")), None)
        name_col = next((c for c in spot.columns if c in ("名称", "name")), None)
        pct_col = next((c for c in spot.columns if "涨跌幅" in c), None)
        turn_col = next((c for c in spot.columns if "换手" in c), None)
        if not (code_col and pct_col):
            return []
        df = spot.copy()
        df["_pct"] = pd.to_numeric(df[pct_col], errors="coerce")
        downs = df[df["_pct"] <= -9.5].sort_values("_pct").head(n)
        out = []
        for _, row in downs.iterrows():
            out.append({
                "code": str(row[code_col]),
                "name": str(row[name_col]) if name_col else "",
                "pct": round(float(row["_pct"]), 2),
                "turnover": round(float(pd.to_numeric(row.get(turn_col), errors="coerce") or 0), 2) if turn_col else None,
            })
        return out


@lru_cache(maxsize=1)
def get_fetcher() -> DataFetcher:
    return DataFetcher()
