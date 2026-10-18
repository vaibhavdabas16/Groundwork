"""Slack Block Kit rendering. Pure functions (unit-tested), no I/O.

Limits respected: <= 50 blocks, section text <= 3000 chars, button value <= 2000 chars, modal
text inputs <= 3000. Result tables are a preview (<= 10 rows, width-capped). Values that look like
e-mail addresses or Brazilian tax ids are redacted before rendering (defence in depth; the serving
warehouse already excludes PII).
"""

from __future__ import annotations

import json
import re
from typing import Any

from contextforge.agent.schemas import AgentResponse, Outcome
from contextforge.context.loader import ContextStore

MAX_SECTION = 2900
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


def esc(text: str) -> str:
    """Escape Slack mrkdwn control characters and neutralise mentions/links from model text."""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return t.replace("@channel", "@​channel").replace("@here", "@​here").replace("@everyone", "@​everyone")


def redact(value: Any) -> tuple[Any, bool]:
    if not isinstance(value, str):
        return value, False
    new = _CPF.sub("[redacted]", _EMAIL.sub("[redacted]", value))
    return new, new != value


def _cut(s: str, n: int = MAX_SECTION) -> str:
    return s if len(s) <= n else s[: n - 20] + "\n… (truncated)"


def table_preview(
    columns: list[str], rows: list[list[Any]], *, max_rows: int = 10, width: int = 24
) -> tuple[str, bool]:
    redacted = False

    def cell(v: Any) -> str:
        nonlocal redacted
        v, r = redact(v)
        redacted |= r
        s = "∅" if v is None else (f"{v:,.2f}" if isinstance(v, float) else str(v))
        return s if len(s) <= width else s[: width - 1] + "…"

    shown = [[cell(v) for v in row] for row in rows[:max_rows]]
    head = [c[:width] for c in columns]
    widths = [max([len(h)] + [len(r[i]) for r in shown]) for i, h in enumerate(head)]
    lines = [" | ".join(h.ljust(w) for h, w in zip(head, widths, strict=False))]
    lines.append("-+-".join("-" * w for w in widths))
    lines += [" | ".join(v.ljust(w) for v, w in zip(r, widths, strict=False)) for r in shown]
    return _cut("```" + "\n".join(lines) + "```"), redacted


def _names(store: ContextStore | None, ids: list[str]) -> list[str]:
    out = []
    for i in ids:
        obj = None
        if store is not None:
            obj = store.metrics.get(i) or store.terms.get(i) or store.assets.get(i)
        out.append(f"{obj.name} (`{i}`)" if obj is not None else f"`{i}`")
    return out


CERT_TEXT = {
    "verified": ":white_check_mark: Uses human-verified definitions.",
    "draft": ":warning: Uses *DRAFT* definitions (not yet human-verified).",
    "mixed": ":warning: Mixes verified and *DRAFT* definitions.",
    "deprecated_used": ":no_entry: Relied on a *DEPRECATED* definition or table.",
    "not_exposed": "Certification not exposed in this mode.",
    "none": "No governed definition was used.",
}


def answer_blocks(
    resp: AgentResponse,
    store: ContextStore | None,
    *,
    request_id: str,
    reviewed_by: str | None = None,
    shadow_note: str | None = None,
    show_feedback: bool = True,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if shadow_note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": esc(shadow_note)}]})
    text = resp.answer or ""
    if resp.outcome == Outcome.CLARIFICATION_REQUIRED and resp.clarification_question:
        text = f"*I need a clarification:* {resp.clarification_question}\n_Reply in this thread to continue, or say `cancel`._"
    elif resp.outcome != Outcome.ANSWER:
        label = {
            Outcome.INSUFFICIENT_CONTEXT: "I can't answer this reliably with the governed context",
            Outcome.UNSUPPORTED_QUESTION: "This isn't supported by the available data",
            Outcome.POLICY_DENIED: "This request isn't permitted",
            Outcome.EXECUTION_ERROR: "I couldn't complete this request",
        }.get(resp.outcome, resp.outcome.value)
        text = f"*{label}.* {text}"
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _cut(esc(text) if not text.startswith("*") else text)},
        }
    )
    if resp.outcome == Outcome.ANSWER and resp.result_columns:
        tbl, redacted = table_preview(resp.result_columns, resp.result_preview)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": tbl}})
        extra = []
        if resp.result_row_count and resp.result_row_count > len(resp.result_preview[:10]):
            extra.append(f"Showing {min(10, len(resp.result_preview))} of {resp.result_row_count} rows.")
        if resp.result_truncated:
            extra.append("Result was capped by the row limit.")
        if redacted:
            extra.append("Some values were redacted.")
        if extra:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " ".join(extra)}]})
    defs = _names(store, [*resp.referenced_metrics, *resp.referenced_terms])
    assets = _names(store, resp.referenced_assets)
    ctx_lines = []
    if defs:
        ctx_lines.append("*Definitions used:* " + ", ".join(defs[:6]))
    if assets:
        ctx_lines.append("*Source tables:* " + ", ".join(assets[:6]))
    if resp.outcome == Outcome.ANSWER:
        ctx_lines.append(CERT_TEXT.get(resp.certification_status, resp.certification_status))
    if resp.assumptions:
        ctx_lines.append("*Assumptions:* " + "; ".join(esc(a) for a in resp.assumptions[:4]))
    if resp.warnings:
        ctx_lines.append("*Caveats:* " + "; ".join(esc(w) for w in resp.warnings[:4]))
    if ctx_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _cut("\n".join(ctx_lines))}})
    footer = f"Trace `{request_id}` · context `{resp.context_version or '-'}`"
    if reviewed_by:
        footer = f"Reviewed and approved by <@{reviewed_by}> · " + footer
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    actions: list[dict[str, Any]] = []
    if resp.executed_sql or resp.referenced_assets:
        actions.append(
            {
                "type": "button",
                "action_id": "cf_show_details",
                "text": {"type": "plain_text", "text": "Show SQL & details"},
                "value": request_id,
            }
        )
    if show_feedback and resp.outcome in (
        Outcome.ANSWER,
        Outcome.INSUFFICIENT_CONTEXT,
        Outcome.UNSUPPORTED_QUESTION,
    ):
        actions += [
            {
                "type": "button",
                "action_id": "cf_feedback_up",
                "text": {"type": "plain_text", "text": ":thumbsup: Helpful"},
                "value": request_id,
            },
            {
                "type": "button",
                "action_id": "cf_feedback_down",
                "text": {"type": "plain_text", "text": ":thumbsdown: Wrong / unhelpful"},
                "value": request_id,
            },
        ]
    if actions:
        blocks.append({"type": "actions", "block_id": f"cf_act_{request_id[:20]}", "elements": actions})
    return blocks[:50]


def details_blocks(resp: AgentResponse, store: ContextStore | None) -> list[dict[str, Any]]:
    b: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "SQL and context details"}}
    ]
    if resp.executed_sql:
        b.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _cut(
                        "*Executed SQL* (as run by the guard)\n```"
                        + resp.executed_sql.replace("```", "'''")
                        + "```"
                    ),
                },
            }
        )
    for name, ids in (
        ("Metrics", resp.referenced_metrics),
        ("Glossary terms", resp.referenced_terms),
        ("Assets", resp.referenced_assets),
    ):
        if ids and store is not None:
            lines = []
            for i in ids[:5]:
                o = store.metrics.get(i) or store.terms.get(i) or store.assets.get(i)
                if o is not None:
                    desc = getattr(o, "definition", None) or getattr(o, "description", "")
                    status = o.certification.status.value if hasattr(o, "certification") else "-"
                    lines.append(f"• *{esc(o.name)}* `{i}` [{status}]: {esc(str(desc))[:400]}")
            if lines:
                b.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": _cut(f"*{name}*\n" + "\n".join(lines))},
                    }
                )
    q = resp.queries
    b.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{len(q)} query attempt(s), {resp.sql_repair_attempts} correction(s) · model `{resp.model.requested_model}` · {resp.latency_ms / 1000:.1f}s · mode `{resp.context_mode}`",  # noqa: E501 - long literal
                }
            ],
        }
    )
    return b[:50]


def status_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "context", "elements": [{"type": "mrkdwn", "text": esc(text)}]}]


def consent_blocks(notice_version: str, notice_text: str) -> list[dict[str, Any]]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut(notice_text)}},
        {
            "type": "actions",
            "block_id": "cf_consent",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": "cf_consent_accept",
                    "text": {"type": "plain_text", "text": "I agree"},
                    "value": notice_version,
                },
                {
                    "type": "button",
                    "action_id": "cf_consent_decline",
                    "text": {"type": "plain_text", "text": "No thanks"},
                    "value": notice_version,
                },
            ],
        },
    ]


def shadow_review_blocks(
    request_id: str, user_id: str, resp: AgentResponse, store: ContextStore | None
) -> list[dict[str, Any]]:
    b: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Answer review (shadow mode)"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _cut(f"*Question from <@{user_id}>:*\n>{esc(resp.question)}")},
        },
    ]
    b += [
        x
        for x in answer_blocks(resp, store, request_id=request_id, show_feedback=False)
        if x["type"] != "actions"
    ]
    if resp.executed_sql:
        b.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _cut("*Executed SQL*\n```" + resp.executed_sql.replace("```", "'''") + "```"),
                },
            }
        )
    b.append(
        {
            "type": "actions",
            "block_id": f"cf_shadow_{request_id[:20]}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": "cf_answer_approve",
                    "text": {"type": "plain_text", "text": "Approve answer"},
                    "value": request_id,
                },
                {
                    "type": "button",
                    "action_id": "cf_answer_clarify",
                    "text": {"type": "plain_text", "text": "Request clarification"},
                    "value": request_id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "action_id": "cf_answer_reject",
                    "text": {"type": "plain_text", "text": "Correct / reject"},
                    "value": request_id,
                },
            ],
        }
    )
    return b[:50]


def proposal_review_blocks(row: Any) -> list[dict[str, Any]]:
    p = row.proposal
    v = row.validation or {}
    reg = v.get("regression") or {}
    value = json.dumps({"pid": p.proposal_id, "rev": row.revision})
    files = ", ".join(f"`{d.path}`" for d in p.diffs) or "none"
    ev = "\n".join(f"• `{e.kind}` `{esc(e.ref)}`: {esc(e.detail)[:200]}" for e in p.draft.evidence[:6])
    lines = [
        f"*{esc(p.title)}*",
        f"*Proposal* `{p.proposal_id}` · revision {row.revision} · tier *{p.review_tier}* · category `{p.category}`",
        f"*Problem:* {esc(p.draft.deficiency)[:600]}",
        f"*Files:* {files}",
        f"*Evidence:*\n{ev}" if ev else "*Evidence:* none",
        f"*Validation:* {v.get('verdict', 'not run')} · regression cases {reg.get('cases', '-')}: baseline {reg.get('baseline_correct', '-')} → candidate {reg.get('candidate_correct', '-')} correct · regressed {reg.get('regressed_cases', [])}",  # noqa: E501 - long literal
    ]
    if row.raw.get("pr_url"):
        lines.append(f"*Pull request:* <{row.raw['pr_url']}|#{row.raw['pr_number']}>")
    if p.review_tier == "elevated":
        lines.append(":rotating_light: *Elevated review*: changes metric SQL or deprecates an object.")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Context change review"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _cut("\n".join(lines))}},
        {
            "type": "actions",
            "block_id": f"cf_prop_{p.proposal_id}_{row.revision}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "action_id": "cf_prop_approve",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "value": value,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "action_id": "cf_prop_reject",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": "cf_prop_changes",
                    "text": {"type": "plain_text", "text": "Request changes"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": "cf_prop_diff",
                    "text": {"type": "plain_text", "text": "View diff"},
                    "value": value,
                },
            ],
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Approval here authorizes the change for the GitHub merge process; required GitHub reviews and checks still apply.",  # noqa: E501 - long literal
                }
            ],
        },
    ]


def diff_blocks(row: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Diff {row.proposal_id} r{row.revision}"}}
    ]
    for d in row.proposal.diffs[:8]:
        out.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _cut(f"`{d.path}`\n```" + d.unified_diff.replace("```", "'''") + "```"),
                },
            }
        )
    if row.raw.get("pr_url"):
        out.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Full diff: <{row.raw['pr_url']}|pull request>"}],
            }
        )
    return out[:50]


def reason_modal(
    callback_id: str,
    title: str,
    private_metadata: str,
    *,
    label: str = "Reason",
    with_category: list[str] | None = None,
    with_correction: bool = False,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if with_category:
        blocks.append(
            {
                "type": "input",
                "block_id": "category",
                "label": {"type": "plain_text", "text": "What was wrong?"},
                "element": {
                    "type": "static_select",
                    "action_id": "v",
                    "options": [
                        {"text": {"type": "plain_text", "text": c.replace("_", " ").title()[:75]}, "value": c}
                        for c in with_category
                    ],
                },
            }
        )
    blocks.append(
        {
            "type": "input",
            "block_id": "reason",
            "label": {"type": "plain_text", "text": label},
            "element": {"type": "plain_text_input", "action_id": "v", "multiline": True, "max_length": 2000},
        }
    )
    if with_correction:
        blocks.append(
            {
                "type": "input",
                "block_id": "correction",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Corrected answer to send (optional; sent as written by you)",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "v",
                    "multiline": True,
                    "max_length": 2900,
                },
            }
        )
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": private_metadata[:3000],
        "title": {"type": "plain_text", "text": title[:24]},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


SECRET_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "CONTEXTFORGE_GITHUB_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "CONTEXTFORGE_PSEUDONYM_KEY",
)
_TOKEN_SHAPES = re.compile(
    r"\b(xox[abpr]-[\w-]{10,}|xapp-[\w-]{10,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)


def scrub(obj: Any) -> Any:
    """Remove configured secret values and token-shaped strings from an outbound Slack payload."""
    import os

    secrets = [v for k in SECRET_ENV if (v := os.environ.get(k)) and len(v) >= 8]

    def s(x: Any) -> Any:
        if isinstance(x, str):
            for sec in secrets:
                x = x.replace(sec, "[secret removed]")
            return _TOKEN_SHAPES.sub("[secret removed]", x)
        if isinstance(x, list):
            return [s(i) for i in x]
        if isinstance(x, dict):
            return {k: s(v) for k, v in x.items()}
        return x

    return s(obj)


class ScrubbingGateway:
    """Wraps any SlackGateway so every outbound text/blocks payload is scrubbed."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def post(
        self,
        channel: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> str:
        return str(self.inner.post(channel, text=scrub(text), blocks=scrub(blocks), thread_ts=thread_ts))

    def update(self, channel: str, ts: str, *, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        self.inner.update(channel, ts, text=scrub(text), blocks=scrub(blocks))

    def ephemeral(
        self,
        channel: str,
        user: str,
        *,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
    ) -> None:
        self.inner.ephemeral(channel, user, text=scrub(text), blocks=scrub(blocks), thread_ts=thread_ts)

    def open_modal(self, trigger_id: str, view: dict[str, Any]) -> None:
        self.inner.open_modal(trigger_id, scrub(view))
