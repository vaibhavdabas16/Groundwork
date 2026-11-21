"""Groundwork command-line interface. All commands use the same core services as the MCP server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from groundwork.config.settings import get_settings
from groundwork.observability.logging import configure_logging


def _print(obj: Any) -> None:
    if hasattr(obj, "model_dump_json"):
        sys.stdout.write(obj.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def cmd_acquire(args: argparse.Namespace) -> int:
    from groundwork.ingestion.acquire import AcquisitionError, acquire

    mode = "zip" if args.zip else "dir" if args.dir else "kaggle"
    try:
        m = acquire(
            get_settings(),
            mode=mode,
            zip_path=Path(args.zip) if args.zip else None,
            dir_path=Path(args.dir) if args.dir else None,
        )
    except AcquisitionError as exc:
        sys.stderr.write(f"ACQUISITION BLOCKED: {exc}\n")
        return 2
    _print({k: v for k, v in m.items() if k != "files"} | {"files": [f["file"] for f in m["files"]]})
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from groundwork.warehouse.build import build

    m = build(get_settings())
    _print({"raw_row_counts": m["raw_row_counts"], "serving_row_counts": m["serving_row_counts"]})
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    from groundwork.ingestion.acquire import load_manifest
    from groundwork.warehouse.build import build_manifest_path
    from groundwork.warehouse.quality import run_quality
    from groundwork.warehouse.report import write_report

    s = get_settings()
    report = run_quality(s)
    bm = json.loads(build_manifest_path(s).read_text()) if build_manifest_path(s).is_file() else None
    write_report(s.root / "docs" / "data_quality_report.md", report, load_manifest(s), bm)
    failed = report["invariants_failed"]
    drift = report["observation_baseline_drift"]
    _print(
        {
            "invariants_failed": failed,
            "observation_baseline_drift": drift,
            "checks": len(report["checks"]),
            "report": "docs/data_quality_report.md",
        }
    )
    return 1 if failed else 0


def cmd_validate(args: argparse.Namespace) -> int:
    from groundwork.context.validation import errors, validate_directory

    s = get_settings()
    store, issues = validate_directory(Path(args.path) if args.path else s.context_dir, s.limits)
    for i in issues:
        sys.stdout.write(str(i) + "\n")
    errs = errors(issues)
    sys.stdout.write(
        f"context {store.version}: {len(store.assets)} assets, {len(store.terms)} terms, {len(store.metrics)} metrics, "
        f"{len(store.joins)} joins, {len(store.unsupported)} unsupported concepts; "
        f"{len(errs)} errors, {len(issues) - len(errs)} warnings\n"
    )
    return 1 if errs else 0


def _service(args: argparse.Namespace) -> Any:
    from groundwork.service.core import GroundworkService

    return GroundworkService(get_settings(), principal_id=getattr(args, "principal", None))


def cmd_search(args: argparse.Namespace) -> int:
    _print(_service(args).search_assets(args.query, None, args.limit))
    return 0


def cmd_define(args: argparse.Namespace) -> int:
    _print(_service(args).get_definition(args.term))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    r = _service(args).run_query(args.sql)
    _print(r)
    return 0 if r.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from groundwork.mcp_server.server import serve

    serve(args.transport)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Liveness/readiness: context valid, serving DB present, a trivial governed query succeeds."""
    s = get_settings()
    out: dict[str, Any] = {"serving_db": s.serving_path.is_file(), "context_dir": s.context_dir.is_dir()}
    try:
        svc = _service(args)
        out["context_version"] = svc.store.version
        r = svc.run_query("SELECT count(*) AS n FROM analytics.orders")
        out["query_ok"] = r.ok
        out["error"] = r.error.model_dump() if r.error else None
    except Exception as exc:
        out["query_ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    _print(out)
    return 0 if out.get("query_ok") else 1


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    p = argparse.ArgumentParser(prog="groundwork")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire", help="download (Kaggle public endpoint) or import the Olist dataset")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--zip", help="manually downloaded archive.zip")
    g.add_argument("--dir", help="directory containing the 9 original CSV files")
    a.set_defaults(fn=cmd_acquire)
    sub.add_parser("build", help="build raw + serving DuckDB warehouses").set_defaults(fn=cmd_build)
    sub.add_parser("quality", help="run data-quality checks and write the report").set_defaults(
        fn=cmd_quality
    )
    v = sub.add_parser("validate-context", help="validate the context repository")
    v.add_argument("--path")
    v.set_defaults(fn=cmd_validate)
    for name, fn, arg in (
        ("search", cmd_search, "query"),
        ("define", cmd_define, "term"),
        ("query", cmd_query, "sql"),
    ):
        sp = sub.add_parser(name)
        sp.add_argument(arg)
        sp.add_argument("--principal")
        if name == "search":
            sp.add_argument("--limit", type=int, default=10)
        sp.set_defaults(fn=fn)
    sv = sub.add_parser("serve", help="start the MCP server")
    sv.add_argument("--transport", default="stdio")
    sv.set_defaults(fn=cmd_serve)
    from groundwork.agent_cli import register
    from groundwork.product_cli import register as register_product

    register(sub)
    register_product(sub)
    h = sub.add_parser("health")
    h.add_argument("--principal")
    h.set_defaults(fn=cmd_health)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
