"""Operational metrics, health endpoints and pilot reporting, computed ONLY from recorded data.

Endpoints (``serve_ops``; bind to localhost / the compose network, never publish publicly):
* ``GET /livez``  - process is up.
* ``GET /readyz`` - operational DB migrated and writable, serving warehouse present, an ACTIVE
  validated context release (not the working tree), and provider/Slack credentials present. Returns
  503 with the failing checks otherwise.
* ``GET /metrics`` - Prometheus text exposition of the counters below (aggregates; no user ids).

Per-user numbers in the pilot report use pseudonymous labels from ``config/product.yaml``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import statistics
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from contextforge.config.settings import Settings
from contextforge.product.audit import AuditLog
from contextforge.product.config import ProductConfig
from contextforge.product.db import Database
from contextforge.product.deployment import ContextDeployer


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, round(q * (len(v) - 1))))
    return v[k]


def snapshot(db: Database, *, since: str | None = None) -> dict[str, Any]:
    where = " WHERE created_at >= ?" if since else ""
    p: tuple[object, ...] = (since,) if since else ()
    reqs = [dict(r) for r in db.query("SELECT * FROM requests" + where, p)]
    outcomes = Counter(r["outcome"] or r["status"] for r in reqs)
    lat = [float(r["latency_ms"]) for r in reqs if r["latency_ms"] is not None]
    sql_fail = mcp_fail = model_fail = db_fail = timeouts = 0
    ctx_versions: Counter[str] = Counter()
    retrieval = 0
    for r in reqs:
        if r["context_version"]:
            ctx_versions[r["context_version"]] += 1
        if not r["response_json"]:
            continue
        resp = json.loads(r["response_json"])
        tools = [t["tool"] for t in resp.get("tool_calls", [])]
        retrieval += int(
            any(t in ("search_assets", "get_asset", "get_definition", "get_join_path") for t in tools)
        )
        for q in resp.get("queries", []):
            if not q.get("ok"):
                sql_fail += 1
                timeouts += int(q.get("error_code") == "execution.timeout")
                db_fail += int(
                    q.get("error_code") in ("execution.warehouse_unavailable", "execution.worker_failed")
                )
        code = (resp.get("error") or {}).get("code") or ""
        mcp_fail += int(code == "context_source_unavailable")
        model_fail += int(code.startswith(("provider_", "model_")))
    fails = Counter(r["failure_category"] for r in reqs if r["failure_category"])
    props = Counter(r[0] for r in db.query("SELECT state FROM proposals"))
    decided = db.query(
        "SELECT p.created_at, p.decided_at, p.decision FROM proposals p WHERE decided_at IS NOT NULL"
    )
    import datetime as dt

    approval_latency = [
        (dt.datetime.fromisoformat(d[1]) - dt.datetime.fromisoformat(d[0])).total_seconds() / 3600
        for d in decided
    ]
    reviews = Counter(r[0] for r in db.query("SELECT state FROM answer_reviews"))
    audit_counts = Counter(r[0] for r in db.query("SELECT action FROM audit_events"))
    return {
        "questions_received": len(reqs),
        "outcomes": dict(outcomes),
        "answers": outcomes.get("ANSWER", 0),
        "clarifications": outcomes.get("CLARIFICATION_REQUIRED", 0),
        "clarification_rate": (outcomes.get("CLARIFICATION_REQUIRED", 0) / len(reqs)) if reqs else None,
        "abstentions_verified_correct": None,  # only measurable through shadow review / eval; never inferred
        "shadow_reviews": dict(reviews),
        "sql_failures": sql_fail,
        "query_timeouts": timeouts,
        "database_failures": db_fail,
        "mcp_failures": mcp_fail,
        "model_failures": model_fail,
        "latency_ms_p50": statistics.median(lat) if lat else None,
        "latency_ms_p95": _pct(lat, 0.95),
        "input_tokens": sum(r["input_tokens"] or 0 for r in reqs),
        "output_tokens": sum(r["output_tokens"] or 0 for r in reqs),
        "cost_usd_known": round(sum(r["cost_usd"] for r in reqs if r["cost_usd"] is not None), 6),
        "cost_unknown_requests": sum(1 for r in reqs if r["response_json"] and r["cost_usd"] is None),
        "context_retrieval_requests": retrieval,
        "context_versions_used": dict(ctx_versions),
        "failure_categories": dict(fails),
        "security_denials": fails.get("PERMISSION_DENIED", 0)
        + audit_counts.get("approval.denied_unauthorized", 0),
        "rejected_questions": audit_counts.get("question.rejected", 0),
        "proposals_by_state": dict(props),
        "open_proposals": sum(v for k, v in props.items() if k not in ("REJECTED", "DEPLOYED", "SUPERSEDED")),
        "approval_latency_hours_p50": statistics.median(approval_latency) if approval_latency else None,
        "rejected_changes": props.get("REJECTED", 0),
        "merged_changes": props.get("MERGED", 0) + props.get("DEPLOYED", 0),
        "regression_failures": audit_counts.get("regression.detected", 0),
        "feedback": dict(Counter(r[0] for r in db.query("SELECT rating FROM feedback"))),
    }


def prometheus(snap: dict[str, Any], ready: bool) -> str:
    out = [f"contextforge_ready {int(ready)}"]
    for k, v in snap.items():
        if isinstance(v, int | float) and not isinstance(v, bool):
            out.append(f"contextforge_{k} {v}")
        elif isinstance(v, dict):
            for label, n in v.items():
                if isinstance(n, int | float):
                    safe = str(label).replace('"', "")
                    out.append(f'contextforge_{k}{{key="{safe}"}} {n}')
    return "\n".join(out) + "\n"


def readiness(
    settings: Settings, db: Database, deployer: ContextDeployer, *, slack_connected: bool | None = None
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        db.one("SELECT 1")
        checks["ops_db"] = db.schema_version() >= 1
    except Exception as exc:
        checks["ops_db"] = f"error: {type(exc).__name__}"
    checks["serving_warehouse"] = settings.serving_path.is_file()
    try:
        rel = deployer.verify()
        checks["context_release"] = rel.version
    except Exception as exc:
        checks["context_release"] = f"not ready: {exc}"
    provider = os.environ.get("CONTEXTFORGE_PROVIDER", "anthropic")
    key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider, "?")
    checks["model_credentials"] = bool(os.environ.get(key))
    if slack_connected is not None:
        checks["slack"] = slack_connected
    ok = all(
        v is True or (k == "context_release" and isinstance(v, str) and v.startswith("ctx-"))
        for k, v in checks.items()
    )
    return {"ready": ok, "checks": checks}


def serve_ops(
    settings: Settings,
    db: Database,
    deployer: ContextDeployer,
    *,
    host: str = "127.0.0.1",
    port: int = 8088,
    slack_state: dict[str, bool] | None = None,
) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str = "application/json") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/livez":
                self._send(200, '{"live": true}')
            elif self.path == "/readyz":
                r = readiness(settings, db, deployer, slack_connected=(slack_state or {}).get("connected"))
                self._send(200 if r["ready"] else 503, json.dumps(r))
            elif self.path == "/metrics":
                r = readiness(settings, db, deployer, slack_connected=(slack_state or {}).get("connected"))
                self._send(200, prometheus(snapshot(db), r["ready"]), "text/plain; version=0.0.4")
            else:
                self._send(404, '{"error": "not_found"}')

        def log_message(self, *_args: Any) -> None:  # keep stdout clean; access logs not needed
            return

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True, name="cf-ops-http").start()
    return srv


def pilot_report(db: Database, cfg: ProductConfig, audit: AuditLog) -> dict[str, Any]:
    labels = {u.slack_user_id: (u.label or f"user-{i + 1:02d}") for i, u in enumerate(cfg.users)}
    reqs = [dict(r) for r in db.query("SELECT user_id, outcome, status, delivery, created_at FROM requests")]
    per_user: dict[str, Counter[str]] = {}
    for r in reqs:
        lab = labels.get(r["user_id"], "unlisted")
        per_user.setdefault(lab, Counter())[r["outcome"] or r["status"]] += 1
    consents = Counter(
        r[0]
        for r in db.query(
            "SELECT decision FROM consents WHERE notice_version = ?", (cfg.consent_notice_version,)
        )
    )
    reviews = [dict(r) for r in db.query("SELECT state FROM answer_reviews")]
    fb = [dict(r) for r in db.query("SELECT rating, category FROM feedback")]
    activity = len(reqs)
    chain = audit.verify()
    return {
        "verified_user_activity": activity > 0,
        "status": "Pilot prepared; no verified user activity yet."
        if activity == 0
        else "Pilot has recorded activity",
        "pilot_users_configured": len(cfg.users),
        "users_with_activity": len(per_user),
        "consents": dict(consents),
        "questions": activity,
        "per_user": {k: dict(v) for k, v in sorted(per_user.items())},
        "shadow_reviews": dict(Counter(r["state"] for r in reviews)),
        "human_verified_correct_answers": sum(1 for r in reviews if r["state"] == "APPROVED"),
        "human_rejected_or_corrected": sum(1 for r in reviews if r["state"] in ("REJECTED", "CORRECTED")),
        "feedback": dict(Counter(f["rating"] for f in fb)),
        "feedback_categories": dict(Counter(f["category"] for f in fb if f["category"])),
        "metrics": snapshot(db),
        "audit_chain": {"ok": chain.ok, "events": chain.events, "head": chain.head, "problem": chain.problem},
    }


def export_requests_csv(db: Database, cfg: ProductConfig, path: Path) -> int:
    labels = {u.slack_user_id: (u.label or f"user-{i + 1:02d}") for i, u in enumerate(cfg.users)}
    rows = db.query(
        "SELECT request_id, user_id, created_at, delivery, status, outcome, failure_category, context_version, model, "
        "latency_ms, input_tokens, output_tokens, cost_usd FROM requests ORDER BY created_at"
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "request_id",
            "user_label",
            "created_at",
            "delivery",
            "status",
            "outcome",
            "failure_category",
            "context_version",
            "model",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "cost_usd",
        ]
    )
    for r in rows:
        d = dict(r)
        w.writerow([d["request_id"], labels.get(d["user_id"], "unlisted"), *[d[k] for k in list(d)[2:]]])
    path.write_text(buf.getvalue(), encoding="utf-8")
    return len(rows)


def purge_expired(db: Database, retention_days: int) -> int:
    """Retention: delete question/answer content older than ``retention_days`` (audit events keep
    ids and states only; they are append-only and are not purged by this job)."""
    import datetime as dt

    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=retention_days)).isoformat(timespec="seconds")
    with db.tx() as c:
        n = c.execute(
            "UPDATE requests SET question='[purged]', response_json=NULL WHERE created_at < ? AND question != '[purged]'",
            (cutoff,),
        ).rowcount
        c.execute("UPDATE feedback SET comment=NULL WHERE created_at < ?", (cutoff,))
        c.execute("UPDATE failures SET question='[purged]' WHERE created_at < ?", (cutoff,))
    return int(n)
