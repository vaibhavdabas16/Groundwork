"""SQLite audit log of tool calls and queries (operational metadata only; never result rows)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL,
    query_id TEXT,
    principal TEXT NOT NULL,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    sql_text TEXT,
    executed_sql TEXT,
    row_count INTEGER,
    truncated INTEGER,
    elapsed_ms REAL,
    context_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_tool_calls_ts ON tool_calls(ts);
"""


class AuditLog:
    def __init__(self, path: Path, retention_days: int, store_sql_text: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.store_sql_text = store_sql_text
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.executescript(_SCHEMA)
        self.purge(retention_days)

    def purge(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cur = self._con.execute("DELETE FROM tool_calls WHERE ts < ?", (cutoff,))
            self._con.commit()
            return cur.rowcount

    def record(self, **fields: Any) -> None:
        if not self.store_sql_text:
            fields["sql_text"] = None
            fields["executed_sql"] = None
        cols = ["ts", *fields.keys()]
        vals = [time.time(), *fields.values()]
        with self._lock:
            self._con.execute(
                f"INSERT INTO tool_calls ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
            )
            self._con.commit()

    def close(self) -> None:
        with self._lock:
            self._con.close()
