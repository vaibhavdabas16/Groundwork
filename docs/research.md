# Research notes (Phase 1)

Sources were read during Phase 1. Where a doc page and the **installed** package disagreed, the
installed package won, and the difference is recorded below. Claims not confirmed on a page are
marked UNVERIFIED.

## 1. Atlan: relevant patterns

### `atlanhq/agent-toolkit` (https://github.com/atlanhq/agent-toolkit)
- The README says the **local MCP server in this repo is deprecated** (maintenance-only).
  Atlan recommends its hosted, OAuth-protected remote MCP instead.
  - **ContextForge takes no dependency on it, on `pyatlan`, or on any Atlan API.**
- Local tools exposed:

  | Area | Tools |
  |---|---|
  | Discovery and governance | `search_assets`, `get_assets_by_dsl`, `traverse_lineage` |
  | Updates | `update_assets` (description and certificate only) |
  | Glossary authoring | `create_glossaries` / `create_glossary_categories` / `create_glossary_terms` |
  | Data quality | DQ rule tools |
  | Querying | `query_asset` |

- `RESTRICTED_TOOLS` env var: a deny-list that hides and blocks tools and logs the decision.
  - Its naming is inconsistent: the deny-list uses `*_tool` names that don't match the tool names.

### Build a context layer (https://docs.atlan.com/agents/how-tos/build-a-context-layer)
- Start narrow: one domain, one team, one AI client.
- Keep 5-10 real business questions as a regression set.
- AI enrichment fills only missing attributes and never overwrites curated values.
- Certificates are Verified / Draft / Deprecated.
- "AI-drafted context is a starting point, not the final product."
- Expose it through MCP, test, fix context at the source, and repeat.

### MCP gateway vs context layer (https://atlan.com/know/ai-agent/mcp-gateway-vs-context-layer/)
- The gateway answers "may this caller reach this tool?" (authn, policy, routing, audit).
- The context layer answers "is the answer right?" (certified definitions, lineage, policy based
  on what data *means*, freshness).
- They are separate guarantees from separate infrastructure.
- The page's quoted accuracy-gain numbers are vendor research, not independently verified.

## 2. What we borrow
| Idea | Where it lives in ContextForge |
|---|---|
| Tool vocabulary (`search_assets`, asset lookup, glossary lookup, lineage/joins, governed query) | `src/contextforge/mcp_server/server.py` (8 tools) |
| Verified / Draft / Deprecated certification | `context/*` `certification.status`; enforced in `context/validation.py` |
| AI drafts are a starting point, humans certify | Validation rejects AI/agent self-certification; `docs/certification_procedure.md` |
| Policy based on meaning (sensitivity classes on columns), not only caller | `context/policies/policy.yaml` + `security/policy.py` |
| Tool deny/allow with logging | `tool_permissions` capability map + audit log (`observability/audit.py`) |
| Golden-question regression set | **Phase 2** (evaluation harness) |
| Gateway vs context layer split | Authorization (`security/policy.py`) is kept apart from context (`context/`, `search/`) |

## 3. What we implement independently
- The typed YAML context schema: assets, glossary, metrics, joins, ownership, unsupported register, policy.
- Cross-document validation.
- BM25 retrieval.
- The SQLGlot-based SQL guard.
- The killable DuckDB worker process.
- The DuckDB warehouse build from the original Olist CSVs.
- The MCP adapter on the official SDK.

None of this is copied from Atlan code.

## 4. Security and operational constraints

### DuckDB (docs: https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview, now 2.0)
Doc guidance:
- "Treat SQL in DuckDB like code in Bash or Python"; settings are defence in depth, "not a
  substitute for proper sandboxing".
- Timeouts must be enforced by the application.

Settings used, all **verified locally** on DuckDB 2.0.0 by running them:
- `enable_external_access=false` blocks `read_csv`, `read_text`, `glob`, file `ATTACH`, `COPY` and `INSTALL`.
- `autoload_known_extensions=false`, `autoinstall_known_extensions=false`.
- `allow_community_extensions=false`, `allow_unsigned_extensions=false`, `allow_persistent_secrets=false`.
- `memory_limit`, `threads`, `max_temp_directory_size`.
- `lock_configuration=true`: `SET enable_external_access=true` and `SET memory_limit` then fail.
- `read_only=True` connection: `CREATE` fails.

Previously UNVERIFIED in the docs, now verified locally: a database file passed to
`duckdb.connect(path, read_only=True, config={... enable_external_access: False ...})` **is**
queryable. External access applies to *other* files.

DuckDB error messages include file paths. The worker redacts quoted fragments before errors cross
the trust boundary.

### MCP stdio
Never write to stdout in a stdio server. All logging is JSON to stderr
(`observability/logging.py`), and the CLI only writes to stdout for non-server commands.

## 5. Docs vs installed SDK versions

| Package | Installed | Notes |
|---|---|---|
| `mcp` | **2.3.0** | v2 renamed `FastMCP` to `mcp.server.mcpserver.MCPServer`; importing `mcp.server.fastmcp` raises `ModuleNotFoundError` (verified). Result fields are snake_case: `structured_content`, `is_error`, `input_schema`, `output_schema` (verified with a throwaway server/client). `stdio_client` + `ClientSession` still exist in 2.3.0 (UNVERIFIED in the docs, **verified by our round-trip tests**). Unexpected tool exceptions return a generic `is_error` message without internals (verified). `ToolAnnotations` fields are snake_case (`read_only_hint`). |
| `duckdb` | **2.0.0** | Settings listed above exist with these names (`duckdb_settings()`). Scalar Python UDFs need numpy (not installed), so pseudonyms are computed in Python and joined in. |
| `sqlglot` | **30.33.0** | `qualify(..., validate_qualify_columns=True, expand_stars=True)` with a caller-filtered schema rejects unknown columns, including inside subqueries, aggregates and CTEs. `AND`/`OR` are `Func` subclasses, so the guard treats them as structural. `LOAD x` parses to `Command` (rejected). |
| `pydantic` | 2.14.4 | |

**Practical consequence:** the v1-era docs and most tutorials (`FastMCP`, `result.structuredContent`)
do **not** work with the installed SDK. The code targets the v2 API, and `pyproject.toml` pins
`mcp>=2.3,<3`.

## 6. Dataset provenance and license
- Brazilian E-Commerce Public Dataset by Olist: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
- **License: CC BY-NC-SA 4.0**, verified from Kaggle's public metadata endpoint
  (`/api/v1/datasets/view/olistbr/brazilian-ecommerce`, field `licenseNameNullable`) and recorded in
  `data/manifests/olist_source_manifest.json`.
  - Non-commercial, share-alike.
  - The CSVs are **not** committed (`.gitignore`); only checksums and manifests are.
- The data is real but anonymised. Company and partner names in review text were replaced by Game of
  Thrones house names. Covers 2016-09-04 to 2018-10-17.
- The public download endpoint worked **anonymously** (no Kaggle account or credentials used).
  Optional `KAGGLE_USERNAME`/`KAGGLE_KEY` are supported in case that changes.
