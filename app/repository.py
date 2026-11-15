"""Read API for samples and metrics.

Before cutover the legacy wide table ``samples`` is the source of truth; after
cutover ``samples`` is the compatibility view over ``sample_metrics``.  Both
cases are read through the same SQL below, and each response is served from a
single read transaction so clients only see a complete committed state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import Settings
from .db import connect

METRICS = ("temperature", "ph")
DEFAULT_PAGE = 100
MAX_PAGE = 1000


class SampleNotFound(LookupError):
    pass


class InvalidQuery(ValueError):
    pass


def _validate_metric(metric: str) -> str:
    if metric not in METRICS:
        raise InvalidQuery(
            f"unknown metric {metric!r}; expected one of {', '.join(METRICS)}"
        )
    return metric


def _validate_id(value: Any, name: str = "sample_id") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidQuery(f"{name} must be an integer")
    return value


def _validate_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidQuery("limit must be an integer")
    if limit < 1 or limit > MAX_PAGE:
        raise InvalidQuery(f"limit must be between 1 and {MAX_PAGE}")
    return limit


class SampleRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """One read transaction = one consistent committed snapshot."""
        # Read-only URI connection: no writes, no journal-mode changes, and it
        # never blocks the migration's short write transactions.
        conn = connect(self.settings, read_only=True)
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("COMMIT")
            conn.close()

    def get_metrics(self, sample_id: int) -> dict[str, Any]:
        """All current metric values for one sample.

        Before cutover the wide table is authoritative (it is what the legacy
        program reads and writes); after cutover the long table is.  Reading
        the same source as the legacy program means a value committed by the
        old program is immediately visible here as well.
        """
        sample_id = _validate_id(sample_id)
        with self._read() as conn:
            if self._samples_is_view(conn):
                rows = conn.execute(
                    "SELECT metric, value FROM sample_metrics WHERE sample_id = ?",
                    (sample_id,),
                ).fetchall()
                if not rows:
                    raise SampleNotFound(f"sample {sample_id} not found")
                values = {r["metric"]: r["value"] for r in rows}
            else:
                row = conn.execute(
                    "SELECT id, temperature, ph FROM samples WHERE id = ?",
                    (sample_id,),
                ).fetchone()
                if row is None:
                    raise SampleNotFound(f"sample {sample_id} not found")
                values = {metric: row[metric] for metric in METRICS}
            result: dict[str, Any] = {"sample_id": sample_id}
            result.update({metric: values.get(metric) for metric in METRICS})
            return result

    def list_metric(
        self,
        metric: str,
        *,
        after_id: int | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """One metric across samples, keyset-paginated by ``sample_id``.

        The analysis load path; served from the long table after cutover and
        from the wide table before it.
        """
        metric = _validate_metric(metric)
        limit = _validate_limit(limit)
        if after_id is not None:
            _validate_id(after_id, "after_id")
        with self._read() as conn:
            is_view = self._samples_is_view(conn)
            if is_view:
                sql = (
                    "SELECT sample_id AS id, value FROM sample_metrics WHERE metric = ?"
                )
                params: list[Any] = [metric]
                if after_id is not None:
                    sql += " AND sample_id > ?"
                    params.append(after_id)
                sql += " ORDER BY sample_id LIMIT ?"
            else:
                sql = f"SELECT id, {metric} AS value FROM samples WHERE 1=1"
                params = []
                if after_id is not None:
                    sql += " AND id > ?"
                    params.append(after_id)
                sql += " ORDER BY id LIMIT ?"
            # One extra row gives a stable keyset cursor.
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_after = rows[-1]["id"] if has_more and rows else None
            return {
                "metric": metric,
                "source": "sample_metrics" if is_view else "wide_table",
                "items": [{"sample_id": r["id"], "value": r["value"]} for r in rows],
                "count": len(rows),
                "next_after_id": next_after,
                "has_more": has_more,
            }

    def list_samples(
        self, *, after_id: int | None = None, limit: int = DEFAULT_PAGE
    ) -> dict[str, Any]:
        """Read the legacy-compatible ``samples`` name."""
        limit = _validate_limit(limit)
        if after_id is not None:
            _validate_id(after_id, "after_id")
        with self._read() as conn:
            sql = "SELECT id, temperature, ph FROM samples"
            params: list[Any] = []
            if after_id is not None:
                sql += " WHERE id > ?"
                params.append(after_id)
            sql += " ORDER BY id LIMIT ?"
            params.append(limit + 1)
            rows = conn.execute(sql, params).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_after = rows[-1]["id"] if has_more and rows else None
            return {
                "source": (
                    "compatibility_view"
                    if self._samples_is_view(conn)
                    else "wide_table"
                ),
                "items": [dict(r) for r in rows],
                "count": len(rows),
                "next_after_id": next_after,
                "has_more": has_more,
            }

    def get_sample(self, sample_id: int) -> dict[str, Any]:
        """Legacy-shaped single-row read through ``samples``."""
        sample_id = _validate_id(sample_id)
        with self._read() as conn:
            row = conn.execute(
                "SELECT id, temperature, ph FROM samples WHERE id = ?",
                (sample_id,),
            ).fetchone()
            if row is None:
                raise SampleNotFound(f"sample {sample_id} not found")
            return dict(row)

    @staticmethod
    def _samples_is_view(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'samples'"
        ).fetchone()
        return row is not None and row["type"] == "view"
