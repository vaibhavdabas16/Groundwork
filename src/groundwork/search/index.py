"""Deterministic BM25 keyword retrieval over the context layer.

Documents: assets, columns, glossary terms, metrics, joins and unsupported/ambiguous concepts.
Field weighting is done by repeating high-signal fields (name, synonyms) in the token stream.
A retrieval score is a relevance signal only - it is never evidence that a definition is correct.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from groundwork.context.loader import ContextStore
from groundwork.context.models import Status

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "per",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "which",
        "who",
        "with",
        "do",
        "does",
        "many",
        "much",
        "show",
        "me",
        "give",
        "list",
    ]
)


def _stem(tok: str) -> str:
    for suf in ("ies", "es", "s"):
        if len(tok) > 4 and tok.endswith(suf) and not tok.endswith("ss"):
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("_", " ")
    return [_stem(t) for t in _TOKEN.findall(text) if t not in _STOP]


DocType = Literal["asset", "column", "glossary_term", "metric", "join", "unsupported_concept"]


@dataclass(frozen=True)
class SearchDoc:
    id: str
    type: DocType
    name: str
    summary: str
    status: str | None
    source_file: str | None
    parent_id: str | None
    domain: str | None
    layer: str | None
    tokens: tuple[str, ...]


@dataclass
class SearchHit:
    doc: SearchDoc
    score: float
    matched_terms: list[str] = field(default_factory=list)


def _weighted(*parts: tuple[str, int]) -> list[str]:
    out: list[str] = []
    for text, weight in parts:
        toks = tokenize(text)
        for _ in range(weight):
            out.extend(toks)
    return out


def _restricted_documents(store: ContextStore, mode: str) -> list[SearchDoc]:
    """Documents for the RAW_SCHEMA / DESCRIPTIVE_METADATA experiment conditions.

    Only queryable-layer (serving) tables and their columns are indexed. RAW_SCHEMA indexes table
    names, column names and data types only; DESCRIPTIVE_METADATA adds table and column
    descriptions. Grain, tags, certification, glossary, metrics, joins, warnings and the
    unsupported-concept register are never indexed, so they cannot leak through scores or summaries.
    """
    with_desc = mode == "descriptions"
    docs: list[SearchDoc] = []
    for a in store.assets.values():
        if a.layer != "serving":
            continue
        cols = " ".join(c.name for c in a.columns)
        docs.append(
            SearchDoc(
                a.id,
                "asset",
                a.qualified_name,
                a.description.strip()[:300] if with_desc else "",
                None,
                None,
                None,
                None,
                a.layer,
                tuple(
                    _weighted(
                        (a.table, 3),
                        (a.description if with_desc else "", 1),
                        (cols, 1),
                    )
                ),
            )
        )
        for c in a.columns:
            docs.append(
                SearchDoc(
                    f"{a.id}:{c.name}",
                    "column",
                    f"{a.qualified_name}.{c.name}",
                    (c.description.strip()[:300] if with_desc else c.data_type),
                    None,
                    None,
                    a.id,
                    None,
                    a.layer,
                    tuple(_weighted((c.name, 3), (c.description if with_desc else "", 1), (a.table, 1))),
                )
            )
    return docs


def build_documents(store: ContextStore, mode: str = "full_context") -> list[SearchDoc]:
    if mode != "full_context":
        return _restricted_documents(store, mode)
    docs: list[SearchDoc] = []
    sf = store.source_files
    for a in store.assets.values():
        docs.append(
            SearchDoc(
                a.id,
                "asset",
                a.name,
                a.description.strip()[:300],
                a.certification.status.value,
                sf.get(a.id),
                None,
                a.domain,
                a.layer,
                tuple(
                    _weighted(
                        (a.name, 3),
                        (a.table, 3),
                        (" ".join(a.tags), 2),
                        (a.description, 1),
                        (a.grain, 1),
                        (a.domain, 1),
                        (" ".join(c.name for c in a.columns), 1),
                    )
                ),
            )
        )
        for c in a.columns:
            cid = f"{a.id}:{c.name}"
            docs.append(
                SearchDoc(
                    cid,
                    "column",
                    f"{a.qualified_name}.{c.name}",
                    c.description.strip()[:300],
                    a.certification.status.value,
                    sf.get(a.id),
                    a.id,
                    a.domain,
                    a.layer,
                    tuple(_weighted((c.name, 3), (c.description, 1), (a.table, 1))),
                )
            )
    for t in store.terms.values():
        docs.append(
            SearchDoc(
                t.id,
                "glossary_term",
                t.name,
                t.definition.strip()[:300],
                t.certification.status.value,
                sf.get(t.id),
                None,
                t.domain,
                None,
                tuple(
                    _weighted(
                        (t.name, 3),
                        (" ".join(t.synonyms), 3),
                        (t.definition, 1),
                        (t.business_interpretation or "", 1),
                        (" ".join(t.ambiguities), 1),
                    )
                ),
            )
        )
    for m in store.metrics.values():
        docs.append(
            SearchDoc(
                m.id,
                "metric",
                m.name,
                m.definition.strip()[:300],
                m.certification.status.value,
                sf.get(m.id),
                None,
                None,
                None,
                tuple(
                    _weighted(
                        (m.name, 3),
                        (" ".join(m.synonyms), 3),
                        (m.definition, 1),
                        (m.business_purpose, 1),
                        (" ".join(d.name for d in m.supported_dimensions), 1),
                    )
                ),
            )
        )
    for j in store.joins.values():
        docs.append(
            SearchDoc(
                j.id,
                "join",
                j.id.removeprefix("join.").replace("__", " to "),
                j.grain_note.strip()[:300],
                j.certification.status.value,
                sf.get(j.id),
                None,
                None,
                None,
                tuple(_weighted((j.left, 2), (j.right, 2), (j.grain_note, 1), (j.fanout_warning or "", 1))),
            )
        )
    for u in store.unsupported.values():
        docs.append(
            SearchDoc(
                u.id,
                "unsupported_concept",
                u.name,
                f"[{u.classification.upper()}] {u.reason}"[:300],
                None,
                sf.get(u.id),
                None,
                None,
                None,
                tuple(_weighted((u.name, 3), (" ".join(u.synonyms), 3), (u.reason, 1))),
            )
        )
    return docs


class SearchIndex:
    def __init__(
        self, store: ContextStore, k1: float = 1.2, b: float = 0.75, *, mode: str = "full_context"
    ) -> None:
        self.store = store
        self.mode = mode
        self.docs = build_documents(store, mode)
        self.k1, self.b = k1, b
        self._tf = [Counter(d.tokens) for d in self.docs]
        self._len = [len(d.tokens) for d in self.docs]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._by_id = {d.id: d for d in self.docs}

    def get(self, doc_id: str) -> SearchDoc | None:
        return self._by_id.get(doc_id)

    def search(
        self,
        query: str,
        *,
        types: set[str] | None = None,
        visible_statuses: set[Status] | None = None,
        include_deprecated: bool = True,
        layers: set[str] | None = None,
        domain: str | None = None,
        limit: int = 10,
    ) -> list[SearchHit]:
        q = list(dict.fromkeys(tokenize(query)))
        if not q:
            return []
        hits: list[SearchHit] = []
        for i, d in enumerate(self.docs):
            if types and d.type not in types:
                continue
            if d.status is not None:
                st = Status(d.status)
                if st == Status.deprecated and not include_deprecated:
                    continue
                if visible_statuses is not None and st not in visible_statuses and st != Status.deprecated:
                    continue
            if layers and d.layer is not None and d.layer not in layers:
                continue
            if domain and (d.domain is None or not d.domain.startswith(domain)):
                continue
            tf = self._tf[i]
            score = 0.0
            matched = []
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                matched.append(t)
                denom = f + self.k1 * (1 - self.b + self.b * self._len[i] / (self._avg or 1))
                score += self._idf[t] * f * (self.k1 + 1) / denom
            if score > 0:
                # Exact phrase bonus on name (deterministic tie-breaker toward precise matches).
                if " ".join(q) == " ".join(tokenize(d.name)):
                    score *= 1.5
                hits.append(SearchHit(d, round(score, 4), matched))
        hits.sort(key=lambda h: (-h.score, h.doc.id))
        return hits[:limit]
