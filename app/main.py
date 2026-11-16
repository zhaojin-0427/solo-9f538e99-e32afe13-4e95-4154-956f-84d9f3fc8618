"""FastAPI application: migration control, progress, and metric queries."""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .imports import ImportBatchRequest, ImportBatchResponse, ImportService
from .migration import MigrationEngine, MigrationError
from .repository import (
    DEFAULT_PAGE,
    MAX_PAGE,
    InvalidQuery,
    SampleNotFound,
    SampleRepository,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("api")


def _json_safe(value: Any) -> Any:
    """Make validation-error payloads JSON-serializable.

    A request may contain ``NaN``/``Infinity`` (Python's JSON parser accepts
    those literals).  Echoing them back in a 422 body would crash the default
    error handler with a non-compliant-JSON ``ValueError``.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = MigrationEngine(settings)
    repository = SampleRepository(settings)
    importer = ImportService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.initialize()
        app.state.settings = settings
        app.state.engine = engine
        app.state.repository = repository
        app.state.importer = importer
        yield
        engine.wait_for_idle(timeout=5)

    app = FastAPI(
        title="Lab wide-to-long migration API",
        version="1.1.0",
        description=(
            "在线将 samples(id, temperature, ph) 宽表迁移为 sample_metrics 长表；"
            "迁移期间旧采集程序仍可读写，切换后 samples 由兼容视图提供。"
            "另提供离线仪器补传的修订号批量导入接口。"
        ),
        lifespan=lifespan,
    )

    def get_engine(request: Request) -> MigrationEngine:
        return request.app.state.engine

    def get_repo(request: Request) -> SampleRepository:
        return request.app.state.repository

    def get_importer(request: Request) -> ImportService:
        return request.app.state.importer

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Same shape as FastAPI's default 422, but safe for non-finite floats.
        errors = jsonable_encoder(exc.errors(), custom_encoder={float: repr})
        return JSONResponse(status_code=422, content={"detail": _json_safe(errors)})

    @app.exception_handler(InvalidQuery)
    async def _invalid_query(_request: Request, exc: InvalidQuery) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(SampleNotFound)
    async def _not_found(_request: Request, exc: SampleNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(MigrationError)
    async def _migration_error(_request: Request, exc: MigrationError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # ------------------------------------------------------------- migration
    @app.post("/migrations", status_code=202)
    def start_migration(request: Request) -> dict[str, Any]:
        """发起迁移。

        幂等：已完成 → `action=reused`（不重建）；运行中 → 返回当前进度；
        失败 → 从已提交的检查点续跑；从未开始 → 新建并启动后台复制。
        """
        state, started = get_engine(request).start()
        return {
            "action": "started" if started else "reused",
            "migration": state,
            "progress_url": "/migrations/current",
        }

    @app.get("/migrations/current")
    def migration_status(request: Request) -> dict[str, Any]:
        """查询迁移进度与状态（not_started / copying / completed / failed）。"""
        engine = get_engine(request)
        state = engine.get_state()
        # The persisted last_error (if any) is authoritative across restarts;
        # the in-memory one only covers a failure earlier in this process.
        return {
            "migration": state,
            "last_error": state.get("last_error") or engine.last_error,
        }

    @app.get("/migrations/current/events")
    def migration_events(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        return {"events": get_engine(request).events(limit=limit)}

    # --------------------------------------------------------------- imports
    @app.post(
        "/imports/offline",
        response_model=ImportBatchResponse,
        response_model_exclude_none=False,
        summary="批量导入离线仪器补传记录（带修订号）",
        responses={
            200: {
                "description": (
                    "整批受理（status=accepted，逐行 created/replaced/unchanged）"
                    "或整批拒绝（status=rejected，原数据不变）"
                ),
                "content": {
                    "application/json": {
                        "example": {
                            "status": "accepted",
                            "row_count": 2,
                            "created": 1,
                            "replaced": 1,
                            "unchanged": 0,
                            "rejected": 0,
                            "rows": [
                                {
                                    "index": 0,
                                    "device_no": "DEV-001",
                                    "source_no": "S-1001",
                                    "revision": 1,
                                    "temperature": 36.5,
                                    "ph": 7.2,
                                    "status": "created",
                                    "sample_id": 43,
                                    "previous_revision": None,
                                    "code": None,
                                    "message": None,
                                },
                                {
                                    "index": 1,
                                    "device_no": "DEV-001",
                                    "source_no": "S-1002",
                                    "revision": 3,
                                    "temperature": 36.8,
                                    "ph": 7.1,
                                    "status": "replaced",
                                    "sample_id": 44,
                                    "previous_revision": 2,
                                    "code": None,
                                    "message": None,
                                },
                            ],
                        }
                    }
                },
            }
        },
    )
    def import_offline(
        request: Request, batch: ImportBatchRequest
    ) -> ImportBatchResponse:
        """离线仪器补传：按 (device_no, source_no, revision) 批量导入温度/pH。

        同一设备与源记录沿用首次分配的 sample_id：更高修订整组替换，同版同值重传
        幂等；旧版、同版异值、批内重复源记录号均整批拒绝且不修改任何数据。
        并发请求按数据库提交顺序判定版本。迁移前后均可用。
        """
        return get_importer(request).import_batch(batch)

    # --------------------------------------------------------------- metrics
    @app.get("/metrics/{metric}")
    def list_metric(
        request: Request,
        metric: str,
        after_id: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """按指标装载长表（键集分页，按 sample_id 递增）。"""
        return get_repo(request).list_metric(metric, after_id=after_id, limit=limit)

    @app.get("/samples/{sample_id}/metrics")
    def sample_metrics(
        request: Request,
        sample_id: Annotated[int, Path()],
    ) -> dict[str, Any]:
        """按样本查询各指标当前值；切换后即长表内容，缺失返回 404。"""
        return get_repo(request).get_metrics(sample_id)

    @app.get("/samples")
    def list_samples(
        request: Request,
        after_id: Annotated[int | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    ) -> dict[str, Any]:
        """兼容视图读取（与旧程序看到的 samples 完全一致）。"""
        return get_repo(request).list_samples(after_id=after_id, limit=limit)

    @app.get("/samples/{sample_id}")
    def get_sample(
        request: Request,
        sample_id: Annotated[int, Path()],
    ) -> dict[str, Any]:
        """兼容视图单行读取（旧程序 SELECT 的等价入口）。"""
        return get_repo(request).get_sample(sample_id)

    @app.get("/metrics", include_in_schema=True)
    def metrics_index() -> dict[str, Any]:
        """Available metrics and their query entry points."""
        temperature = {"metric": "temperature", "url": "/metrics/temperature"}
        ph = {"metric": "ph", "url": "/metrics/ph"}
        return {
            "metrics": [temperature, ph],
            "sample_url": "/samples/{sample_id}/metrics",
        }

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        state = get_engine(request).get_state()
        return {
            "status": "ok",
            "migration_state": state.get("state"),
            "view_installed": state.get("view_installed", False),
        }

    return app


app = create_app()
