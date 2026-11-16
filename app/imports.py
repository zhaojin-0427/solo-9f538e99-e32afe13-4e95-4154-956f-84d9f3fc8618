"""Revision-aware batch import of offline instrument readings.

Offline instruments re-send measurements that were taken while they could not
reach the lab.  Every row therefore carries a stable source identity
``(device_no, source_no)`` and a positive ``revision``.  The first accepted row
for a source identity gets a ``sample_id``; every later row for the same
identity keeps that ``sample_id``.

Version rules (evaluated against the last *accepted import* for the source
identity):

* higher revision  -> replace the whole group (temperature AND pH),
* equal revision + equal values -> idempotent no-op,
* equal revision + different values -> reject the whole batch,
* lower revision -> reject the whole batch.

A batch is atomic: one bad row rejects every row and leaves committed data
untouched.  Duplicate ``(device_no, source_no)`` inside a batch is also a
whole-batch rejection.

Concurrency
===========
Version decisions are made inside ``BEGIN IMMEDIATE``.  SQLite has a single
writer, so transactions acquire the write lock (and therefore commit) in one
serial order.  Each request reads the state committed by the request before
it, which makes "database commit order" the version order: if revision 2
commits before revision 3, revision 3 replaces it; if revision 3 commits
first, the later revision-2 request is stale and is rejected.

The import also works before and after the wide-to-long migration: it writes
through the name ``samples``, which is the legacy table before cutover and the
compatibility view afterwards.  Schema detection happens *after* acquiring
the write lock, so a concurrent cutover cannot slip between the check and the
write.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from .config import Settings
from .db import connect, transaction

MAX_IMPORT_BATCH = 1000
MAX_KEY_LENGTH = 128
_KEY_LOOKUP_CHUNK = 400

RowStatus = Literal["created", "replaced", "unchanged", "rejected"]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _normalize_key(value: Any) -> str:
    """Accept strings or integer device/source numbers, trim, reject the rest."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("must be a string or an integer")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ValueError("must be a string or an integer")
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(f"must be at most {MAX_KEY_LENGTH} characters")
    return value


SourceKey = Annotated[
    str,
    BeforeValidator(_normalize_key),
    Field(min_length=1, max_length=MAX_KEY_LENGTH),
]
FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Revision = Annotated[int, Field(strict=True, ge=1)]


class ImportRow(BaseModel):
    """One offline measurement."""

    model_config = ConfigDict(extra="forbid")

    device_no: SourceKey = Field(
        description="设备号；字符串或整数，会去除首尾空白",
        examples=["DEV-001"],
    )
    source_no: SourceKey = Field(
        description="源记录号；在设备号内唯一，字符串或整数，会去除首尾空白",
        examples=["S-1001"],
    )
    revision: Revision = Field(
        description="正整数修订号；越大越新",
        examples=[3],
    )
    temperature: FiniteFloat = Field(description="温度读数；有限数值", examples=[36.5])
    ph: FiniteFloat = Field(description="pH 读数；有限数值", examples=[7.2])


class ImportBatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "rows": [
                        {
                            "device_no": "DEV-001",
                            "source_no": "S-1001",
                            "revision": 1,
                            "temperature": 36.5,
                            "ph": 7.2,
                        },
                        {
                            "device_no": "DEV-001",
                            "source_no": "S-1002",
                            "revision": 3,
                            "temperature": 36.8,
                            "ph": 7.1,
                        },
                        {
                            "device_no": "DEV-002",
                            "source_no": "S-1002",
                            "revision": 1,
                            "temperature": 37.2,
                            "ph": 6.9,
                        },
                    ]
                }
            ]
        },
    )

    rows: list[ImportRow] = Field(
        min_length=1,
        max_length=MAX_IMPORT_BATCH,
        description=(
            f"One batch, 1..{MAX_IMPORT_BATCH} rows. "
            "Rows are applied in order; the whole batch is atomic."
        ),
    )


class ImportRowResult(BaseModel):
    index: int = Field(description="请求中的行下标（从 0 开始）")
    device_no: str
    source_no: str
    revision: int
    temperature: float
    ph: float
    status: RowStatus = Field(
        description=(
            "created=首次分配 sample_id；replaced=更高修订整组替换；"
            "unchanged=同版同值幂等重传；rejected=该行未写入"
        )
    )
    sample_id: int | None = Field(
        default=None, description="该设备号+源记录号首次分配并一直沿用的样本号"
    )
    previous_revision: int | None = Field(
        default=None, description="受理前已提交的修订号（首次导入为 null）"
    )
    code: str | None = Field(
        default=None,
        description=(
            "拒绝原因：stale_revision / same_revision_different_values / "
            "duplicate_source_in_batch / batch_rejected"
        ),
    )
    message: str | None = None


class ImportBatchResponse(BaseModel):
    status: Literal["accepted", "rejected"] = Field(
        description="accepted=整批已提交；rejected=整批未提交，原数据不变"
    )
    row_count: int
    created: int
    replaced: int
    unchanged: int
    rejected: int
    rows: list[ImportRowResult] = Field(description="与请求行一一对应，顺序相同")


class _RejectBatch(Exception):
    """Internal signal: roll back and return the per-row rejection results."""

    def __init__(self, results: list[ImportRowResult]):
        super().__init__("batch rejected")
        self.results = results


class ImportService:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ---------------------------------------------------------------- public
    def import_batch(self, batch: ImportBatchRequest) -> ImportBatchResponse:
        conn = connect(self.settings)
        try:
            results: list[ImportRowResult] = []
            try:
                # The write lock is taken before reading the current versions
                # and held until COMMIT.  Lock acquisition order is commit
                # order, so concurrent requests are ordered by the database.
                with transaction(conn, "IMMEDIATE"):
                    results = self._plan(conn, batch.rows)
                    if any(r.status == "rejected" for r in results):
                        raise _RejectBatch(self._rejected_batch(results))
                    self._apply(conn, batch.rows, results)
            except _RejectBatch as rejected:
                return self._response("rejected", rejected.results)
            return self._response("accepted", results)
        finally:
            conn.close()

    # --------------------------------------------------------------- planning
    def _plan(
        self, conn: sqlite3.Connection, rows: list[ImportRow]
    ) -> list[ImportRowResult]:
        current = self._load_current(conn, rows)
        positions: dict[tuple[str, str], list[int]] = {}
        for index, row in enumerate(rows):
            positions.setdefault((row.device_no, row.source_no), []).append(index)
        duplicates = {
            key: indexes for key, indexes in positions.items() if len(indexes) > 1
        }

        results: list[ImportRowResult] = []
        for index, row in enumerate(rows):
            key = (row.device_no, row.source_no)
            previous = current.get(key)
            base = {
                "index": index,
                "device_no": row.device_no,
                "source_no": row.source_no,
                "revision": row.revision,
                "temperature": row.temperature,
                "ph": row.ph,
            }
            if key in duplicates:
                sample_id = previous["sample_id"] if previous else None
                results.append(
                    ImportRowResult(
                        **base,
                        status="rejected",
                        sample_id=sample_id,
                        previous_revision=(
                            previous["revision"] if previous else None
                        ),
                        code="duplicate_source_in_batch",
                        message=(
                            "source record occurs more than once in this batch "
                            f"(rows {duplicates[key]})"
                        ),
                    )
                )
                continue

            if previous is None:
                results.append(ImportRowResult(**base, status="created"))
                continue

            sample_id = previous["sample_id"]
            previous_revision = previous["revision"]
            if row.revision < previous_revision:
                results.append(
                    ImportRowResult(
                        **base,
                        status="rejected",
                        sample_id=sample_id,
                        previous_revision=previous_revision,
                        code="stale_revision",
                        message=(
                            f"revision {row.revision} is older than accepted "
                            f"revision {previous_revision}"
                        ),
                    )
                )
            elif row.revision == previous_revision:
                if (
                    row.temperature == previous["temperature"]
                    and row.ph == previous["ph"]
                ):
                    results.append(
                        ImportRowResult(
                            **base,
                            status="unchanged",
                            sample_id=sample_id,
                            previous_revision=previous_revision,
                        )
                    )
                else:
                    results.append(
                        ImportRowResult(
                            **base,
                            status="rejected",
                            sample_id=sample_id,
                            previous_revision=previous_revision,
                            code="same_revision_different_values",
                            message=(
                                f"revision {row.revision} was already accepted "
                                "with different values"
                            ),
                        )
                    )
            else:
                results.append(
                    ImportRowResult(
                        **base,
                        status="replaced",
                        sample_id=sample_id,
                        previous_revision=previous_revision,
                    )
                )
        return results

    def _load_current(
        self, conn: sqlite3.Connection, rows: list[ImportRow]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        keys = sorted({(row.device_no, row.source_no) for row in rows})
        current: dict[tuple[str, str], dict[str, Any]] = {}
        # Chunked so a full batch stays below SQLite's bound-variable limit on
        # older builds too (each key binds two variables).
        for start in range(0, len(keys), _KEY_LOOKUP_CHUNK):
            chunk = keys[start : start + _KEY_LOOKUP_CHUNK]
            placeholders = ",".join("(?,?)" for _ in chunk)
            params = [part for key in chunk for part in key]
            records = conn.execute(
                "SELECT device_no, source_no, sample_id, revision, temperature, ph"
                f" FROM import_records WHERE (device_no, source_no) IN ({placeholders})",
                params,
            ).fetchall()
            current.update(
                {
                    (record["device_no"], record["source_no"]): dict(record)
                    for record in records
                }
            )
        return current

    @staticmethod
    def _rejected_batch(results: list[ImportRowResult]) -> list[ImportRowResult]:
        """Every row of a rejected batch is rejected, not only the bad ones."""
        rejected: list[ImportRowResult] = []
        for result in results:
            if result.status == "rejected":
                rejected.append(result)
                continue
            rejected.append(
                result.model_copy(
                    update={
                        "status": "rejected",
                        "code": "batch_rejected",
                        "message": "batch rejected because another row failed",
                    }
                )
            )
        return rejected

    # --------------------------------------------------------------- applying
    def _apply(
        self,
        conn: sqlite3.Connection,
        rows: list[ImportRow],
        results: list[ImportRowResult],
    ) -> None:
        next_sample_id = self._next_sample_id(conn)
        now = utcnow()
        for row, result in zip(rows, results, strict=True):
            if result.status == "unchanged":
                continue

            if result.status == "created":
                sample_id = next_sample_id
                next_sample_id += 1
                self._insert_sample(
                    conn, sample_id, row.temperature, row.ph
                )
                conn.execute(
                    "INSERT INTO import_records"
                    " (device_no, source_no, sample_id, revision, temperature, ph,"
                    "  created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.device_no,
                        row.source_no,
                        sample_id,
                        row.revision,
                        row.temperature,
                        row.ph,
                        now,
                        now,
                    ),
                )
                result.sample_id = sample_id
                continue

            if result.status == "replaced":
                sample_id = result.sample_id
                assert sample_id is not None
                self._replace_sample(
                    conn, sample_id, row.temperature, row.ph
                )
                conn.execute(
                    "UPDATE import_records SET revision=?, temperature=?, ph=?,"
                    " updated_at=? WHERE device_no=? AND source_no=?",
                    (
                        row.revision,
                        row.temperature,
                        row.ph,
                        now,
                        row.device_no,
                        row.source_no,
                    ),
                )
                continue

            raise AssertionError(f"unexpected planned status {result.status!r}")

    @staticmethod
    def _next_sample_id(conn: sqlite3.Connection) -> int:
        # Ids are never recycled, even after an external DELETE, so a source
        # identity can never be re-pointed to an unrelated sample.  The
        # import_records maximum matters for exactly that deleted-row case.
        row = conn.execute(
            "SELECT MAX(value) AS max_id FROM ("
            "  SELECT MAX(sample_id) AS value FROM import_records"
            "  UNION ALL SELECT MAX(sample_id) AS value FROM sample_metrics"
            "  UNION ALL SELECT MAX(id) AS value FROM samples"
            ")"
        ).fetchone()
        return int(row["max_id"] or 0) + 1

    @staticmethod
    def _sample_exists(conn: sqlite3.Connection, sample_id: int) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM samples WHERE id = ?", (sample_id,)
            ).fetchone()
            is not None
        )

    def _insert_sample(
        self,
        conn: sqlite3.Connection,
        sample_id: int,
        temperature: float,
        ph: float,
    ) -> None:
        conn.execute(
            "INSERT INTO samples (id, temperature, ph) VALUES (?, ?, ?)",
            (sample_id, temperature, ph),
        )

    def _replace_sample(
        self,
        conn: sqlite3.Connection,
        sample_id: int,
        temperature: float,
        ph: float,
    ) -> None:
        cursor = conn.execute(
            "UPDATE samples SET temperature = ?, ph = ? WHERE id = ?",
            (temperature, ph, sample_id),
        )
        # ``UPDATE`` through an INSTEAD OF trigger reports ``rowcount == 0`` on
        # every SQLite version, so existence must be checked explicitly.  The
        # import_records row normally guarantees it, unless the legacy program
        # deleted the sample; then re-create it under the first assigned id.
        if cursor.rowcount == 0 and not self._sample_exists(conn, sample_id):
            self._insert_sample(conn, sample_id, temperature, ph)

    # -------------------------------------------------------------- responses
    @staticmethod
    def _response(
        status: Literal["accepted", "rejected"], results: list[ImportRowResult]
    ) -> ImportBatchResponse:
        counts = {"created": 0, "replaced": 0, "unchanged": 0, "rejected": 0}
        for result in results:
            counts[result.status] += 1
        return ImportBatchResponse(
            status=status,
            row_count=len(results),
            created=counts["created"],
            replaced=counts["replaced"],
            unchanged=counts["unchanged"],
            rejected=counts["rejected"],
            rows=results,
        )
