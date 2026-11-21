"""Shared fixtures.

Tests that need the real Olist warehouse FAIL (not skip) with an actionable message when it has not
been built, so a missing prerequisite can never look like a passing run.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from groundwork.config.settings import Settings, get_settings
from groundwork.service.core import GroundworkService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def real_warehouse(settings: Settings) -> Settings:
    missing = [p for p in (settings.warehouse_path, settings.serving_path) if not p.is_file()]
    if missing:
        pytest.fail(
            f"Real Olist warehouse not built ({', '.join(map(str, missing))}). "
            "Run: `make data` (or `uv run groundwork acquire && uv run groundwork build`)."
        )
    return settings


@pytest.fixture
def isolated_settings(real_warehouse: Settings, tmp_path: Path) -> Settings:
    """Real warehouse, but private data_dir (audit DB) per test."""
    return real_warehouse.model_copy(update={"data_dir": tmp_path})


@pytest.fixture
def service(isolated_settings: Settings) -> Iterator[GroundworkService]:
    svc = GroundworkService(isolated_settings)
    yield svc
    svc.close()


def make_service(settings: Settings, principal: str, **limit_overrides: object) -> GroundworkService:
    s = settings
    if limit_overrides:
        s = settings.model_copy(update={"limits": settings.limits.model_copy(update=limit_overrides)})
    return GroundworkService(s, principal_id=principal)


@pytest.fixture(scope="session")
def serving_con(real_warehouse: Settings) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(real_warehouse.serving_path), read_only=True)
    yield con
    con.close()


@pytest.fixture(scope="session")
def raw_con(real_warehouse: Settings) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(real_warehouse.warehouse_path), read_only=True)
    yield con
    con.close()


@pytest.fixture
def context_copy(tmp_path: Path) -> Path:
    """A writable copy of the real context repository for mutation tests."""
    dst = tmp_path / "context"
    shutil.copytree(ROOT / "context", dst)
    return dst
