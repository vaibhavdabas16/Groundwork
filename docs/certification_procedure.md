# Human review and certification procedure

Every definition shipped in Phase 1 is `draft`. It was proposed by an AI agent
(`agent.claude_code`) from profiling evidence, and **no definition is verified**. The owners in
`context/ownership/owners.yaml` are placeholders, so validation currently makes certification
impossible. That is deliberate.

## Statuses
| status | meaning | agent behaviour |
|---|---|---|
| `draft` | Proposed, not reviewed. | May be used only with an explicit "draft" caveat. Hidden from `verified_only` principals. |
| `verified` | A named human with certification rights reviewed the evidence and approved it. | Preferred source of truth. |
| `deprecated` | Must not be used. Needs `deprecated_reason` and should name `replaced_by`. | Always surfaced, with a do-not-use warning. |

## Rules enforced by `groundwork validate-context`
- `verified` requires `certified_by`, `certified_at` and `decision_ref`.
- The certifier must be an owner of `kind: human` with `can_certify: true` and `placeholder: false`.
- The owning team or person must not be a placeholder.
- Agents and placeholders can never have `can_certify`.
- AI-authored content (`provenance.generated_by: ai | human_and_ai`) cannot be certified by its own author.
- A verified metric must have `sql_status: approved`.
- A draft must not carry certification fields.
- One synonym may not map to two verified definitions, nor to a verified definition and an
  unsupported/ambiguous concept.

## Procedure
1. **Replace placeholders.** A governance admin replaces the placeholder teams in
   `context/ownership/owners.yaml` with real teams. They add each reviewer as `kind: human`,
   `can_certify: true`, using a stable id such as `person.<handle>`.
2. **Open a pull request per definition**, or per small group. The PR must contain:
   - the definition and SQL;
   - evidence: executed results, the `double_counting_check`, and the relevant `data/manifests/dq_report.json` checks;
   - the open ambiguities listed in the definition (status filters, time zone, freight inclusion, ...).
3. **Reviewer checks.** The reviewer, a domain owner:
   - runs the metric SQL through `groundwork query`;
   - checks grain and fanout with `get_join_path`;
   - resolves each listed ambiguity. Resolving one is a *decision*, so record it in the PR.
4. **Update the definition.** Set `owner` to a real team, `sql_status: approved`, and
   `certification: {status: verified, certified_by: person.<reviewer>, certified_at: <UTC>, decision_ref: <PR URL>}`.
   Bump `version` if the definition changed.
5. **CI gate.** CI runs `validate-context` and the full test suite. Merging requires a human
   approval in GitHub; branch protection is to be configured by the repository admin.
6. **Unsupported or ambiguous concepts** (`context/unsupported/`) are resolved the same way. Add a
   metric for the decided interpretation, and either remove the register entry or narrow its synonyms.

An AI agent may draft every step except 4's `certified_by`, and it may never merge.
