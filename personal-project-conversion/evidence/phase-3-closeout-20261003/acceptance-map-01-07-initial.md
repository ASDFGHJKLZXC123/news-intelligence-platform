# P3-01 through P3-07 claim-level reconciliation

Observed 2026-10-04T02:41:45.064341+00:00; user date October 3, 2026, America/Los_Angeles. **Five rows PASS; P3-02 and P3-06 remain PARTIAL until the two new literal scenarios execute.** This map inspects retained proof and current source; it does not rerun suites or close the global phase.

Exact assertion summaries, node IDs, source line spans, per-test JUnit outcomes, native call/line/time provenance, artifact hashes, and all 25 current source hashes are in [the JSON map](acceptance-map-01-07.json). All 25 current relevant files match their verified item-5 or item-6 source hashes. Backend/test verification ended 2026-10-03T23:37:39.934431+00:00; corrected frontend verification is pinned by item 6 at 2026-10-04T01:34:42.364602+00:00.

## P3-01 — PASS

Admission/enrichment ceilings20; capture30 distinct articles.

Exact20/10 numerical boundary is demonstrated by passing PostgreSQL assertions; current browser fixture deliberately uses27admitted/10pending and proves count semantics rather than reproducing20/10.

Executed exact node IDs:

- `tests/integration/test_personal_daily_bounds.py::test_p3_01_twenty_admitted_ten_pending_and_twenty_frozen` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.

Native browser proof: [settings-raw-counts](../phase-3-item-6-20261003/browser/settings-raw-counts.dom.txt), [raw-page-1](../phase-3-item-6-20261003/browser/raw-page-1.dom.txt).

Initial fixture-initial.json is expected database/service state, not a browser result. Browser count semantics are unchanged by item6 classifier-only correction.

Remaining gap: none within this local/synthetic acceptance row.

## P3-02 — PARTIAL

Replay/tracking variants/duplicates and same retry concurrently; rollback gives no charge.

Capture/admission replay, uniqueness, rollback and competing starts are proven. Existing tests did not race retries of one failed logical run.

Executed exact node IDs:

- `tests/integration/test_personal_daily_bounds.py::test_p3_02_concurrent_duplicate_capture_and_rolled_back_admission` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_run_claim_snapshot.py::test_same_date_start_is_idempotent_under_concurrency` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
Remaining gap: New narrow concurrent HTTP retry test awaits root-owned PostgreSQL execution and independent review; do not infer its pass from older suite totals.

Proposed exact new node ID: `tests/integration/test_personal_phase3_closeout.py::test_two_concurrent_http_retries_rotate_one_attempt_and_preserve_frozen_admissions`.

## P3-03 — PASS

A20 pending/B2; ceiling4; stable round-robin and restart.

The exact source-count/order/restart scenario is a passing PostgreSQL demonstration, not a UI or simulated algorithm-only claim.

Executed exact node IDs:

- `tests/integration/test_personal_daily_bounds.py::test_p3_03_round_robin_order_survives_new_sessions` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_03_older_capture_batch_precedes_newer_and_unknown_dates_follow_known` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
Remaining gap: none within this local/synthetic acceptance row.

## P3-04 — PASS

Retained candidate rotatesout; disable/re-enable preserves eligibility/content.

Retained rotation and source re-enable are freshly demonstrated by current passing integration. Fresh browser proves disable/preservation/persistence.

Executed exact node IDs:

- `tests/integration/test_personal_daily_bounds.py::test_p3_04_retained_candidate_survives_rotation_and_disabled_source` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.

Native browser proof: [disabled-source-retained](../phase-3-item-6-20261003/browser/disabled-source-retained.dom.txt), [settings-disabled-reload](../phase-3-item-6-20261003/browser/settings-disabled-reload.dom.txt).

September20 browser re-enable is historical corroboration only; item6 does not claim fresh browser re-enable. No requirement is closed using that historical observation alone.

Remaining gap: none within this local/synthetic acceptance row.

## P3-05 — PASS

Oversizedresponse/501entries/full2000queue.

Every relevant capturebound has its own exact executed assertion. Browser501 authored input uses18uniqueURLs+duplicates, so18retained is correct and is not the500uniqueXMLtest dataset.

Executed exact node IDs:

- `tests/integration/test_personal_daily_bounds.py::test_p3_05_full_two_thousand_queue_pauses_without_requesting_unknown_input` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_05_transport_and_entry_bounds_persist_truthful_receipts[False]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_05_transport_and_entry_bounds_persist_truthful_receipts[True]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.

Native browser proof: [capture-entry-boundary](../phase-3-item-6-20261003/browser/capture-entry-boundary.dom.txt).

Oversizedtransport/full2000queue have fresh PostgreSQL proof, not fresh browser captures. The required representative displayed boundary is the actual entry-limit receipt.

Remaining gap: none within this local/synthetic acceptance row.

## P3-06 — PARTIAL

Retry spans LA midnight; caller oldreportdate cannot resetallowance.

Midnight, actualadmissionday, frozenrun/enrichment limits and attemptmaximum are demonstrated. API start source uses _utc_now() and accepts no callerdate parameter, but that is source proof rather than the missing exact HTTP scenario.

Executed exact node IDs:

- `tests/integration/test_personal_daily_bounds.py::test_p3_06_midnight_retry_keeps_run_identity_and_charges_actual_admission_day` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_daily_bounds.py::test_p3_06_lower_daily_ceiling_applies_now_and_frozen_run_ceiling_cannot_grow` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_run_claim_snapshot.py::test_la_logical_date_retry_preserves_scope_and_stops_after_three_attempts` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
Remaining gap: New narrow old-date HTTP test awaits root-owned PostgreSQL execution and independent review.

Proposed exact new node ID: `tests/integration/test_personal_phase3_closeout.py::test_http_old_caller_dates_cannot_backdate_run_or_reopen_same_day_admission`.

## P3-07 — PASS

AIoff/exhausted/missingmodel/onefeedfails;raw/priorSaved/Briefstayreadable;nopaidprobe.

Worker-level scenarios explicitly assertzero physicalcalls/ledgerrows fordisabled/config/budget paths; actualbrowser separatelyprovesstatecauses andreadability. Onefeedfailure is actualofflineworker proof; completefeed/providerfailures are actualcoordinator→Chromeproof.

Executed exact node IDs:

- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[raw-False-disabled_by_profile]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[assisted-False-ai_disabled]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[assisted-True-configuration_missing]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_budget_block_retains_readable_transferable_articles_without_repeated_admission` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_reading_bounds.py::test_required_provider_failure_stays_failed_with_retained_raw_content` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[False-True-True-1-ai_disabled]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-False-True-1-paid_runtime_disabled]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-True-False-1-configuration_missing]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-True-True-0-allowance_reached]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_worker_falls_back_to_readable_raw_without_dispatch[True-True-True-0.0000001-allowance_reached]` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_item5_composed.py::test_missing_route_api_run_keeps_raw_reader_available_without_paid_dispatch` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.
- `tests/integration/test_personal_worker.py::test_worker_returns_partial_state_for_partial_offline_capture` — PASS in [item-5 JUnit](../phase-3-item-5-20261003/full-integration.xml); assertion detail and current SHA-256 in JSON.

Native browser proof: [allowance-reached-visible](../phase-3-item-6-20261003/browser/allowance-reached-visible.dom.txt), [missing-configuration-visible](../phase-3-item-6-20261003/browser/missing-configuration-visible.dom.txt), [ai-disabled-explicit](../phase-3-item-6-20261003/browser/ai-disabled-explicit.dom.txt), [spending-nonzero-gate-off](../phase-3-item-6-20261003/browser/spending-nonzero-gate-off.dom.txt), [capture-failure-corrected](../phase-3-item-6-20261003/browser/capture-failure-corrected.dom.txt), [provider-failure-corrected](../phase-3-item-6-20261003/browser/provider-failure-corrected.dom.txt), [saved-after-correction](../phase-3-item-6-20261003/browser/saved-after-correction.dom.txt), [brief-after-correction](../phase-3-item-6-20261003/browser/brief-after-correction.dom.txt), [brief-ai-blocked](../phase-3-item-6-20261003/browser/brief-ai-blocked.dom.txt).

Browser fixtures and monetaryobligations are labeledsynthetic;no liveprovider behavior or personalusefulnessjudgment is claimed. CurrentUIfailurefix has meaningful2-failurebaseline then50affectedfrontendpasses/independentreview/nativecorrectedcaptures; earlierloading/pre-fixfailurecaptures do not prove the final wording.

Remaining gap: none within this local/synthetic acceptance row.

## Execution and historical boundary

No new database/test/browser/services execution occurred in this mapping task. The root owns fresh disposable PostgreSQL execution of the new narrow closeout test file. No application correction is proposed absent a reproduced failing implementation.

Item-6 initial/service/API snapshots establish expected fixture records; they are not native browser observations. Native DOM captures establish rendered states; images remain scoped to their native call and may reflect only that call's final screenshot moment. Historical September 20 re-enable/zero-money observations remain separately qualified. Existing real Phase 2 feed/model/download proof and historical/global files were left untouched.

No totals are added across overlapping gates. Item 5 provides individual current test outcomes, not an independent rerun by this mapper. Item 6 provides the current corrected failure-state browser proof and independent scoped review.
