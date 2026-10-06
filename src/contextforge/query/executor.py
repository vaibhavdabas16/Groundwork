"""Trusted-side query executor: process supervision, hard timeout, concurrency limit, result guard."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contextforge.config.settings import QueryLimits
from contextforge.observability.logging import get_logger

log = get_logger("query.executor")


class ExecutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class WorkerResult:
    columns: list[str] = field(default_factory=list)
    column_types: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    truncation_reason: str | None = None
    estimated_max_cardinality: int | None = None
    elapsed_ms: float = 0.0
    plan: str | None = None
    redactions: int = 0


# Last-line DLP at the trusted result boundary. The primary privacy controls are upstream (sensitive
# fields are physically absent from the serving DB and rejected by the SQL guard); this catches
# personal-data-shaped strings that should never appear in an analytics result.
_DLP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("cpf", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    ("cnpj", re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")),
    ("phone_br", re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}-\d{4}\b")),
]


def redact_value(value: Any) -> tuple[Any, int]:
    if not isinstance(value, str):
        return value, 0
    hits = 0
    out = value
    for label, pat in _DLP_PATTERNS:
        out, n = pat.subn(f"[REDACTED:{label}]", out)
        hits += n
    return out, hits


class QueryExecutor:
    def __init__(self, serving_path: Path, limits: QueryLimits) -> None:
        self.serving_path = serving_path
        self.limits = limits
        self._slots = threading.BoundedSemaphore(limits.max_concurrent_queries)

    def _worker_env(self) -> dict[str, str]:
        # Minimal environment: no secrets (pseudonym key, tokens) are inherited by the worker.
        keep = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONHASHSEED")
        return {k: v for k, v in os.environ.items() if k in keep}

    def run(
        self,
        sql: str,
        *,
        mode: str,
        max_rows: int,
        timeout_seconds: float | None = None,
        max_cost_estimated_rows: int | None = None,
    ) -> WorkerResult:
        if not self.serving_path.is_file():
            raise ExecutionError(
                "warehouse_unavailable", "serving warehouse not built; run `contextforge build`"
            )
        if not self._slots.acquire(blocking=False):
            raise ExecutionError(
                "concurrency_limit",
                f"too many concurrent queries (limit {self.limits.max_concurrent_queries})",
            )
        try:
            return self._run(
                sql, mode, max_rows, timeout_seconds or self.limits.timeout_seconds, max_cost_estimated_rows
            )
        finally:
            self._slots.release()

    def _run(self, sql: str, mode: str, max_rows: int, timeout: float, max_cost: int | None) -> WorkerResult:
        req = {
            "db_path": str(self.serving_path),
            "sql": sql,
            "mode": mode,
            "max_rows": max_rows,
            "max_result_bytes": self.limits.max_result_bytes,
            "memory_limit": self.limits.memory_limit,
            "threads": self.limits.threads,
            "max_cost_estimated_rows": max_cost,
            "os_cpu_seconds": int(timeout) + 2,
        }
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="cf-worker-") as cwd:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-m", "contextforge.query.worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=self._worker_env(),
            )
            try:
                out, err = proc.communicate(json.dumps(req).encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()  # hard kill: the query cannot keep running after the timeout
                proc.communicate()
                log.warning("query killed after timeout", extra={"timeout_s": timeout, "pid": proc.pid})
                raise ExecutionError(
                    "timeout", f"query exceeded the {timeout:g}s execution timeout and was terminated"
                ) from None
        if proc.returncode != 0 or not out:
            log.error(
                "worker failed",
                extra={"returncode": proc.returncode, "stderr": err.decode("utf-8", "replace")[-500:]},
            )
            raise ExecutionError("worker_failed", f"query worker exited abnormally (code {proc.returncode})")
        resp = json.loads(out.decode("utf-8"))
        if not resp.get("ok"):
            raise ExecutionError(resp.get("error_code", "execution_error"), resp.get("error", "query failed"))
        result = WorkerResult(
            columns=resp.get("columns", []),
            column_types=resp.get("column_types", []),
            rows=resp.get("rows", []),
            row_count=resp.get("row_count", 0),
            truncated=bool(resp.get("truncated")),
            truncation_reason=resp.get("truncation_reason"),
            estimated_max_cardinality=resp.get("estimated_max_cardinality"),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            plan=resp.get("plan"),
        )
        redactions = 0
        for row in result.rows:
            for i, v in enumerate(row):
                row[i], n = redact_value(v)
                redactions += n
        result.redactions = redactions
        return result
