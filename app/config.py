"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    database_path: Path
    seed_database_path: Path | None = None
    chunk_size: int = 100
    chunk_delay_ms: int = 100
    busy_timeout_ms: int = 30_000
    auto_resume: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        seed_raw = os.environ.get("SEED_DATABASE_PATH", "")
        return cls(
            database_path=Path(
                os.environ.get("DATABASE_PATH", "/app/data/lab.db")
            ).expanduser(),
            seed_database_path=Path(seed_raw).expanduser() if seed_raw else None,
            chunk_size=max(1, _env_int("MIGRATION_CHUNK_SIZE", 100)),
            chunk_delay_ms=max(0, _env_int("MIGRATION_CHUNK_DELAY_MS", 100)),
            busy_timeout_ms=max(1000, _env_int("SQLITE_BUSY_TIMEOUT_MS", 30_000)),
            auto_resume=os.environ.get("AUTO_RESUME", "true").lower()
            not in {"0", "false", "no"},
        )
