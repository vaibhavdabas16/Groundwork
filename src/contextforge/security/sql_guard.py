"""AST-based validation of untrusted (model-generated) SQL.

This is NOT a regex filter. The SQL is parsed with SQLGlot (DuckDB dialect) and checked against:

1. exactly one statement, whose root is a query (SELECT / set operation);
2. an allow-list of expression node types - anything unrecognised fails closed;
3. an allow-list of functions (by SQLGlot canonical name, or exact name for unknown functions);
4. relation allow-list: every table must be an approved relation for the caller (or a CTE);
   table functions, file paths, catalogs, and time-travel clauses are rejected;
5. column allow-list: the query is qualified against a schema that contains ONLY the columns the
   caller is permitted to read; any other column (in SELECT, WHERE, JOIN, aggregates, subqueries,
   CTEs, ``*`` expansion) fails resolution and is rejected;
6. complexity limits (SQL length, AST size, joins, subquery depth).

The SQL that is executed is the *re-generated, fully qualified* SQL from the validated AST, with a
row limit applied - never the caller's original text. The database layer (read-only, external
access disabled, locked configuration, separate serving file without raw sensitive data, killable
worker process) is an independent second line of defence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from contextforge.config.settings import QueryLimits

# Generic on purpose: the guard runs in every context mode and must not carry dataset facts.
CURRENT_DATE_WARNING = (
    "query uses CURRENT_DATE; the warehouse holds historical data - anchor relative windows to the "
    "data's actual date coverage, not today"
)


class SqlRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Expression node classes permitted anywhere in the tree. Function nodes (exp.Func subclasses)
# are checked separately against the function allow-list.
_ALLOWED_NODES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Subquery,
    exp.With,
    exp.CTE,
    exp.From,
    exp.Join,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.Distinct,
    exp.Qualify,
    exp.Table,
    exp.TableAlias,
    exp.Alias,
    exp.Column,
    exp.Identifier,
    exp.Star,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.DataType,
    exp.DataTypeParam,
    exp.Paren,
    exp.Tuple,
    exp.Interval,
    exp.Var,
    exp.Window,
    exp.WindowSpec,
    exp.Filter,
    exp.Between,
    exp.In,
    exp.Is,
    exp.Like,
    exp.ILike,
    exp.Not,
    exp.And,
    exp.Or,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.NullSafeEQ,
    exp.NullSafeNEQ,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    exp.Neg,
    exp.DPipe,
    exp.Exists,
    exp.Any,
    exp.All,
    exp.If,
    exp.Case,
    exp.Bracket,
    exp.IntDiv,
    exp.Cast,
    exp.TryCast,
    exp.Unit if hasattr(exp, "Unit") else exp.Var,
)

# SQLGlot models boolean connectors as Func subclasses; they are structural, not callable functions.
_STRUCTURAL_FUNC_NODES: tuple[type[exp.Expression], ...] = (exp.And, exp.Or, exp.Not, exp.Paren)

# Functions allowed, by SQLGlot canonical sql_name() (upper case) or, for functions SQLGlot does not
# model (exp.Anonymous), by exact lower-case name. Anything else is rejected.
DEFAULT_ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # aggregates
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "MEDIAN",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "QUANTILE",
        "STDDEV",
        "STDDEV_POP",
        "STDDEV_SAMP",
        "VARIANCE",
        "VARIANCE_POP",
        "COUNT_IF",
        "ARG_MAX",
        "ARG_MIN",
        "ANY_VALUE",
        "APPROX_DISTINCT",
        "BOOL_AND",
        "BOOL_OR",
        "LOGICAL_AND",
        "LOGICAL_OR",
        "CORR",
        "COVAR_POP",
        "COVAR_SAMP",
        "MODE",
        # window
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "PERCENT_RANK",
        "CUME_DIST",
        "NTILE",
        "LAG",
        "LEAD",
        "FIRST_VALUE",
        "LAST_VALUE",
        # conditional / scalar
        "COALESCE",
        "NULLIF",
        "IF",
        "CASE",
        "CAST",
        "TRY_CAST",
        "GREATEST",
        "LEAST",
        "ABS",
        "ROUND",
        "FLOOR",
        "CEIL",
        "SQRT",
        "LN",
        "LOG",
        "POW",
        "POWER",
        "EXP",
        "SIGN",
        # strings (no file/regex-heavy functions)
        "LOWER",
        "UPPER",
        "LENGTH",
        "CONCAT",
        "TRIM",
        "SUBSTRING",
        "LEFT",
        "RIGHT",
        "REPLACE",
        "STARTS_WITH",
        "CONCAT_WS",
        # dates
        "DATE_TRUNC",
        "TIMESTAMP_TRUNC",
        "DATETRUNC",
        "DATEDIFF",
        "DATE_DIFF",
        "TIMESTAMP_DIFF",
        "EXTRACT",
        "YEAR",
        "MONTH",
        "DAY",
        "DAYOFWEEK",
        "DAY_OF_WEEK",
        "DAY_OF_YEAR",
        "DAY_OF_MONTH",
        "DAYOFYEAR",
        "WEEK",
        "QUARTER",
        "TIME_TO_STR",
        "STRFTIME",
        "DATE_ADD",
        "DATE_SUB",
        "TS_OR_DS_ADD",
        "TS_OR_DS_TO_DATE",
        "DATE",
        "TO_DATE",
        "TIME_TO_UNIX",
        "STR_TO_TIME",
        "CURRENT_DATE",
        "TIMESTAMP_ADD",
        "DATE_PART",
        "LAST_DAY",
        "DAYOFMONTH",
        "TS_OR_DS_DIFF",
        # safe string aggregation of already-permitted columns
        "GROUP_CONCAT",
        "STRING_AGG",
    }
)


@dataclass(frozen=True)
class ColumnMeta:
    sensitivity: str


@dataclass
class Catalog:
    """The relations/columns a specific caller may read. Keys are lower-case ``schema.table``."""

    schema_name: str
    relations: dict[str, dict[str, ColumnMeta]]
    column_types: dict[str, dict[str, str]] = field(default_factory=dict)

    def sqlglot_schema(self) -> dict[str, object]:
        out: dict[str, dict[str, dict[str, str]]] = {}
        for qname, cols in self.relations.items():
            schema, table = qname.split(".", 1)
            types = self.column_types.get(qname, {})
            out.setdefault(schema, {})[table] = {c: types.get(c, "VARCHAR") for c in cols}
        return dict(out)


@dataclass(frozen=True)
class ValidatedQuery:
    original_sql: str
    executable_sql: str
    relations: tuple[str, ...]
    columns: tuple[str, ...]  # "schema.table.column"
    max_sensitivity: str | None
    ast_nodes: int
    joins: int
    subquery_depth: int
    applied_row_limit: int
    warnings: tuple[str, ...]


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    return node.sql_name().upper()


def _subquery_depth(node: exp.Expression) -> int:
    depth = 0
    parent = node.parent
    while parent is not None:
        if isinstance(parent, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            depth += 1
        parent = parent.parent
    return depth


def validate_sql(
    sql: str,
    catalog: Catalog,
    limits: QueryLimits,
    *,
    max_rows: int,
    allowed_functions: frozenset[str] = DEFAULT_ALLOWED_FUNCTIONS,
    sensitivity_order: tuple[str, ...] = ("public", "internal", "pseudonymous", "restricted", "pii"),
) -> ValidatedQuery:
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("empty", "SQL text is empty")
    if len(sql) > limits.max_sql_chars:
        raise SqlRejected("too_long", f"SQL exceeds {limits.max_sql_chars} characters")
    if "\x00" in sql:
        raise SqlRejected("invalid_characters", "SQL contains NUL bytes")

    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except (ParseError, TokenError) as exc:
        raise SqlRejected(
            "parse_error", f"SQL could not be parsed: {str(exc).splitlines()[0][:200]}"
        ) from None
    except SqlglotError as exc:  # pragma: no cover - defensive
        raise SqlRejected("parse_error", f"SQL could not be parsed: {type(exc).__name__}") from None
    if len(statements) != 1:
        raise SqlRejected("multiple_statements", f"exactly one statement is allowed (got {len(statements)})")
    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise SqlRejected(
            "not_a_query", f"only SELECT queries are allowed (got {type(root).__name__.upper()})"
        )

    # ---- structural allow-list (fail closed) ----
    nodes = list(root.walk())
    if len(nodes) > limits.max_ast_nodes:
        raise SqlRejected("too_complex", f"query has {len(nodes)} AST nodes (limit {limits.max_ast_nodes})")
    cte_names: set[str] = set()
    for node in nodes:
        if isinstance(node, exp.Func) and not isinstance(node, _STRUCTURAL_FUNC_NODES):
            name = _func_name(node)
            if name not in allowed_functions:
                raise SqlRejected("function_not_allowed", f"function {name} is not permitted")
            continue
        if not isinstance(node, _ALLOWED_NODES):
            raise SqlRejected("unsupported_syntax", f"unsupported SQL construct: {type(node).__name__}")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SqlRejected("unsupported_syntax", "recursive CTEs are not permitted")
        if isinstance(node, exp.CTE):
            cte_names.add(node.alias_or_name.lower())
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise SqlRejected("not_a_query", "SELECT INTO is not permitted")

    joins = sum(1 for n in nodes if isinstance(n, exp.Join))
    if joins > limits.max_joins:
        raise SqlRejected("too_complex", f"query has {joins} joins (limit {limits.max_joins})")
    depth = max((_subquery_depth(n) for n in nodes if isinstance(n, exp.Select)), default=1) - 1
    if depth > limits.max_subquery_depth:
        raise SqlRejected("too_complex", f"subquery depth {depth} exceeds {limits.max_subquery_depth}")

    # ---- relation allow-list ----
    relations: set[str] = set()
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected(
                "relation_not_allowed", "table functions and non-identifier relations are not permitted"
            )
        if table.args.get("catalog"):
            raise SqlRejected("relation_not_allowed", "catalog-qualified relations are not permitted")
        for forbidden in ("version", "when", "sample", "joins", "pivots", "laterals"):
            if table.args.get(forbidden):
                raise SqlRejected("unsupported_syntax", f"table modifier '{forbidden}' is not permitted")
        name = table.name.lower()
        db = (table.db or "").lower()
        if not db and name in cte_names:
            continue
        qname = f"{db or catalog.schema_name}.{name}"
        if qname not in catalog.relations:
            raise SqlRejected(
                "relation_not_allowed", f"relation {qname} is not an approved relation for this caller"
            )
        relations.add(qname)
    if not relations:
        raise SqlRejected("relation_not_allowed", "query must read from at least one approved relation")

    # ---- column allow-list via qualification against the caller-filtered schema ----
    try:
        qualified = qualify(
            root.copy(),
            schema=catalog.sqlglot_schema(),
            db=catalog.schema_name,
            dialect="duckdb",
            validate_qualify_columns=True,
            expand_stars=True,
            quote_identifiers=True,
        )
    except SqlglotError as exc:
        msg = str(exc).splitlines()[0][:200]
        raise SqlRejected("column_not_allowed", f"column/relation resolution failed: {msg}") from None

    columns: set[str] = set()
    try:
        scopes = traverse_scope(qualified)
    except SqlglotError as exc:
        raise SqlRejected(
            "unsupported_syntax", f"could not analyse query scopes: {type(exc).__name__}"
        ) from None
    for scope in scopes:
        for col in scope.columns:
            source = scope.sources.get(col.table)
            if isinstance(source, exp.Table):
                qname = f"{(source.db or catalog.schema_name).lower()}.{source.name.lower()}"
                allowed = catalog.relations.get(qname)
                cname = col.name.lower()
                if allowed is None or cname not in allowed:
                    raise SqlRejected("column_not_allowed", f"column {qname}.{cname} is not permitted")
                columns.add(f"{qname}.{cname}")
            elif source is None and col.table:
                raise SqlRejected("column_not_allowed", f"column {col.sql()} could not be resolved")

    max_sens: str | None = None
    for c in columns:
        qname, cname = c.rsplit(".", 1)
        sens = catalog.relations[qname][cname].sensitivity
        if max_sens is None or sensitivity_order.index(sens) > sensitivity_order.index(max_sens):
            max_sens = sens

    # ---- row limit: fetch max_rows + 1 so truncation can be detected ----
    warnings: list[str] = []
    cap = max_rows + 1
    limit_node = qualified.args.get("limit")
    existing: int | None = None
    if isinstance(limit_node, exp.Limit):
        lit = limit_node.expression
        if isinstance(lit, exp.Literal) and lit.is_int:
            existing = int(lit.this)
        else:
            raise SqlRejected("unsupported_syntax", "LIMIT must be an integer literal")
    if existing is None or existing > cap:
        if existing is not None:
            warnings.append(f"LIMIT {existing} reduced to the policy maximum of {max_rows} rows")
        qualified = qualified.limit(cap, copy=False)
    if any(isinstance(n, exp.CurrentDate) for n in qualified.walk()):
        warnings.append(CURRENT_DATE_WARNING)

    return ValidatedQuery(
        original_sql=sql,
        executable_sql=qualified.sql(dialect="duckdb"),
        relations=tuple(sorted(relations)),
        columns=tuple(sorted(columns)),
        max_sensitivity=max_sens,
        ast_nodes=len(nodes),
        joins=joins,
        subquery_depth=depth,
        applied_row_limit=min(existing, cap) if existing is not None else cap,
        warnings=tuple(warnings),
    )
