"""Tushare 增强数据源（可选）。

仅当配置了 TUSHARE_TOKEN 时启用，否则相关方法返回空，由上层降级到 AkShare。
Tushare 优点：数据规范、复权准确、财务数据全。
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import pandas as pd

from core.config import settings
from data.cache import cached_call


def _to_ts_code(symbol: str) -> str:
    """6位代码 -> Tushare 代码：600519 -> 600519.SH。"""
    s = symbol.strip()
    if "." in s:
        return s
    suffix = "SH" if s.startswith(("6", "5", "9")) else "SZ"
    if s.startswith(("4", "8")):
        suffix = "BJ"  # 北交所
    return f"{s}.{suffix}"


class TushareSource:
    def __init__(self):
        self._pro = None
        self._ready = False
        if settings.tushare_token:
            try:
                import tushare as ts
                ts.set_token(settings.tushare_token)
                self._pro = ts.pro_api()
                # 自定义 API 地址（第三方代理）。tushare SDK 未暴露官方设置入口，
                # 通过私有属性 _DataApi__http_url 覆盖请求地址。
                if settings.tushare_api_url:
                    try:
                        self._pro._DataApi__http_url = settings.tushare_api_url
                    except Exception:
                        pass
                self._ready = True
            except Exception:
                self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def get_kline(self, symbol: str, days: int = 250, adjust: str = "qfq") -> pd.DataFrame:
        """日K线，返回标准英文列；失败返回空 DataFrame。"""
        if not self._ready:
            return pd.DataFrame()
        ts_code = _to_ts_code(symbol)
        end = dt.date.today()
        start = end - dt.timedelta(days=int(days * 1.7) + 30)
        key = f"ts_kline:{ts_code}:{adjust}:{start}:{end}"

        def _fetch():
            try:
                import tushare as ts
                adj = {"qfq": "qfq", "hfq": "hfq", "": None}.get(adjust, "qfq")
                # 必须传 api=self._pro，否则 pro_bar 走官方默认地址，自定义代理 URL 失效
                df = ts.pro_bar(
                    ts_code=ts_code, api=self._pro, adj=adj,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    freq="D",
                )
                return df
            except Exception:
                return None

        df = cached_call(key, _fetch, ttl=3600)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns={
            "trade_date": "date", "open": "open", "close": "close",
            "high": "high", "low": "low", "vol": "volume", "amount": "amount",
            "pct_chg": "pct_change",
        })
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df = df.sort_values("date").reset_index(drop=True)
        keep = [c for c in ["date", "open", "close", "high", "low", "volume",
                            "amount", "pct_change"] if c in df.columns]
        return df[keep].tail(days).reset_index(drop=True)

    def get_market_spot_all(self) -> pd.DataFrame:
        """全市场当日快照：真实量比/换手率/市值/PE/PB（一次调用，规避逐股限流）。

        合并 daily_basic（量比/换手/市值/PE/PB）+ daily（收盘/涨跌幅）+ stock_basic（名称）。
        列名对齐 AkShare 快照，供选股引擎直接复用。失败返回空 DataFrame。
        """
        if not self._ready:
            return pd.DataFrame()

        def _fetch():
            try:
                # 最近交易日
                cal = self._pro.trade_cal(
                    exchange="",
                    start_date=(dt.date.today() - dt.timedelta(days=15)).strftime("%Y%m%d"),
                    end_date=dt.date.today().strftime("%Y%m%d"),
                )
                opens = sorted(cal[cal["is_open"] == 1]["cal_date"].tolist())
                trade_date = opens[-1] if opens else dt.date.today().strftime("%Y%m%d")

                db = self._pro.daily_basic(
                    trade_date=trade_date,
                    fields="ts_code,turnover_rate,volume_ratio,pe_ttm,pb,total_mv",
                )
                dl = self._pro.daily(trade_date=trade_date, fields="ts_code,close,pct_chg")
                if db is None or db.empty:
                    return None
                m = db.merge(dl, on="ts_code", how="left") if dl is not None else db
                try:
                    nb = self._pro.stock_basic(exchange="", list_status="L",
                                               fields="ts_code,name")
                    m = m.merge(nb, on="ts_code", how="left")
                except Exception:
                    m["name"] = None

                out = pd.DataFrame({
                    "代码": m["ts_code"].astype(str).str.split(".").str[0],
                    "名称": m.get("name"),
                    "最新价": pd.to_numeric(m.get("close"), errors="coerce"),
                    "涨跌幅": pd.to_numeric(m.get("pct_chg"), errors="coerce"),
                    "换手率": pd.to_numeric(m.get("turnover_rate"), errors="coerce"),
                    "量比": pd.to_numeric(m.get("volume_ratio"), errors="coerce"),
                    "市盈率-动态": pd.to_numeric(m.get("pe_ttm"), errors="coerce"),
                    "市净率": pd.to_numeric(m.get("pb"), errors="coerce"),
                    # total_mv 单位为万元 -> 元（选股引擎按元/1e8 转亿）
                    "总市值": pd.to_numeric(m.get("total_mv"), errors="coerce") * 1e4,
                })
                return out if not out.empty else None
            except Exception:
                return None

        df = cached_call("ts_spot_all", _fetch, ttl=600)
        return df if df is not None else pd.DataFrame()

    def get_daily_basic(self, symbol: str) -> dict:
        """估值指标：市盈率、市净率、总市值等。失败返回 {}。"""
        if not self._ready:
            return {}
        ts_code = _to_ts_code(symbol)
        key = f"ts_basic:{ts_code}"

        def _fetch():
            try:
                df = self._pro.daily_basic(
                    ts_code=ts_code, fields="trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate"
                )
                if df is None or df.empty:
                    return {}
                return df.iloc[0].to_dict()
            except Exception:
                return {}

        return cached_call(key, _fetch, ttl=86400) or {}

    def get_stock_industry(self, symbol: str) -> str:
        """个股所属行业（同花顺行业，来自 stock_basic）。全市场一次性缓存。失败返回 ''。"""
        if not self._ready:
            return ""

        def _fetch():
            try:
                df = self._pro.stock_basic(exchange="", list_status="L",
                                           fields="ts_code,industry")
                if df is None or df.empty:
                    return None
                m = {}
                for _, r in df.iterrows():
                    code = str(r.get("ts_code", "")).split(".")[0]
                    ind = r.get("industry")
                    if code and ind is not None and str(ind) != "nan":
                        m[code] = str(ind)
                return m or None
            except Exception:
                return None

        m = cached_call("ts_stock_industry_map", _fetch, ttl=86400) or {}
        return m.get(symbol.split(".")[0], "")

    # ------------------------------------------------------------------
    # 业绩前瞻（官方业绩预告 + 历史季报趋势）
    # ------------------------------------------------------------------
    def get_forecast(self, symbol: str) -> list:
        """官方业绩预告（最新若干期）。net_profit 单位万元 -> 元。失败返回 []。"""
        if not self._ready:
            return []
        ts_code = _to_ts_code(symbol)

        def _fetch():
            try:
                df = self._pro.forecast(ts_code=ts_code)
                if df is None or df.empty:
                    return None
                df = df.sort_values("end_date", ascending=False).head(4)
                out = []
                for _, r in df.iterrows():
                    def _f(k):
                        v = pd.to_numeric(r.get(k), errors="coerce")
                        return None if pd.isna(v) else float(v)
                    npmin = _f("net_profit_min")
                    npmax = _f("net_profit_max")
                    out.append({
                        "end_date": str(r.get("end_date", "")),
                        "ann_date": str(r.get("ann_date", "")),
                        "type": str(r.get("type", "")),
                        "p_change_min": _f("p_change_min"),
                        "p_change_max": _f("p_change_max"),
                        "net_profit_min": npmin * 1e4 if npmin is not None else None,
                        "net_profit_max": npmax * 1e4 if npmax is not None else None,
                        "summary": str(r.get("summary", "") or ""),
                        "change_reason": str(r.get("change_reason", "") or ""),
                    })
                return out or None
            except Exception:
                return None

        return cached_call(f"ts_forecast:{ts_code}", _fetch, ttl=43200) or []

    def get_express(self, symbol: str) -> list:
        """业绩快报（已结账未正式披露的快报）。失败返回 []。"""
        if not self._ready:
            return []
        ts_code = _to_ts_code(symbol)

        def _fetch():
            try:
                df = self._pro.express(ts_code=ts_code)
                if df is None or df.empty:
                    return None
                df = df.sort_values("end_date", ascending=False).head(2)
                out = []
                for _, r in df.iterrows():
                    def _f(k):
                        v = pd.to_numeric(r.get(k), errors="coerce")
                        return None if pd.isna(v) else float(v)
                    out.append({
                        "end_date": str(r.get("end_date", "")),
                        "revenue": _f("revenue"),
                        "n_income": _f("n_income"),
                        "yoy_net_profit": _f("yoy_net_profit"),
                        "or_last_year": _f("or_last_year"),
                    })
                return out or None
            except Exception:
                return None

        return cached_call(f"ts_express:{ts_code}", _fetch, ttl=43200) or []

    def get_fina_trend(self, symbol: str, periods: int = 6) -> list:
        """历史季报财务趋势（营收/净利同比、ROE、利润率），用于趋势外推。"""
        if not self._ready:
            return []
        ts_code = _to_ts_code(symbol)

        def _fetch():
            try:
                df = self._pro.fina_indicator(ts_code=ts_code)
                if df is None or df.empty:
                    return None
                df = df.sort_values("end_date", ascending=False).head(periods)
                cols = {"end_date": "end_date", "eps": "eps", "roe": "roe",
                        "netprofit_yoy": "netprofit_yoy", "or_yoy": "or_yoy",
                        "grossprofit_margin": "gross_margin",
                        "netprofit_margin": "net_margin"}
                out = []
                for _, r in df.iterrows():
                    rec = {}
                    for src, dst in cols.items():
                        if src == "end_date":
                            rec[dst] = str(r.get(src, ""))
                        else:
                            v = pd.to_numeric(r.get(src), errors="coerce")
                            rec[dst] = None if pd.isna(v) else round(float(v), 2)
                    out.append(rec)
                return out or None
            except Exception:
                return None

        return cached_call(f"ts_fina_trend:{ts_code}:{periods}", _fetch, ttl=43200) or []

    def get_fund_flow(self, symbol: str) -> dict:
        """个股资金流向（主力净流入等），字段对齐东财口径（单位：元）。失败返回 {}。

        优先用 moneyflow_dc（东财口径，直接有主力净额）；缺失则用 moneyflow
        （主力净流入 = 大单+特大单 买卖净额）。Tushare 金额单位为万元，统一×1e4 转元。
        """
        if not self._ready:
            return {}
        ts_code = _to_ts_code(symbol)
        key = f"ts_fund:{ts_code}"

        def _fetch():
            # 1) 优先东财口径 moneyflow_dc（net_amount 即主力净额，万元）
            try:
                df = self._pro.moneyflow_dc(ts_code=ts_code)
                if df is not None and not df.empty:
                    df = df.sort_values("trade_date")
                    last = df.iloc[-1]
                    elg = float(pd.to_numeric(last.get("buy_elg_amount"), errors="coerce") or 0)
                    lg = float(pd.to_numeric(last.get("buy_lg_amount"), errors="coerce") or 0)
                    net = float(pd.to_numeric(last.get("net_amount"), errors="coerce") or 0)
                    return {
                        "日期": str(last.get("trade_date", "")),
                        "主力净流入-净额": net * 1e4,
                        "大单净流入-净额": lg * 1e4,
                        "超大单净流入-净额": elg * 1e4,
                    }
            except Exception:
                pass
            # 2) 备选 moneyflow（主力 = 大单+特大单 净额，万元）
            try:
                df = self._pro.moneyflow(ts_code=ts_code)
                if df is None or df.empty:
                    return {}
                df = df.sort_values("trade_date")
                last = df.iloc[-1]

                def _n(k):
                    return float(pd.to_numeric(last.get(k), errors="coerce") or 0)
                lg_net = _n("buy_lg_amount") - _n("sell_lg_amount")
                elg_net = _n("buy_elg_amount") - _n("sell_elg_amount")
                main_net = lg_net + elg_net
                return {
                    "日期": str(last.get("trade_date", "")),
                    "主力净流入-净额": main_net * 1e4,
                    "大单净流入-净额": lg_net * 1e4,
                    "超大单净流入-净额": elg_net * 1e4,
                }
            except Exception:
                return {}

        return cached_call(key, _fetch, ttl=3600) or {}

    # ------------------------------------------------------------------
    # 板块数据（同花顺体系：规避东财网页接口限流导致的环境差异）
    # ------------------------------------------------------------------
    def _ths_index_list(self) -> pd.DataFrame:
        """同花顺板块列表（通用行业 881xxx + 概念 885xxx），强缓存。

        关键：同花顺有多套并行板块体系，按 ts_code 前缀区分——
        - 881xxx：同花顺**通用行业**板块（半导体/消费电子等，成分股全、贴近市场主线）
        - 885xxx：同花顺**概念**板块（CPO/第三代半导体/苹果概念等热门主线）
        - 700/861/884/877/883 等：申万/中信风格细分指数（成分窄、偏门，不采用）
        过去误用 700xxx 等细分指数，导致候选池选不到士兰微/深南电路等主流强势票。
        新增 category 列：'行业' / '概念'。失败返回空 DataFrame。
        """
        if not self._ready:
            return pd.DataFrame()

        def _fetch():
            try:
                df = self._pro.ths_index(fields="ts_code,name,type,count")
                if df is None or df.empty:
                    return None
                code = df["ts_code"].astype(str)
                df = df.copy()
                df["category"] = None
                df.loc[code.str.startswith("881"), "category"] = "行业"
                df.loc[code.str.startswith("885"), "category"] = "概念"
                df = df[df["category"].notna()].copy()
                return df if not df.empty else None
            except Exception:
                return None

        df = cached_call("ts_ths_index_list", _fetch, ttl=86400)
        return df if df is not None else pd.DataFrame()

    def _ths_recent_pct(self, ts_code: str, days: int = 5) -> tuple:
        """某板块近 days 个交易日累计涨幅 + 当日涨幅。返回 (week_pct, day_pct)。"""
        def _fetch():
            try:
                df = self._pro.ths_daily(
                    ts_code=ts_code, fields="ts_code,trade_date,close,pct_change")
                if df is None or df.empty:
                    return None
                df = df.sort_values("trade_date")
                return df.tail(days + 2).to_dict("list")
            except Exception:
                return None

        data = cached_call(f"ts_ths_daily:{ts_code}:{days}", _fetch, ttl=1800)
        if not data or not data.get("close"):
            return None, None
        closes = [c for c in data["close"] if c is not None]
        pcts = [p for p in data.get("pct_change", []) if p is not None]
        day_pct = round(float(pcts[-1]), 2) if pcts else None
        week_pct = None
        if len(closes) >= 2:
            c0 = float(closes[-min(days + 1, len(closes))])
            c1 = float(closes[-1])
            if c0:
                week_pct = round((c1 / c0 - 1) * 100, 2)
        return week_pct, (day_pct if day_pct is not None else week_pct)

    def get_hot_sectors(self, limit: int = 10) -> list:
        """当日行业板块涨幅榜（热点）。失败返回空列表。"""
        idx = self._ths_index_list()
        if idx.empty:
            return []
        # 通用行业板块按当日涨幅排序
        ind = idx[idx["category"] == "行业"]

        def _fetch():
            rows = []
            for _, r in ind.head(40).iterrows():  # 控制请求量
                wk, dp = self._ths_recent_pct(str(r["ts_code"]), days=1)
                if dp is None:
                    continue
                rows.append({"name": str(r["name"]), "ts_code": str(r["ts_code"]),
                             "type": "行业", "pct": dp})
            if not rows:
                return None
            rows.sort(key=lambda x: x["pct"], reverse=True)
            return rows[:limit]

        out = cached_call(f"ts_hot_sectors:{limit}", _fetch, ttl=1800)
        return [{"name": x["name"], "pct": x["pct"], "leader": ""} for x in out] if out else []

    def get_hot_sectors_week(self, top: int = 12) -> list:
        """近一周板块热度榜（行业+概念按累计涨幅排序，识别市场主线）。"""
        idx = self._ths_index_list()
        if idx.empty:
            return []

        def _fetch():
            rows = []
            # 通用行业 + 概念都纳入主线候选（半导体/AI/CPO 多为概念板块）
            pool = pd.concat([idx[idx["category"] == "行业"],
                              idx[idx["category"] == "概念"]])
            for _, r in pool.iterrows():
                wk, dp = self._ths_recent_pct(str(r["ts_code"]), days=5)
                if wk is None:
                    continue
                rows.append({
                    "name": str(r["name"]),
                    "ts_code": str(r["ts_code"]),
                    "type": str(r["category"]),
                    "week_pct": wk,
                    "day_pct": dp if dp is not None else wk,
                    "leader": "",
                    "fund_flow": "",
                })
            if not rows:
                return None
            rows.sort(key=lambda x: (x["week_pct"] if x["week_pct"] is not None else -999),
                      reverse=True)
            # 去重同名
            seen, uniq = set(), []
            for x in rows:
                if x["name"] not in seen:
                    seen.add(x["name"])
                    uniq.append(x)
            return uniq[:top]

        out = cached_call(f"ts_hot_sectors_week:{top}", _fetch, ttl=3600)
        return out or []

    def get_board_cons(self, board_name: str, board_type: str = "行业") -> list:
        """板块成分股 [{代码,名称,涨跌幅,总市值}]。失败返回空列表。"""
        idx = self._ths_index_list()
        if idx.empty:
            return []
        match = idx[(idx["name"] == board_name) & (idx["category"] == board_type)]
        if match.empty:  # 退而匹配任意类型同名板块
            match = idx[idx["name"] == board_name]
        if match.empty:
            return []
        ts_code = str(match.iloc[0]["ts_code"])
        key = f"ts_board_cons:{ts_code}"

        def _fetch():
            try:
                m = self._pro.ths_member(ts_code=ts_code)
                if m is None or m.empty:
                    return None
                out = []
                for _, r in m.iterrows():
                    code = str(r.get("con_code", "")).split(".")[0]
                    if not code:
                        continue
                    out.append({
                        "代码": code,
                        "名称": str(r.get("con_name", "")),
                        "涨跌幅": None,   # 成分股逐日涨幅由引擎用 K线计算，无需逐股请求
                        "总市值": None,
                    })
                return out or None
            except Exception:
                return None

        out = cached_call(key, _fetch, ttl=21600)
        return out or []


@lru_cache(maxsize=1)
def get_tushare() -> TushareSource:
    return TushareSource()
