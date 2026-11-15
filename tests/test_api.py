"""API tests (FastAPI TestClient against the real SQLite database)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import legacy.acquisition as legacy
from app.main import create_app


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def wait_for_migration(client: TestClient, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/migrations/current").json()["migration"]
        if state["state"] in {"completed", "failed"}:
            return state
        time.sleep(0.02)
    raise AssertionError(f"migration did not finish: {state}")


def test_health_and_not_started(client):
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["migration_state"] == "not_started"
    assert (
        client.get("/migrations/current").json()["migration"]["state"] == "not_started"
    )


def test_start_and_poll_migration(client):
    resp = client.post("/migrations")
    assert resp.status_code == 202
    body = resp.json()
    assert body["action"] == "started"
    assert body["migration"]["state"] in {"copying", "completed"}

    state = wait_for_migration(client)
    assert state["state"] == "completed"
    assert state["progress"] == 1.0
    assert state["metric_rows"] == state["metric_rows_expected"] == 36
    assert state["view_installed"] is True

    events = client.get("/migrations/current/events").json()["events"]
    assert [e["event"] for e in events] == ["started", "cutover_completed"]


def test_repeat_start_is_idempotent(client):
    client.post("/migrations")
    wait_for_migration(client)
    again = client.post("/migrations").json()
    assert again["action"] == "reused"
    assert again["migration"]["state"] == "completed"

    metrics_before = client.get("/metrics/temperature", params={"limit": 1000}).json()
    client.post("/migrations")
    metrics_after = client.get("/metrics/temperature", params={"limit": 1000}).json()
    assert metrics_before["items"] == metrics_after["items"]


def test_sample_metrics_endpoint(client):
    client.post("/migrations")
    wait_for_migration(client)
    assert client.get("/samples/1/metrics").json() == {
        "sample_id": 1,
        "temperature": 36.5,
        "ph": 7.2,
    }
    assert client.get("/samples/7/metrics").json() == {
        "sample_id": 7,
        "temperature": None,
        "ph": None,
    }
    assert client.get("/samples/999/metrics").status_code == 404


def test_metric_endpoint_pagination_and_validation(client):
    client.post("/migrations")
    wait_for_migration(client)
    first = client.get("/metrics/ph", params={"limit": 3}).json()
    assert first["metric"] == "ph"
    assert first["count"] == 3
    assert first["has_more"] is True
    assert [i["sample_id"] for i in first["items"]] == [1, 2, 3]

    seen = []
    cursor = None
    while True:
        params = {"limit": 4}
        if cursor is not None:
            params["after_id"] = cursor
        page = client.get("/metrics/ph", params=params).json()
        seen.extend(i["sample_id"] for i in page["items"])
        cursor = page["next_after_id"]
        if not page["has_more"]:
            break
    assert seen == sorted(seen)
    assert len(seen) == 18

    assert client.get("/metrics/humidity").status_code == 422
    assert client.get("/metrics/ph", params={"limit": 0}).status_code == 422


def test_legacy_writes_visible_through_api(client, settings):
    client.post("/migrations")
    wait_for_migration(client)
    conn = legacy.open_db(settings.database_path)
    try:
        new_id = legacy.insert(conn, temperature=35.0, ph=7.0)
        legacy.update(conn, 1, ph=None)
        legacy.delete(conn, 2)
    finally:
        conn.close()

    assert client.get(f"/samples/{new_id}/metrics").json()["temperature"] == 35.0
    assert client.get("/samples/1/metrics").json()["ph"] is None
    assert client.get("/samples/2/metrics").status_code == 404
    listed = client.get("/metrics/temperature", params={"limit": 1000}).json()
    assert new_id in [i["sample_id"] for i in listed["items"]]
    assert 2 not in [i["sample_id"] for i in listed["items"]]


def test_api_before_migration_serves_wide_table(client):
    body = client.get("/samples/1/metrics").json()
    assert body == {"sample_id": 1, "temperature": 36.5, "ph": 7.2}
    listed = client.get("/samples").json()
    assert listed["source"] == "wide_table"
    assert listed["count"] == 18


def test_metrics_index_and_validation(client):
    index = client.get("/metrics").json()
    assert [m["metric"] for m in index["metrics"]] == ["temperature", "ph"]
    assert client.get("/samples/abc/metrics").status_code == 422
