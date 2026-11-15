#!/usr/bin/env python3
"""Generate the bundled sample database containing the legacy wide table.

The generated database is a pure legacy acquisition database:
``samples(id INTEGER PRIMARY KEY, temperature REAL, ph REAL)`` -- no long tables,
no migration bookkeeping.  The service copies it once on first start.

Usage::

    python scripts/make_seed.py seed/lab_wide.db
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

# Hand-picked samples covering NULLs, gaps in ids, negative values and
# fractional readings.
SAMPLES: list[tuple[int, float | None, float | None]] = [
    (1, 36.5, 7.20),
    (2, 36.6, 7.18),
    (3, None, 7.05),  # temperature sensor missing
    (4, 36.9, None),  # pH probe missing
    (5, 37.1, 7.41),
    (6, 36.4, 7.33),
    (7, None, None),  # both readings missing
    (8, 36.8, 6.95),
    (9, 37.6, 7.52),
    (10, 36.2, 7.11),
    (12, 36.7, 7.25),  # id 11 intentionally absent
    (13, 37.0, 7.29),
    (20, 36.55, 7.31),
    (21, 36.75, 7.22),
    (22, 36.65, 7.27),
    (40, 38.2, 6.80),
    (41, 38.0, 6.88),
    (42, None, 6.91),
]


def build(path: Path, rows: list[tuple[int, float | None, float | None]]) -> None:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(
            "CREATE TABLE samples ( id INTEGER PRIMARY KEY, temperature REAL, ph REAL)"
        )
        conn.executemany(
            "INSERT INTO samples (id, temperature, ph) VALUES (?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="output database path")
    args = parser.parse_args()
    build(args.path, SAMPLES)
    print(f"wrote {len(SAMPLES)} wide rows to {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
