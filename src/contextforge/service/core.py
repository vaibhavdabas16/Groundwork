"""ContextForge core service: the single entry point for every interface.

Every public method: (1) checks the tool permission for the server-side principal, (2) enforces the
per-request tool-call budget, (3) does its work through the policy engine, and (4) writes an audit
record. Interfaces (MCP, CLI, future Slack/eval) must call these methods and nothing lower-level.
"""

from __future__ import annotations

import heapq
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, TypeVar

from contextforge.config.settings import ContextMode, Settings
from contextforge.context.loader import ContextStore, load_context
from contextforge.context.models import Asset, Capability, Status
from contextforge.context.validation import errors, validate_store
from contextforge.observability.audit import AuditLog
from contextforge.observability.logging import get_logger
from contextforge.query.executor import ExecutionError, QueryExecutor
from contextforge.search.index import SearchIndex, tokenize
from contextforge.security.policy import AccessDenied, PolicyEngine
from contextforge.security.sql_guard import CURRENT_DATE_WARNING, SqlRejected, validate_sql
from contextforge.service.schemas import (
    AssetRef,
    AssetResponse,
    DefinitionResponse,
    Envelope,
    ErrorInfo,
    ExplainResponse,
    JoinPathResponse,
    JoinStep,
    PolicyResponse,
    QueryResponse,
    RequestContext,
    ResultType,
    SearchFilters,
    SearchResponse,
    SearchResultItem,
    ValidateContextResponse,
)

log = get_logger("service")
E = TypeVar("E", bound=Envelope)

_REVERSE = {"one_to_many": "many_to_one", "many_to_one": "one_to_many"}


class ContextError(RuntimeError):
    pass


class _Budget:
    """Bounded in-memory map of request_id -> tool calls used."""

    def __init__(self, limit: int, max_tracked: int = 10_000) -> None:
        self.limit = limit
        self.max_tracked = max_tracked
        self._used: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, request_id: str) -> None:
        with self._lock:
            n = self._used.pop(request_id, 0) + 1
            self._used[request_id] = n
            while len(self._used) > self.max_tracked:
                self._used.popitem(last=False)
        if n > self.limit:
            raise AccessDenied(
                "tool_call_budget_exceeded", f"request {request_id} exceeded {self.limit} tool calls"
            )


class ContextForgeService:
    def __init__(
        self,
        settings: Settings,
        principal_id: str | None = None,
        *,
        strict_context: bool = True,
        context_mode: ContextMode | None = None,
    ) -> None:
        self.settings = settings
        self.mode = ContextMode(context_mode or settings.context_mode)
        self.store: ContextStore = load_context(settings.context_dir)
        issues = validate_store(self.store, settings.limits)
        errs = errors(issues)
        if errs and strict_context:
            raise ContextError("context repository is invalid:\n" + "\n".join(str(e) for e in errs[:20]))
        self.context_issues = issues
        pol = self.store.policy
        self.principal_id = principal_id or settings.principal_id
        hard_max_rows = min(settings.limits.max_rows, pol.query.default_max_rows)
        self.policy = PolicyEngine(self.store, self.principal_id, hard_max_rows)
        limits = settings.limits.model_copy(
            update={
                "timeout_seconds": min(settings.limits.timeout_seconds, pol.query.timeout_seconds),
                "max_concurrent_queries": min(
                    settings.limits.max_concurrent_queries, pol.query.max_concurrent_queries
                ),
                "max_tool_calls_per_request": min(
                    settings.limits.max_tool_calls_per_request, pol.query.max_tool_calls_per_request
                ),
            }
        )
        self.limits = limits
        self.allowed_functions = frozenset(f.upper() for f in pol.query.allowed_functions)
        self.index = SearchIndex(self.store, mode=self.mode.value)
        self.executor = QueryExecutor(settings.serving_path, limits)
        self.budget = _Budget(limits.max_tool_calls_per_request)
        self.audit = AuditLog(
            settings.data_dir / "ops" / "audit.sqlite",
            pol.retention.query_audit_log_days,
            pol.retention.store_sql_text,
        )

    # ------------------------------------------------------------------ plumbing
    def _envelope(self, cls: type[E], rid: str, **kw: Any) -> E:
        return cls(
            request_id=rid,
            context_version=self.store.version,
            principal=self.principal_id,
            context_mode=self.mode.value,
            **kw,
        )

    @property
    def full_context(self) -> bool:
        return self.mode == ContextMode.full_context

    def _require_full_context(self, what: str) -> None:
        if not self.full_context:
            raise AccessDenied(
                "not_available_in_context_mode",
                f"{what} are not available in this server configuration.",
            )

    def _query_warnings(self, guard_warnings: tuple[str, ...] | list[str]) -> list[str]:
        out = list(guard_warnings)
        if self.full_context and CURRENT_DATE_WARNING in out:
            out.append(
                "Governed context: the Olist data covers purchases from 2016-09 to 2018-10 "
                "(see term.dataset_as_of_date)."
            )
        return out

    def _restricted_asset_view(self, a: Asset) -> dict[str, Any]:
        """Asset document for RAW_SCHEMA / DESCRIPTIVE_METADATA: physical schema (+ descriptions)."""
        with_desc = self.mode == ContextMode.descriptions
        cols = []
        for c in a.columns:
            col: dict[str, Any] = {"name": c.name, "data_type": c.data_type, "nullable": c.nullable}
            if with_desc:
                col["description"] = c.description
            cols.append(col)
        out: dict[str, Any] = {
            "id": a.id,
            "qualified_name": a.qualified_name,
            "schema": a.schema_name,
            "table": a.table,
            "kind": a.kind,
            "columns": cols,
        }
        if with_desc:
            out["description"] = a.description
        return out

    def _guard(
        self,
        tool: str,
        cls: type[E],
        request_context: RequestContext | dict[str, Any] | None,
        fn: Callable[[str], E],
        audit_extra: Callable[[E], dict[str, Any]] | None = None,
    ) -> E:
        try:
            rc = (
                request_context
                if isinstance(request_context, RequestContext)
                else RequestContext.model_validate(request_context or {})
            )
        except Exception as exc:
            rid = uuid.uuid4().hex
            resp = self._envelope(
                cls,
                rid,
                ok=False,
                error=ErrorInfo(code="invalid_request_context", message=str(exc).splitlines()[0]),
            )
            self.audit.record(
                request_id=rid,
                principal=self.principal_id,
                tool=tool,
                status="error",
                error_code="invalid_request_context",
                context_version=self.store.version,
            )
            return resp
        rid = rc.request_id or uuid.uuid4().hex
        try:
            self.policy.require_tool(tool)
            self.budget.consume(rid)
            resp = fn(rid)
        except AccessDenied as exc:
            resp = self._envelope(cls, rid, ok=False, error=ErrorInfo(code=exc.code, message=exc.message))
        except SqlRejected as exc:
            resp = self._envelope(
                cls, rid, ok=False, error=ErrorInfo(code=f"sql_rejected.{exc.code}", message=exc.message)
            )
        except ExecutionError as exc:
            resp = self._envelope(
                cls, rid, ok=False, error=ErrorInfo(code=f"execution.{exc.code}", message=exc.message)
            )
        extra = audit_extra(resp) if audit_extra else {}
        self.audit.record(
            request_id=rid,
            principal=self.principal_id,
            tool=tool,
            status="ok" if resp.ok else "error",
            error_code=resp.error.code if resp.error else None,
            context_version=self.store.version,
            **extra,
        )
        return resp

    def _resolve_asset(self, ref: str) -> Asset:
        a = self.store.assets.get(ref) or self.store.asset_by_qualified_name(ref)
        if a is None or (not self.full_context and a.layer != "serving"):
            raise AccessDenied("not_found", f"asset {ref!r} not found")
        if not self.policy.can_see_status(a.certification.status):
            raise AccessDenied(
                "not_visible",
                f"asset {ref!r} is {a.certification.status.value}; caller sees only "
                f"{sorted(s.value for s in self.policy.allowed_certification)}",
            )
        return a

    # ------------------------------------------------------------------ tools
    def search_assets(
        self,
        query: str,
        filters: SearchFilters | dict[str, Any] | None = None,
        limit: int = 10,
        request_context: RequestContext | dict[str, Any] | None = None,
    ) -> SearchResponse:
        def run(rid: str) -> SearchResponse:
            f = filters if isinstance(filters, SearchFilters) else SearchFilters.model_validate(filters or {})
            lim = max(1, min(int(limit), 50))
            if not self.full_context:
                # Only schema objects exist in restricted modes; domain filters would leak curation.
                kept: list[ResultType] = [
                    t for t in (f.types or ["asset", "column"]) if t in ("asset", "column")
                ]
                f = SearchFilters(types=kept or ["asset", "column"], layers=f.layers)
            visible = {s for s in Status if self.policy.can_see_status(s)}
            hits = self.index.search(
                query,
                types=set(f.types) if f.types else None,
                visible_statuses=visible,
                include_deprecated=f.include_deprecated,
                layers=set(f.layers) if f.layers else None,
                domain=f.domain,
                limit=lim,
            )
            queryable_ids = (
                {a.id for a in self.policy.queryable_assets()}
                if self.policy.has(Capability.query_execute)
                else set()
            )
            results = []
            for h in hits:
                d = h.doc
                q: bool | None = None
                if d.type == "asset":
                    q = d.id in queryable_ids
                elif d.type == "column" and d.parent_id:
                    asset = self.store.assets[d.parent_id]
                    q = self.policy.column_access(asset, d.id.split(":", 1)[1]).queryable
                results.append(
                    SearchResultItem(
                        id=d.id,
                        name=d.name,
                        type=d.type,
                        summary=d.summary,
                        certification_status=d.status,
                        deprecated=d.status == Status.deprecated.value,
                        source_file=d.source_file if self.full_context else None,
                        context_version=self.store.version,
                        relevance_score=h.score,
                        matched_terms=h.matched_terms,
                        parent_id=d.parent_id,
                        queryable=q,
                    )
                )
            warnings = []
            missing = None
            if not tokenize(query):
                missing = "Query contains no searchable terms."
            elif not self.full_context:
                if not results:
                    missing = "No tables or columns match this query."
                return self._envelope(
                    SearchResponse, rid, query=query, results=results, missing_context_explanation=missing
                )
            elif not results:
                missing = (
                    "No context matches this query. The concept may be undefined in the governed context "
                    "layer; do not guess a definition - ask a data owner or propose a draft definition."
                )
            elif all(r.type == "unsupported_concept" for r in results[:1]):
                warnings.append(
                    "Top match is registered as unsupported/ambiguous - read its guidance before answering."
                )
            if any(r.certification_status == Status.draft.value for r in results):
                warnings.append("Some results are DRAFT (not human-verified).")
            if any(r.deprecated for r in results):
                warnings.append("Some results are DEPRECATED - do not use them.")
            return self._envelope(
                SearchResponse,
                rid,
                query=query,
                results=results,
                missing_context_explanation=missing,
                warnings=warnings,
            )

        return self._guard("search_assets", SearchResponse, request_context, run)

    def get_asset(
        self, asset_id: str, request_context: RequestContext | dict[str, Any] | None = None
    ) -> AssetResponse:
        def run(rid: str) -> AssetResponse:
            a = self._resolve_asset(asset_id)
            qcols: list[str] = []
            withheld: list[str] = []
            for c in a.columns:
                (qcols if self.policy.column_access(a, c.name).queryable else withheld).append(c.name)
            if not self.full_context:
                acc0 = self.policy.column_access(a, None)
                return self._envelope(
                    AssetResponse,
                    rid,
                    asset=self._restricted_asset_view(a),
                    queryable=acc0.queryable,
                    queryable_columns=qcols if acc0.queryable else [],
                    withheld_columns=withheld if acc0.queryable else [c.name for c in a.columns],
                )
            joins = sorted(j.id for j in self.store.joins.values() if a.id in (j.left, j.right))
            warnings = []
            if a.certification.status == Status.draft:
                warnings.append("Asset documentation is DRAFT (not human-verified).")
            if a.deprecated:
                warnings.append(f"DEPRECATED: {a.certification.deprecated_reason}")
            if a.layer == "raw":
                warnings.append(
                    "Raw ingestion asset: documented for lineage only, never queryable through agent tools."
                )
            for qw in a.quality_warnings:
                warnings.append(f"data quality: {qw.description}")
            access = self.policy.column_access(a, None)
            return self._envelope(
                AssetResponse,
                rid,
                asset=a.model_dump(mode="json", by_alias=True),
                certification_status=a.certification.status.value,
                queryable=access.queryable,
                queryable_columns=qcols if access.queryable else [],
                withheld_columns=withheld if access.queryable else [c.name for c in a.columns],
                joins=joins,
                source_file=self.store.source_files.get(a.id),
                warnings=warnings,
            )

        return self._guard("get_asset", AssetResponse, request_context, run)

    def get_definition(
        self, term_or_metric_id: str, request_context: RequestContext | dict[str, Any] | None = None
    ) -> DefinitionResponse:
        def run(rid: str) -> DefinitionResponse:
            self._require_full_context("Business definitions")
            key = term_or_metric_id.strip()
            found: list[tuple[str, str]] = []  # (kind, id)
            for kind, d in (
                ("metric", self.store.metrics),
                ("glossary_term", self.store.terms),
                ("unsupported_concept", self.store.unsupported),
            ):
                if key in d:
                    found = [(kind, key)]
                    break
            if not found:
                low = key.lower()
                for m in self.store.metrics.values():
                    if low in {s.lower() for s in [m.name, *m.synonyms]}:
                        found.append(("metric", m.id))
                for t in self.store.terms.values():
                    if low in {s.lower() for s in [t.name, *t.synonyms]}:
                        found.append(("glossary_term", t.id))
                for u in self.store.unsupported.values():
                    if low in {s.lower() for s in [u.name, *u.synonyms]}:
                        found.append(("unsupported_concept", u.id))
            if not found:
                hits = self.index.search(
                    key,
                    types={"metric", "glossary_term", "unsupported_concept"},
                    limit=5,
                    visible_statuses={s for s in Status if self.policy.can_see_status(s)},
                )
                return self._envelope(
                    DefinitionResponse,
                    rid,
                    resolution="not_found",
                    candidates=[
                        {
                            "id": h.doc.id,
                            "name": h.doc.name,
                            "type": h.doc.type,
                            "status": h.doc.status,
                            "relevance_score": h.score,
                        }
                        for h in hits
                    ],
                    warnings=[
                        "No exact definition. Candidates are keyword matches, not equivalents - do not "
                        "substitute one without confirming it means the same thing."
                    ],
                )
            # unsupported/ambiguous registrations take precedence: the concept needs a decision.
            unsupported = [f for f in found if f[0] == "unsupported_concept"]
            if unsupported:
                u = self.store.unsupported[unsupported[0][1]]
                return self._envelope(
                    DefinitionResponse,
                    rid,
                    resolution="unsupported_concept",
                    id=u.id,
                    definition=u.model_dump(mode="json"),
                    source_file=self.store.source_files.get(u.id),
                    candidates=[{"id": c, "type": self.store.kind_of(c)} for c in u.candidate_definitions],
                    warnings=[f"'{key}' is registered as {u.classification.upper()}: {u.guidance}"],
                )
            if len({i for _, i in found}) > 1:
                # Prefer a term that explicitly links the other matches (term + its metric).
                terms = [i for k, i in found if k == "glossary_term"]
                others = {i for k, i in found if k != "glossary_term"}
                if not (len(terms) == 1 and others <= set(self.store.terms[terms[0]].metrics)):
                    return self._envelope(
                        DefinitionResponse,
                        rid,
                        resolution="ambiguous",
                        candidates=[{"id": i, "type": k} for k, i in found],
                        warnings=[f"'{key}' maps to several definitions; choose one explicitly by id."],
                    )
                found = [("glossary_term", terms[0])]
            kind, oid = found[0]
            obj = self.store.metrics[oid] if kind == "metric" else self.store.terms[oid]
            status = obj.certification.status
            if not self.policy.can_see_status(status):
                return self._envelope(
                    DefinitionResponse,
                    rid,
                    resolution="not_visible",
                    id=oid,
                    certification_status=status.value,
                    warnings=[
                        f"A {status.value} definition exists but this caller may only use "
                        f"{sorted(s.value for s in self.policy.allowed_certification)} definitions."
                    ],
                )
            warnings = []
            if status == Status.draft:
                warnings.append("DRAFT definition: proposed, not human-verified. State this when using it.")
            if status == Status.deprecated:
                c = obj.certification
                warnings.append(f"DEPRECATED: {c.deprecated_reason} Use {c.replaced_by} instead.")
            return self._envelope(
                DefinitionResponse,
                rid,
                resolution=kind,
                id=oid,
                certification_status=status.value,  # type: ignore[arg-type]
                definition=obj.model_dump(mode="json"),
                source_file=self.store.source_files.get(oid),
                warnings=warnings,
            )

        return self._guard("get_definition", DefinitionResponse, request_context, run)

    def get_join_path(
        self,
        source_asset: str,
        target_asset: str,
        request_context: RequestContext | dict[str, Any] | None = None,
    ) -> JoinPathResponse:
        def run(rid: str) -> JoinPathResponse:
            self._require_full_context("Curated join paths")
            src, dst = self._resolve_asset(source_asset), self._resolve_asset(target_asset)
            edges: dict[str, list[tuple[str, str, str]]] = {}
            for j in self.store.joins.values():
                if not self.policy.can_see_status(j.certification.status):
                    continue
                if j.direction in ("left_to_right", "both"):
                    edges.setdefault(j.left, []).append((j.right, j.id, j.cardinality))
                if j.direction in ("right_to_left", "both"):
                    edges.setdefault(j.right, []).append(
                        (j.left, j.id, _REVERSE.get(j.cardinality, j.cardinality))
                    )
            # Dijkstra: each hop costs 1, each fanout hop costs 10 more -> prefer fanout-free paths.
            heap: list[tuple[int, str, list[tuple[str, str, str, str]]]] = [(0, src.id, [])]
            best: dict[str, int] = {}
            path: list[tuple[str, str, str, str]] | None = None
            while heap:
                cost, node, p = heapq.heappop(heap)
                if node == dst.id:
                    path = p
                    break
                if best.get(node, 1 << 30) <= cost:
                    continue
                best[node] = cost
                for nxt, jid, card in edges.get(node, []):
                    fan = card in ("one_to_many", "many_to_many")
                    heapq.heappush(heap, (cost + 1 + (10 if fan else 0), nxt, [*p, (node, nxt, jid, card)]))
            if path is None:
                return self._envelope(
                    JoinPathResponse,
                    rid,
                    source_asset=src.id,
                    target_asset=dst.id,
                    found=False,
                    warnings=["No documented join path. Do not invent join keys."],
                )
            steps = []
            for a, b, jid, card in path:
                j = self.store.joins[jid]
                forward = j.left == a
                on = [
                    {
                        "from_column": (p.left if forward else p.right),
                        "to_column": (p.right if forward else p.left),
                    }
                    for p in j.join_columns
                ]
                steps.append(
                    JoinStep(
                        join_id=jid,
                        from_asset=a,
                        to_asset=b,
                        on=on,
                        cardinality=card,
                        causes_fanout=card in ("one_to_many", "many_to_many"),
                        fanout_warning=j.fanout_warning,
                        bridge=j.bridge,
                        certification_status=j.certification.status.value,
                    )
                )
            fan = any(s.causes_fanout for s in steps)
            hint_parts = []
            for s in steps:
                ta = self.store.assets[s.to_asset]
                fa = self.store.assets[s.from_asset]
                cond = " AND ".join(
                    f"{fa.table}.{c['from_column']} = {ta.table}.{c['to_column']}" for c in s.on
                )
                hint_parts.append(f"JOIN {ta.qualified_name} AS {ta.table} ON {cond}")
            warnings = []
            if fan:
                warnings.append(
                    "This path multiplies rows (fanout). Aggregate to the finer grain first or use the "
                    "bridge tables listed on each step."
                )
            if src.id == dst.id:
                warnings.append("Source and target are the same asset.")
            return self._envelope(
                JoinPathResponse,
                rid,
                source_asset=src.id,
                target_asset=dst.id,
                found=True,
                steps=steps,
                fanout_risk=fan,
                sql_join_hint=f"FROM {src.qualified_name} AS {src.table} " + " ".join(hint_parts),
                warnings=warnings,
            )

        return self._guard("get_join_path", JoinPathResponse, request_context, run)

    def get_policy(
        self, asset_or_column_id: str, request_context: RequestContext | dict[str, Any] | None = None
    ) -> PolicyResponse:
        def run(rid: str) -> PolicyResponse:
            ref, _, col = asset_or_column_id.partition(":")
            if not col and ref.count(".") >= 3 and not self.store.assets.get(ref):
                # also accept asset.analytics.orders.customer_key style
                base, _, maybe_col = ref.rpartition(".")
                if self.store.assets.get(base):
                    ref, col = base, maybe_col
            a = self._resolve_asset(ref)
            acc = self.policy.column_access(a, col or None)
            pol = self.store.policy
            if not self.full_context:
                # Access decision only: handling notes and certification carry curated semantics.
                return self._envelope(
                    PolicyResponse,
                    rid,
                    target=f"{a.id}:{col}" if col else a.id,
                    sensitivity=acc.sensitivity,
                    queryable_by_caller=acc.queryable,
                    reason=acc.reason,
                    caller_capabilities=sorted(
                        c.value for c in self.policy.capabilities if not c.value.startswith("metrics.")
                    ),
                    caller_max_sensitivity=self.policy.max_sensitivity.value,
                    caller_max_rows=self.policy.max_rows,
                    policy_id=pol.id,
                    policy_version=pol.version,
                )
            return self._envelope(
                PolicyResponse,
                rid,
                target=f"{a.id}:{col}" if col else a.id,
                sensitivity=acc.sensitivity,
                queryable_by_caller=acc.queryable,
                reason=acc.reason,
                handling=acc.handling,
                caller_capabilities=sorted(c.value for c in self.policy.capabilities),
                caller_max_sensitivity=self.policy.max_sensitivity.value,
                caller_max_rows=self.policy.max_rows,
                asset_certification_status=a.certification.status.value,
                deprecated=a.deprecated,
                policy_id=pol.id,
                policy_version=pol.version,
            )

        return self._guard("get_policy", PolicyResponse, request_context, run)

    def explain_query(
        self, sql: str, request_context: RequestContext | dict[str, Any] | None = None
    ) -> ExplainResponse:
        qid = uuid.uuid4().hex

        def run(rid: str) -> ExplainResponse:
            vq = validate_sql(
                sql,
                self.policy.catalog(),
                self.limits,
                max_rows=self.policy.max_rows,
                allowed_functions=self.allowed_functions,
            )
            res = self.executor.run(vq.executable_sql, mode="explain", max_rows=self.policy.max_rows)
            limit = self.store.policy.query.max_cost_estimated_rows
            within = None if res.estimated_max_cardinality is None else res.estimated_max_cardinality <= limit
            return self._envelope(
                ExplainResponse,
                rid,
                query_id=qid,
                valid=True,
                executable_sql=vq.executable_sql,
                relations=list(vq.relations),
                columns_read=list(vq.columns),
                max_sensitivity=vq.max_sensitivity,
                plan=res.plan,
                estimated_max_cardinality=res.estimated_max_cardinality,
                cost_limit_estimated_rows=limit,
                within_cost_limit=within,
                warnings=self._query_warnings(vq.warnings),
            )

        return self._guard(
            "explain_query",
            ExplainResponse,
            request_context,
            run,
            lambda r: {"query_id": qid, "sql_text": sql, "executed_sql": r.executable_sql},
        )

    def run_query(
        self, sql: str, request_context: RequestContext | dict[str, Any] | None = None
    ) -> QueryResponse:
        qid = uuid.uuid4().hex

        def run(rid: str) -> QueryResponse:
            vq = validate_sql(
                sql,
                self.policy.catalog(),
                self.limits,
                max_rows=self.policy.max_rows,
                allowed_functions=self.allowed_functions,
            )
            res = self.executor.run(
                vq.executable_sql,
                mode="execute",
                max_rows=self.policy.max_rows,
                max_cost_estimated_rows=self.store.policy.query.max_cost_estimated_rows,
            )
            refs = []
            warnings = self._query_warnings(vq.warnings)
            for rel in vq.relations:
                a = self.store.asset_by_qualified_name(rel)
                if a is not None:
                    refs.append(
                        AssetRef(
                            asset_id=a.id,
                            certification_status=a.certification.status.value if self.full_context else None,
                            deprecated=a.deprecated if self.full_context else False,
                        )
                    )
                    if self.full_context and a.certification.status == Status.draft:
                        warnings.append(f"{a.id} documentation is DRAFT (not human-verified).")
            if res.truncated:
                warnings.append(
                    f"Result truncated ({res.truncation_reason}); at most {self.policy.max_rows} rows are returned."
                )
            if res.redactions:
                warnings.append(f"{res.redactions} value(s) redacted at the result boundary.")
            return self._envelope(
                QueryResponse,
                rid,
                query_id=qid,
                columns=res.columns,
                column_types=res.column_types,
                rows=res.rows,
                row_count=res.row_count,
                truncated=res.truncated,
                truncation_reason=res.truncation_reason,
                max_rows=self.policy.max_rows,
                executable_sql=vq.executable_sql,
                asset_refs=refs,
                max_sensitivity=vq.max_sensitivity,
                redactions=res.redactions,
                elapsed_ms=res.elapsed_ms,
                warnings=warnings,
            )

        return self._guard(
            "run_query",
            QueryResponse,
            request_context,
            run,
            lambda r: {
                "query_id": qid,
                "sql_text": sql,
                "executed_sql": r.executable_sql,
                "row_count": r.row_count,
                "truncated": int(r.truncated),
                "elapsed_ms": r.elapsed_ms,
            },
        )

    def validate_context(
        self, request_context: RequestContext | dict[str, Any] | None = None
    ) -> ValidateContextResponse:
        def run(rid: str) -> ValidateContextResponse:
            self.policy.require(Capability.context_admin)
            store = load_context(self.settings.context_dir)
            issues = validate_store(store, self.limits)
            errs = errors(issues)
            return self._envelope(
                ValidateContextResponse,
                rid,
                valid=not errs,
                error_count=len(errs),
                warning_count=len(issues) - len(errs),
                issues=[str(i) for i in issues],
            )

        return self._guard("validate_context", ValidateContextResponse, request_context, run)

    def close(self) -> None:
        self.audit.close()
