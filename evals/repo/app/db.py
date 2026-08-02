"""Tiny database helper shared by every module in this app.

House rule: `sql` is always a static string with `?` placeholders and every
value travels in `params`. Nothing in this module ever interpolates a value
into a statement.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

_DB_PATH = "app.db"

MAX_PAGE_SIZE = 100


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """Run a read query with bound parameters."""
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Run a write and return the number of affected rows."""
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.rowcount


def clamp_page_size(requested: int | None) -> int:
    """Every list endpoint bounds its page size through this helper."""
    if not requested or requested < 1:
        return 25
    return min(int(requested), MAX_PAGE_SIZE)
