"""Cross-document validation of the context repository.

Rejects: invalid asset/term/metric/owner references, circular metric dependencies, missing or
placeholder owners on verified objects, invalid certification (incl. AI self-certification),
broken joins, duplicate ids, missing policy information, unsafe metric SQL, and programmatically
detectable contradictions (e.g. one synonym bound to two verified definitions; a serving asset
exposing restricted columns; an asset classified less sensitive than its columns).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from groundwork.config.settings import QueryLimits
from groundwork.context.loader import ContextStore, Issue, load_context
from groundwork.context.models import Certification, Provenance, Sensitivity, Status
from groundwork.security.sql_guard import (
    DEFAULT_ALLOWED_FUNCTIONS,
    Catalog,
    ColumnMeta,
    SqlRejected,
    validate_sql,
)

# Functions that must never be allow-listed by a policy, whatever an editor writes.
HARD_DENIED_FUNCTIONS = frozenset(
    {
        "READ_CSV",
        "READ_CSV_AUTO",
        "READ_PARQUET",
        "READ_JSON",
        "READ_JSON_AUTO",
        "READ_TEXT",
        "READ_BLOB",
        "GLOB",
        "GETENV",
        "CURRENT_SETTING",
        "QUERY",
        "QUERY_TABLE",
        "SNIFF_CSV",
        "PARQUET_SCAN",
        "DUCKDB_SETTINGS",
        "DUCKDB_EXTENSIONS",
        "DUCKDB_SECRETS",
        "SYSTEM",
        "LOAD",
        "INSTALL",
        "ATTACH",
        "SQL_AUTO_COMPLETE",
        "READ_XLSX",
        "ICEBERG_SCAN",
        "DELTA_SCAN",
        "HTTP_GET",
    }
)


def _cert_issues(
    store: ContextStore, oid: str, cert: Certification, prov: Provenance, owner: str | None
) -> list[Issue]:
    f = store.source_files.get(oid)
    out: list[Issue] = []
    if cert.status == Status.verified:
        if owner is None or owner not in store.owners:
            out.append(
                Issue("error", "verified_missing_owner", "verified object needs a valid owner", f, oid)
            )
        elif store.owners[owner].placeholder:
            out.append(
                Issue("error", "verified_placeholder_owner", f"owner {owner} is a placeholder", f, oid)
            )
        if not cert.certified_by or not cert.certified_at or not cert.decision_ref:
            out.append(
                Issue(
                    "error",
                    "verified_missing_certification",
                    "verified requires certified_by, certified_at and decision_ref",
                    f,
                    oid,
                )
            )
        if cert.certified_by:
            certifier = store.owners.get(cert.certified_by)
            if certifier is None:
                out.append(
                    Issue("error", "unknown_certifier", f"certifier {cert.certified_by} not found", f, oid)
                )
            elif certifier.kind != "human" or not certifier.can_certify or certifier.placeholder:
                out.append(
                    Issue(
                        "error",
                        "invalid_certifier",
                        f"certifier {cert.certified_by} must be a non-placeholder human with can_certify",
                        f,
                        oid,
                    )
                )
            if cert.certified_by == prov.authored_by and prov.generated_by != "human":
                out.append(
                    Issue(
                        "error",
                        "self_certification",
                        "AI-assisted content cannot be certified by its own author",
                        f,
                        oid,
                    )
                )
            author = store.owners.get(prov.authored_by)
            if author is not None and author.kind == "agent" and cert.certified_by == author.id:
                out.append(
                    Issue("error", "self_certification", "an agent cannot certify its own work", f, oid)
                )
    elif cert.status == Status.draft:
        if cert.certified_by or cert.certified_at:
            out.append(
                Issue(
                    "error",
                    "draft_with_certification",
                    "draft objects must not carry certified_by/certified_at",
                    f,
                    oid,
                )
            )
    elif cert.status == Status.deprecated and not cert.deprecated_reason:
        out.append(
            Issue("error", "deprecated_missing_reason", "deprecated objects need deprecated_reason", f, oid)
        )
    if prov.authored_by not in store.owners:
        out.append(
            Issue("error", "unknown_author", f"authored_by {prov.authored_by} not in ownership", f, oid)
        )
    if cert.replaced_by and store.kind_of(cert.replaced_by) is None:
        out.append(Issue("error", "broken_reference", f"replaced_by {cert.replaced_by} not found", f, oid))
    return out


def _linked(store: ContextStore, ids: set[str]) -> bool:
    """True when the ids are one glossary term plus metrics that the term explicitly references."""
    terms = [i for i in ids if i in store.terms]
    if len(terms) != 1:
        return False
    others = ids - set(terms)
    return all(o in store.metrics for o in others) and others <= set(store.terms[terms[0]].metrics)


def _serving_catalog(store: ContextStore) -> Catalog:
    relations: dict[str, dict[str, ColumnMeta]] = {}
    types: dict[str, dict[str, str]] = {}
    for a in store.assets.values():
        if a.layer != "serving":
            continue
        q = a.qualified_name.lower()
        relations[q] = {c.name.lower(): ColumnMeta(c.sensitivity.value) for c in a.columns}
        types[q] = {c.name.lower(): c.data_type for c in a.columns}
    return Catalog(schema_name="analytics", relations=relations, column_types=types)


def validate_store(store: ContextStore, limits: QueryLimits | None = None) -> list[Issue]:
    limits = limits or QueryLimits()
    issues: list[Issue] = list(store.load_issues)
    sf = store.source_files

    def ref(kind: str, target: str, oid: str, what: str) -> None:
        if store.kind_of(target) != kind:
            issues.append(
                Issue(
                    "error", "broken_reference", f"{what} {target!r} is not a known {kind}", sf.get(oid), oid
                )
            )

    # ---- policy presence ----
    if len(store.policies) != 1:
        issues.append(
            Issue(
                "error",
                "policy_missing",
                f"exactly one policy document required, found {len(store.policies)}",
            )
        )
    # ---- owners ----
    for a in store.assets.values():
        ref("owner", a.owner, a.id, "owner")
        if a.steward:
            ref("owner", a.steward, a.id, "steward")
        issues += _cert_issues(store, a.id, a.certification, a.provenance, a.owner)
        names = [c.name for c in a.columns]
        if len(set(names)) != len(names):
            issues.append(Issue("error", "duplicate_column", "duplicate column names", sf.get(a.id), a.id))
        for pk in a.primary_key:
            if pk not in names:
                issues.append(
                    Issue("error", "broken_reference", f"primary key column {pk} missing", sf.get(a.id), a.id)
                )
        for fk in a.foreign_keys:
            target = store.assets.get(fk.references)
            if target is None:
                issues.append(
                    Issue(
                        "error",
                        "broken_reference",
                        f"FK target {fk.references} not found",
                        sf.get(a.id),
                        a.id,
                    )
                )
                continue
            for fk_col in fk.columns:
                if fk_col not in names:
                    issues.append(
                        Issue("error", "broken_reference", f"FK column {fk_col} missing", sf.get(a.id), a.id)
                    )
            for ref_col in fk.ref_columns:
                if target.column(ref_col) is None:
                    issues.append(
                        Issue(
                            "error",
                            "broken_reference",
                            f"FK ref column {fk.references}.{ref_col} missing",
                            sf.get(a.id),
                            a.id,
                        )
                    )
        for s in a.lineage.sources:
            ref("asset", s, a.id, "lineage source")
        for metric_ref in a.metrics:
            ref("metric", metric_ref, a.id, "metric reference")
        max_col = max((c.sensitivity for c in a.columns), key=lambda s: s.rank, default=Sensitivity.public)
        if max_col.rank > a.sensitivity.rank:
            issues.append(
                Issue(
                    "error",
                    "contradictory_sensitivity",
                    f"asset sensitivity {a.sensitivity.value} is lower than column sensitivity {max_col.value}",
                    sf.get(a.id),
                    a.id,
                )
            )
        if a.layer == "serving":
            bad = [c.name for c in a.columns if c.sensitivity in (Sensitivity.restricted, Sensitivity.pii)]
            if bad:
                issues.append(
                    Issue(
                        "error",
                        "serving_exposes_sensitive",
                        f"serving assets must not contain restricted/pii columns: {bad}",
                        sf.get(a.id),
                        a.id,
                    )
                )
    for t in store.terms.values():
        ref("owner", t.owner, t.id, "owner")
        issues += _cert_issues(store, t.id, t.certification, t.provenance, t.owner)
        for x in t.assets:
            ref("asset", x, t.id, "asset")
        for metric_ref in t.metrics:
            ref("metric", metric_ref, t.id, "metric")
        for col in t.columns:
            aid, _, cname = col.partition(":")
            asset = store.assets.get(aid)
            if asset is None or asset.column(cname) is None:
                issues.append(
                    Issue(
                        "error", "broken_reference", f"column reference {col} not found", sf.get(t.id), t.id
                    )
                )
    for o in store.owners.values():
        if o.can_certify and (o.kind != "human" or o.placeholder):
            issues.append(
                Issue(
                    "error",
                    "invalid_certifier",
                    "only non-placeholder humans may have can_certify",
                    sf.get(o.id),
                    o.id,
                )
            )

    # ---- metrics ----
    serving = _serving_catalog(store)
    allowed_functions = DEFAULT_ALLOWED_FUNCTIONS
    if store.policies:
        allowed_functions = frozenset(f.upper() for f in store.policy.query.allowed_functions)
    for m in store.metrics.values():
        ref("owner", m.owner, m.id, "owner")
        issues += _cert_issues(store, m.id, m.certification, m.provenance, m.owner)
        for x in m.assets:
            ref("asset", x, m.id, "asset")
            used_asset = store.assets.get(x)
            if used_asset is not None and used_asset.layer != "serving":
                issues.append(
                    Issue(
                        "error",
                        "metric_on_raw_asset",
                        f"metric must use serving assets, not {x}",
                        sf.get(m.id),
                        m.id,
                    )
                )
        for d in m.depends_on:
            ref("metric", d, m.id, "dependency")
        for dim in m.supported_dimensions:
            ref("asset", dim.asset, m.id, "dimension asset")
        if m.certification.status == Status.verified and m.sql_status != "approved":
            issues.append(
                Issue(
                    "error",
                    "verified_unapproved_sql",
                    "verified metric must have approved SQL",
                    sf.get(m.id),
                    m.id,
                )
            )
        try:
            vq = validate_sql(
                m.sql, serving, limits, max_rows=limits.max_rows, allowed_functions=allowed_functions
            )
            declared = {store.assets[a].qualified_name.lower() for a in m.assets if a in store.assets}
            undeclared = set(vq.relations) - declared
            if undeclared:
                issues.append(
                    Issue(
                        "error",
                        "metric_undeclared_asset",
                        f"metric SQL reads {sorted(undeclared)} which are not listed in assets",
                        sf.get(m.id),
                        m.id,
                    )
                )
        except SqlRejected as exc:
            issues.append(
                Issue("error", "unsafe_metric_sql", f"{exc.code}: {exc.message}", sf.get(m.id), m.id)
            )

    # circular dependencies
    graph = {m.id: list(m.depends_on) for m in store.metrics.values()}
    state: dict[str, int] = {}

    def dfs(node: str, path: list[str]) -> None:
        state[node] = 1
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                cycle = [*path[path.index(nxt) :], nxt] if nxt in path else [node, nxt]
                issues.append(Issue("error", "circular_dependency", " -> ".join(cycle), sf.get(node), node))
            elif state.get(nxt) is None and nxt in graph:
                dfs(nxt, [*path, nxt])
        state[node] = 2

    for n in graph:
        if n not in state:
            dfs(n, [n])

    # ---- joins ----
    pairs: dict[frozenset[str], list[str]] = defaultdict(list)
    for j in store.joins.values():
        ref("owner", j.owner, j.id, "owner")
        issues += _cert_issues(store, j.id, j.certification, j.provenance, j.owner)
        for side, aid in (("left", j.left), ("right", j.right)):
            if aid not in store.assets:
                issues.append(
                    Issue("error", "broken_join", f"{side} asset {aid} not found", sf.get(j.id), j.id)
                )
        la, ra = store.assets.get(j.left), store.assets.get(j.right)
        for pair in j.join_columns:
            if la is not None and la.column(pair.left) is None:
                issues.append(
                    Issue("error", "broken_join", f"{j.left}.{pair.left} not found", sf.get(j.id), j.id)
                )
            if ra is not None and ra.column(pair.right) is None:
                issues.append(
                    Issue("error", "broken_join", f"{j.right}.{pair.right} not found", sf.get(j.id), j.id)
                )
        if j.bridge and j.bridge not in store.assets:
            issues.append(
                Issue("error", "broken_join", f"bridge asset {j.bridge} not found", sf.get(j.id), j.id)
            )
        if j.cardinality in ("one_to_many", "many_to_one", "many_to_many") and not j.fanout_warning:
            issues.append(
                Issue(
                    "error",
                    "missing_fanout_warning",
                    "multiplying join needs a fanout_warning",
                    sf.get(j.id),
                    j.id,
                )
            )
        pairs[frozenset((j.left, j.right))].append(j.id)
    for ids in pairs.values():
        if len(ids) > 1:
            issues.append(
                Issue("warning", "duplicate_join_pair", f"multiple joins between same assets: {ids}")
            )

    # ---- unsupported concepts ----
    for u in store.unsupported.values():
        ref("owner", u.decision_needed_from, u.id, "decision owner")
        for cand in u.candidate_definitions:
            if store.kind_of(cand) not in ("metric", "glossary_term"):
                issues.append(
                    Issue("error", "broken_reference", f"candidate {cand} not found", sf.get(u.id), u.id)
                )

    # ---- synonym contradictions ----
    bindings: dict[str, list[tuple[str, Status]]] = defaultdict(list)
    for t in store.terms.values():
        for s in [t.name, *t.synonyms]:
            bindings[s.strip().lower()].append((t.id, t.certification.status))
    for m in store.metrics.values():
        for s in [m.name, *m.synonyms]:
            bindings[s.strip().lower()].append((m.id, m.certification.status))
    unsupported_names = {s.strip().lower() for u in store.unsupported.values() for s in [u.name, *u.synonyms]}
    for word, owners in bindings.items():
        verified = [o for o, st in owners if st == Status.verified]
        if len(verified) > 1:
            issues.append(
                Issue(
                    "error",
                    "contradictory_definitions",
                    f"'{word}' bound to multiple verified definitions {verified}",
                )
            )
        elif len({o for o, _ in owners}) > 1 and not _linked(store, {o for o, _ in owners}):
            issues.append(
                Issue(
                    "warning",
                    "ambiguous_synonym",
                    f"'{word}' bound to multiple definitions {[o for o, _ in owners]}",
                )
            )
        if word in unsupported_names and verified:
            issues.append(
                Issue(
                    "error",
                    "contradictory_definitions",
                    f"'{word}' is registered as unsupported/ambiguous but bound to verified {verified}",
                )
            )

    # ---- policy ----
    if len(store.policies) == 1:
        pol = store.policy
        pf = sf.get(pol.id)
        bad_fn = sorted(f for f in pol.query.allowed_functions if f.upper() in HARD_DENIED_FUNCTIONS)
        if bad_fn:
            issues.append(
                Issue(
                    "error", "unsafe_policy", f"policy allow-lists hard-denied functions {bad_fn}", pf, pol.id
                )
            )
        unknown_fn = sorted(
            f for f in pol.query.allowed_functions if f.upper() not in DEFAULT_ALLOWED_FUNCTIONS
        )
        if unknown_fn:
            issues.append(
                Issue(
                    "error",
                    "unsafe_policy",
                    f"policy allow-lists functions outside the reviewed set {unknown_fn}",
                    pf,
                    pol.id,
                )
            )
        role_ids = {r.id for r in pol.roles}
        classes = {c.sensitivity for c in pol.sensitivity_classes}
        for s in Sensitivity:
            if s not in classes:
                issues.append(
                    Issue("error", "policy_missing", f"no sensitivity class for {s.value}", pf, pol.id)
                )
        for r in pol.roles:
            for aid in r.queryable_assets:
                granted = store.assets.get(aid)
                if granted is None:
                    issues.append(
                        Issue(
                            "error", "broken_reference", f"role {r.id} grants unknown asset {aid}", pf, pol.id
                        )
                    )
                elif granted.layer != "serving":
                    issues.append(
                        Issue("error", "unsafe_policy", f"role {r.id} grants raw asset {aid}", pf, pol.id)
                    )
            for sc in pol.sensitivity_classes:
                if (
                    not sc.agent_queryable
                    and sc.sensitivity.rank <= r.max_sensitivity.rank
                    and sc.sensitivity.rank < Sensitivity.restricted.rank
                ):
                    issues.append(
                        Issue(
                            "warning",
                            "policy_inconsistent",
                            f"role {r.id} clearance covers non-queryable class {sc.sensitivity.value}",
                            pf,
                            pol.id,
                        )
                    )
        for p in pol.principals:
            for role_ref in p.roles:
                if role_ref not in role_ids:
                    issues.append(
                        Issue(
                            "error",
                            "broken_reference",
                            f"principal {p.id} has unknown role {role_ref}",
                            pf,
                            pol.id,
                        )
                    )
        ref("owner", pol.owner, pol.id, "owner")
    return issues


def validate_directory(root: Path, limits: QueryLimits | None = None) -> tuple[ContextStore, list[Issue]]:
    store = load_context(root)
    return store, validate_store(store, limits)


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
