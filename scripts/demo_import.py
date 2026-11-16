#!/usr/bin/env python3
"""Demo the offline re-send import API against a running service.

    docker compose exec migration-api python scripts/demo_import.py
    python scripts/demo_import.py --api http://127.0.0.1:8000

It walks through: first upload (created), identical retransmit (unchanged),
higher revision (replaced, same sample_id), stale revision (whole batch
rejected) and a duplicate source number inside one batch (whole batch
rejected).  Works before and after the wide-to-long migration.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def post(api: str, rows: list[dict]) -> tuple[int, dict]:
    data = json.dumps({"rows": rows}).encode()
    req = urllib.request.Request(
        f"{api}/imports/offline",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def show(title: str, status: int, body: dict) -> None:
    print(f"\n{title}  -> HTTP {status} status={body.get('status')}")
    for row in body.get("rows", []):
        print(
            f"  #{row['index']} {row['device_no']}/{row['source_no']}"
            f" rev={row['revision']}"
            f" t={row['temperature']} ph={row['ph']}"
            f" => {row['status']} sample_id={row['sample_id']}"
            + (f" code={row['code']}" if row["code"] else "")
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    upload = [
        {
            "device_no": "DEMO-DEV-01",
            "source_no": "DEMO-SRC-1001",
            "revision": 1,
            "temperature": 36.5,
            "ph": 7.2,
        },
        {
            "device_no": "DEMO-DEV-01",
            "source_no": "DEMO-SRC-1002",
            "revision": 3,
            "temperature": 36.8,
            "ph": 7.1,
        },
    ]
    show("1) first upload", *post(args.api, upload))
    show("2) identical retransmit (idempotent)", *post(args.api, upload))

    newer = [
        {
            "device_no": "DEMO-DEV-01",
            "source_no": "DEMO-SRC-1001",
            "revision": 2,
            "temperature": 36.9,
            "ph": 6.95,
        }
    ]
    show("3) revision 2 replaces the whole group", *post(args.api, newer))

    stale = [
        {
            "device_no": "DEMO-DEV-02",
            "source_no": "DEMO-SRC-2001",
            "revision": 1,
            "temperature": 36.0,
            "ph": 7.0,
        },
        {
            "device_no": "DEMO-DEV-01",
            "source_no": "DEMO-SRC-1001",
            "revision": 1,
            "temperature": 30.0,
            "ph": 5.0,
        },
    ]
    show("4) stale revision rejects the whole batch", *post(args.api, stale))

    duplicate = [
        {
            "device_no": "DEMO-DEV-03",
            "source_no": "DEMO-SRC-3001",
            "revision": 1,
            "temperature": 36.0,
            "ph": 7.0,
        },
        {
            "device_no": "DEMO-DEV-03",
            "source_no": "DEMO-SRC-3001",
            "revision": 2,
            "temperature": 36.1,
            "ph": 7.0,
        },
    ]
    show("5) duplicate source in batch", *post(args.api, duplicate))

    print("\nInspect the imported samples (ids from step 1):")
    for source in ("DEMO-SRC-1001", "DEMO-SRC-1002"):
        print(f"  use GET /samples/{{sample_id}}/metrics for {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
