# P3-01 through P3-07 claim-level reconciliation

Observed 2026-10-04T02:59:29.039013+00:00; user date October 3, 2026, America/Los_Angeles. **Status: PASS for these seven local/synthetic acceptance rows.** Exact individual outcomes come from the root-owned current-source `personal-final` gate; this mapper performs no runtime operations and does not independently close the whole phase.

The [JSON map](acceptance-map-01-07.json) retains exact assertion summaries, test node IDs/line spans, individual outcomes, source hashes/dates, browser call/line/time provenance, artifact hashes and any gap. The [initial provisional map](acceptance-map-01-07-initial.json) preserves the original literal gaps.

All 28 relevant sources match their verification records: backend/tests at 2026-10-04T02:55:45.892892+00:00, corrected frontend at 2026-10-04T01:34:42.364602+00:00. The one-line workflow failure persistence correction and baseline test failures remain retained separately. Earlier item-5 coordinator evidence is identified as its previous source epoch, not silently promoted to current runtime proof.

<a id="p3-01"></a>

## P3-01 — PASS

Admission and enrichment ceilings of 20; capture 30 distinct articles.

The exact 20 admitted/10 pending boundary is proved by individual PostgreSQL assertions. The current browser fixture uses 27 admitted/10 pending and independently demonstrates the distinction between retained counts and completed processing.

Exact executed test nodes:

- `tests/integration/test_personal_daily_bounds.py::test_p3_01_twenty_admitted_ten_pending_and_twenty_frozen` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Native browser proof: [settings-raw-counts](../phase-3-item-6-20261003/browser/settings-raw-counts.dom.txt), [raw-page-1](../phase-3-item-6-20261003/browser/raw-page-1.dom.txt).

Initial fixture-initial.json establishes expected service/database records; it is not a browser result. Count and raw-reader components were unchanged by the narrow item-6 failure classifier correction.

Remaining gap: none within this local/synthetic acceptance row.

<a id="p3-02"></a>

## P3-02 — PASS

Replay tracking variants and duplicates; retry the same failed run concurrently; rolled-back admission spends no charge.

Capture/admission replay, canonical uniqueness and rollback are proved separately from the new literal concurrent HTTP retry. The latter demonstrates one new attempt/token/delivery, unchanged frozen membership and no repeated admission charge.

Exact executed test nodes:

- `tests/integration/test_personal_daily_bounds.py::test_p3_02_concurrent_duplicate_capture_and_rolled_back_admission` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_run_claim_snapshot.py::test_same_date_start_is_idempotent_under_concurrency` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_phase3_closeout.py::test_two_concurrent_http_retries_rotate_one_attempt_and_preserve_frozen_admissions` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Remaining gap: none within this local/synthetic acceptance row.

<a id="p3-03"></a>

## P3-03 — PASS

Source A has 20 pending and source B has two; admit four in stable round-robin order across reconnection.

The exact A1/B1/A2/B2 order survives engine disposal and new sessions. This is durable reconnection proof; no operating-system process restart is inferred.

Exact executed test nodes:

- `tests/integration/test_personal_daily_bounds.py::test_p3_03_round_robin_order_survives_new_sessions` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_03_older_capture_batch_precedes_newer_and_unknown_dates_follow_known` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Remaining gap: none within this local/synthetic acceptance row.

<a id="p3-04"></a>

## P3-04 — PASS

Retained candidate rotates out; disabling and re-enabling its source preserves eligibility and original content.

Current-source integration proves retained rotation and source re-enable. Fresh browser observations prove disabling preserves stored records and saved settings persist.

Exact executed test nodes:

- `tests/integration/test_personal_daily_bounds.py::test_p3_04_retained_candidate_survives_rotation_and_disabled_source` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Native browser proof: [disabled-source-retained](../phase-3-item-6-20261003/browser/disabled-source-retained.dom.txt), [settings-disabled-reload](../phase-3-item-6-20261003/browser/settings-disabled-reload.dom.txt).

September 20 browser re-enable is historical corroboration only. Item 6 does not claim fresh browser re-enable, and no requirement is closed using that historical observation alone.

Remaining gap: none within this local/synthetic acceptance row.

<a id="p3-05"></a>

## P3-05 — PASS

Oversized response, 501 entries and a full 2,000-item pending queue.

Each capture bound has exact executed assertions. The browser's 501 authored items contain 18 unique URLs plus duplicates, so 18 retained records is correct; that fixture is distinct from the 500 unique-entry integration dataset.

Exact executed test nodes:

- `tests/integration/test_personal_daily_bounds.py::test_p3_05_full_two_thousand_queue_pauses_without_requesting_unknown_input` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_05_transport_and_entry_bounds_persist_truthful_receipts[False]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_05_transport_and_entry_bounds_persist_truthful_receipts[True]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Native browser proof: [capture-entry-boundary](../phase-3-item-6-20261003/browser/capture-entry-boundary.dom.txt).

Oversized transport and a full 2,000-item queue have fresh PostgreSQL proof. The fresh representative browser boundary is the rendered entry-limit receipt with an unknown remote total.

Remaining gap: none within this local/synthetic acceptance row.

<a id="p3-06"></a>

## P3-06 — PASS

Retry spans Los Angeles midnight; an old caller date cannot reset the admission allowance.

Midnight accounting uses the actual local admission day while preserving the original run identity and frozen ceilings. The new actual HTTP request supplies four old caller-date fields; the independently derived server date and unchanged one-run/two-charge result prove that request cannot reopen credit.

Exact executed test nodes:

- `tests/integration/test_personal_daily_bounds.py::test_p3_06_midnight_retry_keeps_run_identity_and_charges_actual_admission_day` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_06_lower_daily_ceiling_applies_now_and_frozen_run_ceiling_cannot_grow` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_run_claim_snapshot.py::test_la_logical_date_retry_preserves_scope_and_stops_after_three_attempts` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_phase3_closeout.py::test_http_old_caller_dates_cannot_backdate_run_or_reopen_same_day_admission` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Remaining gap: none within this local/synthetic acceptance row.

<a id="p3-07"></a>

## P3-07 — PASS

AI disabled, exhausted allowance, missing model configuration and one failed feed preserve raw and prior Saved/Brief reading without paid probes.

Ordinary worker cases explicitly assert zero physical provider calls and paid ledger rows for disabled, configuration and allowance paths. Actual browser observations separately show the distinct causes and raw/prior Saved/Brief readability. Partial feed failure uses the actual offline worker; complete feed and provider failures use actual coordinator records displayed in Chrome.

Exact executed test nodes:

- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[raw-False-disabled_by_profile]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[assisted-False-ai_disabled]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[assisted-True-configuration_missing]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_budget_block_retains_readable_transferable_articles_without_repeated_admission` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_required_provider_failure_stays_failed_with_retained_raw_content` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[False-True-True-1-ai_disabled]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-False-True-1-paid_runtime_disabled]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-True-False-1-configuration_missing]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-True-True-0-allowance_reached]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-True-True-0.0000001-allowance_reached]` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_item5_composed.py::test_missing_route_api_run_keeps_raw_reader_available_without_paid_dispatch` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.
- `tests/integration/test_personal_worker.py::test_worker_returns_partial_state_for_partial_offline_capture` — **PASS** in [personal-final JUnit](personal-final.xml); exact assertions and source hash are in JSON.

Native browser proof: [allowance-reached-visible](../phase-3-item-6-20261003/browser/allowance-reached-visible.dom.txt), [missing-configuration-visible](../phase-3-item-6-20261003/browser/missing-configuration-visible.dom.txt), [ai-disabled-explicit](../phase-3-item-6-20261003/browser/ai-disabled-explicit.dom.txt), [spending-nonzero-gate-off](../phase-3-item-6-20261003/browser/spending-nonzero-gate-off.dom.txt), [capture-failure-corrected](../phase-3-item-6-20261003/browser/capture-failure-corrected.dom.txt), [provider-failure-corrected](../phase-3-item-6-20261003/browser/provider-failure-corrected.dom.txt), [saved-after-correction](../phase-3-item-6-20261003/browser/saved-after-correction.dom.txt), [brief-after-correction](../phase-3-item-6-20261003/browser/brief-after-correction.dom.txt), [brief-ai-blocked](../phase-3-item-6-20261003/browser/brief-ai-blocked.dom.txt).

Browser inputs and monetary obligations are labeled synthetic. No live provider behavior or personal usefulness judgment is claimed. The item-6 UI correction retained two focused baseline failures, 50 affected frontend passes, an independent rerun and corrected native captures. Earlier loading or pre-fix failure captures do not establish the final wording. These browser observations predate the later coordinator failure-stage persistence fix; the failed-run/raw/read UI inputs are unchanged, and no browser rerun after that backend fix is claimed.

Remaining gap: none within this local/synthetic acceptance row.

## Execution and historical boundaries

The root's current gate supplies fresh individual personal integration results after the coordinator correction. Item-5 integration totals overlap those cases and are not added; its retained units and item-6 frontend/browser proofs remain separately sourced. No acceptance row is inferred from a suite total.

Fixture/controller/API snapshots establish expected records. Native DOM captures establish rendered states; images remain scoped to their function call and screenshot moment. Historical September 20 observations and completed Phase 2 feed/model/download proof remain separately qualified and unchanged.

No live paid activation, running broker/worker service, operating-system restart, completely isolated browser network, Phase 4/5 implementation or personal usefulness judgment is claimed.
