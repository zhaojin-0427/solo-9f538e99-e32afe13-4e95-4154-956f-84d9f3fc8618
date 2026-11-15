"""Unit tests for the migration engine and compatibility layer."""

from __future__ import annotations

import sqlite3
import threading

import pytest

import legacy.acquisition as legacy
from app.db import connect
from app.repository import SampleNotFound, SampleRepository

SEED_ROWS = {
    1: (36.5, 7.20),
    3: (None, 7.05),
    4: (36.9, None),
    7: (None, None),
    42: (None, 6.91),
}


def long_rows(settings):
    conn = connect(settings)
    try:
        return conn.execute(
            "SELECT sample_id, metric, value FROM sample_metrics"
            " ORDER BY sample_id, metric"
        ).fetchall()
    finally:
        conn.close()


def sample_rows(settings):
    conn = connect(settings)
    try:
        return {
            r["id"]: (r["temperature"], r["ph"])
            for r in conn.execute("SELECT id, temperature, ph FROM samples")
        }
    finally:
        conn.close()


def type_of_samples(settings):
    conn = connect(settings)
    try:
        return conn.execute(
            "SELECT type FROM sqlite_master WHERE name='samples'"
        ).fetchone()["type"]
    finally:
        conn.close()


def test_engine_starts_from_seed(engine, settings):
    assert type_of_samples(settings) == "table"
    rows = sample_rows(settings)
    assert len(rows) == 18
    assert rows[1] == (36.5, 7.2)
    assert rows[3] == (None, 7.05)
    assert engine.get_state()["state"] == "not_started"


def test_migration_copies_values_and_nulls(migrated, settings):
    assert type_of_samples(settings) == "view"
    state = migrated.get_state()
    assert state["state"] == "completed"
    assert state["view_installed"] is True
    assert state["metric_rows"] == state["metric_rows_expected"] == 36

    rows = sample_rows(settings)
    assert len(rows) == 18
    for sample_id, expected in SEED_ROWS.items():
        assert rows[sample_id] == expected

    metrics = long_rows(settings)
    assert len(metrics) == 36
    for sample_id in rows:
        assert [m["metric"] for m in metrics if m["sample_id"] == sample_id] == [
            "ph",
            "temperature",
        ]
    numbers = {(m["sample_id"], m["metric"]): m["value"] for m in metrics}
    assert numbers[(1, "temperature")] == 36.5
    assert numbers[(1, "ph")] == 7.2
    assert numbers[(3, "temperature")] is None
    assert numbers[(3, "ph")] == 7.05
    assert numbers[(4, "ph")] is None
    assert numbers[(7, "temperature")] is None
    assert numbers[(42, "temperature")] is None
    nulls = [m for m in metrics if m["value"] is None]
    assert {m["sample_id"] for m in nulls} == {3, 4, 7, 42}


def test_repeated_start_does_not_rebuild(migrated, settings):
    before = long_rows(settings)
    state, started = migrated.start()
    assert started is False
    assert state["state"] == "completed"
    assert long_rows(settings) == before
    # second engine instance sees the completed database and does nothing
    from app.migration import MigrationEngine

    other = MigrationEngine(migrated.settings)
    other.initialize()
    state2, started2 = other.start()
    assert started2 is False
    assert state2["state"] == "completed"
    assert long_rows(settings) == before


def test_legacy_crud_after_cutover(migrated, settings):
    conn = legacy.open_db(settings.database_path)
    try:
        new_id = legacy.insert(conn, temperature=39.0, ph=6.5)
        assert new_id > 42
        assert legacy.select(conn, new_id) == {
            "id": new_id,
            "temperature": 39.0,
            "ph": 6.5,
        }
        legacy.update(conn, 3, temperature=37.7)
        assert legacy.select(conn, 3)["temperature"] == 37.7
        legacy.update(conn, 3, ph=None)
        assert legacy.select(conn, 3)["ph"] is None
        legacy.delete(conn, 4)
        assert legacy.select(conn, 4) is None
        # recreate the same id after deletion
        assert legacy.insert(conn, sample_id=4, temperature=1.0, ph=2.0) == 4
        assert legacy.select(conn, 4) == {"id": 4, "temperature": 1.0, "ph": 2.0}
    finally:
        conn.close()

    rows = sample_rows(settings)
    assert rows[3] == (37.7, None)
    assert rows[4] == (1.0, 2.0)
    assert rows[new_id] == (39.0, 6.5)
    # one row per sample per metric, including NULLs
    metrics = long_rows(settings)
    assert len(metrics) == len(rows) * 2
    assert {m["sample_id"] for m in metrics} == set(rows)


def test_legacy_duplicate_id_rejected_after_cutover(migrated, settings):
    conn = legacy.open_db(settings.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            legacy.insert(conn, sample_id=1, temperature=1.0, ph=2.0)
    finally:
        conn.close()


def test_legacy_id_update_moves_rows(migrated, settings):
    conn = legacy.open_db(settings.database_path)
    try:
        conn.execute("UPDATE samples SET id = 1001 WHERE id = 1")
        assert legacy.select(conn, 1) is None
        assert legacy.select(conn, 1001) == {
            "id": 1001,
            "temperature": 36.5,
            "ph": 7.2,
        }
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE samples SET id = 2 WHERE id = 1001")
    finally:
        conn.close()


def test_legacy_commit_is_visible_in_api(migrated, settings):
    repo = SampleRepository(settings)
    conn = legacy.open_db(settings.database_path)
    try:
        new_id = legacy.insert(conn, temperature=36.1, ph=7.44)
        assert repo.get_metrics(new_id) == {
            "sample_id": new_id,
            "temperature": 36.1,
            "ph": 7.44,
        }
        legacy.update(conn, 2, ph=None)
        assert repo.get_metrics(2)["ph"] is None
        legacy.delete(conn, 5)
        with pytest.raises(SampleNotFound):
            repo.get_metrics(5)
    finally:
        conn.close()


def test_list_metric_keyset_and_nulls(migrated, settings):
    repo = SampleRepository(settings)
    page = repo.list_metric("temperature", limit=5)
    assert page["count"] == 5
    assert page["has_more"] is True
    assert [i["sample_id"] for i in page["items"]] == [1, 2, 3, 4, 5]
    seen = [i["sample_id"] for i in page["items"]]
    cursor = page["next_after_id"]
    while cursor is not None:
        page = repo.list_metric("temperature", after_id=cursor, limit=5)
        seen.extend(i["sample_id"] for i in page["items"])
        cursor = page["next_after_id"]
    assert seen == sorted(sample_rows(settings))
    all_ph = repo.list_metric("ph", limit=1000)
    assert len(all_ph["items"]) == 18
    assert next(i for i in all_ph["items"] if i["sample_id"] == 3)["value"] == 7.05


def test_api_before_cutover_reads_wide_table(engine, settings):
    repo = SampleRepository(settings)
    assert repo.get_metrics(1) == {"sample_id": 1, "temperature": 36.5, "ph": 7.2}
    assert repo.list_samples()["source"] == "wide_table"
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.update(conn, 1, temperature=36.55)
    finally:
        conn.close()
    assert repo.get_metrics(1)["temperature"] == 36.55


def test_cutover_preserves_late_updates_deletes_and_recreate(settings):
    """Writes that land while copying must win over the copied snapshot."""
    from app.migration import MigrationEngine

    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        # Make the dataset large enough that copying spans many chunks.
        for i in range(1000, 1200):
            legacy.insert(conn, sample_id=i, temperature=float(i % 50), ph=7.0)
    finally:
        conn.close()
    state, _ = eng.start()
    assert state["state"] in {"copying", "completed"}

    # Write concurrently while the copy loop is running.
    results: dict[str, object] = {}
    conn = legacy.open_db(settings.database_path)
    try:
        # corrected value on a low id (copied first)
        legacy.update(conn, 1, temperature=99.9)
        results["updated"] = legacy.select(conn, 1)
        # deleted sample on a low id
        legacy.delete(conn, 2)
        results["deleted_missing"] = legacy.select(conn, 2) is None
        # recreated same id after delete
        legacy.insert(conn, sample_id=2, temperature=11.1, ph=3.3)
        results["recreated"] = legacy.select(conn, 2)
        # brand new high id during migration
        new_id = legacy.insert(conn, temperature=22.2, ph=4.4)
        results["new_id"] = new_id
        results["new"] = legacy.select(conn, new_id)
    finally:
        conn.close()

    eng.wait_for_idle(60)
    assert eng.get_state()["state"] == "completed"

    rows = sample_rows(settings)
    assert rows[1] == (99.9, 7.2)
    assert rows[2] == (11.1, 3.3)
    assert rows[results["new_id"]] == (22.2, 4.4)
    assert len(rows) == 18 + 200 + 1 + 0  # +200 bulk, +1 new; id 2 recreated not added
    metrics = long_rows(settings)
    assert len(metrics) == len(rows) * 2
    counts: dict[int, int] = {}
    for m in metrics:
        counts[m["sample_id"]] = counts.get(m["sample_id"], 0) + 1
    assert set(counts.values()) == {2}
    assert set(counts) == set(rows)


def test_retry_after_failure_resumes(settings, monkeypatch):
    from app.migration import MigrationEngine

    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        for i in range(1000, 1050):
            legacy.insert(conn, sample_id=i, temperature=1.0, ph=2.0)
    finally:
        conn.close()

    original = MigrationEngine._cutover

    def broken(self):
        raise RuntimeError("simulated cutover failure")

    monkeypatch.setattr(MigrationEngine, "_cutover", broken)
    eng.start()
    eng.wait_for_idle(30)
    state = eng.get_state()
    assert state["state"] == "failed"
    assert "simulated cutover failure" in state["last_error"]
    # legacy still works on the wide table after the failure
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.update(conn, 1, ph=7.7)
    finally:
        conn.close()
    assert sample_rows(settings)[1] == (36.5, 7.7)

    monkeypatch.setattr(MigrationEngine, "_cutover", original)
    state2, started = eng.start()
    assert started is True
    assert state2["attempt"] == 2
    eng.wait_for_idle(30)
    final = eng.get_state()
    assert final["state"] == "completed"
    assert final["metric_rows"] == final["metric_rows_expected"]
    assert sample_rows(settings)[1] == (36.5, 7.7)


def test_interrupted_run_resumes_on_initialize(settings):
    """A crash mid-copy leaves the wide table usable and copies resume later."""
    from app.migration import MigrationEngine
    from app.schema import install_migration_triggers

    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        for i in range(1000, 1100):
            legacy.insert(conn, sample_id=i, temperature=float(i), ph=2.0)
    finally:
        conn.close()

    # Simulate a crash after some chunks: state copying + checkpoint, triggers
    # installed, then a legacy write committed during the outage.
    db = connect(settings)
    try:
        db.execute("BEGIN IMMEDIATE")
        install_migration_triggers(db)
        db.execute(
            "INSERT INTO migration_state (id, run_id, state, created_at,"
            " started_at, attempt, total_samples, samples_copied, last_id)"
            " VALUES (1,'run-x','copying','now','now',1,118,20,20)"
        )
        db.execute("COMMIT")
    finally:
        db.close()
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.update(conn, 1, temperature=88.8)
    finally:
        conn.close()

    # A fresh process (auto-resume enabled) picks the run up.
    resumed = MigrationEngine(
        type(settings)(
            database_path=settings.database_path,
            seed_database_path=settings.seed_database_path,
            chunk_size=7,
            chunk_delay_ms=0,
            busy_timeout_ms=10_000,
            auto_resume=True,
        )
    )
    resumed.initialize()
    resumed.wait_for_idle(60)
    state = resumed.get_state()
    assert state["state"] == "completed"
    rows = sample_rows(settings)
    assert rows[1] == (88.8, 7.2)
    assert len(rows) == 118
    assert len(long_rows(settings)) == 118 * 2


def test_every_query_sees_consistent_snapshot(migrated, settings):
    """During migration, API reads never see a half-copied long table."""
    from app.migration import MigrationEngine

    # Fresh DB with many rows so reads race the copy loop.
    s = type(settings)(
        database_path=settings.database_path.parent / "race.db",
        seed_database_path=settings.seed_database_path,
        chunk_size=5,
        chunk_delay_ms=1,
        busy_timeout_ms=10_000,
        auto_resume=False,
    )
    eng = MigrationEngine(s)
    eng.initialize()
    conn = legacy.open_db(s.database_path)
    try:
        for i in range(1000, 1300):
            legacy.insert(conn, sample_id=i, temperature=1.0, ph=2.0)
    finally:
        conn.close()
    repo = SampleRepository(s)

    errors: list[BaseException] = []

    def reader():
        try:
            for _ in range(40):
                page = repo.list_samples(limit=1000)
                assert page["count"] == 318
                for item in page["items"]:
                    assert item["temperature"] == (
                        1.0 if item["id"] >= 1000 else item["temperature"]
                    )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    eng.start()
    eng.wait_for_idle(60)
    for t in threads:
        t.join(30)
    assert errors == []
    assert eng.get_state()["state"] == "completed"


def test_late_chunk_does_not_overwrite_correction(settings):
    """Directly exercise copy-vs-trigger precedence at the SQL level."""
    from app.db import connect as db_connect
    from app.migration import MigrationEngine
    from app.schema import install_migration_triggers

    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.insert(conn, sample_id=900, temperature=10.0, ph=1.0)
    finally:
        conn.close()

    db = db_connect(settings)
    try:
        db.execute("BEGIN IMMEDIATE")
        install_migration_triggers(db)
        db.execute("COMMIT")
    finally:
        db.close()

    conn = legacy.open_db(settings.database_path)
    try:
        # Trigger mirrors the correction into the long table.
        legacy.update(conn, 900, temperature=20.0)
    finally:
        conn.close()

    # A stale copy of the old value (DO NOTHING) must not overwrite it.
    db = db_connect(settings)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO sample_metrics(sample_id, metric, value)"
            " VALUES(900,'temperature',10.0)"
            " ON CONFLICT(sample_id, metric) DO NOTHING"
        )
        db.execute("COMMIT")
        row = db.execute(
            "SELECT value FROM sample_metrics WHERE sample_id=900 AND metric='temperature'"
        ).fetchone()
        assert row["value"] == 20.0
    finally:
        db.close()


def test_legacy_sql_forms_after_cutover(migrated, settings):
    """Plain legacy SQL forms keep working through the compatibility view."""
    conn = legacy.open_db(settings.database_path)
    try:
        # SELECT with WHERE / ORDER BY / aggregate
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 18
        assert (
            conn.execute(
                "SELECT id FROM samples WHERE temperature IS NULL ORDER BY id"
            ).fetchall()[0][0]
            == 3
        )
        # multi-row UPDATE through a column expression
        conn.execute("UPDATE samples SET ph = ph + 0.1 WHERE id <= 3")
        assert abs(legacy.select(conn, 1)["ph"] - 7.3) < 1e-9
        # UPDATE with a subquery value
        conn.execute(
            "UPDATE samples SET temperature ="
            " (SELECT AVG(temperature) FROM samples) WHERE id = 1"
        )
        assert legacy.select(conn, 1)["temperature"] is not None
        # INSERT ... SELECT
        conn.execute("INSERT INTO samples (id, temperature, ph) SELECT 7001, 1.0, 2.0")
        assert legacy.select(conn, 7001) == {
            "id": 7001,
            "temperature": 1.0,
            "ph": 2.0,
        }
        # DELETE with a range predicate
        conn.execute("DELETE FROM samples WHERE id >= 7001")
        assert legacy.select(conn, 7001) is None
        # explicit transaction spanning several statements
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO samples (id, temperature, ph) VALUES (7002, 3.0, 4.0)"
        )
        conn.execute("UPDATE samples SET temperature = 3.5 WHERE id = 7002")
        conn.execute("COMMIT")
        assert legacy.select(conn, 7002)["temperature"] == 3.5
    finally:
        conn.close()
    # still exactly two metric rows per sample
    metrics = long_rows(settings)
    counts: dict[int, int] = {}
    for m in metrics:
        counts[m["sample_id"]] = counts.get(m["sample_id"], 0) + 1
    assert set(counts.values()) == {2}


def test_null_written_by_legacy_update_keeps_row(migrated, settings):
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.update(conn, 1, temperature=None, ph=None)
    finally:
        conn.close()
    assert sample_rows(settings)[1] == (None, None)
    rows = [m for m in long_rows(settings) if m["sample_id"] == 1]
    assert {m["metric"] for m in rows} == {"temperature", "ph"}
    assert all(m["value"] is None for m in rows)


def test_migration_of_empty_database(settings):
    from app.config import Settings

    empty = Settings(
        database_path=settings.database_path.parent / "empty.db",
        seed_database_path=None,
        chunk_size=4,
        chunk_delay_ms=0,
        busy_timeout_ms=10_000,
        auto_resume=False,
    )
    from app.migration import MigrationEngine

    eng = MigrationEngine(empty)
    eng.initialize()
    _state, started = eng.start()
    assert started is True
    eng.wait_for_idle(30)
    final = eng.get_state()
    assert final["state"] == "completed"
    assert final["samples_total"] == 0
    assert final["metric_rows"] == 0
    conn = legacy.open_db(empty.database_path)
    try:
        legacy.insert(conn, temperature=1.0, ph=2.0)
        assert legacy.select(conn, 1) == {"id": 1, "temperature": 1.0, "ph": 2.0}
    finally:
        conn.close()


def test_externally_cut_over_database_is_not_migrated_twice(settings):
    """If samples is already a view, a POST must only record completion."""
    from app.db import connect as db_connect
    from app.migration import MigrationEngine
    from app.schema import create_base_schema, replace_wide_table_with_view

    eng = MigrationEngine(settings)
    eng.initialize()
    conn = legacy.open_db(settings.database_path)
    try:
        legacy.insert(conn, sample_id=100, temperature=1.0, ph=2.0)
    finally:
        conn.close()

    db = db_connect(settings)
    try:
        db.execute("BEGIN IMMEDIATE")
        create_base_schema(db)
        db.execute(
            "INSERT INTO sample_metrics(sample_id, metric, value)"
            " VALUES(100,'temperature',1.0),(100,'ph',2.0)"
        )
        db.execute("DROP TABLE samples")
        replace_wide_table_with_view(db)
        db.execute("COMMIT")
    finally:
        db.close()

    before = long_rows(settings)
    state, started = eng.start()
    assert started is False
    assert state["state"] == "completed"
    assert state["view_installed"] is True
    assert long_rows(settings) == before
    events = [e["event"] for e in eng.events()]
    assert "recovered_completed" in events


def test_update_id_to_null_rejected_like_wide_table(migrated, settings):
    conn = legacy.open_db(settings.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE samples SET id = NULL WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE samples SET id = 2 WHERE id = 1")
    finally:
        conn.close()
    assert sample_rows(settings)[1] == (36.5, 7.2)
