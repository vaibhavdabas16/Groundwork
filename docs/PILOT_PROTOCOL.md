# Pilot protocol (7 days, 5–10 users)

## Roles
- **Pilot owner:** runs the deployment and recruits users.
- **Answer reviewers:** two analysts familiar with the Olist data.
- **Context reviewers:** one per domain, plus at least one elevated reviewer.
- **Pilot users:** they must **consent** in Slack before any question is processed.

## Consent and privacy
- The bot shows a notice (version `pilot-notice-v1`) on first contact. It states what is stored (Slack user id, questions, answers, SQL, timings, feedback), the retention period (`retention_days`), and that reviewers see questions in shadow mode.
- A user can opt out at any time by saying `stop`.
- Reports use pseudonymous labels (`users[].label`), not names.
- Raw results beyond a 10-row preview are never stored.

## Stages
| Day | Stage | Exit criterion |
|---|---|---|
| 0 | A. Install and internal validation: DEPLOYMENT checklist; `product check`; `/readyz` 200; the pilot owner asks 10 dev-split questions | All 10 handled with no unhandled errors; the audit chain verifies |
| 1–2 | B. Shadow question collection (`delivery_mode: shadow`): users ask real questions; every ANSWER is held | ≥ 30 real questions collected |
| 2–3 | C. Human correctness review: reviewers approve, correct or reject each held answer and assign a failure category to wrong ones | Every held answer has a decision |
| 4–5 | D. Limited live answering, **only if** the human-approved rate ≥ 80% with no privacy or security violations; otherwise stay in shadow | Recorded go/no-go decision (pilot owner) |
| 3–6 | E. Feedback and failure analysis: review the failures; proposals go through validation → PR → review | Every context-fixable failure has a proposal, a decision request or a documented reason |
| 7 | F. Final report: `contextforge pilot report --out pilot.json`, `pilot export --out requests.csv`, `audit export` | `PILOT_RESULTS.md` filled in from those files only |

## Measures, all from recorded data
- Questions by outcome; clarification rate.
- Human-verified correct answers (APPROVED) versus rejected or corrected; feedback 👍/👎 and categories.
- Latency p50/p95, tokens, cost (when prices are configured).
- Failures by category; proposals by state; approval latency; merged and rejected changes; regressions.
- Security denials; delivery failures.

## Rules
- Never count automated tests or the pilot owner's own questions as user activity. Tag them by using a separate label.
- Never tune prompts on the held-out evaluation split.
- Never edit held-out cases.
