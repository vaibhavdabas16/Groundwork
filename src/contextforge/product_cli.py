"""CLI for Phase 3: ``product``, ``context``, ``proposals``, ``failures``, ``audit``, ``pilot``, ``opsdb``.

Exit codes: 0 ok, 1 failure, 2 usage/config error, 3 BLOCKED (missing external credentials).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from contextforge.config.settings import Settings, get_settings

EXIT_BLOCKED = 3


def _print(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def _blocked(reason: str, missing: list[str]) -> int:
    _print({"status": "BLOCKED", "reason": reason, "missing_configuration": missing})
    return EXIT_BLOCKED


def ops_db(settings: Settings) -> Any:
    from contextforge.product.db import Database

    return Database(Path(os.environ.get("CONTEXTFORGE_OPS_DB", settings.data_dir / "ops" / "product.sqlite")))


def agent_config() -> Any:
    from contextforge.agent.providers.registry import DEFAULT_MODELS
    from contextforge.agent.schemas import AgentConfig

    provider = os.environ.get("CONTEXTFORGE_PROVIDER", "anthropic")
    model = os.environ.get("CONTEXTFORGE_MODEL") or DEFAULT_MODELS.get(provider, "")
    return AgentConfig(provider=provider, model=model)


def provider_factory(cfg: Any) -> Any:
    from contextforge.agent.providers.registry import make_provider

    return lambda: make_provider(cfg)


def load_cfg(settings: Settings, *, strict: bool = True) -> Any:
    from contextforge.context.loader import load_context
    from contextforge.product.config import default_path, load_product_config

    return load_product_config(
        default_path(settings.root), load_context(settings.context_dir) if strict else None
    )


def assemble(settings: Settings, gateway: Any, *, with_improvement: bool = True) -> Any:
    """Wire the product core with the improvement engine, validator and PR workflow."""
    from contextforge.agent.providers.base import ProviderNotConfigured
    from contextforge.product import blocks
    from contextforge.product.core import ProductCore
    from contextforge.product.github import GitHubClient
    from contextforge.product.proposals.engine import EngineBlocked, ImprovementEngine
    from contextforge.product.proposals.validation import ProposalValidator
    from contextforge.product.proposals.workflow import ProposalWorkflow

    cfg = load_cfg(settings)
    db = ops_db(settings)
    acfg = agent_config()
    pf = provider_factory(acfg)
    core = ProductCore(settings, cfg, db, gateway, agent_config=acfg, provider_factory=pf)

    def notify_review(row: Any) -> str | None:
        ch = cfg.slack.review_channel_id
        if not ch:
            core.alert(f"proposal {row.proposal_id} awaits review but no review_channel_id is configured")
            return None
        return core._safe_post(
            ch,
            text=f"Context change review: {row.proposal.title}",
            blocks_=blocks.proposal_review_blocks(row),
        )

    workflow = ProposalWorkflow(
        settings,
        cfg,
        core.proposals,
        core.audit,
        core.deployer,
        github_factory=lambda: GitHubClient.from_env(cfg.github.repository, api_url=cfg.github.api_url),
        context_dir=core.context_dir,
        notify_review=notify_review,
        alert=core.alert,
    )
    core.workflow = workflow
    if with_improvement:
        engine = ImprovementEngine(
            settings,
            config=cfg,
            proposals=core.proposals,
            failures=core.failures,
            audit=core.audit,
            traces=core.traces,
            context_dir=core.context_dir,
            provider_factory=pf,
        )
        validator = ProposalValidator(
            settings,
            core.proposals,
            core.audit,
            context_dir=core.context_dir,
            agent_config=acfg,
            provider_factory=pf,
        )

        def improve(failure_id: str) -> None:
            def job() -> None:
                from contextforge.product.proposals.models import State

                try:
                    row = engine.process(failure_id)
                    if row is None:
                        return
                    if row.proposal.draft.kind == "human_decision":
                        if row.state == State.PROPOSED:
                            core.proposals.transition(
                                row.proposal_id,
                                State.BLOCKED,
                                actor="system.improver",
                                reason="awaiting a human business decision",
                            )
                            dr = row.proposal.draft.decision_request
                            if cfg.slack.review_channel_id and dr:
                                core._safe_post(
                                    cfg.slack.review_channel_id,
                                    text=f"Decision needed from {dr.decision_owner}: {dr.question}\n"
                                    + "\n".join(f"• {o}" for o in dr.options)
                                    + f"\n_Proposal `{row.proposal_id}`; record the decision as a context PR._",
                                )
                        return
                    if row.state == State.PROPOSED:
                        report = validator.validate(row.proposal_id)
                        if report["verdict"] == State.AWAITING_REVIEW.value:
                            workflow.open_pull_request(row.proposal_id)
                except (EngineBlocked, ProviderNotConfigured) as exc:
                    core.alert(f"improvement for {failure_id} BLOCKED: {exc}")
                except Exception as exc:
                    core.alert(f"improvement for {failure_id} failed: {type(exc).__name__}: {str(exc)[:200]}")

            threading.Thread(target=job, daemon=True, name=f"cf-improve-{failure_id}").start()

        core.on_failure = improve
    return core


# ---------------- commands ----------------


def cmd_check(args: argparse.Namespace) -> int:
    from contextforge.product.audit import AuditLog
    from contextforge.product.config import ProductConfigError
    from contextforge.product.deployment import ContextDeployer
    from contextforge.product.ops import readiness

    s = get_settings()
    out: dict[str, Any] = {}
    try:
        cfg = load_cfg(s)
        out["product_config"] = {
            "ok": True,
            "delivery_mode": cfg.delivery_mode,
            "pilot_users": len(cfg.users),
            "context_reviewers": len(cfg.context_reviewers),
            "answer_reviewers": len(cfg.answer_reviewers),
            "allowed_team_ids": len(cfg.slack.allowed_team_ids),
            "github_repository": cfg.github.repository,
        }
    except ProductConfigError as exc:
        out["product_config"] = {"ok": False, "error": str(exc)}
    db = ops_db(s)
    out["readiness"] = readiness(s, db, ContextDeployer(s, db, AuditLog(db)))
    env = {
        k: bool(os.environ.get(k))
        for k in (
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "SLACK_SIGNING_SECRET",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_APP_ID",
            "CONTEXTFORGE_GITHUB_TOKEN",
        )
    }
    out["credentials_present"] = env
    _print(out)
    return 0 if out["product_config"]["ok"] and out["readiness"]["ready"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    from contextforge.product.ops import purge_expired, serve_ops
    from contextforge.product.slack_app import SlackNotConfigured, build_app, run_socket_mode

    s = get_settings()
    state = {"connected": False}
    try:
        app, core = build_app(lambda gw: assemble(s, gw))
    except SlackNotConfigured as exc:
        return _blocked(str(exc), exc.missing)
    rel = core.deployer.active()
    if rel is None and not args.allow_working_tree:
        _print(
            {
                "error": "no context release deployed; run `contextforge context deploy --path context` (or --allow-working-tree for dev)"  # noqa: E501 - long literal
            }
        )
        return 2
    serve_ops(s, core.db, core.deployer, host=args.ops_host, port=args.ops_port, slack_state=state)
    interrupted = core.recover_on_startup()
    stop = threading.Event()

    def periodic() -> None:
        while not stop.wait(60):
            try:
                core.redeliver_pending()
                if core.workflow is not None:
                    from contextforge.product.proposals.models import State

                    for row in core.proposals.list(
                        [State.AWAITING_REVIEW, State.APPROVED, State.MERGE_ELIGIBLE]
                    ):
                        if row.raw.get("pr_number"):
                            core.workflow.sync(row.proposal_id)
                            if row.state == State.APPROVED:
                                core.workflow.check_merge_eligibility(row.proposal_id)
                purge_expired(core.db, core.cfg.retention_days)
            except Exception:
                from contextforge.observability.logging import get_logger

                get_logger("product").exception("periodic task failed")

    threading.Thread(target=periodic, daemon=True, name="cf-periodic").start()

    def shutdown(*_a: Any) -> None:
        stop.set()
        core.pool.shutdown(wait=True, cancel_futures=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    sys.stderr.write(
        json.dumps(
            {
                "event": "starting",
                "mode": "http" if args.http else "socket",
                "interrupted_requests": interrupted,
                "context_release": rel.version if rel else "working-tree",
            }
        )
        + "\n"
    )
    state["connected"] = True
    if args.http:
        app.start(port=args.http)
    else:
        try:
            run_socket_mode(app)
        except SlackNotConfigured as exc:
            return _blocked(str(exc), exc.missing)
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    from contextforge.product.audit import AuditLog
    from contextforge.product.deployment import ContextDeployer
    from contextforge.product.ops import serve_ops

    s = get_settings()
    db = ops_db(s)
    serve_ops(s, db, ContextDeployer(s, db, AuditLog(db)), host=args.ops_host, port=args.ops_port)
    sys.stderr.write(f"ops endpoints on http://{args.ops_host}:{args.ops_port}\n")
    while True:
        time.sleep(3600)


def _deployer(s: Settings) -> Any:
    from contextforge.product.audit import AuditLog
    from contextforge.product.deployment import ContextDeployer

    db = ops_db(s)
    return ContextDeployer(s, db, AuditLog(db))


def cmd_context(args: argparse.Namespace) -> int:
    from contextforge.product.deployment import DeploymentRejected

    s = get_settings()
    d = _deployer(s)
    actor = f"cli:{args.actor}"
    try:
        if args.context_cmd == "deploy":
            if args.git_commit:
                rel = d.deploy_git_commit(s.root, args.git_commit, base_branch=args.base_branch, actor=actor)
            else:
                rel = d.deploy(Path(args.path).resolve(), actor=actor, source_label=args.path)
            _print(
                {
                    "status": "DEPLOYED",
                    "version": rel.version,
                    "deployment_id": rel.deployment_id,
                    "content_hash": rel.content_hash,
                }
            )
        elif args.context_cmd == "rollback":
            rel = d.rollback(actor=actor, to_version=args.to)
            _print({"status": "ROLLED_BACK", "version": rel.version})
        elif args.context_cmd == "status":
            rel = d.active()
            ver = None
            if rel:
                try:
                    ver = d.verify().version
                except DeploymentRejected as exc:
                    ver = f"INVALID: {exc}"
            _print({"active": rel.version if rel else None, "verified": ver, "history": d.history(10)})
    except DeploymentRejected as exc:
        _print({"status": "REJECTED", "reason": str(exc), "problems": exc.problems[:20]})
        return 1
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    from contextforge.agent.providers.base import ProviderNotConfigured
    from contextforge.product.github import GitHubError, GitHubNotConfigured
    from contextforge.product.proposals.engine import EngineBlocked

    s = get_settings()

    class NullGateway:
        def post(self, *a: Any, **k: Any) -> str:
            raise RuntimeError("Slack not available in CLI mode")

        update = ephemeral = open_modal = post

    core = assemble(s, NullGateway(), with_improvement=False)
    from contextforge.product.proposals.engine import ImprovementEngine
    from contextforge.product.proposals.validation import ProposalValidator

    acfg = agent_config()
    try:
        if args.prop_cmd == "list":
            _print(
                [
                    {
                        "proposal_id": r.proposal_id,
                        "state": r.state.value,
                        "revision": r.revision,
                        "tier": r.review_tier,
                        "title": r.proposal.title,
                        "pr": r.raw.get("pr_url"),
                    }
                    for r in core.proposals.list()
                ]
            )
        elif args.prop_cmd == "show":
            row = core.proposals.get(args.id)
            _print(
                row.raw | {"body_json": row.proposal.body(), "validation_json": row.validation}
                if row
                else {"error": "not found"}
            )
        elif args.prop_cmd == "draft":
            eng = ImprovementEngine(
                s,
                config=core.cfg,
                proposals=core.proposals,
                failures=core.failures,
                audit=core.audit,
                traces=core.traces,
                context_dir=core.context_dir,
                provider_factory=provider_factory(acfg),
            )
            row = eng.process(args.failure_id)
            _print(
                {"proposal_id": row.proposal_id, "state": row.state.value}
                if row
                else {"result": "no proposal (not context-fixable or no action)"}
            )
        elif args.prop_cmd == "validate":
            v = ProposalValidator(
                s,
                core.proposals,
                core.audit,
                context_dir=core.context_dir,
                agent_config=acfg,
                provider_factory=provider_factory(acfg),
                repetitions=args.repetitions,
            )
            rep = v.validate(args.id)
            _print(rep)
            return (
                0
                if rep["verdict"] == "AWAITING_REVIEW"
                else (EXIT_BLOCKED if rep["verdict"] == "BLOCKED" else 1)
            )
        elif args.prop_cmd == "open-pr":
            row = core.workflow.open_pull_request(args.id)
            _print(
                {
                    "proposal_id": row.proposal_id,
                    "pr": row.raw.get("pr_url"),
                    "head_sha": row.raw.get("head_sha"),
                }
            )
        elif args.prop_cmd == "sync":
            _print({"state": core.workflow.sync(args.id).value})
        elif args.prop_cmd == "eligible":
            ok, reasons = core.workflow.check_merge_eligibility(args.id)
            _print({"merge_eligible": ok, "reasons": reasons})
            return 0 if ok else 1
        elif args.prop_cmd == "deploy":
            rel = core.workflow.deploy_merged(args.id, actor=f"cli:{args.actor}")
            _print({"status": "DEPLOYED", "version": rel.version})
    except GitHubNotConfigured as exc:
        return _blocked(str(exc), exc.missing)
    except (EngineBlocked, ProviderNotConfigured) as exc:
        return _blocked(str(exc), getattr(exc, "missing", ["model provider credentials"]))
    except (GitHubError, ValueError, KeyError) as exc:
        _print({"error": type(exc).__name__, "message": str(exc)})
        return 1
    return 0


def cmd_failures(args: argparse.Namespace) -> int:
    s = get_settings()
    db = ops_db(s)
    rows = db.query(
        "SELECT failure_id, trace_id, category, status, context_fix_appropriate, created_at FROM failures ORDER BY created_at DESC LIMIT ?",  # noqa: E501 - long literal
        (args.limit,),
    )
    _print([dict(r) for r in rows])
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from contextforge.product.audit import AuditLog

    s = get_settings()
    log = AuditLog(ops_db(s))
    if args.audit_cmd == "verify":
        rep = log.verify(args.expected_head)
        _print(rep.__dict__)
        return 0 if rep.ok else 1
    if args.audit_cmd == "export":
        events = log.events(resource_id=args.resource, limit=args.limit)
        Path(args.out).write_text(
            "\n".join(json.dumps(e, default=str) for e in events) + "\n", encoding="utf-8"
        )
        _print({"exported": len(events), "head": log.head(), "out": args.out})
    return 0


def cmd_pilot(args: argparse.Namespace) -> int:
    from contextforge.product.audit import AuditLog
    from contextforge.product.ops import export_requests_csv, pilot_report, purge_expired

    s = get_settings()
    db = ops_db(s)
    cfg = load_cfg(s)
    if args.pilot_cmd == "report":
        rep = pilot_report(db, cfg, AuditLog(db))
        if args.out:
            Path(args.out).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        _print(rep)
    elif args.pilot_cmd == "export":
        _print({"rows": export_requests_csv(db, cfg, Path(args.out)), "out": args.out})
    elif args.pilot_cmd == "purge":
        _print({"purged_requests": purge_expired(db, cfg.retention_days)})
    return 0


def cmd_opsdb(args: argparse.Namespace) -> int:
    s = get_settings()
    db = ops_db(s)
    if args.db_cmd == "migrate":
        _print({"applied": db.migrate(), "schema_version": db.schema_version()})
    elif args.db_cmd == "backup":
        dest = Path(args.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(db.path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)  # consistent online backup (SQLite backup API)
        src.close()
        dst.close()
        from contextforge.product.audit import AuditLog
        from contextforge.product.db import Database

        rep = AuditLog(Database(dest)).verify()
        _print({"backup": str(dest), "audit_chain_ok": rep.ok, "events": rep.events, "head": rep.head})
    return 0


def register(sub: Any) -> None:
    pr = sub.add_parser("product", help="Slack product runtime")
    ps = pr.add_subparsers(dest="product_cmd", required=True)
    ps.add_parser("check", help="validate config, credentials presence and readiness").set_defaults(
        fn=cmd_check
    )
    r = ps.add_parser("run", help="run the Slack app (Socket Mode by default) + ops endpoints")
    r.add_argument(
        "--http", type=int, default=None, help="serve Slack HTTP events on this port instead of Socket Mode"
    )
    r.add_argument("--ops-host", default=os.environ.get("CONTEXTFORGE_OPS_HOST", "127.0.0.1"))
    r.add_argument("--ops-port", type=int, default=int(os.environ.get("CONTEXTFORGE_OPS_PORT", "8088")))
    r.add_argument(
        "--allow-working-tree",
        action="store_true",
        help="dev only: serve the working-tree context without a release",
    )
    r.set_defaults(fn=cmd_run)
    o = ps.add_parser("ops", help="serve only /livez /readyz /metrics")
    o.add_argument("--ops-host", default="127.0.0.1")
    o.add_argument("--ops-port", type=int, default=8088)
    o.set_defaults(fn=cmd_ops)

    c = sub.add_parser("context", help="context releases: deploy / rollback / status")
    cs = c.add_subparsers(dest="context_cmd", required=True)
    d = cs.add_parser("deploy")
    g = d.add_mutually_exclusive_group(required=True)
    g.add_argument("--path")
    g.add_argument("--git-commit")
    d.add_argument("--base-branch", default="main")
    d.add_argument("--actor", required=True, help="who is deploying (recorded in the audit log)")
    rb = cs.add_parser("rollback")
    rb.add_argument("--to", default=None)
    rb.add_argument("--actor", required=True)
    cs.add_parser("status")
    for p in (d, rb):
        p.set_defaults(fn=cmd_context)
    cs.choices["status"].set_defaults(fn=cmd_context, actor="cli")

    pp = sub.add_parser("proposals", help="context-fix proposals")
    pps = pp.add_subparsers(dest="prop_cmd", required=True)
    pps.add_parser("list").set_defaults(fn=cmd_proposals)
    for name in ("show", "open-pr", "sync", "eligible"):
        x = pps.add_parser(name)
        x.add_argument("--id", required=True)
        x.set_defaults(fn=cmd_proposals)
    dr = pps.add_parser("draft")
    dr.add_argument("--failure-id", required=True)
    dr.set_defaults(fn=cmd_proposals)
    va = pps.add_parser("validate")
    va.add_argument("--id", required=True)
    va.add_argument("--repetitions", type=int, default=3)
    va.set_defaults(fn=cmd_proposals)
    de = pps.add_parser("deploy")
    de.add_argument("--id", required=True)
    de.add_argument("--actor", required=True)
    de.set_defaults(fn=cmd_proposals)

    f = sub.add_parser("failures", help="classified failures")
    f.add_argument("--limit", type=int, default=50)
    f.set_defaults(fn=cmd_failures)

    au = sub.add_parser("audit", help="append-only audit log")
    aus = au.add_subparsers(dest="audit_cmd", required=True)
    v = aus.add_parser("verify")
    v.add_argument("--expected-head", default=None)
    v.set_defaults(fn=cmd_audit)
    ex = aus.add_parser("export")
    ex.add_argument("--out", required=True)
    ex.add_argument("--resource", default=None)
    ex.add_argument("--limit", type=int, default=100000)
    ex.set_defaults(fn=cmd_audit)

    pl = sub.add_parser("pilot", help="pilot reporting")
    pls = pl.add_subparsers(dest="pilot_cmd", required=True)
    rp = pls.add_parser("report")
    rp.add_argument("--out", default=None)
    rp.set_defaults(fn=cmd_pilot)
    pe = pls.add_parser("export")
    pe.add_argument("--out", required=True)
    pe.set_defaults(fn=cmd_pilot)
    pls.add_parser("purge").set_defaults(fn=cmd_pilot)

    od = sub.add_parser("opsdb", help="operational database")
    ods = od.add_subparsers(dest="db_cmd", required=True)
    ods.add_parser("migrate").set_defaults(fn=cmd_opsdb)
    bk = ods.add_parser("backup")
    bk.add_argument("--out", required=True)
    bk.set_defaults(fn=cmd_opsdb)
