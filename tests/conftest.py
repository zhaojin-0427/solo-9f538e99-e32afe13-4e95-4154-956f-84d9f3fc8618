"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.migration import MigrationEngine

SEED = ROOT / "seed" / "lab_wide.db"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "lab.db",
        seed_database_path=SEED,
        chunk_size=4,
        chunk_delay_ms=0,
        busy_timeout_ms=10_000,
        auto_resume=False,
    )


@pytest.fixture()
def engine(settings: Settings) -> MigrationEngine:
    eng = MigrationEngine(settings)
    eng.initialize()
    return eng


@pytest.fixture()
def migrated(engine: MigrationEngine):
    """Engine with a completed migration, worker joined."""
    _state, started = engine.start()
    assert started
    engine.wait_for_idle(30)
    return engine
