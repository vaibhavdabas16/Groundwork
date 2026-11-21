# Evaluation label changelog

Every change to evaluation cases, gold SQL, comparators or expected outcomes is recorded here.
Held-out locks and re-locks are appended automatically by `contextforge eval gold lock`.

## 2026-11-20 - pre-lock revisions (before any agent run of any kind)

Source: independent AI cross-check of all 90 cases (a separate Claude subagent with read-only
database access; recorded in `evals/reviews.yaml` under `ai_cross_checks`, NOT a human review).
It found no incorrect gold SQL. Changes made in response, all before the held-out lock and
before any experimental result existed:

- cf-006: question reworded ("recorded in total, across all order statuses") - "sold" was ambiguous w.r.t. canceled orders.
- cf-017: added alternative "any status delivered in March 2018" (6,825 vs 6,824).
- cf-027: added alternative "item rows / all orders" (1.1328).
- cf-033: added alternative omitting the zero-value `not_defined` payment type.
- cf-035, cf-055: added calendar-day (`date_diff('day')`) alternatives; fractional vs whole days differ by more than the tolerance.
- cf-038: added Portuguese-category-name alternative (same ranking).
- cf-052: abs_tolerance tightened 0.005 -> 0.0005 (default was ~16% relative on a 3% rate).
- cf-057: question changed to sellers active in 2018 - the original answer (3,095) equalled the seller count and could not discriminate.
- cf-s01, cf-s02, cf-s03, cf-s04, cf-s05, cf-s06, cf-s11, cf-s20: added `answer_check` so that an ANSWER which follows the governed guidance in `context/unsupported/concepts.yaml` is accepted (deterministic phrase groups; cf-s04 also checks the value). Without this, an agent obeying the governed guidance would have been scored as false-confident. The governed context itself was NOT edited to fit the labels.
- cf-s14: `must_mention_all` migrated to the `must_mention` group format (same semantics).
- Also fixed (Phase 1 defect found while freezing gold): the SQL guard rejected allow-listed `dayofweek/dayofyear/dayofmonth` because SQLGlot renames them; their normalised names were added to the allow-list.

Not changed (documented instead): cf-053/cf-070/cf-077 date-level vs timestamp-level lateness is a
deliberate context-dependence test; cf-082 scores only the value, not the deprecation flag.

## 2026-11-19T20:22:28+00:00 - held-out lock
- fingerprint: 6f0320515aed8e5a (70 cases)
- reason: initial freeze before any experimental comparison

## 2026-11-19T20:34:41+00:00 - held-out re-lock
- fingerprint: fb771b63e298df9d (70 cases)
- reason: Snapshot identity changed from the serving-DB file checksum (deployment-specific: depends on the pseudonymisation key) to a content-based snapshot_id (source checksums + build code). Labels, gold SQL and expected rows are unchanged; no agent run existed.
- changed cases: cf-002, cf-005, cf-006, cf-007, cf-009, cf-010, cf-012, cf-013, cf-017, cf-018, cf-019, cf-020, cf-022, cf-023, cf-025, cf-027, cf-028, cf-029, cf-030, cf-032, cf-033, cf-035, cf-037, cf-039, cf-040, cf-041, cf-044, cf-046, cf-048, cf-049, cf-050, cf-051, cf-053, cf-054, cf-055, cf-056, cf-057, cf-058, cf-059, cf-060, cf-062, cf-066, cf-067, cf-068, cf-070, cf-071, cf-072, cf-074, cf-075, cf-077, cf-078, cf-080, cf-081, cf-082, cf-083, cf-s04

## 2026-11-21T11:21:08+00:00 - held-out re-lock
- fingerprint: 6303fabda4cca300 (70 cases)
- reason: Project rename ContextForge -> Groundwork changed the bytes of warehouse/build.py and schema.py (imports and messages only), which are part of the snapshot id. Gold SQL re-executed via eval gold freeze: all rows and columns identical, no label or case change.
- changed cases: cf-002, cf-005, cf-006, cf-007, cf-009, cf-010, cf-012, cf-013, cf-017, cf-018, cf-019, cf-020, cf-022, cf-023, cf-025, cf-027, cf-028, cf-029, cf-030, cf-032, cf-033, cf-035, cf-037, cf-039, cf-040, cf-041, cf-044, cf-046, cf-048, cf-049, cf-050, cf-051, cf-053, cf-054, cf-055, cf-056, cf-057, cf-058, cf-059, cf-060, cf-062, cf-066, cf-067, cf-068, cf-070, cf-071, cf-072, cf-074, cf-075, cf-077, cf-078, cf-080, cf-081, cf-082, cf-083, cf-s04
