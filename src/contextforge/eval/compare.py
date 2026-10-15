"""Execution-result comparison (the primary correctness signal).

Rules (fixed before any live evaluation; changes must be logged in docs/EVALUATION_METHODOLOGY.md):

* Values are normalised: numbers (int/float/Decimal/numeric strings) -> float; booleans -> bool;
  strings -> trimmed, case-folded; ISO dates/timestamps kept as strings; NULL == NULL.
* Numbers match when ``|a-b| <= abs_tol`` or ``|a-b| <= rel_tol * max(|a|,|b|)``. With
  ``percent_scaling`` a fraction may also be given as a percentage (``b == 100*a``).
* ``month_columns``: '2018-03', '2018-03-01', '2018-03-01T00:00:00' all normalise to '2018-03'.
* scalar: the gold result is 1x1; the agent result must have exactly one row and SOME column of it
  must match (extra columns are allowed).
* rows: the agent result must have exactly as many rows as gold. Each compared gold column is
  mapped to a distinct agent column (names are ignored; mapping is searched by values), then rows
  are matched as a multiset (``ordered: false``) or position by position (``ordered: true``).
  Extra agent columns are allowed; missing or extra rows are not.
* Empty agent results never match (gold results are verified non-empty at freeze time).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from contextforge.eval.cases import Comparator

_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})(-01)?([ T]00:00:00(\.0+)?)?$")


@dataclass
class Comparison:
    match: bool
    reason: str
    matched_label: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _norm(v: Any, *, month: bool = False) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, str):
        s = v.strip()
        if month:
            m = _MONTH.match(s)
            if m:
                return f"{m.group(1)}-{m.group(2)}"
        if _NUM.match(s):
            return float(s)
        low = s.casefold()
        if low in ("true", "false"):
            return low == "true"
        return low
    return str(v).strip().casefold()


def _eq(a: Any, b: Any, c: Comparator) -> bool:
    """a: gold, b: agent."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        if isinstance(a, bool) and isinstance(b, bool):
            return a == b
        if isinstance(a, bool) and isinstance(b, float):
            return float(a) == b
        if isinstance(b, bool) and isinstance(a, float):
            return float(b) == a
        return False
    if isinstance(a, float) and isinstance(b, float):

        def close(x: float, y: float, scale: float = 1.0) -> bool:
            d = abs(x - y)
            return d <= c.abs_tolerance * scale or d <= c.rel_tolerance * max(abs(x), abs(y))

        if close(a, b):
            return True
        return bool(c.percent_scaling and close(a * 100.0, b, 100.0))
    return bool(a == b)


def _column(rows: list[list[Any]], i: int, month: bool) -> list[Any]:
    return [_norm(r[i], month=month) for r in rows]


def _sort_key(v: Any) -> tuple[int, Any]:
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, float):
        return (2, v)
    return (3, str(v))


def _columns_compatible(g: list[Any], a: list[Any], c: Comparator, ordered: bool) -> bool:
    if ordered:
        return all(_eq(x, y, c) for x, y in zip(g, a, strict=True))
    gs, as_ = sorted(g, key=_sort_key), sorted(a, key=_sort_key)
    return all(_eq(x, y, c) for x, y in zip(gs, as_, strict=True))


def _rows_match(g_rows: list[list[Any]], a_rows: list[list[Any]], c: Comparator, ordered: bool) -> bool:
    if ordered:
        return all(
            all(_eq(x, y, c) for x, y in zip(gr, ar, strict=True))
            for gr, ar in zip(g_rows, a_rows, strict=True)
        )
    unused = list(range(len(a_rows)))
    for gr in g_rows:
        hit = next((j for j in unused if all(_eq(x, y, c) for x, y in zip(gr, a_rows[j], strict=True))), None)
        if hit is None:
            return False
        unused.remove(hit)
    return True


def compare_one(
    gold_cols: list[str],
    gold_rows: list[list[Any]],
    agent_cols: list[str],
    agent_rows: list[list[Any]],
    c: Comparator,
) -> Comparison:
    if not agent_rows:
        return Comparison(False, "agent result is empty")
    if c.type == "scalar":
        if len(gold_rows) != 1 or len(gold_rows[0]) != 1:
            return Comparison(False, "gold is not 1x1 (label error)")
        if len(agent_rows) != 1:
            return Comparison(False, f"expected a single row, agent returned {len(agent_rows)}")
        g = _norm(gold_rows[0][0])
        for i, v in enumerate(agent_rows[0]):
            if _eq(g, _norm(v), c):
                return Comparison(
                    True,
                    "scalar match",
                    details={"agent_column": agent_cols[i] if i < len(agent_cols) else i},
                )
        return Comparison(False, "no column of the agent row matches the gold value", details={"gold": g})
    # rows
    want = c.columns or gold_cols
    try:
        g_idx = [gold_cols.index(x) for x in want]
    except ValueError:
        return Comparison(False, f"comparator columns {want} not in gold columns {gold_cols} (label error)")
    if len(agent_rows) != len(gold_rows):
        return Comparison(False, f"row count {len(agent_rows)} != gold {len(gold_rows)}")
    n_agent = len(agent_rows[0])
    month = set(c.month_columns)
    g_vals = {gi: _column(gold_rows, gi, gold_cols[gi] in month) for gi in g_idx}
    # candidate agent columns per gold column (value-compatible)
    cands: dict[int, list[int]] = {}
    for gi in g_idx:
        is_month = gold_cols[gi] in month
        cands[gi] = [
            aj
            for aj in range(n_agent)
            if _columns_compatible(g_vals[gi], _column(agent_rows, aj, is_month), c, c.ordered)
        ]
        if not cands[gi]:
            return Comparison(False, f"no agent column matches gold column {gold_cols[gi]!r}")

    def search(k: int, used: dict[int, int]) -> dict[int, int] | None:
        if k == len(g_idx):
            gproj = [[g_vals[gi][r] for gi in g_idx] for r in range(len(gold_rows))]
            aproj = [
                [_norm(agent_rows[r][used[gi]], month=gold_cols[gi] in month) for gi in g_idx]
                for r in range(len(agent_rows))
            ]
            return dict(used) if _rows_match(gproj, aproj, c, c.ordered) else None
        gi = g_idx[k]
        for aj in cands[gi]:
            if aj in used.values():
                continue
            used[gi] = aj
            found = search(k + 1, used)
            if found is not None:
                return found
            del used[gi]
        return None

    mapping = search(0, {})
    if mapping is None:
        return Comparison(False, "columns match individually but rows do not align")
    return Comparison(
        True,
        "rows match",
        details={
            "column_mapping": {
                gold_cols[g]: agent_cols[a] if a < len(agent_cols) else a for g, a in mapping.items()
            }
        },
    )


def compare_to_expected(
    expected: dict[str, Any], agent_cols: list[str], agent_rows: list[list[Any]], c: Comparator
) -> Comparison:
    primary = compare_one(expected["columns"], expected["rows"], agent_cols, agent_rows, c)
    if primary.match:
        primary.matched_label = "primary"
        return primary
    for alt in expected.get("alternatives", []):
        r = compare_one(alt["columns"], alt["rows"], agent_cols, agent_rows, c)
        if r.match:
            r.matched_label = alt["label"]
            r.reason += f" ({alt['label']} interpretation)"
            return r
    return primary
