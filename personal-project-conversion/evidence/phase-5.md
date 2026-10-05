# Phase 5 — validation and release evidence

**Status: In progress — trial preparation, October 4, 2026 (America/Los_Angeles). Release assessment: insufficient evidence.** The user selected raw reading first with model spending off and requested a blank three-session ordinary-reading baseline. No actual application sessions or user quality/usefulness judgments have occurred.

Use the [trial guide](../PHASE5_TRIAL_GUIDE.md), [prepared manifest](../trials/phase-5-20261004/trial.json), [baseline log](../trials/phase-5-20261004/baseline.csv), and [release assessment](../trials/phase-5-20261004/release-assessment.json). The [original Phase 5 contract](../05-validation-and-release.md) and targets remain unchanged. Prior Phase 4B proofs remain historical local/synthetic acceptance; see [Phase 4B evidence](phase-4b.md).

## Preparation delivered

- A fixed trial identifier, source-hash manifest for the dirty checkout, empty user observation/review/correction/missed-date records, and an inactive raw configuration draft. Runtime profile, database migration observation, baseline, and actual launch provenance remain pending. The draft uses the saved feed choices without applying or replacing selected preferences.
- Read-only evidence export from an explicitly selected local PostgreSQL database, with consistent transaction and database-enforced read-only checks. Frozen Today candidate populations include candidates outside the brief's five-event selection. Prior attempt gaps and incomplete observations are explicit. Database captures cannot become actual reading sessions without separate user observations.
- Deterministic SHA-256/day-balanced group and summary selection, full populations/exclusions, immutable selection extension, and linked sample history. Missing source evidence stays visible; a missing publication timestamp blocks summary selection. Neither selection nor reliability analysis supplies human quality judgments.
- Reliability tabulation separates the original seven real distinct-date sessions from extensions and refuses duplicate logical run identities. Placeholder or unknown evidence cannot pass.

## Required acceptance

| ID | Current result | Missing actual evidence |
| --- | --- | --- |
| P5-01 | Pending | Seven real distinct-date sessions; at least six supported completions without developer intervention; retained failures/recovery. |
| P5-02 | Pending | Thirty actual sampled groups and all user relevance judgments; at least 24 relevant. |
| P5-03 | Pending | Thirty group/source inspections; at least 27 coherent; singleton/multi-article counts. |
| P5-04 | Pending | Ten distinct published summary units with complete all-claim source reviews and reliable earliest-publication ordering. |
| P5-05 | Pending | Actual session/data/spending observations; no saved-data loss, hidden reset, duplicate charge admission, or uncapped dispatch. |
| P5-06 | Pending | Three ordinary baseline observations, seven application reading logs, cost/upkeep comparison, and the user's continued-use judgment. |
| P5-07 | Pending | Verified actual-use release setup/provenance, permitted captured-data demonstration, complete phase records, prioritized remaining issues. |

Totals: **0 passed, 7 pending; 0 actual sessions, 0 baseline observations, 0 sampled quality units, 0 user judgments.** Raw reading is a supported reliability/usefulness mode, but produces neither grouped-event samples nor brief-summary samples. It cannot close P5-02–P5-04.

## Verification and preservation

Preparation verification is recorded in [the dated verification result](phase-5-20261004/verification.json) and [independent review](phase-5-20261004/independent-review.md). These are synthetic/tooling tests, not actual-use evidence. No new live feed/provider requests, runtime activation, migrations, database changes, public deployment, or scheduling were performed. The actual database export path remains unverified against a live database in this preparation; its query/extraction checks use synthetic unit fixtures.

The final focused scope passed **99 tests: 15 record, 61 sampling, and 23 capture cases**, with zero skips, errors, or failures. The separate reviewer independently reran the same 99 cases and exercised capture-to-sampler gaps with synthetic fixtures; those overlapping reruns are not additional unique tests. Scoped lint/format checks passed for all seven new Python files, Git whitespace passed, and [73 local documentation targets](phase-5-20261004/final-check.json) resolved. The initialized trial's runtime source hashes still match; no earlier whole-project acceptance suite was rerun.

The [preparation baseline](phase-5-20261004/baseline.json) records all existing file hashes, Git HEAD/index/staged-diff identity, `.env` hash, and selected preferences before these changes. [Final preservation](phase-5-20261004/preservation.json) compares protected work and historical evidence. No staging, commit, push, cleanup of shared resources, or paid activation is part of this phase preparation.

## Known limitations and next work

The [remaining issues](../trials/phase-5-20261004/remaining-issues.json) prioritize actual baseline/runtime binding and observations, followed by later assisted-trial prerequisites. Current `Report` lacks an immutable publication timestamp: `created_at` is not publication time and `updated_at` can change when stale marking occurs. Captured summaries therefore retain `published_at: null` and block deterministic earliest-publication selection. The existing run row also does not retain every overwritten attempt outcome; capture each attempt before retry and preserve its independent observation. Capture-time Saved rows do not prove prior absence of loss.

The old $0.25 allowance covered only completed Phase 2 verification and provides no recurring authorization. A future profile change to assisted mode needs a newly identified relevance/usefulness trial, a spending decision and current prices, exact runtime activation/provenance, and the missing publication-order evidence. No future quality result is presumed.
