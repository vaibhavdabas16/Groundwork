"""Real MCP transport tests: spawn the server as a subprocess over stdio and drive it with the
official SDK client (initialize -> list_tools -> call_tool -> close)."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from contextforge.config.settings import Settings

pytestmark = [pytest.mark.mcp, pytest.mark.integration]

EXPECTED_TOOLS = {
    "search_assets",
    "get_asset",
    "get_definition",
    "get_join_path",
    "get_policy",
    "explain_query",
    "run_query",
    "validate_context",
}


def _params(settings: Settings, tmp: Path, principal: str) -> StdioServerParameters:
    env = {
        "CONTEXTFORGE_PRINCIPAL": principal,
        "CONTEXTFORGE_DATA_DIR": str(tmp),  # private audit DB
        "CONTEXTFORGE_SERVING_PATH": str(settings.serving_path),
        "CONTEXTFORGE_WAREHOUSE_PATH": str(settings.warehouse_path),
        "CONTEXTFORGE_CONTEXT_DIR": str(settings.context_dir),
        "CONTEXTFORGE_ROOT": str(settings.root),
    }
    for k in ("SYSTEMROOT", "PATH", "TEMP", "TMP"):
        if k in os.environ:
            env[k] = os.environ[k]
    return StdioServerParameters(
        command=sys.executable, args=["-m", "contextforge.mcp_server.server"], env=env, cwd=str(settings.root)
    )


def run_session(
    settings: Settings, tmp: Path, principal: str, body: Callable[[ClientSession], Awaitable[Any]]
) -> Any:
    async def main() -> Any:
        async with (
            stdio_client(_params(settings, tmp, principal)) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await body(session)

    return asyncio.run(asyncio.wait_for(main(), timeout=120))


def test_tool_discovery_and_schemas(real_warehouse: Settings, tmp_path: Path) -> None:
    async def body(s: ClientSession) -> Any:
        return (await s.list_tools()).tools

    tools = run_session(real_warehouse, tmp_path, "principal.local_analyst", body)
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED_TOOLS
    assert by_name["run_query"].input_schema["required"] == ["sql"]
    assert "request_context" in by_name["run_query"].input_schema["properties"]
    assert by_name["search_assets"].output_schema is not None
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


def test_context_tools_and_authorized_query_round_trip(
    real_warehouse: Settings, tmp_path: Path, serving_con: duckdb.DuckDBPyConnection
) -> None:
    sql = (
        "SELECT o.order_status, count(*) AS n, sum(t.items_price_total) AS sales "
        "FROM analytics.orders o JOIN analytics.order_item_totals t ON t.order_id = o.order_id "
        "GROUP BY 1 ORDER BY 1"
    )

    async def body(s: ClientSession) -> dict[str, Any]:
        out = {}
        rc = {"request_id": "mcp-test-1", "purpose": "integration test"}
        out["search"] = await s.call_tool(
            "search_assets", {"query": "delivered orders", "limit": 5, "request_context": rc}
        )
        out["asset"] = await s.call_tool("get_asset", {"asset_id": "asset.analytics.order_payments"})
        out["defn"] = await s.call_tool("get_definition", {"term_or_metric_id": "revenue"})
        out["join"] = await s.call_tool(
            "get_join_path", {"source_asset": "analytics.orders", "target_asset": "analytics.order_payments"}
        )
        out["policy"] = await s.call_tool(
            "get_policy", {"asset_or_column_id": "asset.raw.order_reviews:review_comment_message"}
        )
        out["explain"] = await s.call_tool("explain_query", {"sql": sql})
        out["query"] = await s.call_tool("run_query", {"sql": sql, "request_context": rc})
        return out

    out = run_session(real_warehouse, tmp_path, "principal.local_analyst", body)
    for name, res in out.items():
        assert not res.is_error, (name, res.content)
        assert res.structured_content["ok"], (name, res.structured_content)
        assert res.structured_content["context_version"].startswith("ctx-")
        assert res.structured_content["principal"] == "principal.local_analyst"

    search = out["search"].structured_content
    assert search["request_id"] == "mcp-test-1"
    assert any(r["id"] in ("metric.delivered_order_count", "term.delivered_order") for r in search["results"])
    assert all(
        {
            "id",
            "name",
            "type",
            "summary",
            "certification_status",
            "source_file",
            "context_version",
            "relevance_score",
        }
        <= set(r)
        for r in search["results"]
    )
    assert (
        out["asset"]
        .structured_content["asset"]["grain"]
        .startswith("one row per (order_id, payment_sequential)")
    )
    assert out["defn"].structured_content["resolution"] == "unsupported_concept"
    join = out["join"].structured_content
    assert (
        join["found"]
        and join["fanout_risk"]
        and join["steps"][0]["bridge"] == "asset.analytics.order_payment_totals"
    )
    pol = out["policy"].structured_content
    assert pol["sensitivity"] == "pii" and pol["queryable_by_caller"] is False
    assert out["explain"].structured_content["valid"] and out["explain"].structured_content["plan"]

    q = out["query"].structured_content
    assert q["query_id"] and q["truncated"] is False and q["columns"] == ["order_status", "n", "sales"]
    assert {a["asset_id"] for a in q["asset_refs"]} == {
        "asset.analytics.orders",
        "asset.analytics.order_item_totals",
    }
    expected = serving_con.execute(sql).fetchall()
    assert [(r[0], r[1], round(r[2], 2)) for r in q["rows"]] == [
        (a, b, round(float(c), 2)) for a, b, c in expected
    ]


def test_invalid_arguments_and_structured_errors(real_warehouse: Settings, tmp_path: Path) -> None:
    async def body(s: ClientSession) -> dict[str, Any]:
        return {
            "bad_limit": await s.call_tool("search_assets", {"query": "orders", "limit": 0}),
            "missing": await s.call_tool("run_query", {}),
            "wrong_type": await s.call_tool("get_asset", {"asset_id": 123}),
            "self_role": await s.call_tool(
                "run_query",
                {"sql": "SELECT 1 FROM analytics.orders", "request_context": {"role": "role.context_admin"}},
            ),
            "unknown_tool": await s.call_tool("drop_database", {}),
            "drop": await s.call_tool("run_query", {"sql": "DROP TABLE analytics.orders"}),
            "leak": await s.call_tool(
                "run_query", {"sql": "SELECT customer_unique_id FROM analytics.customers"}
            ),
            "admin": await s.call_tool("validate_context", {}),
            "unknown_asset": await s.call_tool("get_asset", {"asset_id": "asset.analytics.nope"}),
        }

    out = run_session(real_warehouse, tmp_path, "principal.local_analyst", body)
    # Protocol-level argument validation -> isError results
    for k in ("bad_limit", "missing", "wrong_type", "self_role", "unknown_tool"):
        assert out[k].is_error, k
    # Domain errors -> structured, machine-readable envelopes
    assert out["drop"].structured_content["error"]["code"] == "sql_rejected.not_a_query"
    assert out["leak"].structured_content["error"]["code"] == "sql_rejected.column_not_allowed"
    assert out["admin"].structured_content["error"]["code"] == "capability_denied"
    assert out["unknown_asset"].structured_content["error"]["code"] == "not_found"
    for k in ("drop", "leak", "admin", "unknown_asset"):
        assert out[k].structured_content["ok"] is False and out[k].structured_content["request_id"]


def test_server_principal_is_fixed_by_server_config(real_warehouse: Settings, tmp_path: Path) -> None:
    async def body(s: ClientSession) -> Any:
        return await s.call_tool("run_query", {"sql": "SELECT count(*) FROM analytics.orders"})

    res = run_session(real_warehouse, tmp_path, "principal.local_metadata", body)
    assert res.structured_content["ok"] is False
    assert res.structured_content["error"]["code"] == "capability_denied"
    assert res.structured_content["principal"] == "principal.local_metadata"


def test_http_transport_refused(real_warehouse: Settings) -> None:
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "contextforge.mcp_server.server", "streamable-http"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(real_warehouse.root),
        check=False,
    )
    assert proc.returncode == 2 and "disabled" in proc.stderr


def test_context_modes_over_real_stdio_same_interface_neutral_instructions(
    real_warehouse: Settings, tmp_path: Path
) -> None:
    """The experiment condition is fixed server-side; the tool interface is identical in every mode."""
    from contextforge.agent.mcp_client import McpToolClient, server_parameters
    from contextforge.config.settings import ContextMode

    async def one(mode: ContextMode) -> dict[str, Any]:
        params = server_parameters(
            real_warehouse, mode=mode, principal="principal.local_analyst", ops_dir=tmp_path / mode.value
        )
        async with McpToolClient(params) as c:
            assert c.session is not None
            init = await c.session.initialize()
            defn = await c.call("get_definition", {"term_or_metric_id": "revenue"})
            search = await c.call("search_assets", {"query": "revenue payment", "limit": 20})
            return {
                "tools": {n: (t.description, t.input_schema) for n, t in c.tools.items()},
                "instructions": getattr(init, "instructions", "") or "",
                "defn": defn,
                "search_types": {r["type"] for r in search.data["results"]},
                "mode": search.data["context_mode"],
            }

    res = {m: asyncio.run(asyncio.wait_for(one(m), 120)) for m in ContextMode}
    assert res[ContextMode.raw_schema]["tools"] == res[ContextMode.full_context]["tools"]
    assert res[ContextMode.descriptions]["tools"] == res[ContextMode.full_context]["tools"]
    for m in (ContextMode.raw_schema, ContextMode.descriptions):
        r = res[m]
        assert r["mode"] == m.value
        assert "draft" not in r["instructions"].lower() and "olist" not in r["instructions"].lower()
        assert r["defn"].error_code == "not_available_in_context_mode"
        assert r["search_types"] <= {"asset", "column"}
    full = res[ContextMode.full_context]
    assert full["defn"].ok and full["defn"].data["resolution"] == "unsupported_concept"
    assert "unsupported_concept" in full["search_types"] or "metric" in full["search_types"]
