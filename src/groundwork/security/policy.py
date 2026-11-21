"""Central authorization and policy enforcement.

Caller identity is established server-side (process configuration / transport authentication),
never from tool arguments. Every service method goes through ``PolicyEngine`` before touching
context or data.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundwork.context.loader import ContextStore
from groundwork.context.models import Asset, Capability, Principal, Role, Sensitivity, Status
from groundwork.security.sql_guard import Catalog, ColumnMeta


class AccessDenied(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ColumnAccess:
    asset_id: str
    column: str | None
    sensitivity: str
    queryable: bool
    reason: str
    handling: str | None


class PolicyEngine:
    def __init__(self, store: ContextStore, principal_id: str, hard_max_rows: int) -> None:
        self.store = store
        self.policy = store.policy
        principal = next((p for p in self.policy.principals if p.id == principal_id), None)
        if principal is None:
            raise AccessDenied("unknown_principal", f"principal {principal_id!r} is not configured in policy")
        self.principal: Principal = principal
        roles = {r.id: r for r in self.policy.roles}
        missing = [r for r in principal.roles if r not in roles]
        if missing:
            raise AccessDenied("unknown_role", f"principal references undefined roles {missing}")
        self.roles: list[Role] = [roles[r] for r in principal.roles]
        self.capabilities: frozenset[Capability] = frozenset(c for r in self.roles for c in r.capabilities)
        self.max_sensitivity: Sensitivity = max((r.max_sensitivity for r in self.roles), key=lambda s: s.rank)
        self.allowed_certification: frozenset[Status] = frozenset(
            s for r in self.roles for s in r.allowed_certification
        )
        self.include_deprecated = any(r.include_deprecated for r in self.roles)
        self.max_rows = min(max(r.max_rows for r in self.roles), hard_max_rows)
        self._sens_classes = {c.sensitivity: c for c in self.policy.sensitivity_classes}

    # ---------------- capabilities ----------------
    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def require(self, cap: Capability) -> None:
        if cap not in self.capabilities:
            raise AccessDenied(
                "capability_denied", f"principal {self.principal.id} lacks capability {cap.value}"
            )

    def require_tool(self, tool: str) -> None:
        cap = self.policy.tool_permissions.get(tool)
        if cap is None:
            raise AccessDenied("tool_not_permitted", f"tool {tool!r} has no permission mapping; denied")
        self.require(cap)

    # ---------------- visibility ----------------
    def can_see_status(self, status: Status) -> bool:
        if status == Status.deprecated:
            return True  # deprecated objects are always surfaced (clearly labelled) so callers avoid them
        if status == Status.draft:
            return Status.draft in self.allowed_certification and self.has(Capability.metrics_read_draft)
        return Status.verified in self.allowed_certification

    # ---------------- data access ----------------
    def _column_queryable(self, sensitivity: Sensitivity) -> tuple[bool, str]:
        cls = self._sens_classes.get(sensitivity)
        if cls is None:
            return False, f"no sensitivity class defined for {sensitivity.value}; denied"
        if not cls.agent_queryable:
            return False, f"sensitivity {sensitivity.value} is never queryable through agent tools"
        if sensitivity.rank > self.max_sensitivity.rank:
            return (
                False,
                f"sensitivity {sensitivity.value} exceeds caller clearance {self.max_sensitivity.value}",
            )
        return True, "permitted"

    def queryable_assets(self) -> list[Asset]:
        allowed_ids = {a for r in self.roles for a in r.queryable_assets}
        out = []
        for aid in sorted(allowed_ids):
            asset = self.store.assets.get(aid)
            if asset is None or asset.layer != "serving":
                continue
            if asset.deprecated and not self.include_deprecated:
                continue
            if asset.certification.status == Status.draft and Status.draft not in self.allowed_certification:
                continue
            out.append(asset)
        return out

    def catalog(self) -> Catalog:
        relations: dict[str, dict[str, ColumnMeta]] = {}
        types: dict[str, dict[str, str]] = {}
        schema_name = "analytics"
        for asset in self.queryable_assets():
            cols = {}
            ctypes = {}
            for col in asset.columns:
                ok, _ = self._column_queryable(col.sensitivity)
                if ok:
                    cols[col.name.lower()] = ColumnMeta(col.sensitivity.value)
                    ctypes[col.name.lower()] = col.data_type
            if cols:
                q = asset.qualified_name.lower()
                relations[q] = cols
                types[q] = ctypes
                schema_name = asset.schema_name
        return Catalog(schema_name=schema_name, relations=relations, column_types=types)

    def column_access(self, asset: Asset, column: str | None) -> ColumnAccess:
        queryable_ids = {a.id for a in self.queryable_assets()}
        if column is None:
            sens = asset.sensitivity
            ok = asset.id in queryable_ids and self.has(Capability.query_execute)
            reason = (
                "asset is queryable by caller"
                if ok
                else (
                    "caller lacks query.execute"
                    if asset.id in queryable_ids
                    else "asset is not in the caller's queryable asset allow-list (raw, deprecated, draft-restricted, or "
                    "not granted)"
                )
            )
        else:
            col = asset.column(column)
            if col is None:
                raise AccessDenied("not_found", f"column {column!r} not found on {asset.id}")
            sens = col.sensitivity
            col_ok, col_reason = self._column_queryable(sens)
            ok = col_ok and asset.id in queryable_ids and self.has(Capability.query_execute)
            reason = (
                col_reason
                if not col_ok
                else ("permitted" if ok else "asset not queryable by caller or caller lacks query.execute")
            )
        cls = self._sens_classes.get(sens)
        return ColumnAccess(asset.id, column, sens.value, ok, reason, cls.handling if cls else None)
