"""Offline re-send import tests: revisions, idempotency, atomic rejection."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import legacy.acquisition as legacy
from app.config import Settings
from app.db import connect
from app.imports import ImportBatchRequest, ImportService
from app.main import create_app
from app.migration import MigrationEngine


def row(
    device_no: Any = "DEV-001",
    source_no: Any = "S-1001",
    revision: Any = 1,
    temperature: Any = 36.5,
    ph: Any = 7.2,
) -> dict[str, Any]:
    return {
        "device_no": device_no,
        "source_no": source_no,
        "revision": revision,
        "temperature": temperature,
        "ph": ph,
    }


def post(client: TestClient, rows: list[dict[str, Any]]) -> Any:
    return client.post("/imports/offline", json={"rows": rows})


def import_records(settings: Settings) -> dict[tuple[str, str], dict[str, Any]]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM import_records ORDER BY device_no, source_no"
        ).fetchall()
        return {(r["device_no"], r["source_no"]): dict(r) for r in rows}
    finally:
        conn.close()


def sample(settings: Settings, sample_id: int) -> dict[str, Any] | None:
    conn = connect(settings)
    try:
        found = conn.execute(
            "SELECT id, temperature, ph FROM samples WHERE id = ?", (sample_id,)
        ).fetchone()
        return dict(found) if found is not None else None
    finally:
        conn.close()


def all_samples(settings: Settings) -> dict[int, tuple[float | None, float | None]]:
    conn = connect(settings)
    try:
        return {
            r["id"]: (r["temperature"], r["ph"])
            for r in conn.execute("SELECT id, temperature, ph FROM samples")
        }
    finally:
        conn.close()


@pytest.fixture()
def client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture()
def migrated_client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as c:
        c.post("/migrations")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (
                c.get("/migrations/current").json()["migration"]["state"]
                == "completed"
            ):
                break
            time.sleep(0.02)
        else:  # pragma: no cover - slow environment
            raise AssertionError("migration did not complete")
        yield c


# ---------------------------------------------------------------- happy path
def test_create_assigns_new_sample_ids(client, settings):
    resp = post(
        client,
        [
            row(source_no="S-1", temperature=36.5, ph=7.2),
            row(device_no="DEV-002", source_no="S-1", temperature=37.1, ph=6.9),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert (body["created"], body["replaced"], body["unchanged"], body["rejected"]) == (
        2,
        0,
        0,
        0,
    )
    first, second = body["rows"]
    # Seed database has 18 rows, highest id 42.
    assert first["sample_id"] == 43
    assert second["sample_id"] == 44
    assert first["status"] == second["status"] == "created"
    assert first["previous_revision"] is None
    assert client.get("/samples/43/metrics").json() == {
        "sample_id": 43,
        "temperature": 36.5,
        "ph": 7.2,
    }
    assert client.get("/samples/44/metrics").json() == {
        "sample_id": 44,
        "temperature": 37.1,
        "ph": 6.9,
    }
    records = import_records(settings)
    assert records[("DEV-001", "S-1")]["sample_id"] == 43
    assert records[("DEV-002", "S-1")]["sample_id"] == 44


def test_same_revision_same_values_is_idempotent(client, settings):
    payload = [row(revision=4)]
    first = post(client, payload).json()
    sample_id = first["rows"][0]["sample_id"]
    before = all_samples(settings)

    again = post(client, payload).json()
    assert again["status"] == "accepted"
    assert again["unchanged"] == 1
    assert again["rows"][0]["status"] == "unchanged"
    assert again["rows"][0]["sample_id"] == sample_id
    assert again["rows"][0]["previous_revision"] == 4
    assert all_samples(settings) == before
    assert import_records(settings)[("DEV-001", "S-1001")]["revision"] == 4


def test_higher_revision_replaces_the_whole_group(client, settings):
    created = post(client, [row(revision=1, temperature=36.5, ph=7.2)]).json()
    sample_id = created["rows"][0]["sample_id"]

    replaced = post(client, [row(revision=2, temperature=36.9, ph=6.8)]).json()
    assert replaced["status"] == "accepted"
    assert replaced["replaced"] == 1
    result = replaced["rows"][0]
    assert result["status"] == "replaced"
    assert result["sample_id"] == sample_id
    assert result["previous_revision"] == 1
    assert client.get(f"/samples/{sample_id}/metrics").json() == {
        "sample_id": sample_id,
        "temperature": 36.9,
        "ph": 6.8,
    }
    # Both values were replaced, not merged with the earlier revision.
    assert sample(settings, sample_id) == {
        "id": sample_id,
        "temperature": 36.9,
        "ph": 6.8,
    }


def test_fractional_revision_order_is_numeric(client):
    post(client, [row(revision=2)])
    body = post(client, [row(revision=10, temperature=36.6)]).json()
    assert body["status"] == "accepted"
    assert body["replaced"] == 1


# --------------------------------------------------------------- rejections
def test_stale_revision_rejects_whole_batch_without_changes(client, settings):
    post(client, [row(revision=5, temperature=36.5, ph=7.2)])
    before = all_samples(settings)
    stored = import_records(settings)

    resp = post(
        client,
        [
            # A brand-new source that would otherwise be accepted.
            row(device_no="DEV-003", source_no="N-1", revision=1),
            # Older revision for the existing source.
            row(revision=4, temperature=30.0, ph=5.0),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["rejected"] == 2
    assert body["created"] == body["replaced"] == body["unchanged"] == 0
    assert body["rows"][0]["code"] == "batch_rejected"
    assert body["rows"][1]["code"] == "stale_revision"
    assert body["rows"][1]["previous_revision"] == 5

    assert all_samples(settings) == before
    assert import_records(settings) == stored
    # DEV-003/N-1 would have been sample 44 (43 was taken by the accepted row).
    assert client.get("/samples/44/metrics").status_code == 404


def test_same_revision_different_values_rejects_whole_batch(client, settings):
    post(client, [row(revision=3, temperature=36.5, ph=7.2)])
    before = all_samples(settings)

    body = post(client, [row(revision=3, temperature=36.5, ph=7.3)]).json()
    assert body["status"] == "rejected"
    assert body["rows"][0]["status"] == "rejected"
    assert body["rows"][0]["code"] == "same_revision_different_values"
    assert all_samples(settings) == before


@pytest.mark.parametrize(
    "second",
    [
        row(revision=3, temperature=36.5, ph=7.3),  # same revision, other pH
        row(revision=2, temperature=36.5, ph=7.2),  # older revision, same values
    ],
)
def test_rejections_are_atomic_even_if_one_row_is_fine(client, settings, second):
    post(client, [row(revision=3)])
    before = all_samples(settings)
    body = post(
        client,
        [
            row(device_no="DEV-900", source_no="fresh", revision=1),
            second,
        ],
    ).json()
    assert body["status"] == "rejected"
    assert body["rows"][0]["code"] == "batch_rejected"
    assert all_samples(settings) == before


def test_duplicate_source_in_batch_rejects_everything(client, settings):
    resp = post(
        client,
        [
            row(source_no="DUP", revision=1, temperature=1.0),
            row(source_no="DUP", revision=2, temperature=2.0),
            row(source_no="OK", revision=1),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["rejected"] == 3
    assert [r["code"] for r in body["rows"]] == [
        "duplicate_source_in_batch",
        "duplicate_source_in_batch",
        "batch_rejected",
    ]
    assert all_samples(settings) == {
        r["id"]: (r["temperature"], r["ph"])
        for r in connect(settings)
        .execute("SELECT id, temperature, ph FROM samples")
        .fetchall()
    }
    assert import_records(settings) == {}
    assert 43 not in all_samples(settings)


def test_duplicate_after_previously_accepted_source(client, settings):
    created = post(client, [row(source_no="DUP", revision=2)]).json()
    sample_id = created["rows"][0]["sample_id"]
    before = all_samples(settings)

    body = post(
        client,
        [
            row(source_no="DUP", revision=3, temperature=40.0),
            row(source_no="DUP", revision=4, temperature=41.0),
        ],
    ).json()
    assert body["status"] == "rejected"
    assert body["rows"][0]["sample_id"] == sample_id
    assert body["rows"][1]["sample_id"] == sample_id
    assert all_samples(settings) == before


def test_key_normalization_keeps_one_identity(client, settings):
    first = post(client, [row(device_no=12, source_no=100, revision=1)]).json()
    sample_id = first["rows"][0]["sample_id"]
    assert first["rows"][0]["device_no"] == "12"
    assert first["rows"][0]["source_no"] == "100"

    second = post(
        client,
        [row(device_no=" 12 ", source_no=" 100 ", revision=2, temperature=37.0)],
    ).json()
    assert second["status"] == "accepted"
    assert second["rows"][0]["status"] == "replaced"
    assert second["rows"][0]["sample_id"] == sample_id
    assert len(import_records(settings)) == 1


# --------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "bad_row",
    [
        row(revision=0),
        row(revision=-1),
        row(revision=1.0),
        row(revision="1"),
        row(revision=True),
        row(revision=None),
        row(temperature="36.5"),
        row(temperature=True),
        row(temperature=None),
        row(ph=None),
        row(device_no=""),
        row(device_no="   "),
        row(device_no="x" * 129),
        row(device_no=True),
        row(source_no=1.5),
        {k: v for k, v in row().items() if k != "ph"},
        {**row(), "unexpected": 1},
    ],
)
def test_invalid_rows_are_422(client, bad_row):
    assert post(client, [bad_row]).status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"rows": []},
        {"rows": "not-a-list"},
        {},
        {"rows": [row()] * 1001},
    ],
)
def test_invalid_batches_are_422(client, payload):
    assert client.post("/imports/offline", json=payload).status_code == 422


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_values_rejected(client, literal):
    # Standard JSON has no NaN/Infinity, but Python's parser accepts the
    # literals; they must still be rejected by validation.
    payload = (
        '{"rows": [{"device_no": "DEV-001", "source_no": "S-1001",'
        ' "revision": 1, "temperature": ' + literal + ', "ph": 7.2}]}'
    )
    resp = client.post(
        "/imports/offline",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_validation_failure_changes_nothing(client, settings):
    post(client, [row(revision=2)])
    before = all_samples(settings)
    bad_batch = [row(revision=1, temperature=30.0), row(revision="x")]
    assert post(client, bad_batch).status_code == 422
    assert all_samples(settings) == before


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        '{"rows": "x"}',
        '{"rows": [{"device_no": "D", "source_no": "S", "revision": 1,'
        ' "temperature": 36.5, "ph": 7.2}]} trailing',
    ],
)
def test_malformed_json_is_422(client, payload):
    resp = client.post(
        "/imports/offline",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_batch_size_limit_is_enforced(client):
    allowed = [row(source_no=f"S-{i}") for i in range(1000)]
    body = post(client, allowed).json()
    assert body["status"] == "accepted"
    assert body["created"] == 1000
    too_many = [row(source_no=f"T-{i}") for i in range(1001)]
    assert post(client, too_many).status_code == 422


# ------------------------------------------------------- migration interplay
def test_import_before_and_after_cutover(migrated_client, settings):
    # The client fixture only imports after cutover; do an import on the
    # migrated database and verify both the compat view and the long table.
    body = post(
        migrated_client,
        [row(source_no="AFTER", revision=1, temperature=36.25, ph=7.15)],
    ).json()
    sample_id = body["rows"][0]["sample_id"]
    assert sample_id == 43
    assert migrated_client.get(f"/samples/{sample_id}/metrics").json() == {
        "sample_id": sample_id,
        "temperature": 36.25,
        "ph": 7.15,
    }
    listed = migrated_client.get("/metrics/temperature", params={"limit": 1000}).json()
    assert listed["items"][-1] == {"sample_id": sample_id, "value": 36.25}

    replaced = post(
        migrated_client,
        [row(source_no="AFTER", revision=2, temperature=36.45, ph=7.05)],
    ).json()
    assert replaced["rows"][0]["status"] == "replaced"
    assert replaced["rows"][0]["sample_id"] == sample_id
    conn = connect(settings)
    try:
        values = {
            r["metric"]: r["value"]
            for r in conn.execute(
                "SELECT metric, value FROM sample_metrics WHERE sample_id = ?",
                (sample_id,),
            )
        }
    finally:
        conn.close()
    assert values == {"temperature": 36.45, "ph": 7.05}


def test_import_while_migration_is_copying(settings):
    fast = replace(settings, chunk_size=2, chunk_delay_ms=1)
    engine = MigrationEngine(fast)
    engine.initialize()
    conn = legacy.open_db(fast.database_path)
    try:
        for i in range(1000, 1200):
            legacy.insert(conn, sample_id=i, temperature=1.0, ph=2.0)
    finally:
        conn.close()

    engine.start()
    importer = ImportService(fast)
    body = importer.import_batch(
        ImportBatchRequest(
            rows=[
                {
                    "device_no": "DEV-M",
                    "source_no": "S-M",
                    "revision": 1,
                    "temperature": 36.7,
                    "ph": 7.3,
                }
            ]
        )
    )
    assert body.status == "accepted"
    sample_id = body.rows[0].sample_id
    assert sample_id == 1200

    engine.wait_for_idle(60)
    state = engine.get_state()
    assert state["state"] == "completed"
    assert sample(fast, sample_id) == {
        "id": sample_id,
        "temperature": 36.7,
        "ph": 7.3,
    }

    # A later revision after cutover keeps the same sample id.
    body = importer.import_batch(
        ImportBatchRequest(
            rows=[
                {
                    "device_no": "DEV-M",
                    "source_no": "S-M",
                    "revision": 2,
                    "temperature": 36.9,
                    "ph": 7.1,
                }
            ]
        )
    )
    assert body.rows[0].status == "replaced"
    assert body.rows[0].sample_id == sample_id
    assert sample(fast, sample_id)["temperature"] == 36.9


@pytest.mark.parametrize("migrated_first", [False, True])
def test_deleted_sample_is_recreated_under_first_sample_id(
    settings, engine, migrated_first
):
    """A legacy DELETE must not break the stable source -> sample mapping."""
    if migrated_first:
        engine.start()
        engine.wait_for_idle(60)
        assert engine.get_state()["state"] == "completed"

    importer = ImportService(settings)
    created = importer.import_batch(
        ImportBatchRequest(
            rows=[
                {
                    "device_no": "DEV-D",
                    "source_no": "S-D",
                    "revision": 1,
                    "temperature": 36.5,
                    "ph": 7.2,
                }
            ]
        )
    )
    sample_id = created.rows[0].sample_id
    assert sample_id is not None

    conn = legacy.open_db(settings.database_path)
    try:
        legacy.delete(conn, sample_id)
        assert legacy.select(conn, sample_id) is None
    finally:
        conn.close()

    replaced = importer.import_batch(
        ImportBatchRequest(
            rows=[
                {
                    "device_no": "DEV-D",
                    "source_no": "S-D",
                    "revision": 2,
                    "temperature": 36.8,
                    "ph": 7.1,
                }
            ]
        )
    )
    assert replaced.status == "accepted"
    assert replaced.rows[0].status == "replaced"
    assert replaced.rows[0].sample_id == sample_id
    assert sample(settings, sample_id) == {
        "id": sample_id,
        "temperature": 36.8,
        "ph": 7.1,
    }
    # Ids are not recycled: the next new source gets a fresh, higher id.
    fresh = importer.import_batch(
        ImportBatchRequest(
            rows=[
                {
                    "device_no": "DEV-D",
                    "source_no": "S-E",
                    "revision": 1,
                    "temperature": 36.0,
                    "ph": 7.0,
                }
            ]
        )
    )
    assert fresh.rows[0].sample_id == sample_id + 1


def test_import_does_not_disturb_existing_rows(client, settings):
    before = all_samples(settings)
    body = post(client, [row(source_no="KEEP", revision=1)]).json()
    assert body["status"] == "accepted"
    after = all_samples(settings)
    new_id = body["rows"][0]["sample_id"]
    assert {k: v for k, v in after.items() if k != new_id} == before
    # Existing id 1 is untouched.
    assert after[1] == before[1] == (36.5, 7.2)


# ---------------------------------------------------------------- concurrency
class GatedImporter(ImportService):
    """Pauses one import between planning and commit to force commit order."""

    def __init__(self, settings: Settings, block_revision: int):
        super().__init__(settings)
        self.block_revision = block_revision
        self.reached = threading.Event()
        self.release = threading.Event()

    def _apply(self, conn, rows, results):  # type: ignore[no-untyped-def]
        if rows[0].revision == self.block_revision and not self.release.is_set():
            self.reached.set()
            self.release.wait(10)
        super()._apply(conn, rows, results)


def _run_import(
    importer: ImportService, revision: int, temperature: float
) -> tuple[str, Any]:
    resp = importer.import_batch(
        ImportBatchRequest(
            rows=[
                {
                    "device_no": "DEV-C",
                    "source_no": "S-C",
                    "revision": revision,
                    "temperature": temperature,
                    "ph": 7.0,
                }
            ]
        )
    )
    return resp.status, resp.rows[0]


def test_commit_order_decides_between_concurrent_revisions(settings, engine):
    """A higher revision that commits first wins; the later lower one is stale."""
    higher = GatedImporter(settings, block_revision=2)
    lower = GatedImporter(settings, block_revision=1)

    outcome: dict[str, Any] = {}

    def run_higher():
        outcome["higher"] = _run_import(higher, 2, 37.0)

    high_thread = threading.Thread(target=run_higher)
    high_thread.start()
    assert higher.reached.wait(10), "higher revision never reached its commit point"

    low_thread = threading.Thread(
        target=lambda: outcome.update(lower=_run_import(lower, 1, 30.0))
    )
    low_thread.start()
    # The lower revision cannot enter its transaction while the higher one
    # holds the database write lock, so it cannot yet reach the gate.
    lower_entered_early = lower.reached.wait(0.3)
    higher.release.set()

    high_thread.join(15)
    # Defensive: if the lower import somehow reached its gate, do not hang.
    lower.release.set()
    low_thread.join(15)
    assert not high_thread.is_alive() and not low_thread.is_alive()
    assert lower_entered_early is False

    high_status, high_row = outcome["higher"]
    low_status, low_row = outcome["lower"]
    assert (high_status, high_row.status) == ("accepted", "created")
    assert low_status == "rejected"
    assert low_row.code == "stale_revision"
    assert low_row.sample_id == high_row.sample_id
    assert sample(settings, high_row.sample_id)["temperature"] == 37.0


def test_commit_order_allows_later_higher_revision(settings, engine):
    """Same race the other way: revision 1 commits, then revision 2 replaces it."""
    lower = GatedImporter(settings, block_revision=1)
    higher = GatedImporter(settings, block_revision=2)
    outcome: dict[str, Any] = {}

    def run_lower():
        outcome["lower"] = _run_import(lower, 1, 30.0)

    low_thread = threading.Thread(target=run_lower)
    low_thread.start()
    assert lower.reached.wait(10)

    high_thread = threading.Thread(
        target=lambda: outcome.update(higher=_run_import(higher, 2, 37.0))
    )
    high_thread.start()
    higher_entered_early = higher.reached.wait(0.3)
    lower.release.set()

    low_thread.join(15)
    # Defensive: if the higher import reached its gate, do not hang.
    higher.release.set()
    high_thread.join(15)
    assert not low_thread.is_alive() and not high_thread.is_alive()
    assert higher_entered_early is False

    low_status, low_row = outcome["lower"]
    high_status, high_row = outcome["higher"]
    assert (low_status, low_row.status) == ("accepted", "created")
    assert (high_status, high_row.status) == ("accepted", "replaced")
    assert high_row.sample_id == low_row.sample_id
    assert sample(settings, low_row.sample_id) == {
        "id": low_row.sample_id,
        "temperature": 37.0,
        "ph": 7.0,
    }


def test_racing_imports_converge_on_highest_revision(settings, engine):
    importer = ImportService(settings)
    revisions = list(range(1, 13))
    barrier = threading.Barrier(len(revisions))
    outcomes: list[tuple[int, str, Any]] = []
    lock = threading.Lock()

    def worker(revision: int):
        barrier.wait(10)
        status, result = _run_import(importer, revision, float(revision) * 10)
        with lock:
            outcomes.append((revision, status, result))

    threads = [threading.Thread(target=worker, args=(r,)) for r in revisions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(not t.is_alive() for t in threads)
    assert len(outcomes) == len(revisions)

    accepted = [o for o in outcomes if o[1] == "accepted"]
    created = [o for o in outcomes if o[2].status == "created"]
    assert len(created) == 1
    assert accepted, "at least the highest revision must be accepted"
    sample_ids = {
        o[2].sample_id for o in outcomes if o[2].sample_id is not None
    }
    assert len(sample_ids) == 1
    sample_id = sample_ids.pop()

    record = import_records(settings)[("DEV-C", "S-C")]
    assert record["sample_id"] == sample_id
    assert record["revision"] == max(revisions)
    assert record["temperature"] == 120.0
    assert sample(settings, sample_id) == {
        "id": sample_id,
        "temperature": 120.0,
        "ph": 7.0,
    }
    # Exactly one new sample was added to the 18 seed rows.
    assert len(all_samples(settings)) == 19


def test_concurrent_distinct_sources_get_unique_ids(settings, engine):
    importer = ImportService(settings)
    count = 20
    barrier = threading.Barrier(count)
    sample_ids: list[int] = []
    lock = threading.Lock()

    def worker(n: int):
        barrier.wait(10)
        resp = importer.import_batch(
            ImportBatchRequest(
                rows=[
                    {
                        "device_no": "DEV-P",
                        "source_no": f"S-{n}",
                        "revision": 1,
                        "temperature": 36.0 + n / 100,
                        "ph": 7.0,
                    }
                ]
            )
        )
        assert resp.status == "accepted"
        with lock:
            sample_ids.append(resp.rows[0].sample_id)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(sample_ids) == count
    assert len(set(sample_ids)) == count
    assert len(all_samples(settings)) == 18 + count
