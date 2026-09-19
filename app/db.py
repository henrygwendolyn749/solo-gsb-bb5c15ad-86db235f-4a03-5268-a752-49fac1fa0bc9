"""SQLite 连接、建表与事务封装。

使用 WAL 提升读并发；写事务统一采用 ``BEGIN IMMEDIATE``，
配合 busy_timeout 与锁冲突重试，保证“选匹配 / 渲染 / 推状态”
在同一事务中串行执行，避免同一会话的并发请求重复触发迁移。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    name        TEXT PRIMARY KEY,
    definition  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token            TEXT PRIMARY KEY,
    scenario         TEXT NOT NULL,
    state            TEXT NOT NULL,
    context          TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (scenario) REFERENCES scenarios(name)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    route       TEXT,
    rule        TEXT,
    from_state  TEXT,
    to_state    TEXT,
    status_code INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (token) REFERENCES sessions(token)
);

CREATE INDEX IF NOT EXISTS idx_events_token ON events(token, id);
"""

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=settings.busy_timeout_ms / 1000.0,
        isolation_level=None,  # 手动控制事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {settings.busy_timeout_ms}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn() -> sqlite3.Connection:
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def init_db() -> None:
    import os

    directory = os.path.dirname(settings.db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


class WriteLocked(sqlite3.OperationalError):
    """重试次数耗尽后仍拿不到写锁。"""


@contextmanager
def write_tx(retries: int | None = None) -> Iterator[sqlite3.Connection]:
    """打开一个 IMMEDIATE 写事务，锁冲突时重试。

    块内抛出的异常若带有 ``commit_on_abort = True`` 属性，则提交事务
    （用于“会话已建立但无匹配规则”这类需要落库并返回错误码的场景）。
    """
    conn = get_conn()
    attempts = settings.db_retries if retries is None else retries
    for attempt in range(attempts + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception as exc:
                if getattr(exc, "commit_on_abort", False):
                    conn.commit()
                else:
                    conn.rollback()
                raise
            else:
                conn.commit()
                return
        except sqlite3.OperationalError as exc:  # database is locked
            if "locked" not in str(exc).lower() or attempt == attempts:
                raise WriteLocked(str(exc)) from exc
            time.sleep(0.02 * min(attempt + 1, 10))
    raise WriteLocked("unreachable")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
