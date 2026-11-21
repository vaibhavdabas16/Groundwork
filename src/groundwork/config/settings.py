"""Runtime configuration.

All settings come from environment variables (prefix ``GROUNDWORK_``) with safe defaults.
Nothing here is secret except ``pseudonym_key``, which is never logged and has no default
committed to source: if it is unset, the build generates a random key and stores it in the
(gitignored) data directory.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


def _project_root() -> Path:
    env = os.environ.get("GROUNDWORK_ROOT")
    if env:
        return Path(env).resolve()
    # src/groundwork/config/settings.py -> project root is 3 levels above the package.
    return Path(__file__).resolve().parents[3]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class ContextMode(StrEnum):
    """Experimental context-exposure condition, fixed server-side at process start.

    * ``raw_schema``: table names, column names and data types only.
    * ``descriptions``: raw_schema + table and column descriptions.
    * ``full_context``: the whole governed context layer (glossary, metrics, joins, warnings,
      certification, unsupported-concept register). This is the production default.

    Safety (permissions, SQL guard, limits) is identical in every mode. Filtering is applied in
    ``GroundworkService`` - the trusted boundary - never by prompt instructions.
    """

    raw_schema = "raw_schema"
    descriptions = "descriptions"
    full_context = "full_context"


class QueryLimits(BaseModel):
    max_rows: int = Field(default=1000, ge=1, le=100_000)
    max_result_bytes: int = Field(default=2_000_000, ge=1_000)
    timeout_seconds: float = Field(default=20.0, gt=0, le=600)
    max_concurrent_queries: int = Field(default=2, ge=1, le=64)
    memory_limit: str = "1GB"
    threads: int = Field(default=2, ge=1, le=64)
    max_sql_chars: int = Field(default=20_000, ge=100)
    max_ast_nodes: int = Field(default=4_000, ge=10)
    max_joins: int = Field(default=8, ge=0)
    max_subquery_depth: int = Field(default=4, ge=0)
    max_tool_calls_per_request: int = Field(default=50, ge=1)


class Settings(BaseModel):
    root: Path
    data_dir: Path
    raw_dir: Path
    manifest_dir: Path
    warehouse_path: Path
    serving_path: Path
    context_dir: Path
    log_dir: Path
    principal_id: str
    limits: QueryLimits
    pseudonym_key_env: str | None = Field(default=None, repr=False)
    enable_http: bool = False
    context_mode: ContextMode = ContextMode.full_context

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = _project_root()
    data_dir = Path(os.environ.get("GROUNDWORK_DATA_DIR", root / "data")).resolve()
    limits = QueryLimits(
        max_rows=_env_int("GROUNDWORK_MAX_ROWS", 1000),
        max_result_bytes=_env_int("GROUNDWORK_MAX_RESULT_BYTES", 2_000_000),
        timeout_seconds=_env_float("GROUNDWORK_QUERY_TIMEOUT_SECONDS", 20.0),
        max_concurrent_queries=_env_int("GROUNDWORK_MAX_CONCURRENT_QUERIES", 2),
        memory_limit=os.environ.get("GROUNDWORK_QUERY_MEMORY_LIMIT", "1GB"),
        threads=_env_int("GROUNDWORK_QUERY_THREADS", 2),
        max_tool_calls_per_request=_env_int("GROUNDWORK_MAX_TOOL_CALLS_PER_REQUEST", 50),
    )
    return Settings(
        root=root,
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        manifest_dir=root / "data" / "manifests",
        warehouse_path=Path(
            os.environ.get("GROUNDWORK_WAREHOUSE_PATH", data_dir / "warehouse" / "olist_raw.duckdb")
        ),
        serving_path=Path(
            os.environ.get("GROUNDWORK_SERVING_PATH", data_dir / "warehouse" / "olist_serving.duckdb")
        ),
        context_dir=Path(os.environ.get("GROUNDWORK_CONTEXT_DIR", root / "context")).resolve(),
        log_dir=data_dir / "logs",
        principal_id=os.environ.get("GROUNDWORK_PRINCIPAL", "principal.local_analyst"),
        limits=limits,
        pseudonym_key_env=os.environ.get("GROUNDWORK_PSEUDONYM_KEY") or None,
        enable_http=os.environ.get("GROUNDWORK_ENABLE_HTTP", "").lower() in ("1", "true", "yes"),
        context_mode=ContextMode(os.environ.get("GROUNDWORK_CONTEXT_MODE") or "full_context"),
    )
