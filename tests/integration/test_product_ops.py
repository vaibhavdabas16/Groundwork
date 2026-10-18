"""Ops endpoints, metrics, pilot report, backup and CLI BLOCKED paths (real local components)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from contextforge.config.settings import Settings
from contextforge.product.audit import AuditLog
from contextforge.product.config import ProductConfig
from contextforge.product.db import Database
from contextforge.product.deployment import ContextDeployer
from contextforge.product.ops import pilot_report, readiness, serve_ops, snapshot

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_pilot_report_never_invents_activity(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.sqlite")
    rep = pilot_report(db, ProductConfig(), AuditLog(db))
    assert rep["status"] == "Pilot prepared; no verified user activity yet."
    assert rep["questions"] == 0 and rep["verified_user_activity"] is False
    snap = snapshot(db)
    assert (
        snap["questions_received"] == 0
        and snap["latency_ms_p50"] is None
        and snap["abstentions_verified_correct"] is None
    )


def test_readiness_requires_a_deployed_release_and_endpoints_serve(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    db = Database(tmp_path / "ops.sqlite")
    dep = ContextDeployer(real_warehouse, db, AuditLog(db), releases_root=tmp_path / "rel")
    r = readiness(real_warehouse, db, dep)
    assert not r["ready"] and "not ready" in str(r["checks"]["context_release"])
    dep.deploy(real_warehouse.context_dir, actor="tester")
    assert str(readiness(real_warehouse, db, dep)["checks"]["context_release"]).startswith("ctx-")
    srv = serve_ops(real_warehouse, db, dep, port=0)
    port = srv.server_address[1]
    assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/livez"))["live"] is True
    with pytest.raises(
        urllib.error.HTTPError
    ) as ei:  # model credentials absent in CI -> 503, never a fake "ready"
        if os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("credentials present; readiness may legitimately pass")
        urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz")
    assert ei.value.code == 503
    metrics = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics").read().decode()
    assert "contextforge_questions_received 0" in metrics and "contextforge_ready 0" in metrics
    srv.shutdown()


def test_cli_reports_blocked_without_slack_or_github_credentials(tmp_path: Path) -> None:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("SLACK_", "GITHUB_", "CONTEXTFORGE_GITHUB"))
    }
    env["CONTEXTFORGE_OPS_DB"] = str(tmp_path / "ops.sqlite")
    r = subprocess.run(
        [sys.executable, "-m", "contextforge.cli", "product", "run"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 3 and "BLOCKED" in r.stdout and "SLACK_BOT_TOKEN" in r.stdout


def test_backup_is_consistent_and_chain_verifies(tmp_path: Path) -> None:
    db = Database(tmp_path / "ops.sqlite")
    log = AuditLog(db)
    for i in range(5):
        log.append(
            action="feedback.recorded",
            actor_id="slack:U1",
            actor_type="human",
            resource_type="request",
            resource_id=f"r{i}",
        )
    env = dict(os.environ, CONTEXTFORGE_OPS_DB=str(tmp_path / "ops.sqlite"))
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "contextforge.cli",
            "opsdb",
            "backup",
            "--out",
            str(tmp_path / "bk" / "b.sqlite"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["audit_chain_ok"] and out["events"] == 5 and out["head"] == log.head()
