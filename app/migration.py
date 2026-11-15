"""Wide-to-long migration engine.

Design
======
Phase 1 -- online, resumable copy
    ``samples`` stays the legacy wide table.  AFTER INSERT / UPDATE / DELETE
    triggers mirror every committed legacy write into ``sample_metrics``
    immediately.  Existing rows are copied in id-ordered chunks.  A chunk only
    *adds* rows that are absent (``ON CONFLICT DO NOTHING``), so a late chunk
    can never overwrite a value that a trigger has already corrected, and it
    cannot resurrect a deleted sample because it only copies ids that exist at
    that instant.  ``last_id`` is committed in the same transaction as its
    chunk, so a restart resumes exactly where it stopped.

Phase 2 -- atomic cutover
    One ``BEGIN IMMEDIATE`` transaction:
      1. final catch-up from the wide table (overwrites older copied values),
      2. delete long rows for samples deleted during migration,
      3. validate exactly ``samples * 2`` metric rows,
      4. drop the mirroring triggers,
      5. drop the wide table and install the compatibility view plus INSTEAD OF
         triggers for legacy INSERT / UPDATE / DELETE.
    SQLite DDL is transactional, so the switch either fully commits or leaves
    the wide table untouched.  In WAL mode readers see either the old table or
    the new view, never an intermediate state.

Idempotency and recovery
    ``migration_state`` holds a single row and acts as the lock.  A completed
    migration is never rebuilt (repeated POST is a no-op).  A failed or
    interrupted migration resumes from its checkpoint.  A crash at any point
    leaves the last committed state intact; if the crash happens before the
    cutover commit, the legacy wide table is still there and fully usable.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from .config import Settings
from .db import connect, transaction
from .schema import (
    WIDE_TABLE_SQL,
    create_base_schema,
    drop_migration_triggers,
    ensure_wide_table,
    install_migration_triggers,
    replace_wide_table_with_view,
)

logger = logging.getLogger("migration")

METRICS = ("temperature", "ph")

# _copy_one_chunk results
CONTINUE = "continue"  # copied a chunk; run the loop again
DONE = "done"  # no ids left, ready for cutover
ABORT = "abort"  # run is no longer active


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class MigrationError(RuntimeError):
    """Domain error surfaced to API clients."""


class MigrationEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._thread: threading.Thread | None = None
        # Re-entrant: start()/resume_if_needed() hold it while spawning.
        self._lock = threading.RLock()
        self._last_error: str | None = None

    # ------------------------------------------------------------------ setup
    def initialize(self) -> None:
        """Create schema; seed a brand-new database; resume interrupted runs."""
        conn = connect(self.settings)
        try:
            with transaction(conn):
                create_base_schema(conn)
                self._maybe_seed(conn)
                ensure_wide_table(conn)
                self._ensure_indexes(conn)
        finally:
            conn.close()
        if self.settings.auto_resume:
            self.resume_if_needed()

    @staticmethod
    def _ensure_indexes(conn: sqlite3.Connection) -> None:
        # Supports "one row per sample and metric" lookups in either direction.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sample_metrics_metric"
            " ON sample_metrics(metric, sample_id)"
        )

    def _maybe_seed(self, conn: sqlite3.Connection) -> None:
        """Populate a fresh database from the bundled wide sample database.

        The seed is opened read-only and is never modified.  A database that
        already has ``samples`` (table or compatibility view) is left alone.
        """
        if conn.execute(
            "SELECT 1 FROM sqlite_master"
            " WHERE type IN ('table','view') AND name='samples'"
        ).fetchone():
            return
        seed = self.settings.seed_database_path
        if seed is None or not seed.exists():
            return
        logger.info("seeding database from %s", seed)
        seed_conn = sqlite3.connect(f"file:{seed}?mode=ro", uri=True)
        seed_conn.row_factory = sqlite3.Row
        try:
            seed_obj = seed_conn.execute(
                "SELECT type FROM sqlite_master WHERE name='samples'"
            ).fetchone()
            if seed_obj is None:
                raise MigrationError(f"seed database {seed} has no samples table")
            if seed_obj["type"] != "table":
                raise MigrationError(
                    f"seed database {seed} must contain the wide samples table"
                )
            rows = seed_conn.execute(
                "SELECT id, temperature, ph FROM samples ORDER BY id"
            ).fetchall()
        finally:
            seed_conn.close()
        conn.execute(WIDE_TABLE_SQL)
        conn.executemany(
            "INSERT INTO samples (id, temperature, ph) VALUES (?, ?, ?)",
            [(r["id"], r["temperature"], r["ph"]) for r in rows],
        )

    # ------------------------------------------------------------ public API
    def get_state(self) -> dict[str, Any]:
        conn = connect(self.settings)
        try:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT * FROM migration_state WHERE id = 1"
                ).fetchone()
                if row is None:
                    return {"state": "not_started", "run_id": None}
                state = self._state_payload(conn, row)
                return state
            finally:
                if conn.in_transaction:
                    conn.execute("COMMIT")
        finally:
            conn.close()

    def _state_payload(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        payload = dict(row)
        samples_total = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()[
            "n"
        ]
        metric_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM sample_metrics"
        ).fetchone()["n"]
        copied = payload.get("samples_copied") or 0
        total = payload.get("total_samples") or 0
        if payload["state"] == "completed":
            progress = 1.0
        elif total > 0:
            progress = round(min(1.0, copied / total), 4)
        else:
            progress = 0.0
        payload.update(
            {
                "samples_total": samples_total,
                "metric_rows": metric_rows,
                "metric_rows_expected": samples_total * len(METRICS),
                "progress": progress,
                "view_installed": self._is_view_installed(conn),
            }
        )
        payload.pop("last_id", None)
        return payload

    def start(self) -> tuple[dict[str, Any], bool]:
        """Start or resume migration.  Returns ``(state, new_work_started)``.

        Idempotent:
          * completed  -> no work, returns the completed state,
          * copying    -> returns live progress (worker already running),
          * failed     -> resumes from its committed checkpoint,
          * absent     -> creates a run and starts the worker.
        """
        with self._lock:
            conn = connect(self.settings)
            try:
                with transaction(conn):
                    create_base_schema(conn)
                    self._ensure_indexes(conn)
                    row = conn.execute(
                        "SELECT * FROM migration_state WHERE id = 1"
                    ).fetchone()

                    if row is not None and row["state"] == "completed":
                        return self._state_payload(conn, row), False

                    if self._is_view_installed(conn):
                        # Cutover committed but bookkeeping did not (or the DB
                        # was switched externally).  Never migrate twice.
                        run_id = row["run_id"] if row is not None else str(uuid.uuid4())
                        if row is None:
                            now = utcnow()
                            conn.execute(
                                "INSERT INTO migration_state"
                                " (id, run_id, state, created_at, started_at,"
                                "  completed_at) VALUES (1, ?, 'completed', ?, ?, ?)",
                                (run_id, now, now, now),
                            )
                        else:
                            conn.execute(
                                "UPDATE migration_state SET state='completed',"
                                " completed_at=?, last_error=NULL WHERE id=1",
                                (utcnow(),),
                            )
                        self._event(conn, run_id, "recovered_completed")
                        return self._state_payload(
                            conn,
                            conn.execute(
                                "SELECT * FROM migration_state WHERE id = 1"
                            ).fetchone(),
                        ), False

                    ensure_wide_table(conn)

                    if row is not None and row["state"] == "copying":
                        worker_alive = (
                            self._thread is not None and self._thread.is_alive()
                        )
                        if worker_alive:
                            return self._state_payload(conn, row), False
                        conn.execute(
                            "UPDATE migration_state SET attempt=attempt+1,"
                            " started_at=?, last_error=NULL WHERE id=1",
                            (utcnow(),),
                        )
                        self._event(conn, row["run_id"], "resumed")
                        run_id = row["run_id"]
                    elif row is not None:
                        # failed -> retry from checkpoint
                        conn.execute(
                            "UPDATE migration_state SET state='copying',"
                            " attempt=attempt+1, started_at=?, completed_at=NULL,"
                            " last_error=NULL,"
                            " total_samples=(SELECT COUNT(*) FROM samples)"
                            " WHERE id=1",
                            (utcnow(),),
                        )
                        self._event(conn, row["run_id"], "resumed")
                        run_id = row["run_id"]
                    else:
                        run_id = str(uuid.uuid4())
                        conn.execute(
                            "INSERT INTO migration_state"
                            " (id, run_id, state, created_at, started_at, attempt,"
                            "  total_samples)"
                            " VALUES (1, ?, 'copying', ?, ?, 1,"
                            "         (SELECT COUNT(*) FROM samples))",
                            (run_id, utcnow(), utcnow()),
                        )
                        self._event(conn, run_id, "started")

                    # Mirroring triggers are installed in the same transaction
                    # that marks the run active, so no committed legacy write
                    # can be missed between "start" and the first chunk.
                    install_migration_triggers(conn)
                    self._last_error = None
                    self._spawn_worker()
                    return self._state_payload(
                        conn,
                        conn.execute(
                            "SELECT * FROM migration_state WHERE id = 1"
                        ).fetchone(),
                    ), True
            finally:
                conn.close()

    def resume_if_needed(self) -> None:
        """On process startup, continue a run interrupted mid-copy.

        A ``failed`` run is *not* auto-retried: it stays failed (with its
        error) until an operator explicitly POSTs /migrations again.  This
        avoids a crash loop on a permanent error.
        """
        state = self.get_state()
        if state.get("state") == "copying":
            # Interrupted while copying: safe to continue from the checkpoint.
            logger.info("auto-resuming interrupted migration %s", state.get("run_id"))
            self.start()

    def wait_for_idle(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ---------------------------------------------------------- worker logic
    def _spawn_worker(self) -> None:
        # Caller holds self._lock.
        if self._thread is not None and self._thread.is_alive():
            return
        thread = threading.Thread(
            target=self._run, name="migration-worker", daemon=True
        )
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        try:
            while True:
                result = self._copy_one_chunk()
                if result == CONTINUE:
                    time.sleep(self.settings.chunk_delay_ms / 1000.0)
                    continue
                if result == ABORT:
                    return
                break  # DONE
            self._cutover()
        except Exception as exc:
            logger.exception("migration failed")
            self._last_error = str(exc)
            self._record_failure(str(exc))

    def _copy_one_chunk(self) -> str:
        """Copy one id-ordered chunk.  Returns CONTINUE / DONE / ABORT."""
        conn = connect(self.settings)
        try:
            with transaction(conn):
                state = conn.execute(
                    "SELECT * FROM migration_state WHERE id = 1"
                ).fetchone()
                if state is None or state["state"] != "copying":
                    return ABORT
                if self._is_view_installed(conn):
                    return ABORT
                self._ensure_triggers(conn)
                lower = state["last_id"]
                lower = -(2**63) if lower is None else lower
                rows = conn.execute(
                    "SELECT id, temperature, ph FROM samples"
                    " WHERE id > ? ORDER BY id LIMIT ?",
                    (lower, self.settings.chunk_size),
                ).fetchall()
                if not rows:
                    return DONE
                copied = self._copy_rows(conn, rows)
                conn.execute(
                    "UPDATE migration_state"
                    " SET last_id=?, samples_copied=samples_copied+?,"
                    "     total_samples=MAX(total_samples,"
                    "       (SELECT COUNT(*) FROM samples))"
                    " WHERE id=1",
                    (rows[-1]["id"], copied),
                )
                # Even a short chunk returns CONTINUE: late legacy INSERTs may
                # have ids above it, and the next pass (usually empty) is what
                # proves the copy is complete before cutover.
                return CONTINUE
        finally:
            conn.close()

    def _copy_rows(self, conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
        """Copy metric rows for a chunk without overwriting mirrored writes."""
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in ids)
        existing = {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM samples WHERE id IN ({placeholders})", ids
            )
        }
        for metric in METRICS:
            values = [(r["id"], metric, r[metric]) for r in rows if r["id"] in existing]
            if not values:
                continue
            conn.executemany(
                "INSERT INTO sample_metrics (sample_id, metric, value)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (sample_id, metric) DO NOTHING",
                values,
            )
        return len(existing)

    def _ensure_triggers(self, conn: sqlite3.Connection) -> None:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger'"
            " AND name='_migration_samples_insert'"
        ).fetchone()
        if present is None:
            install_migration_triggers(conn)

    def _cutover(self) -> None:
        """Final catch-up and atomic switch to the compatibility view."""
        conn = connect(self.settings)
        try:
            with transaction(conn):
                state = conn.execute(
                    "SELECT * FROM migration_state WHERE id = 1"
                ).fetchone()
                if state is None or state["state"] == "completed":
                    return
                if self._is_view_installed(conn):
                    conn.execute(
                        "UPDATE migration_state SET state='completed',"
                        " completed_at=?, last_error=NULL WHERE id=1",
                        (utcnow(),),
                    )
                    return

                # We hold the write lock, so this reflects every committed
                # legacy write and no new one can commit until we finish.
                for metric in METRICS:
                    conn.execute(
                        "INSERT INTO sample_metrics (sample_id, metric, value)"
                        f" SELECT id, ?, {metric} FROM samples WHERE true"
                        " ON CONFLICT (sample_id, metric) DO UPDATE"
                        " SET value = excluded.value",
                        (metric,),
                    )
                conn.execute(
                    "DELETE FROM sample_metrics"
                    " WHERE sample_id NOT IN (SELECT id FROM samples)"
                )
                total_rows = conn.execute(
                    "SELECT COUNT(*) AS n FROM sample_metrics"
                ).fetchone()["n"]
                samples = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()[
                    "n"
                ]
                if total_rows != samples * len(METRICS):
                    raise MigrationError(
                        "post-copy validation failed: "
                        f"{total_rows} metric rows for {samples} samples"
                    )
                drop_migration_triggers(conn)
                replace_wide_table_with_view(conn)
                conn.execute(
                    "UPDATE migration_state SET state='completed',"
                    " completed_at=?, last_error=NULL,"
                    " samples_copied=?, total_samples=? WHERE id=1",
                    (utcnow(), samples, samples),
                )
                self._event(
                    conn,
                    state["run_id"],
                    "cutover_completed",
                    f"samples={samples}, metric_rows={total_rows}",
                )
        finally:
            # Failure recording is done by _run(), which also handles errors
            # from the copy loop.
            conn.close()

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _is_view_installed(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'samples'"
        ).fetchone()
        return row is not None and row["type"] == "view"

    def _record_failure(self, message: str) -> None:
        try:
            conn = connect(self.settings)
        except Exception:  # pragma: no cover - DB unavailable
            logger.exception("cannot record migration failure")
            return
        try:
            with transaction(conn):
                state = conn.execute(
                    "SELECT * FROM migration_state WHERE id = 1"
                ).fetchone()
                if state is None or state["state"] == "completed":
                    return
                conn.execute(
                    "UPDATE migration_state SET state='failed', last_error=?"
                    " WHERE id=1",
                    (message[:2000],),
                )
                self._event(conn, state["run_id"], "failed", message[:2000])
        except Exception:  # pragma: no cover - best effort
            logger.exception("cannot record migration failure")
        finally:
            conn.close()

    @staticmethod
    def _event(
        conn: sqlite3.Connection, run_id: str, event: str, detail: str | None = None
    ) -> None:
        conn.execute(
            "INSERT INTO migration_events (run_id, at, event, detail)"
            " VALUES (?, ?, ?, ?)",
            (run_id, utcnow(), event, detail),
        )

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = connect(self.settings)
        try:
            rows = conn.execute(
                "SELECT seq, run_id, at, event, detail FROM migration_events"
                " ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]
        finally:
            conn.close()
