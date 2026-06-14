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
                df = ts.pro_bar(
                    ts_code=ts_code, adj=adj,
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


@lru_cache(maxsize=1)
def get_tushare() -> TushareSource:
    return TushareSource()
