#!/usr/bin/env python3
"""End-to-end demo: migrate the wide table while the legacy program keeps working.

Runs against a running API (see README).  It uses only HTTP for API calls and
plain SQL through ``legacy.acquisition`` for the old program.

    python scripts/demo.py --db data/lab.db --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import legacy.acquisition as legacy


def call(method: str, url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_done(api: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, body = call("GET", f"{api}/migrations/current")
        state = body["migration"]
        if state["state"] in {"completed", "failed"}:
            return state
        print(
            f"  copying: {state.get('samples_copied')}/{state.get('total_samples')}"
            f"  metric_rows={state.get('metric_rows')}"
        )
        time.sleep(0.5)
    raise SystemExit("timed out waiting for migration")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/lab.db")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"database not found: {db} (start the API first)")

    print("1) before migration, read the wide table through the old program")
    conn = legacy.open_db(db)
    try:
        print("   ", legacy.select(conn, 1))
    finally:
        conn.close()

    print("2) start migration")
    status, body = call("POST", f"{args.api}/migrations")
    print(
        f"   HTTP {status} action={body['action']} state={body['migration']['state']}"
    )

    print("3) while copying, old program keeps writing")
    conn = legacy.open_db(db)
    try:
        # a correction on a row that may already have been copied
        legacy.update(conn, 1, temperature=37.25)
        # a delete plus a same-id recreate
        legacy.delete(conn, 3)
        legacy.insert(conn, sample_id=3, temperature=36.85, ph=7.02)
        # a fresh sample
        new_id = legacy.insert(conn, temperature=36.3, ph=7.36)
        print(f"    corrected id=1, recreated id=3, inserted id={new_id}")
    finally:
        conn.close()

    print("4) wait for cutover")
    state = wait_done(args.api)
    print(
        f"   state={state['state']} progress={state['progress']}"
        f" metric_rows={state['metric_rows']}"
        f" expected={state['metric_rows_expected']}"
    )
    if state["state"] != "completed":
        print(json.dumps(state, indent=2))
        return 1

    print("5) after cutover: old SELECT, new API and writes")
    conn = legacy.open_db(db)
    try:
        print("    legacy SELECT id=1 :", legacy.select(conn, 1))
        print("    legacy SELECT id=3 :", legacy.select(conn, 3))
        print(f"    legacy SELECT id={new_id}:", legacy.select(conn, new_id))
        legacy.insert(conn, sample_id=5000, temperature=35.0, ph=7.5)
    finally:
        conn.close()

    for path in (
        "/samples/1/metrics",
        "/samples/3/metrics",
        f"/samples/{new_id}/metrics",
        "/samples/5000/metrics",
        "/metrics/temperature?limit=3",
        "/migrations/current",
    ):
        status, body = call("GET", f"{args.api}{path}")
        print(
            f"    GET {path} -> {status} {json.dumps(body, ensure_ascii=False)[:160]}"
        )

    print("6) re-posting the migration is a no-op")
    status, body = call("POST", f"{args.api}/migrations")
    print(
        f"   HTTP {status} action={body['action']} state={body['migration']['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
