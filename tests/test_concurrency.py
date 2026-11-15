"""Concurrency / interruption tests: cutover is atomic for readers and writers."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import legacy.acquisition as legacy
from app.db import connect
from app.migration import MigrationEngine
from app.repository import SampleRepository


def wait_state(engine: MigrationEngine, wanted: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = engine.get_state()
        if state["state"] == wanted:
            return state
        if state["state"] == "failed":
            raise AssertionError(f"migration failed: {state.get('last_error')}")
        time.sleep(0.01)
    raise AssertionError(f"state {wanted!r} not reached: {engine.get_state()}")


def test_writers_and_readers_during_cutover(settings):
    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        for i in range(2000, 2400):
            legacy.insert(conn, sample_id=i, temperature=1.0, ph=2.0)
    finally:
        conn.close()

    repo = SampleRepository(settings)
    stop = threading.Event()
    errors: list[BaseException] = []
    writes: list[int] = []

    def writer():
        try:
            w = legacy.open_db(settings.database_path)
            while not stop.is_set():
                new_id = legacy.insert(w, temperature=5.0, ph=6.0)
                writes.append(new_id)
                legacy.update(w, new_id, ph=6.5)
                assert legacy.select(w, new_id)["ph"] == 6.5
                time.sleep(0.002)
            w.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                page = repo.list_samples(limit=1000)
                # Each response is one snapshot: ids must be unique and the
                # total must never drop below the seed count.
                ids = [item["id"] for item in page["items"]]
                assert len(ids) == len(set(ids))
                assert page["count"] >= 418
                time.sleep(0.002)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    state, _ = eng.start()
    assert state["state"] in {"copying", "completed"}
    wait_state(eng, "completed")
    stop.set()
    for t in threads:
        t.join(30)
    assert errors == []

    # Every generated id was written exactly once; values survive cutover.
    assert len(writes) == len(set(writes))
    w = legacy.open_db(settings.database_path)
    try:
        for sample_id in writes:
            row = legacy.select(w, sample_id)
            assert row is not None
            assert row["temperature"] == 5.0
            assert row["ph"] == 6.5
    finally:
        w.close()

    db = connect(settings)
    try:
        assert (
            db.execute("SELECT COUNT(*) FROM sample_metrics").fetchone()[0]
            == db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] * 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sample_metrics WHERE sample_id NOT IN"
                " (SELECT id FROM samples)"
            ).fetchone()[0]
            == 0
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()


def test_old_reader_snapshot_survives_cutover(settings):
    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        for i in range(3000, 3050):
            legacy.insert(conn, sample_id=i, temperature=1.0, ph=2.0)
    finally:
        conn.close()

    reader = sqlite3.connect(settings.database_path, isolation_level=None)
    reader.execute("BEGIN")
    before = reader.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert before == 68

    eng.start()
    wait_state(eng, "completed")

    # The transaction that started before the switch still sees its snapshot.
    same_tx = reader.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert same_tx == 68
    reader.execute("COMMIT")
    after = reader.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert after == 68
    # And a write still works through the new view.
    reader.execute("INSERT INTO samples(id, temperature, ph) VALUES(9999, 1.0, 2.0)")
    assert reader.execute("SELECT * FROM samples WHERE id=9999").fetchone() == (
        9999,
        1.0,
        2.0,
    )
    reader.close()


def test_failure_before_cutover_keeps_wide_table_usable(settings, monkeypatch):
    from app.migration import MigrationEngine as Engine

    eng = Engine(settings)
    eng.initialize()

    def boom(self):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Engine, "_cutover", boom)
    eng.start()
    wait_state(eng, "failed")

    conn = legacy.open_db(settings.database_path)
    try:
        legacy.update(conn, 1, temperature=12.3)
        legacy.delete(conn, 2)
        legacy.insert(conn, sample_id=2, temperature=13.4, ph=8.8)
        assert legacy.select(conn, 1)["temperature"] == 12.3
    finally:
        conn.close()
    # full legacy usage still works on the wide table
    assert eng.get_state()["view_installed"] is False


def test_state_row_survives_restart_without_rebuild(settings):
    eng = MigrationEngine(settings)
    eng.initialize()
    eng.start()
    wait_state(eng, "completed")
    completed_at = eng.get_state()["completed_at"]

    fresh = MigrationEngine(settings)
    fresh.initialize()
    state, started = fresh.start()
    assert started is False
    assert state["state"] == "completed"
    assert state["completed_at"] == completed_at
    assert state["metric_rows"] == 36


def test_completed_status_reports_last_error_only_when_failed(settings):
    eng = MigrationEngine(settings)
    eng.initialize()
    assert eng.get_state()["state"] == "not_started"
    assert eng.last_error is None


@pytest.mark.parametrize("metric", ["temperature", "ph"])
def test_metric_rows_exactly_one_per_sample(settings, metric):
    eng = MigrationEngine(settings)
    eng.initialize()
    eng.start()
    wait_state(eng, "completed")
    db = connect(settings)
    try:
        duplicates = db.execute(
            "SELECT sample_id, COUNT(*) c FROM sample_metrics"
            " WHERE metric = ? GROUP BY sample_id HAVING c != 1",
            (metric,),
        ).fetchall()
        assert duplicates == []
        samples = db.execute("SELECT COUNT(*) n FROM samples").fetchone()["n"]
        rows = db.execute(
            "SELECT COUNT(*) n FROM sample_metrics WHERE metric = ?", (metric,)
        ).fetchone()["n"]
        assert rows == samples
    finally:
        db.close()
