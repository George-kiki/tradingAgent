"""每日荐股系统的本地 SQLite 持久化层。

设计 5 张表：
- recommendations       每日推荐明细（含选股因子快照，供后续反思）
- recommendation_results 推荐结果回评（次日收益、胜负判定）
- daily_winrate         每个推荐批次（按 base_date）的胜率汇总
- reflections           反思记录（多维诊断 + 结论 + 调整）
- strategy_weights      动态策略权重与约束（反思迭代的载体，按生效日存储）

所有写操作每次开新连接（check_same_thread=False），简单且线程安全，
适配 FastAPI 多线程场景。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Any, Optional

from core.config import settings


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RecommendDB:
    """荐股系统数据访问对象。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or settings.recommend_db
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(
                """
                -- 每日推荐明细（支持 mode 字段区分 daily/tail）
                CREATE TABLE IF NOT EXISTS recommendations (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    base_date    TEXT NOT NULL,   -- 选股所依据的数据日期（= 入场参考日）
                    rec_date     TEXT,            -- 建议操作日（次一交易日，结算时回填）
                    symbol       TEXT NOT NULL,
                    name         TEXT,
                    rank         INTEGER,
                    score        REAL,            -- 加权综合评分
                    entry_price  REAL,            -- 入场参考价（base_date 收盘）
                    industry     TEXT,
                    tags         TEXT,            -- JSON 数组
                    reason       TEXT,            -- 选中详细原因
                    factors      TEXT,            -- JSON：选股因子快照
                    kline_mini   TEXT,            -- JSON：近60日K线
                    ai_score     REAL,            -- LLM后置复核评分
                    ai_flags     TEXT,            -- JSON：LLM复核标签
                    ai_reason    TEXT,            -- LLM复核理由
                    mode         TEXT DEFAULT 'daily',  -- 推荐模式：daily(每日荐股) / tail(尾盘荐股)
                    created_at   TEXT,
                    UNIQUE(base_date, symbol, mode)
                );

                -- 推荐结果回评
                CREATE TABLE IF NOT EXISTS recommendation_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    rec_id      INTEGER NOT NULL,
                    base_date   TEXT NOT NULL,
                    eval_date   TEXT,
                    symbol      TEXT NOT NULL,
                    entry_price REAL,
                    eval_price  REAL,
                    next_pct    REAL,
                    is_win      INTEGER,
                    note        TEXT,
                    mode        TEXT DEFAULT 'daily',  -- 与推荐批次 mode 对应
                    created_at  TEXT,
                    UNIQUE(base_date, symbol, mode),
                    FOREIGN KEY(rec_id) REFERENCES recommendations(id)
                );

                -- 批次胜率汇总
                CREATE TABLE IF NOT EXISTS daily_winrate (
                    base_date   TEXT PRIMARY KEY,
                    total       INTEGER,
                    wins        INTEGER,
                    win_rate    REAL,
                    avg_return  REAL,
                    best_symbol TEXT,
                    worst_symbol TEXT,
                    created_at  TEXT
                );

                -- 反思记录
                CREATE TABLE IF NOT EXISTS reflections (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    reflect_date  TEXT,
                    based_on_date TEXT,
                    prev_win_rate REAL,
                    threshold     REAL,
                    dimensions    TEXT,
                    conclusion    TEXT,
                    adjustments   TEXT,
                    llm_used      INTEGER,
                    created_at    TEXT
                );

                -- 动态策略权重与约束
                CREATE TABLE IF NOT EXISTS strategy_weights (
                    effective_date TEXT PRIMARY KEY,
                    weights        TEXT,
                    filters        TEXT,
                    source         TEXT,
                    note           TEXT,
                    created_at     TEXT
                );
            """
            )
            # 迁移：为旧 DB 补上 mode 字段（旧数据自动归为 'daily'）
            for tbl in ("recommendations", "recommendation_results"):
                try:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN mode TEXT DEFAULT 'daily'")
                except Exception:
                    pass
            # 迁移：为旧 DB 补上 LLM 复核字段
            for col_decl in ("ai_score REAL", "ai_flags TEXT", "ai_reason TEXT"):
                try:
                    c.execute(f"ALTER TABLE recommendations ADD COLUMN {col_decl}")
                except Exception:
                    pass
            # 迁移：胜率汇总按 mode 隔离。旧表以 base_date 为主键，会导致 daily/tail
            # 同日互相覆盖；这里重建为 (base_date, mode) 复合主键。
            info = c.execute("PRAGMA table_info(daily_winrate)").fetchall()
            cols = [r["name"] for r in info]
            pk_cols = [r["name"] for r in info if r["pk"]]
            if "mode" not in cols or pk_cols == ["base_date"]:
                c.execute("ALTER TABLE daily_winrate RENAME TO daily_winrate_old")
                c.execute(
                    """
                    CREATE TABLE daily_winrate (
                        base_date   TEXT NOT NULL,
                        mode        TEXT NOT NULL DEFAULT 'daily',
                        total       INTEGER,
                        wins        INTEGER,
                        win_rate    REAL,
                        avg_return  REAL,
                        best_symbol TEXT,
                        worst_symbol TEXT,
                        created_at  TEXT,
                        PRIMARY KEY(base_date, mode)
                    )
                    """
                )
                old_cols = [r["name"] for r in c.execute("PRAGMA table_info(daily_winrate_old)").fetchall()]
                mode_expr = "mode" if "mode" in old_cols else "'daily'"
                c.execute(
                    f"""
                    INSERT OR REPLACE INTO daily_winrate
                    (base_date, mode, total, wins, win_rate, avg_return, best_symbol, worst_symbol, created_at)
                    SELECT base_date, {mode_expr}, total, wins, win_rate, avg_return,
                           best_symbol, worst_symbol, created_at
                    FROM daily_winrate_old
                    """
                )
                c.execute("DROP TABLE daily_winrate_old")

    # ------------------------------------------------------------------
    # 推荐明细
    # ------------------------------------------------------------------
    def save_recommendations(self, base_date: str, picks: list[dict],
                             mode: str = "daily") -> None:
        symbols = [p["symbol"] for p in picks]
        with self._conn() as c:
            if symbols:
                ph = ",".join("?" * len(symbols))
                c.execute(
                    f"DELETE FROM recommendations WHERE base_date=? AND mode=? AND symbol NOT IN ({ph})",
                    (base_date, mode, *symbols),
                )
                c.execute(
                    f"DELETE FROM recommendation_results WHERE base_date=? AND mode=? AND symbol NOT IN ({ph})",
                    (base_date, mode, *symbols),
                )
            for i, p in enumerate(picks, 1):
                c.execute(
                    """INSERT OR REPLACE INTO recommendations
                    (id, base_date, rec_date, symbol, name, rank, score, entry_price,
                     industry, tags, reason, factors, kline_mini,
                     ai_score, ai_flags, ai_reason, mode, created_at)
                    VALUES (
                        (SELECT id FROM recommendations WHERE base_date=? AND symbol=? AND mode=?),
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        base_date, p["symbol"], mode,
                        base_date, None, p["symbol"], p.get("name"), i,
                        p.get("score"), p.get("entry_price"), p.get("industry"),
                        json.dumps(p.get("tags", []), ensure_ascii=False),
                        p.get("reason"),
                        json.dumps(p.get("factors", {}), ensure_ascii=False),
                        json.dumps(p.get("kline_mini", {}), ensure_ascii=False),
                        p.get("ai_score"),
                        json.dumps(p.get("ai_flags", []), ensure_ascii=False),
                        p.get("ai_reason"),
                        mode,
                        _now(),
                    ),
                )

    def get_recommendations(self, base_date: str, mode: str = "daily") -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM recommendations WHERE base_date=? AND mode=? ORDER BY rank",
                (base_date, mode),
            ).fetchall()
        return [self._rec_row(r) for r in rows]

    def latest_recommendation_date(self, before: Optional[str] = None,
                                   mode: Optional[str] = None) -> Optional[str]:
        sql = "SELECT DISTINCT base_date FROM recommendations"
        args: list = []
        conds = []
        if before:
            conds.append("base_date < ?")
            args.append(before)
        if mode:
            conds.append("mode = ?")
            args.append(mode)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY base_date DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(sql, tuple(args)).fetchone()
        return row["base_date"] if row else None

    def all_recommendation_dates(self, mode: Optional[str] = None) -> list[str]:
        sql = "SELECT DISTINCT base_date FROM recommendations"
        args = ()
        if mode:
            sql += " WHERE mode = ?"
            args = (mode,)
        sql += " ORDER BY base_date DESC"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [r["base_date"] for r in rows]

    @staticmethod
    def _rec_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("tags", "factors", "kline_mini", "ai_flags"):
            try:
                d[k] = json.loads(d.get(k) or ("[]" if k in ("tags", "ai_flags") else "{}"))
            except Exception:
                d[k] = [] if k in ("tags", "ai_flags") else {}
        return d

    # ------------------------------------------------------------------
    # 结果回评
    # ------------------------------------------------------------------
    def save_result(self, rec_id: int, base_date: str, eval_date: str, symbol: str,
                    entry_price: float, eval_price: float, next_pct: float,
                    is_win: int, note: str = "", mode: str = "daily") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO recommendation_results
                (id, rec_id, base_date, eval_date, symbol, entry_price, eval_price,
                 next_pct, is_win, note, mode, created_at)
                VALUES (
                    (SELECT id FROM recommendation_results WHERE base_date=? AND symbol=? AND mode=?),
                    ?,?,?,?,?,?,?,?,?,?,?)""",
                (base_date, symbol, mode,
                 rec_id, base_date, eval_date, symbol,
                 entry_price, eval_price, next_pct, is_win, note, mode, _now()),
            )
        with self._conn() as c:
            c.execute("UPDATE recommendations SET rec_date=? WHERE base_date=? AND symbol=? AND mode=?",
                      (eval_date, base_date, symbol, mode))

    def has_result(self, base_date: str, symbol: str, mode: str = "daily") -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM recommendation_results WHERE base_date=? AND symbol=? AND mode=?",
                (base_date, symbol, mode),
            ).fetchone()
        return row is not None

    def get_results(self, base_date: str, mode: str = "daily") -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM recommendation_results WHERE base_date=? AND mode=? ORDER BY next_pct DESC",
                (base_date, mode),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_picks_with_results(self, base_date: str, mode: str = "daily") -> list[dict]:
        recs = {r["symbol"]: r for r in self.get_recommendations(base_date, mode=mode)}
        out = []
        for res in self.get_results(base_date, mode=mode):
            rec = recs.get(res["symbol"], {})
            merged = {**rec, **{f"result_{k}": v for k, v in res.items()}}
            merged["next_pct"] = res["next_pct"]
            merged["is_win"] = res["is_win"]
            out.append(merged)
        return out

    # ------------------------------------------------------------------
    # 胜率
    # ------------------------------------------------------------------
    def save_winrate(self, base_date: str, total: int, wins: int, win_rate: float,
                     avg_return: float, best: str = "", worst: str = "",
                     mode: str = "daily") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO daily_winrate
                (base_date, mode, total, wins, win_rate, avg_return, best_symbol, worst_symbol, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (base_date, mode, total, wins, win_rate, avg_return, best, worst, _now()),
            )

    def get_winrate(self, base_date: str, mode: str = "daily") -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM daily_winrate WHERE base_date=? AND mode=?",
                (base_date, mode),
            ).fetchone()
        return dict(row) if row else None

    def latest_winrate(self, before: Optional[str] = None, mode: str = "daily") -> Optional[dict]:
        sql = "SELECT * FROM daily_winrate"
        args: list = []
        conds = ["mode = ?"]
        args.append(mode)
        if before:
            conds.append("base_date < ?")
            args.append(before)
        sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY base_date DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def recent_winrates(self, n: int = 5, before: Optional[str] = None,
                        mode: str = "daily") -> list[dict]:
        """获取最近 N 批次的胜率记录（用于滚动窗口评估）。"""
        sql = "SELECT * FROM daily_winrate"
        args: list = [mode]
        conds = ["mode = ?"]
        if before:
            conds.append("base_date < ?")
            args.append(before)
        sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY base_date DESC LIMIT ?"
        args.append(n)
        with self._conn() as c:
            rows = c.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in reversed(rows)]  # 按日期升序

    def winrate_history(self, limit: int = 30, mode: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM daily_winrate"
        args: list = []
        if mode:
            sql += " WHERE mode = ?"
            args.append(mode)
        sql += " ORDER BY base_date DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 反思
    # ------------------------------------------------------------------
    def save_reflection(self, reflect_date: str, based_on_date: str, prev_win_rate: float,
                        threshold: float, dimensions: list, conclusion: str,
                        adjustments: dict, llm_used: int) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO reflections
                (reflect_date, based_on_date, prev_win_rate, threshold, dimensions,
                 conclusion, adjustments, llm_used, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (reflect_date, based_on_date, prev_win_rate, threshold,
                 json.dumps(dimensions, ensure_ascii=False),
                 conclusion, json.dumps(adjustments, ensure_ascii=False),
                 llm_used, _now()),
            )

    def get_reflection(self, reflect_date: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM reflections WHERE reflect_date=? ORDER BY id DESC LIMIT 1",
                (reflect_date,),
            ).fetchone()
        if not row:
            return None
        return self._reflection_row(row)

    def recent_reflections(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM reflections ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._reflection_row(r) for r in rows]

    @staticmethod
    def _reflection_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("dimensions", "adjustments"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except Exception:
                d[k] = {} if k == "adjustments" else []
        return d

    # ------------------------------------------------------------------
    # 策略权重 / 约束
    # ------------------------------------------------------------------
    def save_weights(self, effective_date: str, weights: dict, filters: dict,
                     source: str = "reflection", note: str = "") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO strategy_weights
                (effective_date, weights, filters, source, note, created_at)
                VALUES (?,?,?,?,?,?)""",
                (effective_date,
                 json.dumps(weights, ensure_ascii=False),
                 json.dumps(filters, ensure_ascii=False),
                 source, note, _now()),
            )

    def latest_weights(self, on_or_before: Optional[str] = None) -> Optional[dict]:
        """取生效日 <= 指定日期 的最新一条权重/约束。"""
        sql = "SELECT * FROM strategy_weights"
        args: tuple = ()
        if on_or_before:
            sql += " WHERE effective_date <= ?"
            args = (on_or_before,)
        sql += " ORDER BY effective_date DESC LIMIT 1"
        with self._conn() as c:
            row = c.execute(sql, args).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("weights", "filters"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except Exception:
                d[k] = {}
        return d
