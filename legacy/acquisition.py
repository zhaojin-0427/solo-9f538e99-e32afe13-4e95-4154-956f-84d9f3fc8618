"""Legacy acquisition program client.

This script stands in for the old acquisition program: it talks directly to
``samples(id, temperature, ph)`` with plain SQL, exactly as before.  It works
unchanged before, during and after migration because the cutover keeps that
name as a compatibility view with INSTEAD OF triggers.

Usage::

    python -m legacy.acquisition --db data/lab.db select 1
    python -m legacy.acquisition --db data/lab.db select-all
    python -m legacy.acquisition --db data/lab.db insert --temperature 36.5 --ph 7.2
    python -m legacy.acquisition --db data/lab.db insert --id 42 --temperature 36.5 --ph 7.2
    python -m legacy.acquisition --db data/lab.db update 1 --temperature 37.0
    python -m legacy.acquisition --db data/lab.db update 1 --ph null
    python -m legacy.acquisition --db data/lab.db delete 1

It also exposes the same operations as functions so the demo and tests can use
it as a library.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_UNSET = object()


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def select(conn: sqlite3.Connection, sample_id: int) -> dict[str, Any] | None:
    """Legacy SELECT of one sample."""
    row = conn.execute(
        "SELECT id, temperature, ph FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def select_all(conn: sqlite3.Connection, limit: int = 1000) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, temperature, ph FROM samples ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def _samples_is_view(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'samples'"
    ).fetchone()
    return row is not None and row["type"] == "view"


def insert(
    conn: sqlite3.Connection,
    *,
    sample_id: int | None = None,
    temperature: float | None = None,
    ph: float | None = None,
) -> int:
    """Legacy INSERT; returns the row's id.

    With the wide table, ``lastrowid`` works as before.  After cutover the
    compat trigger allocates the id and records it in ``_insert_context``
    (SQLite restores ``last_insert_rowid()`` when a trigger finishes, so it
    cannot be used here); read it in the same transaction.
    """
    if sample_id is not None:
        conn.execute(
            "INSERT INTO samples (id, temperature, ph) VALUES (?, ?, ?)",
            (sample_id, temperature, ph),
        )
        return sample_id

    # Take the write lock *before* deciding whether ``samples`` is the wide
    # table or the compatibility view: a cutover may commit between the check
    # and the insert otherwise.  Inside one transaction the schema cannot
    # change under us.
    started_here = not conn.in_transaction
    if started_here:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if _samples_is_view(conn):
            conn.execute(
                "INSERT INTO samples (temperature, ph) VALUES (?, ?)",
                (temperature, ph),
            )
            # The compat trigger allocates the id; SQLite restores
            # last_insert_rowid() at trigger end, so read the helper row.
            row = conn.execute(
                "SELECT sample_id FROM _insert_context WHERE id = 1"
            ).fetchone()
            if row is None:  # pragma: no cover - trigger always writes it
                raise RuntimeError("could not determine generated sample id")
            new_id = int(row["sample_id"])
        else:
            cur = conn.execute(
                "INSERT INTO samples (temperature, ph) VALUES (?, ?)",
                (temperature, ph),
            )
            new_id = int(cur.lastrowid)
        if started_here:
            conn.execute("COMMIT")
        return new_id
    except BaseException:
        if started_here and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def update(
    conn: sqlite3.Connection,
    sample_id: int,
    *,
    temperature: float | None | object = _UNSET,
    ph: float | None | object = _UNSET,
) -> None:
    """Legacy UPDATE of one or both readings.  ``None`` writes SQL NULL."""
    sets: list[str] = []
    params: list[Any] = []
    if temperature is not _UNSET:
        sets.append("temperature = ?")
        params.append(temperature)
    if ph is not _UNSET:
        sets.append("ph = ?")
        params.append(ph)
    if not sets:
        raise ValueError("update needs temperature and/or ph")
    params.append(sample_id)
    conn.execute(f"UPDATE samples SET {', '.join(sets)} WHERE id = ?", params)


def delete(conn: sqlite3.Connection, sample_id: int) -> None:
    conn.execute("DELETE FROM samples WHERE id = ?", (sample_id,))


# --------------------------------------------------------------------- CLI
def emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _optional_float(raw: str) -> float | None:
    if raw.lower() in {"null", "none", "nan"}:
        return None
    return float(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Legacy samples table client")
    parser.add_argument(
        "--db",
        default="data/lab.db",
        help="SQLite database path (default: data/lab.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sel = sub.add_parser("select", help="SELECT one sample")
    p_sel.add_argument("sample_id", type=int)

    p_all = sub.add_parser("select-all", help="SELECT samples")
    p_all.add_argument("--limit", type=int, default=50)

    p_ins = sub.add_parser("insert", help="INSERT a sample")
    p_ins.add_argument("--id", type=int, default=None)
    p_ins.add_argument("--temperature", type=_optional_float, default=None)
    p_ins.add_argument("--ph", type=_optional_float, default=None)

    p_upd = sub.add_parser("update", help="UPDATE a sample")
    p_upd.add_argument("sample_id", type=int)
    p_upd.add_argument("--temperature", type=_optional_float, default=_UNSET)
    p_upd.add_argument("--ph", type=_optional_float, default=_UNSET)

    p_del = sub.add_parser("delete", help="DELETE a sample")
    p_del.add_argument("sample_id", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.db)
    if not path.exists():
        print(f"database not found: {path}", file=sys.stderr)
        return 2
    conn = open_db(path)
    try:
        if args.command == "select":
            row = select(conn, args.sample_id)
            emit(row if row is not None else {"id": args.sample_id, "found": False})
        elif args.command == "select-all":
            emit(select_all(conn, args.limit))
        elif args.command == "insert":
            new_id = insert(
                conn,
                sample_id=args.id,
                temperature=args.temperature,
                ph=args.ph,
            )
            emit({"inserted": True, "id": new_id})
        elif args.command == "update":
            update(
                conn,
                args.sample_id,
                temperature=args.temperature,
                ph=args.ph,
            )
            emit({"updated": True, "id": args.sample_id})
        elif args.command == "delete":
            delete(conn, args.sample_id)
            emit({"deleted": True, "id": args.sample_id})
        else:  # pragma: no cover
            raise SystemExit(f"unknown command {args.command}")
    except sqlite3.IntegrityError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
