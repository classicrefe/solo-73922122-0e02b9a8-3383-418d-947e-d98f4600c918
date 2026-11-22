"""SQLite 数据层。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    turbine     TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    available   TEXT NOT NULL,
    actual      TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (turbine, start_utc)
);

CREATE TABLE IF NOT EXISTS orders (
    order_no    TEXT PRIMARY KEY,
    cause       TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    end_utc     TEXT NOT NULL,
    cap         TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    conflicted  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS order_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no    TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    cause       TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    end_utc     TEXT NOT NULL,
    cap         TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (order_no, revision)
);

CREATE TABLE IF NOT EXISTS attribution_days (
    turbine     TEXT NOT NULL,
    date        TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (turbine, date)
);

CREATE TABLE IF NOT EXISTS attribution_slots (
    turbine     TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    date        TEXT NOT NULL,
    available   TEXT NOT NULL,
    actual      TEXT NOT NULL,
    unsplit     TEXT NOT NULL,
    PRIMARY KEY (turbine, start_utc)
);

CREATE TABLE IF NOT EXISTS attribution_losses (
    turbine     TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    date        TEXT NOT NULL,
    cause       TEXT NOT NULL,
    amount      TEXT NOT NULL,
    PRIMARY KEY (turbine, start_utc, cause)
);

CREATE INDEX IF NOT EXISTS idx_meters_turbine_time ON meters (turbine, start_utc);
CREATE INDEX IF NOT EXISTS idx_attribution_slots_date ON attribution_slots (turbine, date);
CREATE INDEX IF NOT EXISTS idx_attribution_losses_date ON attribution_losses (turbine, date);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：为早期库补 ``orders.conflicted`` 列。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    if "conflicted" not in columns:
        conn.execute(
            "ALTER TABLE orders ADD COLUMN conflicted INTEGER NOT NULL DEFAULT 0"
        )


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；异常时整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
