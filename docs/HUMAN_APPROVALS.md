# Human approvals

There are two approval types. They are independent: separate tables, buttons, permissions and audit actions.

## 1. Answer approval (shadow mode)
**Who:** `answer_reviewers` in `config/product.yaml`. Each must map to a non-placeholder human owner.

**Card:** with `delivery_mode: shadow`, every `ANSWER` is held and a card is posted to `review_channel_id`. It shows:
- the question;
- the proposed answer;
- the result preview;
- the executed SQL;
- definitions and certification;
- caveats;
- the trace id.

**Actions:**
| Button | Effect |
|---|---|
| Approve answer | The user receives the answer, labelled "Reviewed and approved by @reviewer". |
| Request clarification | A modal; the reviewer's question is sent attributed to the reviewer, and the user's reply continues the original question. |
| Correct / reject | A modal with a category, a reason and an optional correction. The correction is sent **as written by the reviewer**: the bot never impersonates them. The category becomes a human-diagnosed failure. |

**Refused (tested):**
- the requester reviewing their own answer;
- non-reviewers;
- replays of an already-decided review.

Clarifications and abstentions carry no data and are delivered directly.

## 2. Context-change approval
**Who:** `context_reviewers` with `domains` patterns, and `elevated: true` for metric SQL and deprecations.

**Card:** shows the proposal id, revision, tier, category, deficiency, files, evidence, the validation verdict, regression counts and the PR link.

**Actions:** Approve, Reject (reason modal), Request changes (reason modal), View diff.

**Backend checks, inside one transaction (`ProposalStore.decide`):**
1. The clicking Slack user is a configured context reviewer.
2. They are not the proposal author. The AI author identity can never be a reviewer.
3. They cover all affected domains, and are elevated if the tier is elevated.
4. The proposal is `AWAITING_REVIEW`, meaning validation passed.
5. The card's revision equals the current revision; stale cards are refused.

Each decision records the reviewer owner id, the Slack user id, a timestamp, the revision and the reason. Refusals are audited too.

**After approval:**
- A Slack approval makes the proposal eligible for the GitHub process. It does **not** merge anything.
- `MERGE_ELIGIBLE` additionally requires:
  - an open PR whose head SHA equals the validated commit;
  - all checks passing;
  - when `require_github_approval` is set (the default), a human GitHub approval on that head commit.
- **Merging:** humans merge on GitHub by default. `auto_merge: true` merges only once every one of those conditions holds.
- **A push to the PR after approval** sends the proposal back to `PROPOSED`, which requires re-validation and re-approval.
- **A PR merged outside the workflow** becomes `BLOCKED` and is not deployed.

## What is NOT authorization
- A Slack message saying "approved".
- An LLM saying a change is correct.
- A GitHub approval without the Slack approval.
- A Slack approval without GitHub branch protection.

## Records so far
**Zero.** No human approval of an answer or a context change has happened: no Slack workspace or reviewers were configured during development. See `PILOT_RESULTS.md`.
