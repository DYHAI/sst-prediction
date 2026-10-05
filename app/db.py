"""SQLite 存储层（纯标准库）。

整站状态都在 data/sst.db 里，data/ 目录不进 git，和主站的约定一致。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

DB_PATH = os.environ.get(
    "SST_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sst.db"),
)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS players (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'human',   -- human | team | product
    affiliation TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    region      TEXT NOT NULL,
    horizon     INTEGER NOT NULL,
    target_date TEXT NOT NULL,                   -- YYYY-MM-DD
    value       REAL NOT NULL,                   -- °C
    comment     TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_unique
    ON submissions(player_id, region, horizon, target_date);
CREATE INDEX IF NOT EXISTS idx_sub_target ON submissions(target_date);

CREATE TABLE IF NOT EXISTS truth (
    region      TEXT NOT NULL,
    date        TEXT NOT NULL,
    sst         REAL NOT NULL,
    n_cells     INTEGER,
    valid_frac  REAL,
    source      TEXT,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (region, date)
);

CREATE TABLE IF NOT EXISTS products (
    product     TEXT NOT NULL,                   -- hycom / gfs / cfs ...
    region      TEXT NOT NULL,
    horizon     INTEGER NOT NULL,
    run_date    TEXT NOT NULL,                   -- YYYY-MM-DD（实际用到的起报日）
    target_date TEXT NOT NULL,
    value       REAL NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (product, region, horizon, target_date)
);
CREATE INDEX IF NOT EXISTS idx_prod_target ON products(target_date);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                   -- truth | product:hycom ...
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    ok          INTEGER,
    detail      TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None, timeout: float = 30.0) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: str | None = None) -> None:
    # 抓取任务可能正在写库，建表被锁住时不要让整个服务卡在启动阶段：
    # 表结构是幂等的，等不到就直接放过，服务照常提供只读接口。
    try:
        with connect(path, timeout=3.0) as conn:
            conn.executescript(SCHEMA)
    except sqlite3.OperationalError as e:  # database is locked
        if "locked" not in str(e):
            raise


@contextmanager
def session(path: str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 账号

def create_player(conn, name: str, kind: str = "human", affiliation: str = "") -> dict:
    token = secrets.token_urlsafe(24)
    cur = conn.execute(
        "INSERT INTO players(name, token, kind, affiliation, created_at, last_seen)"
        " VALUES(?,?,?,?,?,?)",
        (name.strip()[:40], token, kind, affiliation.strip()[:80], now_iso(), now_iso()),
    )
    return {"id": cur.lastrowid, "name": name, "token": token, "kind": kind}


def player_by_token(conn, token: str):
    return conn.execute("SELECT * FROM players WHERE token=?", (token,)).fetchone()


def ensure_product_players(conn, products: dict) -> None:
    """把官方预报产品注册成特殊的"选手"，让它们出现在同一张榜上。"""
    for code, meta in products.items():
        row = conn.execute(
            "SELECT id FROM players WHERE token=?", (f"product:{code}",)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO players(name, token, kind, affiliation, created_at)"
                " VALUES(?,?,?,?,?)",
                (meta["name"], f"product:{code}", "product", meta.get("org", ""), now_iso()),
            )


# ---------------------------------------------------------------- 时间

def deadline_utc(target_date: str, horizon: int) -> str:
    """某个 (目标日, 时效) 的截止时间：目标日往前 horizon 天的 00:00 UTC。"""
    d = date.fromisoformat(target_date) - timedelta(days=horizon)
    return f"{d.isoformat()}T00:00:00Z"


def is_open(target_date: str, horizon: int, at: datetime | None = None) -> bool:
    at = at or datetime.now(timezone.utc)
    dl = datetime.strptime(deadline_utc(target_date, horizon), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return at < dl


def open_rounds(days_ahead: int = 7, now: datetime | None = None) -> list[dict]:
    """当前还可提交的 (目标日, 时效) 列表。"""
    from . import regions as R

    now = now or datetime.now(timezone.utc)
    today = now.date()
    out: list[dict] = []
    for k in range(0, days_ahead + 1):
        d = today + timedelta(days=k)
        for h in R.HORIZONS:
            if is_open(d.isoformat(), h, at=now):
                out.append(
                    {
                        "target_date": d.isoformat(),
                        "horizon": h,
                        "deadline": deadline_utc(d.isoformat(), h),
                        "lead_days": k,
                    }
                )
    return out


def as_epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    ).timestamp()
