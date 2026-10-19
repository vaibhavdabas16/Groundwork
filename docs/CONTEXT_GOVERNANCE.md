# Context governance

## Who may change what
| Change | Who may draft | Who must approve | Path |
|---|---|---|---|
| Column description, glossary alias or clarification, join documentation, fanout warning, metric caveat, unsupported-concept entry | AI improvement engine or a human | A context reviewer covering the domain, in Slack **plus** a GitHub review | Proposal → PR → merge → deploy |
| Metric SQL change, deprecation | AI or a human | An **elevated** context reviewer, in Slack **plus** a GitHub review | Same; tier `elevated` |
| A business decision (ambiguous metric, contradictory context, missing source data) | Engine posts a **decision request**; it never drafts the policy itself | The owner named in `decision_needed_from` | A human writes the decision as a normal PR |
| Certification (`verified`, `certified_by`) | **Humans only** | Per `certification_procedure.md` | Manual PR; AI edits reset `verified` to `draft` |
| Ownership, policy, principals, eval gold, scoring, CI, code | Never through proposals | Code review | Normal engineering PR |

## Diagnosis before proposal
`failures.classify` maps every unsuccessful request to one category. Only these categories can lead to a context change:
- `MISSING_BUSINESS_DEFINITION`
- `MISSING_COLUMN_DESCRIPTION`
- `MISSING_JOIN_GUIDANCE`
- `INCORRECT_JOIN_GRAIN`
- `DEPRECATED_ASSET_USAGE`

`AMBIGUOUS_METRIC`, `CONTRADICTORY_CONTEXT` and `INSUFFICIENT_SOURCE_DATA` become **human-decision requests**.

Everything else is not context-fixable and never produces a proposal:
- `SQL_*`;
- `QUERY_RESOURCE_LIMIT`;
- `PERMISSION_DENIED`;
- `DATABASE_UNAVAILABLE`;
- `MODEL_UNAVAILABLE`;
- `CONTEXT_RETRIEVAL_FAILURE`;
- `EVALUATION_REGRESSION`;
- `UNKNOWN_ERROR`.

Shadow-mode reviewers can assign a category when rejecting a wrong answer. That diagnosis is recorded as the human's.

## Evidence requirements
Each operation must cite evidence that the system observed:
- a real warehouse column (`asset:column`);
- a context object id;
- a governed query id the drafter executed in that session;
- the originating trace;
- a data-quality finding.

Uncited or fabricated evidence is refused before anything is stored.

## Release
Only a deployed release is served (`context deploy`):
- it is validated at load;
- it is copied to an immutable directory;
- it is switched atomically;
- it is verified by content hash;
- rollback re-validates the target.

An invalid release is rejected and the previous one stays active.

The version used is recorded on every request, trace and audit event.
