# Phase 1 handoff - ContextForge

Everything below was executed in this environment unless it is marked otherwise.
The status of each component is in `docs/IMPLEMENTATION_STATUS.md`.

## 1. Architecture actually implemented
The diagram is in `README.md`. In layers:

| Layer | Module | Responsibility |
|---|---|---|
| A. Raw data and ingestion | `ingestion/acquire.py`, `warehouse/build.py` (`raw` schema), `warehouse/quality.py` | Download/import, checksums, license, typed load, DQ |
| B. Safe analytical access | `warehouse/build.py: SERVING_SQL` → `olist_serving.duckdb` (`analytics` schema) | Approved relations, pseudonyms, order-grain bridges |
| C. Governed context storage | `context/` (YAML) + `context/models.py`, `loader.py`, `validation.py` | Typed, cross-validated, content-versioned |
| D. Context retrieval | `search/index.py` | Deterministic BM25 with visibility filters |
| E. MCP protocol adapter | `mcp_server/server.py` | Thin; forwards to the service |
| F. Authorization and policy | `security/policy.py` | Principal → roles → capabilities; per-caller catalog; column sensitivity |
| G. Query execution | `security/sql_guard.py` → `query/executor.py` → `query/worker.py` | AST validation, killable read-only worker |
| Shared core | `service/core.py` (`ContextForgeService`) | Single entry point: auth, budget, audit, typed responses |

## 2. Important design decisions
- **Two physical DuckDB files.** The raw DB holds all source columns. The serving DB holds no raw
  sensitive values at all, so a bypassed validator still cannot leak them.
- **The SQL guard regenerates SQL** from a qualified AST, validated against a *caller-filtered*
  schema. Column allow-listing is done by SQLGlot qualification, not by string matching.
- **One process per query** (`python -I`, minimal env, temp CWD), killed on timeout. This costs
  about 0.4 s per query on Windows; it was chosen for an enforceable timeout.
- **Pseudonyms** are HMAC-SHA256 computed in Python. DuckDB Python UDFs would need numpy, and
  computing in Python keeps the key out of SQL text.
- **Order-grain bridge tables** (`order_item_totals`, `order_payment_totals`, `order_review_summary`)
  make fanout-free order-level analysis the easy path.
- **Unsupported/ambiguous register** (`context/unsupported/`). `get_definition` returns these ahead
  of any candidate metric, so agents surface ambiguity instead of guessing.
- **Every AI-authored definition is `draft`.** Placeholder owners make certification structurally
  impossible until humans are added.
- **mcp v2 API** (`MCPServer`) is used, because v1 `FastMCP` does not exist in the installed SDK.

## 3. Repository structure
```
src/contextforge/{config,ingestion,warehouse,context,search,security,query,service,mcp_server,observability,common}/, cli.py
context/{assets/{serving,raw},glossary,metrics,joins,policies,ownership,unsupported}/
data/manifests/   olist_source_manifest.json, warehouse_build_manifest.json, dq_baseline.json, dq_report.json
tests/{unit,integration,security,mcp}/
docs/             research.md, security.md, certification_procedure.md, data_quality_report.md,
                  IMPLEMENTATION_STATUS.md, PHASE_1_HANDOFF.md
.github/workflows/ci.yml, Dockerfile, docker-compose.yml, Makefile, .env.example, AGENTS.md
```

## 4. Dataset provenance and checksums
- Source: https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce (dataset id 55151; Kaggle
  `current_version_number` 2; last updated 2021-10-01). Retrieved during Phase 1 via the
  anonymous public endpoint (exact timestamp in `data/manifests/`). A second, independent download produced identical hashes.
- License: **CC BY-NC-SA 4.0** (Kaggle metadata API). The data is not committed.
- Archive SHA-256: `967e41e04fc306fe604e2a693f488995a8b41e5047418f8a5c8e4abd6deca784`

| file | SHA-256 | bytes |
|---|---|---|
| olist_customers_dataset.csv | `983a422239e1712ded753b3bf9ecf47dc73f144d306029dcfa99e70a226883d2` | 9,033,957 |
| olist_geolocation_dataset.csv | `b514f6fc991b9566aeba02aa5d67e2c3630f034b60a0e05aa0d082a3b66d88d6` | 61,273,883 |
| olist_order_items_dataset.csv | `0bc4d068c4fe38cbb01bd90e8746e3c613fe7b4baef75fab7b0e329701c3e279` | 15,438,671 |
| olist_order_payments_dataset.csv | `4f713964f2815dbbaa40b9488268c55aac3627bfce5aa96cf58d1f3616de3cc0` | 5,777,138 |
| olist_order_reviews_dataset.csv | `012b61c7593e34f51fa614efdf802b9c7056ce6aae5307ddb93236e7cfc797d7` | 14,451,670 |
| olist_orders_dataset.csv | `8df58ef3d2d7e9944010f7beecd9b75367f5588ec6e3c91cec19ae3345ef9ecf` | 17,654,914 |
| olist_products_dataset.csv | `3e6569628a17fbc75fd206ee357b59e20364b9afa90f5b6cd5b4d624c58aa9cc` | 2,379,446 |
| olist_sellers_dataset.csv | `1f643d2b950373b85735e7794b20986f528d7a000432e7c6f9bcbb44d0846a0e` | 174,703 |
| product_category_name_translation.csv | `a81f0d1f27b27e7293f761bc79e3ce8f348ee39c4b3ed3e49bde38f478586278` | 2,613 |

## 5. Actual database row counts
Raw (equal to the Python `csv` record counts):
- orders 99,441
- customers 99,441
- order_items 112,650
- order_payments 103,886
- order_reviews 99,224
- products 32,951
- sellers 3,095
- geolocation 1,000,163
- product_category_name_translation 71

Serving (`analytics.*`):
- The same counts for orders, customers, order_items, order_payments, order_reviews, products,
  sellers and translation.
- geolocation_zip_prefix 19,015
- order_item_totals 98,666
- order_payment_totals 99,440
- order_review_summary 98,673

Key data findings (all in `docs/data_quality_report.md`):
- 96,096 distinct people versus 99,441 customer_ids.
- 789 duplicate review_ids; 547 orders with multiple reviews; 768 with none.
- 775 orders without items; 1 order without payments.
- 303 orders where payments ≠ items + freight.
- Naive payments × items × reviews join inflates payments by **BRL 4,407,970.42**.
- Last purchase 2018-10-17.

## 6. Context definitions implemented (all `draft`, 1 `deprecated`)
- **Assets:** 21 (12 serving, 9 raw for lineage only).
- **Glossary terms:** 17.
- **Joins:** 11, each with cardinality and fanout/bridge guidance.
- **Unsupported/ambiguous concepts:** 13 — net revenue, revenue, GMV, AOV, active customer, churn,
  profit/margin, CLV, return rate, conversion rate, seller rating, relative time windows, inventory.
- **Metrics:** 16. Values were executed during Phase 1:

| metric | value |
|---|---|
| order_count | 99,441 |
| delivered_order_count | 96,478 |
| canceled_order_count | 625 |
| cancellation_rate | 0.006285 |
| product_sales_value | 13,591,643.70 |
| freight_value_total | 2,251,909.54 |
| items_gross_value | 15,843,553.24 |
| payment_value_total | 16,008,872.12 (reconciliation recorded) |
| average_order_product_value | 137.75 |
| distinct_customers | 96,096 |
| repeat_customer_rate | 0.03119 |
| average_delivery_days | 12.558 |
| on_time_delivery_rate | 0.93227 |
| average_review_score | 4.0868 |
| active_seller_count | 3,095 |
| payment_value_naive_item_join | 20,308,134.71 — **deprecated** anti-pattern |

## 7. MCP tool inventory
All tools are read-only (annotation `read_only_hint=True`). Every response carries `ok`,
`request_id`, `context_version`, `principal`, `error{code,message}` and `warnings`.

| Tool | Capability | Key response fields |
|---|---|---|
| search_assets(query, filters, limit, request_context) | metadata.read | results[id,name,type,summary,certification_status,deprecated,source_file,context_version,relevance_score,matched_terms,queryable], missing_context_explanation |
| get_asset(asset_id) | metadata.read | asset, certification_status, queryable, queryable_columns, withheld_columns, joins |
| get_definition(term_or_metric_id) | metadata.read | resolution (metric / glossary_term / unsupported_concept / ambiguous / not_found / not_visible), definition, candidates |
| get_join_path(source_asset, target_asset) | metadata.read | steps[cardinality, causes_fanout, fanout_warning, bridge], fanout_risk, sql_join_hint |
| get_policy(asset_or_column_id) | metadata.read | sensitivity, queryable_by_caller, reason, handling, caller capabilities and limits |
| explain_query(sql) | query.explain | query_id, executable_sql, relations, columns_read, plan, estimated_max_cardinality, within_cost_limit |
| run_query(sql, request_context) | query.execute | query_id, columns, rows, row_count, truncated, truncation_reason, asset_refs, max_sensitivity, elapsed_ms |
| validate_context() | context.admin | valid, issues (denied to both agent principals) |

## 8. Security controls
See `docs/SECURITY.md`. In summary:
- server-side identity;
- capability map (fail closed);
- SQLGlot AST guard with a column allow-list;
- a physically separate serving DB with pseudonyms;
- a read-only, external-access-disabled, configuration-locked DuckDB worker process with a hard kill;
- row, byte, concurrency, cost and tool-call limits;
- sanitized errors plus DLP redaction;
- an audit log;
- the HTTP transport refused.

## 9. Exact commands executed (this session)
```
uv init --lib --name contextforge --python 3.12 --no-readme .
uv add "mcp>=1.0" duckdb "pydantic>=2" sqlglot pyyaml httpx
uv add --dev pytest ruff mypy types-PyYAML pytest-timeout
uv run contextforge acquire                # equivalent first run via python -c acquire(mode='kaggle')
uv run contextforge build
uv run contextforge quality
uv run contextforge validate-context
uv run contextforge search "revenue" --limit 5
uv run contextforge query "SELECT p.product_category_name_english ... LIMIT 3"
uv run contextforge health
uv run ruff check src tests ; uv run ruff format src tests ; uv run mypy
uv run pytest tests/unit | tests/integration | tests/security | tests/mcp | (all)
# from-zero verification in a clean copy (no .venv, no data):
uv sync --frozen && uv run contextforge acquire && uv run contextforge build && \
  uv run contextforge quality && uv run contextforge validate-context && uv run pytest -q
```

## 10. Actual test results
Final runs:
- `ruff check`: all checks passed.
- `mypy` (strict): no issues in 33 source files.
- unit: 96 passed
- integration: 22 passed
- security: 63 passed
- mcp: 5 passed
- **Total: 186 passed, 0 failed, 0 skipped.** This was reproduced in the clean from-zero copy.

Failures found and fixed during development:

| Failure | Cause | Fix |
|---|---|---|
| Context validation: YAML `on:` key | YAML 1.1 parses `on` as boolean `true`, so the join schema rejected it | Renamed the field to `join_columns` |
| SQL guard rejected `AND` | SQLGlot models connectors as `Func` subclasses | Treat connectors as structural nodes |
| Integration: build failed when the DB file was named `raw.duckdb` | Catalog/schema name ambiguity | Fully qualify with `current_database()` |
| **Repeatability test** found nondeterminism | `mode()` ties in the geolocation aggregate, and `arg_max` ties on equal review timestamps | Explicit deterministic tie-breaks |
| Test bugs | Decimal × float; a visibility assertion that ignored the by-design visible unsupported register | Corrected; the stronger assertion is documented in the test |

## 11. Remaining blockers
- **No human certifiers exist.** Nothing can be `verified` until real owners and reviewers are added
  (`docs/certification_procedure.md`). This blocks meaningful `verified_only` evaluation in Phase 2.
- **Remote MCP needs authentication.** It requires an OAuth resource server (`TokenVerifier`), a
  token-to-principal mapping, and TLS.
- **Not run here:** Docker, `make`, and GitHub Actions (no Docker or make installed; no remote repo).
- **No git commits** have been made yet.

## 12. Known limitations
- About 0.4 s of process-spawn overhead per query. A pre-forked worker pool is possible later; it
  must keep the kill-on-timeout semantics.
- On Windows there is no OS memory rlimit for workers.
- The cost limit is advisory, based on cardinality estimates.
- BM25 has no semantic matching; synonyms must be curated in context.
- Metric SQL returns whole-dataset values. Dimensioned or windowed variants are expressed as
  `supported_dimensions` metadata, not as parameterised templates.
- The time zone of source timestamps is undocumented (assumed Brazil local; UNVERIFIED).
- The DLP patterns are heuristic.
- The audit log is local SQLite.

## 13. What Phase 2 must reuse
- `ContextForgeService` is the only API for the agent, eval harness and Slack. Construct it with a
  principal. For example, use `principal.local_strict` vs `principal.local_analyst` to compare
  context modes.
- The context models, loader and validation, `SearchIndex`, `PolicyEngine`, `validate_sql` and
  `QueryExecutor` (through the service).
- The metric SQL and `double_counting_check` as gold answers for the evaluation set.
- `context/unsupported/` as expected "should decline or clarify" cases.
- The MCP server as the agent's tool surface. `tests/mcp` shows the client pattern for the
  installed SDK v2.
- The audit log's `request_id` and `query_id` for eval traceability.

## 14. What Phase 2 must not rewrite unnecessarily
- The SQL guard, the worker isolation and the serving-DB separation. Extend the allow-lists
  through policy plus review; don't bypass them.
- The certification rules: the eval and proposal system must create **drafts** and PRs, never
  verified content.
- The warehouse build and DQ baseline: rebuild only from checksum-verified sources.
- The response envelopes (`service/schemas.py`): add fields, don't break existing ones.
