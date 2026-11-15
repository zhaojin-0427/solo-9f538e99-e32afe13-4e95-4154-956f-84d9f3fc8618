"""SQLite connection and transaction helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import Settings


def connect(settings: Settings, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a configured connection.

    Connections use autocommit (``isolation_level=None``); transactions are
    started explicitly so a whole cutover can be one atomic unit.
    """
    path = settings.database_path
    if read_only:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=settings.busy_timeout_ms / 1000.0,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            path, timeout=settings.busy_timeout_ms / 1000.0, isolation_level=None
        )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(settings.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(
    conn: sqlite3.Connection, mode: str = "IMMEDIATE"
) -> Iterator[sqlite3.Connection]:
    """Run a block in a write transaction; commit on success, roll back on error."""
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")
