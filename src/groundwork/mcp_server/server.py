"""MCP protocol adapter (official MCP Python SDK v2, ``MCPServer``).

This module is deliberately thin: each tool forwards to ``GroundworkService``, which owns
authorization, policy, SQL validation, execution and audit. Caller identity is fixed at process
start (``GROUNDWORK_PRINCIPAL``) - no tool argument can change it.

Transports:
* ``stdio`` (default, local development).
* ``streamable-http`` is REFUSED: remote exposure stays disabled until authentication (OAuth 2.1
  resource server / TokenVerifier) and per-token principal mapping are implemented and tested.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from groundwork import __version__
from groundwork.config.settings import Settings, get_settings
from groundwork.observability.logging import configure_logging, get_logger
from groundwork.service.core import GroundworkService
from groundwork.service.schemas import (
    AssetResponse,
    DefinitionResponse,
    ExplainResponse,
    JoinPathResponse,
    PolicyResponse,
    QueryResponse,
    RequestContext,
    SearchFilters,
    SearchResponse,
    ValidateContextResponse,
)

log = get_logger("mcp")

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

INSTRUCTIONS = (
    "Groundwork exposes a governed business-context layer and a read-only analytics warehouse "
    "(Olist Brazilian e-commerce, 2016-09..2018-10). Before writing SQL: search_assets / "
    "get_definition to find certified or draft definitions, get_join_path to avoid join fanout. "
    "Draft definitions are NOT human-verified - say so. Unsupported/ambiguous concepts must not be "
    "guessed. Only the analytics schema is queryable; run_query enforces row/time limits."
)
# Used for the RAW_SCHEMA / DESCRIPTIVE_METADATA experiment conditions: must not leak curated semantics.
NEUTRAL_INSTRUCTIONS = (
    "Read-only analytics warehouse. Use the tools to discover tables and columns and to run "
    "read-only SELECT queries over the analytics schema; run_query enforces row/time limits."
)


def build_server(service: GroundworkService) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(
        name="groundwork",
        title="Groundwork",
        version=__version__,
        instructions=INSTRUCTIONS if service.full_context else NEUTRAL_INSTRUCTIONS,
        log_level="WARNING",
    )

    @server.tool(annotations=READ_ONLY)
    def search_assets(
        query: Annotated[
            str,
            Field(
                min_length=1, max_length=500, description="Keywords: table, column, metric, term or synonym"
            ),
        ],
        filters: SearchFilters | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        request_context: RequestContext | None = None,
    ) -> SearchResponse:
        """Search governed context (assets, columns, glossary terms, metrics, joins, unsupported concepts)."""
        return service.search_assets(query, filters, limit, request_context)

    @server.tool(annotations=READ_ONLY)
    def get_asset(
        asset_id: Annotated[
            str, Field(min_length=1, max_length=200, description="Stable asset id or schema.table")
        ],
        request_context: RequestContext | None = None,
    ) -> AssetResponse:
        """Full documentation of one asset: grain, columns, keys, quality warnings, lineage, certification."""
        return service.get_asset(asset_id, request_context)

    @server.tool(annotations=READ_ONLY)
    def get_definition(
        term_or_metric_id: Annotated[
            str, Field(min_length=1, max_length=200, description="Metric/term id, name or synonym")
        ],
        request_context: RequestContext | None = None,
    ) -> DefinitionResponse:
        """Resolve a metric, glossary term or unsupported/ambiguous concept by id, name or synonym."""
        return service.get_definition(term_or_metric_id, request_context)

    @server.tool(annotations=READ_ONLY)
    def get_join_path(
        source_asset: Annotated[str, Field(min_length=1, max_length=200)],
        target_asset: Annotated[str, Field(min_length=1, max_length=200)],
        request_context: RequestContext | None = None,
    ) -> JoinPathResponse:
        """Documented join path between two assets, with cardinality and fanout warnings."""
        return service.get_join_path(source_asset, target_asset, request_context)

    @server.tool(annotations=READ_ONLY)
    def get_policy(
        asset_or_column_id: Annotated[
            str, Field(min_length=1, max_length=250, description="asset id, or asset_id:column")
        ],
        request_context: RequestContext | None = None,
    ) -> PolicyResponse:
        """Sensitivity classification and whether the calling principal may query an asset or column."""
        return service.get_policy(asset_or_column_id, request_context)

    @server.tool(annotations=READ_ONLY)
    def explain_query(
        sql: Annotated[str, Field(min_length=1, max_length=20000)],
        request_context: RequestContext | None = None,
    ) -> ExplainResponse:
        """Validate SQL against governance policy and return the normalised SQL and an advisory query plan."""
        return service.explain_query(sql, request_context)

    @server.tool(annotations=READ_ONLY)
    def run_query(
        sql: Annotated[
            str,
            Field(min_length=1, max_length=20000, description="A single read-only SELECT over analytics.*"),
        ],
        request_context: RequestContext | None = None,
    ) -> QueryResponse:
        """Execute one validated read-only SELECT with row, size, time and concurrency limits."""
        return service.run_query(sql, request_context)

    @server.tool(annotations=READ_ONLY)
    def validate_context(request_context: RequestContext | None = None) -> ValidateContextResponse:
        """Administrative: re-validate the context repository. Requires the context.admin capability."""
        return service.validate_context(request_context)

    return server


def serve(transport: str = "stdio", settings: Settings | None = None) -> None:
    configure_logging()
    if transport != "stdio":
        log.error("refusing non-stdio transport", extra={"transport": transport})
        sys.stderr.write(
            "Remote MCP transports are disabled: authentication/authorization for HTTP is not implemented.\n"
            "Use stdio. See docs/security.md#remote-exposure.\n"
        )
        raise SystemExit(2)
    settings = settings or get_settings()
    service = GroundworkService(settings)
    log.info(
        "starting MCP server",
        extra={
            "principal": service.principal_id,
            "context_version": service.store.version,
            "context_mode": service.mode.value,
        },
    )
    try:
        build_server(service).run("stdio")
    finally:
        service.close()


def main() -> None:
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    serve(transport)


if __name__ == "__main__":
    main()
