# P3-08–P3-14 acceptance map

Recorded 2026-10-04T02:59:02.490232+00:00 UTC; user date October 3, 2026 (America/Los_Angeles). **All seven rows are covered by current-source software/synthetic evidence, including the matched-fixture P3-12 triad.** This map does not infer live/recurring paid use or itself promote whole-project/global phase completion.

The basis is each exact executed case and its assertions paired with the matching implementation. No tests, application changes, provider/feed calls, service starts, activation, or resource mutation were performed by this mapping task. The parent coordinated the new tests, narrow correction and execution. The machine-readable companion is [acceptance-map-08-14.json](acceptance-map-08-14.json).

## Current source and execution identity

The authoritative current personal gate executed at `2026-10-03T19:52:49.341622-07:00` against branch `codex/stage10-hardening`, HEAD `7c7fcebbeba9c7b9671d6eb38631c2625cc8b454`. [JUnit](personal-final.xml), [log](personal-final.log), [result](personal-final-result.json) and [exact command](personal-final-command.json) retain 100 passed, zero skips/errors/failures, unchanged protected/database inventories and no source drift during execution. This map selects 31 distinct passed nodes below. The 100-case total is context; it is not the acceptance basis.

Fresh read-only comparison against [current tested source manifest](personal-final-source-after.json) finds all **593/593** regular-source hashes matching, including all **28** relevant implementation, migration and test files in the JSON. The new closeout test and corrected coordinator have exact current hashes:

- [services/personal/coordinator.py](../../../services/personal/coordinator.py): `5478e2949c09f1ceea7d2236fb4a7f9cdf7d89d35271dd51b581919c68254c16`.
- [tests/integration/test_personal_phase3_closeout.py](../../../tests/integration/test_personal_phase3_closeout.py): `0c683bb17fc6d7ac8369ee4b658432e0ea1c7d01c0a7ac09c81bf5694e89c649`.

The earlier item 5 complete integration gate remains historical combined proof: 238 passed at `2026-10-03T16:33:46.755713-07:00`. Of its 592 regular sources, 588 still match; the three item 6 frontend differences and the new coordinator flush correction are retained explicitly in JSON `source_history`. The current personal gate re-executes every mapped prior personal case plus the new closeout cases. Earlier 238, 100, six composed proofs, worker/spending selections and independent reruns overlap; they are never summed. Historical scoped README statements about later pending items remain dated boundaries.

Native browser captures remain the item 6 frontend epoch, before the one-line backend failure-stage correction. The current backend/API cases were re-executed here; no fresh native browser rendering after that correction is claimed. Frontend code hashes remain the item 6 corrected hashes.

## Retained C3 failure and correction

[Original eight-case JUnit](final-targeted.xml) and [failed result](final-targeted-result.json) remain **six passed/two failed** at `2026-10-03T19:50:21.354715-07:00`. The default test incorrectly expected a successful `report` stage, whose durable result is actually in `run.result`, `Report` and snapshot. The required-failure case exposed a real problem: dirty workflow stage JSON was discarded by the ownership refresh in `finish_run`. The [single-flush correction](coordinator-failure-stage-fix.diff) persists it first. The failed assertion was retained; the correct default oracle and all three matched outcomes then passed on current source.

The shared fixture SHA-256 `f1fddce95550b567f275aa2986bd3a06440fe015089b82093db8a2edd3a78f07` identifies identical authored RSS fields, not generated database UUID equality. Each separate disposable DB captures/admit-charges and freezes its own typed scope.

<a id="p3-08"></a>

## P3-08 — covered synthetic

Remaining $0.03; two requests requiring $0.02 each: one reserve/dispatch only, across API/worker and different run dates.

**One workspace lock serializes simultaneous reservations across two logical dates.**

- Two ThreadPoolExecutor workers synchronize at a barrier; different PersonalRun.local_date values share one workspace.
- Exactly [allowance_reached, dispatched]; exactly one PersonalPaidRequest with reserved_usd=0.02.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_spending.py::test_competing_run_dates_share_one_month_reservation_allowance` — [assertions](../../../tests/integration/test_personal_spending.py#L129); 1.725s.

Implementation: [services/personal/spending.py:185](../../../services/personal/spending.py#L185), [services/personal/spending.py:389](../../../services/personal/spending.py#L389), [services/personal/spending.py:425](../../../services/personal/spending.py#L425), [services/personal/spending.py:477](../../../services/personal/spending.py#L477).

**API-created competing run reduces the registered ordinary worker’s shared credit before generation dispatch.**

- API start returns 202 and enqueues a run on the following date; its generation request reserves/dispatches 0.02 and remains uncertain.
- Registered worker sends embedding only, publishes no report, reports allowance_reached; finalized=0.00024, unresolved=0.02, remaining=0.00976.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_paid_worker.py::test_api_queued_other_run_obligation_competes_with_ordinary_worker` — [assertions](../../../tests/integration/test_personal_paid_worker.py#L271); 1.731s.

Implementation: [apps/api/personal.py:715](../../../apps/api/personal.py#L715), [workers/personal_tasks.py:849](../../../workers/personal_tasks.py#L849), [services/personal/paid_runtime.py:148](../../../services/personal/paid_runtime.py#L148).

Qualification: This is a composition test with a preexisting API-created obligation, not a second simultaneous API/worker barrier. The separate real concurrent ledger test supplies the race proof.

**Actual API enqueuer and registered task enter the production durable dispatch boundary.**

- Real TestClient POST, production enqueuer, registered Celery task; broker delivery and provider transport are replaced only.
- Independent DB observation at each physical send verifies committed dispatching, exact workspace/run/attempt/profile/token, frozen route and conservative serialized payload bounds.
- Five distinct physical requests (one embedding and four generation); exact final synthetic charge 0.00744, reserved=unresolved=0.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_item5_composed.py::test_api_enqueuer_registered_worker_preserves_frozen_identity_payload_and_exact_cost` — [assertions](../../../tests/integration/test_personal_item5_composed.py#L87); 1.827s.

Implementation: [apps/api/personal.py:387](../../../apps/api/personal.py#L387), [workers/personal_tasks.py:975](../../../workers/personal_tasks.py#L975), [services/personal/paid_runtime.py:178](../../../services/personal/paid_runtime.py#L178).

Scope limits: Production serializers/adapters/coordinator/ledger execute with synthetic MockTransport; no external provider, broker service or purchased work is proved.

<a id="p3-09"></a>

## P3-09 — covered synthetic

Crash/timeout after paid dispatch; retry/restart retain first unresolved obligation, reserve additional credit, and reconcile known cost once without converting unknown cost to zero.

**A committed dispatch survives a missing completion write and ledger reconstruction.**

- Rebuilt SpendingLedger sees the original dispatched 0.02 obligation and blocks another 0.02 reservation under 0.03 credit.
- Two identical trustworthy receipts reconcile only once: finalized=0.002; new retry UUID has reserved=0.02; remaining=0.008; rewriting usage raises.
- Business transaction rollback leaves independently committed paid request dispatching and rolls back business coverage.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_spending.py::test_crash_reconstruction_retains_cost_retry_requires_new_credit_and_receipt_is_idempotent` — [assertions](../../../tests/integration/test_personal_spending.py#L151); 1.621s.
- `tests/integration/test_personal_spending.py::test_independent_commit_survives_business_rollback_without_row_lock_deadlock` — [assertions](../../../tests/integration/test_personal_spending.py#L239); 1.432s.

Implementation: [services/personal/spending.py:425](../../../services/personal/spending.py#L425), [services/personal/spending.py:477](../../../services/personal/spending.py#L477), [services/personal/spending.py:533](../../../services/personal/spending.py#L533).

**Every physical provider retry has a distinct durable request; timeout uncertainty has no automatic expiry credit.**

- Scripted embedding 500 then 200 yields two physical sends and two distinct request identities, one uncertain and one reconciled.
- Scripted HTTP ReadTimeout leaves actual_usd null and positive unresolved reservation; moving the clock to January 2027 does not release the original logical run’s credit.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_spending.py::test_provider_embedding_retry_has_separate_durable_reservations` — [assertions](../../../tests/integration/test_personal_spending.py#L396); 1.676s.
- `tests/integration/test_personal_spending.py::test_scripted_http_timeout_retains_each_physical_request_and_no_expiry_credit` — [assertions](../../../tests/integration/test_personal_spending.py#L448); 1.472s.

Implementation: [services/personal/paid_runtime.py:148](../../../services/personal/paid_runtime.py#L148), [services/personal/spending.py:514](../../../services/personal/spending.py#L514), [services/personal/spending.py:523](../../../services/personal/spending.py#L523), [services/personal/spending.py:344](../../../services/personal/spending.py#L344).

**Ordinary registered worker retry preserves frozen snapshot and route while old unknown requests remain charged conservatively.**

- Generation timeout leaves run failed/partially_failed, report failed/unpublished and uncertain rows matching physical sends.
- After active interest/price settings change, explicit retry performs four generation calls without recapture, keeps snapshot ID/hash/profile/enrichment IDs and old route, creates attempt-2 reconciled rows, and retains every attempt-1 uncertain reservation unchanged.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_paid_worker.py::test_ordinary_failed_report_retry_reuses_snapshot_and_retains_uncertainty` — [assertions](../../../tests/integration/test_personal_paid_worker.py#L316); 2.089s.

Implementation: [workers/personal_tasks.py:849](../../../workers/personal_tasks.py#L849), [services/personal/runs.py:189](../../../services/personal/runs.py#L189), [services/personal/briefs.py:617](../../../services/personal/briefs.py#L617).

Scope limits: Crash is simulated at the durable handoff and a fresh ledger object reconstructs state; no operating-system crash or external provider receipt recovery is claimed. Older-period unresolved obligations remain disclosed; current monthly charges and the logical run ceiling follow the contract’s distinct accounting rules.

<a id="p3-10"></a>

## P3-10 — covered synthetic

Reservation before UTC month boundary, dispatch next month, completion later: dispatch-month charge, correct local reset and no credit gap.

**Dispatch moves an undispatched reservation atomically to the dispatch month; late receipt stays there.**

- Reserve 2026-09-30 23:59:59 UTC, dispatch 2026-10-01 00:00:01 UTC, reconcile 2026-11-01 UTC.
- Stored accounting_month=2026-10-01, dispatch timestamp month=10, reconciled timestamp month=11; October finalized=0.002.
- Los Angeles next_reset_at is exactly 2026-10-31T17:00:00-07:00.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_spending.py::test_month_handoff_moves_reservation_and_late_receipt_stays_in_dispatch_month` — [assertions](../../../tests/integration/test_personal_spending.py#L182); 1.527s.

Implementation: [services/personal/spending.py:201](../../../services/personal/spending.py#L201), [services/personal/spending.py:477](../../../services/personal/spending.py#L477), [services/personal/spending.py:636](../../../services/personal/spending.py#L636).

**The next bucket is revalidated before send; only confirmed pre-dispatch cancellation releases credit.**

- A competing 0.02 October dispatch blocks the old September 0.02 reservation when it attempts to dispatch against 0.03 October credit.
- Old confirmed pre-send reservation becomes cancelled; cancellation of the already-dispatching competing request raises and leaves it dispatching.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_spending.py::test_month_handoff_revalidates_next_bucket_and_confirmed_cancel_only_before_send` — [assertions](../../../tests/integration/test_personal_spending.py#L203); 1.56s.

Implementation: [services/personal/spending.py:389](../../../services/personal/spending.py#L389), [services/personal/spending.py:477](../../../services/personal/spending.py#L477), [services/personal/spending.py:514](../../../services/personal/spending.py#L514).

**Current summary exposes current-period values separately from old unresolved obligations and frozen route identities.**

- API metadata keeps charged/reserved frozen routes when today’s profile changes or is disabled; older unresolved routes remain represented while irrelevant old finalized/cancelled/non-billable routes are excluded.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_spending_metadata.py::test_spending_get_distinguishes_frozen_routes_from_disabled_current_configuration` — [assertions](../../../tests/integration/test_personal_spending_metadata.py#L41); 1.449s.

Implementation: [services/personal/spending.py:599](../../../services/personal/spending.py#L599), [services/personal/spending.py:636](../../../services/personal/spending.py#L636).

Related retained local synthetic browser/API proof: [personal-project-conversion/evidence/phase-3-item-6-20261003/fixture-spending.json](../phase-3-item-6-20261003/fixture-spending.json), [personal-project-conversion/evidence/phase-3-item-6-20261003/api-snapshot-spending-rev10.json](../phase-3-item-6-20261003/api-snapshot-spending-rev10.json), [personal-project-conversion/evidence/phase-3-item-6-20261003/browser/spending-nonzero-gate-off.dom.txt](../phase-3-item-6-20261003/browser/spending-nonzero-gate-off.dom.txt), [personal-project-conversion/evidence/phase-3-item-6-20261003/browser/settings-pre-run-reload.dom.txt](../phase-3-item-6-20261003/browser/settings-pre-run-reload.dom.txt).

Item 6 synthetic browser shows October UTC accounting, Los Angeles October 31 5 PM PDT reset, finalized 0.20, current reserved 0.15, all-period unresolved 0.30 and remaining 0.65 of 1.00. These fields deliberately separate current-period credit from all-period unresolved disclosure. A pre-run saved New York setting showed October 31 8 PM EDT.

Scope limits: Synthetic clocks/receipts and local synthetic browser state prove accounting/display; no live billed request crossed a month.

<a id="p3-11"></a>

## P3-11 — covered synthetic

Populated upgrade preserves prior usage, saves, reports and ungrouped article identities; known spend imported once, unknown disclosed before paid enablement; no unrestricted catch-up.

**Actual 0020-to-current-head upgrade preserves populated identities and retained raw content.**

- Seeds source, article, event, saved watch entry, published report, workspace/profile/run and prior usage at 0020, then upgrades to current head.
- Same article/title/summary/unknown publication time, published report/title, watch entry label/event, capture identity and original admission time/owner remain.
- Migrated run is succeeded/frozen with original admitted article ID and zero enrichment IDs; retained raw capture is disabled_by_profile.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_bounds_upgrade.py::test_populated_upgrade_preserves_sources_raw_saved_reports_and_imports_known_unknown_usage_once` — [assertions](../../../tests/integration/test_personal_bounds_upgrade.py#L61); 2.587s.

Implementation: [db/migrations/versions/0021_personal_daily_bounds.py](../../../db/migrations/versions/0021_personal_daily_bounds.py), [db/migrations/versions/0022_personal_spending.py](../../../db/migrations/versions/0022_personal_spending.py).

**Known historical cost imports once; missing/timeout cost remains unknown and blocks paid enablement.**

- Known cost=0.005, unknown=None, timeout with zero recorded cost/tokens remains None; repeated import leaves exactly three legacy rows and zero paid requests.
- Summary finalized=0.005, unreconciled_legacy_count=2; guard code legacy_usage_unreconciled before paid work.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_bounds_upgrade.py::test_populated_upgrade_preserves_sources_raw_saved_reports_and_imports_known_unknown_usage_once` — [assertions](../../../tests/integration/test_personal_bounds_upgrade.py#L61); 2.587s.
- `tests/integration/test_personal_spending.py::test_legacy_costs_import_once_and_unknown_blocks_paid_enablement` — [assertions](../../../tests/integration/test_personal_spending.py#L316); 1.376s.
- `tests/integration/test_personal_spending.py::test_read_summary_discloses_post_upgrade_legacy_without_mutation_or_guarded_double_count` — [assertions](../../../tests/integration/test_personal_spending.py#L362); 1.36s.

Implementation: [services/personal/spending.py:214](../../../services/personal/spending.py#L214), [services/personal/spending.py:247](../../../services/personal/spending.py#L247), [services/personal/spending.py:308](../../../services/personal/spending.py#L308).

**Repeated spending GET discloses the migration result without changing retained data or starting catch-up.**

- Two actual API reads return HTTP 200 and identical expected summary; original legacy provider/model retained and unknown role/version/price fields null.
- SQL write observer records no writes; full value snapshots of fifteen retained tables are unchanged; PersonalPaidRequest is empty before/after.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_bounds_upgrade.py::test_populated_upgrade_preserves_sources_raw_saved_reports_and_imports_known_unknown_usage_once` — [assertions](../../../tests/integration/test_personal_bounds_upgrade.py#L61); 2.587s.

Implementation: [apps/api/personal_reading.py:74](../../../apps/api/personal_reading.py#L74), [services/personal/spending.py:636](../../../services/personal/spending.py#L636), [services/personal/coordinator.py:522](../../../services/personal/coordinator.py#L522).

Qualification: No worker/service starts in the migration test. The test proves migration and GET do not launch processing; later ordinary runs must explicitly freeze transfer scope.

Scope limits: The upgrade dataset is synthetic and actual disposable PostgreSQL; no upgrade of the shared development/personal-use database occurred.

<a id="p3-12"></a>

## P3-12 — covered synthetic

Process the same frozen fixture through default, raw and required-stage failure: explicit disabled stages, honest failed/partial outcome, default brief only from persisted eligible snapshot.

**The exact same authored fixture is processed through default, raw and required-stage failure on current source.**

- Every mode reconstructs the same two authored RSS records and asserts the fixed canonical SHA256 f1fddce95550b567f275aa2986bd3a06440fe015089b82093db8a2edd3a78f07; generated DB identities differ.
- Actual v2 coordinator captures/admit-charges exactly two, retains authored title/summary/URL/publication-or-unknown, persists frozen original profile and both admitted IDs. Each mode durably marks entity_linking/event_embeddings/historical_analogies/forecasting disabled_by_profile.
- Raw: succeeds with zero enrichment IDs, no embedding/report calls, no snapshot/report, all five optional pipeline stages disabled_by_profile, two readable raw API records and zero briefs.
- Required embedding failure occurs after the frozen two-ID scope and two pinned article revisions: workflow failure stage persists, run failed with personal_workflow_failed, both captures provider_failed, no snapshot/report, two raw API records with error and zero briefs.
- Default: succeeds and publishes using original frozen agency interest/profile/route despite an active-profile edit after freeze; snapshot source candidates and citable claims contain only the eligible agency article, excluding the non-interest league item. Report/run result/snapshot IDs and snapshot canonical input hash agree.
- After current Article title/summary/URL mutate, actual report-detail and claim-evidence HTTP response bytes remain identical to their pre-mutation reads; frozen source title/URL/citations stay agency-authored. All three modes have zero PersonalPaidRequest rows.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_phase3_closeout.py::test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes[default]` — [assertions](../../../tests/integration/test_personal_phase3_closeout.py#L319); 1.871s.
- `tests/integration/test_personal_phase3_closeout.py::test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes[raw]` — [assertions](../../../tests/integration/test_personal_phase3_closeout.py#L319); 1.521s.
- `tests/integration/test_personal_phase3_closeout.py::test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes[stage_failure]` — [assertions](../../../tests/integration/test_personal_phase3_closeout.py#L319); 1.598s.

Implementation: [services/personal/coordinator.py:522](../../../services/personal/coordinator.py#L522), [services/personal/coordinator.py:646](../../../services/personal/coordinator.py#L646), [services/personal/coordinator.py:746](../../../services/personal/coordinator.py#L746), [services/personal/snapshots.py:344](../../../services/personal/snapshots.py#L344), [services/personal/briefs.py:282](../../../services/personal/briefs.py#L282), [services/personal/briefs.py:617](../../../services/personal/briefs.py#L617), [tests/integration/test_personal_phase3_closeout.py:256](../../../tests/integration/test_personal_phase3_closeout.py#L256).

Qualification: The three modes share authored retained inputs and a fixed source-field hash, rather than generated UUID equality. Each mode freezes its own typed DB scope before optional work. Providers are deterministic offline adapters; no billable transport, live feed or provider request occurs.

**Default coordinator can capture/group/prepare claims and publish; raw path records disabled stages.**

- Default publishes from initially empty article/event/claim/evidence/report tables, freezes identical admitted/enrichment ID, persists grouping, claims, evidence and exact report text.
- Raw coordinator records embedding disabled_by_profile; raw registered worker succeeds with raw capture/admission, unpublished result and zero LLMRun rows.
- Current source explicitly marks entity_linking, event_embeddings, historical_analogies and forecasting disabled_by_profile in every coordinator run.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_coordinator.py::test_actual_coordinator_captures_groups_claims_and_publishes_from_empty_tables` — [assertions](../../../tests/integration/test_personal_coordinator.py#L98); 1.516s.
- `tests/integration/test_personal_coordinator.py::test_pending_capture_is_bounded_and_round_robin_admission_creates_only_selected_articles` — [assertions](../../../tests/integration/test_personal_coordinator.py#L241); 1.539s.
- `tests/integration/test_personal_worker.py::test_raw_worker_needs_no_model_route_credentials_or_fixture` — [assertions](../../../tests/integration/test_personal_worker.py#L399); 1.611s.

Implementation: [services/personal/coordinator.py:687](../../../services/personal/coordinator.py#L687), [services/personal/coordinator.py:746](../../../services/personal/coordinator.py#L746).

**Enabled provider/grounding failure stays failed; partial capture stays partial and raw remains readable.**

- Broken required embedding provider raises PersonalCoordinatorError; run is failed; two raw API records remain provider_failed with nonempty error.
- Failed required regeneration publishes no report for both provider and budget causes; partial capture remains partial through explicit retry.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_reading_bounds.py::test_required_provider_failure_stays_failed_with_retained_raw_content` — [assertions](../../../tests/integration/test_personal_reading_bounds.py#L209); 1.441s.
- `tests/integration/test_personal_brief_generation.py::test_failed_personal_grounding_regeneration_cannot_publish[provider]` — [assertions](../../../tests/integration/test_personal_brief_generation.py#L566); 1.928s.
- `tests/integration/test_personal_brief_generation.py::test_failed_personal_grounding_regeneration_cannot_publish[budget]` — [assertions](../../../tests/integration/test_personal_brief_generation.py#L566); 3.001s.
- `tests/integration/test_personal_brief_generation.py::test_partial_capture_remains_partial_across_explicit_retry` — [assertions](../../../tests/integration/test_personal_brief_generation.py#L611); 1.555s.

Implementation: [services/personal/coordinator.py:646](../../../services/personal/coordinator.py#L646), [services/personal/briefs.py:786](../../../services/personal/briefs.py#L786), [services/personal/reader.py:34](../../../services/personal/reader.py#L34).

**Published default report references the persisted snapshot and source revisions; frozen-source corruption is rejected.**

- Report link holds the exact persisted snapshot ID; expected selected event and exact source-grounded report sections are retained.
- Claim preparation persists supported/abstained identities once; modified pinned observation text and invalid exact claim span each reject snapshot freeze.
- Ordinary retry reuses snapshot ID/hash and scope without recapture despite changed settings.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_brief_generation.py::test_fresh_captured_source_is_composed_grounded_and_published` — [assertions](../../../tests/integration/test_personal_brief_generation.py#L275); 2.845s.
- `tests/integration/test_personal_run_claim_snapshot.py::test_fresh_articles_produce_exact_claim_evidence_paths_and_snapshot_rejects_bad_span` — [assertions](../../../tests/integration/test_personal_run_claim_snapshot.py#L319); 1.565s.
- `tests/integration/test_personal_paid_worker.py::test_ordinary_failed_report_retry_reuses_snapshot_and_retains_uncertainty` — [assertions](../../../tests/integration/test_personal_paid_worker.py#L316); 2.089s.
- `tests/integration/test_personal_item5_composed.py::test_api_enqueuer_registered_worker_preserves_frozen_identity_payload_and_exact_cost` — [assertions](../../../tests/integration/test_personal_item5_composed.py#L87); 1.827s.

Implementation: [services/personal/snapshots.py:344](../../../services/personal/snapshots.py#L344), [services/personal/briefs.py:282](../../../services/personal/briefs.py#L282), [services/personal/briefs.py:617](../../../services/personal/briefs.py#L617).

**Resolved C3:** The literal matched-input default/raw/required-failure triad now passed. The prior missing coverage and initial failures remain in JSON and the original gate artifacts.

Scope limits: The matched triad uses deterministic offline adapters and separate real disposable PostgreSQL databases. It proves the specified source/profile/outcome boundaries, not live provider operation. The initial two-failure gate is retained and superseded by the corrected current-source gate, not erased.

<a id="p3-13"></a>

## P3-13 — covered synthetic

Default 100 old transferable raw plus 100 fresh: 100 fresh admissions, old 100 enrichment first, new readable capacity deferral, no old readmission; raw admits and selects zero.

**All three default ceilings are 100 and old enrichment does not consume new admission capacity.**

- Desk.configure uses daily/run/enrichment=100, matching PersonalSettingsUpdate defaults. First raw run admits 100 fresh-at-that-time records and freezes zero enrichment IDs.
- Next assisted run admits 100 additional fresh records, selects exactly sorted original 100 IDs, with no overlap with the fresh set.
- All fresh rows are deferred_enrichment_capacity; exact 100 explicit transfers and total 200 Article records.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_daily_bounds.py::test_p3_13_old_raw_scope_does_not_reduce_new_admission_and_capacity_deferral_transfers` — [assertions](../../../tests/integration/test_personal_daily_bounds.py#L525); 2.658s.

Implementation: [services/personal/settings.py:80](../../../services/personal/settings.py#L80), [services/personal/coordinator.py:522](../../../services/personal/coordinator.py#L522).

**Old article admission identity/time stays fixed; capacity-deferred new raw can transfer later without another admission.**

- Old admitted_at remains START and admitted_run_id remains original raw run; only processing_run_id changes.
- Third run admits zero, selects the previous fresh 100, records 100 transfers reason deferred_enrichment_capacity, and still has 200 articles.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_daily_bounds.py::test_p3_13_old_raw_scope_does_not_reduce_new_admission_and_capacity_deferral_transfers` — [assertions](../../../tests/integration/test_personal_daily_bounds.py#L525); 2.658s.

Implementation: [services/personal/coordinator.py:522](../../../services/personal/coordinator.py#L522), [services/personal/snapshots.py:72](../../../services/personal/snapshots.py#L72).

**Capacity-deferred raw records remain available to the common reader, and disabled-source content stays retained without selection.**

- Disabled-source raw Article exists, keeps its original processing owner and disabled_by_profile; only enabled source transfers.
- Actual raw API returns retained admitted content with unknown dates, pagination and reason; source projects stored enrichment_state unchanged for ungrouped captures.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_daily_bounds.py::test_p3_13_existing_raw_from_disabled_feed_stays_readable_and_outside_new_scope` — [assertions](../../../tests/integration/test_personal_daily_bounds.py#L620); 1.72s.
- `tests/integration/test_personal_reading_bounds.py::test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog[raw-False-disabled_by_profile]` — [assertions](../../../tests/integration/test_personal_reading_bounds.py#L101); 1.708s.

Implementation: [services/personal/reader.py:34](../../../services/personal/reader.py#L34), [services/personal/reader.py:78](../../../services/personal/reader.py#L78).

Qualification: The 100-capacity case proves durable Article/capture retention and deferred state directly. Readability uses the same tested raw API and state projection; no separate 100-record browser assertion is claimed.

Related retained local synthetic browser/API proof: [personal-project-conversion/evidence/phase-3-item-6-20261003/browser-raw-pagination-comparison.json](../phase-3-item-6-20261003/browser-raw-pagination-comparison.json), [personal-project-conversion/evidence/phase-3-item-6-20261003/browser/raw-page-1.dom.txt](../phase-3-item-6-20261003/browser/raw-page-1.dom.txt), [personal-project-conversion/evidence/phase-3-item-6-20261003/browser/raw-page-2.dom.txt](../phase-3-item-6-20261003/browser/raw-page-2.dom.txt), [personal-project-conversion/evidence/phase-3-item-6-20261003/browser/raw-page-3.dom.txt](../phase-3-item-6-20261003/browser/raw-page-3.dom.txt).

Scope limits: Transfer/count test executes capture/admission/scope primitives on real PostgreSQL, with synthetic feeds; it does not perform paid enrichment of those 100 records.

<a id="p3-14"></a>

## P3-14 — covered synthetic

Retryable failed or exhausted hard-failure raw scope cannot be silently taken/retried by a later run; raw/error remain visible.

**Failed ownership remains fenced both before and after exhausting explicit attempts.**

- After required failure, next run selects zero of the old scope. Two explicit retries keep the exact original IDs; fourth retry raises exhausted.
- Later run selects only its own fresh article; zero PersonalEnrichmentTransfer rows, old processing owner unchanged, state provider_failed, original run attempt=3, old Article still exists.
- Explicit scope collision rejects active/retryable and exhausted failed ownership rather than silently reassigning it.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_daily_bounds.py::test_p3_14_failed_retry_scope_and_exhausted_failure_are_never_implicitly_transferred` — [assertions](../../../tests/integration/test_personal_daily_bounds.py#L577); 1.709s.
- `tests/integration/test_personal_run_claim_snapshot.py::test_article_scope_cannot_be_owned_by_two_active_or_retryable_runs` — [assertions](../../../tests/integration/test_personal_run_claim_snapshot.py#L260); 1.548s.

Implementation: [services/personal/coordinator.py:522](../../../services/personal/coordinator.py#L522), [services/personal/runs.py:189](../../../services/personal/runs.py#L189), [services/personal/snapshots.py:72](../../../services/personal/snapshots.py#L72).

**Failure does not delete raw content or suppress its reason.**

- Actual raw GET returns both retained failed records with provider_failed and nonempty error; run failed; disabled optional entity_linking retains disabled_by_profile.
- Current common reader derives error from failed/partially_failed processing run independently of retry availability, while retaining title/snippet/URL and original admission identity.

Executed exact nodes (all **passed** in current personal-final JUnit):

- `tests/integration/test_personal_reading_bounds.py::test_required_provider_failure_stays_failed_with_retained_raw_content` — [assertions](../../../tests/integration/test_personal_reading_bounds.py#L209); 1.441s.

Implementation: [services/personal/coordinator.py:646](../../../services/personal/coordinator.py#L646), [services/personal/reader.py:34](../../../services/personal/reader.py#L34), [services/personal/runs.py:342](../../../services/personal/runs.py#L342).

Qualification: The exact exhausted case proves persisted state/owner/content; failure visibility is composed with the same reader and an executed failed raw GET, not a separately executed exhausted-run browser scenario.

Related retained local synthetic browser/API proof: [personal-project-conversion/evidence/phase-3-item-6-20261003/fixture-provider-failure.json](../phase-3-item-6-20261003/fixture-provider-failure.json), [personal-project-conversion/evidence/phase-3-item-6-20261003/browser/provider-failure-corrected.dom.txt](../phase-3-item-6-20261003/browser/provider-failure-corrected.dom.txt).

Scope limits: Explicit retries and failures are deterministic synthetic cases. No external failure, spontaneous background retry, or paid operation is claimed.

## Cleanup and preservation qualifications

These qualifications were verified from retained raw results and separate adjudications. They do not convert failed broad checks into green checks. The parent’s [closeout before inventory](containers-before.json) independently retains the shared/missing-manifest/unrelated exact IDs and omits all five completed owned PG/Redis IDs listed in JSON. No resource command ran in this mapping task.

- Item 5: [owned cleanup complete](../phase-3-item-5-20261003/resource-cleanup-adjudication.json); [original broad comparison remains failed](../phase-3-item-5-20261003/resource-cleanup.json). All 19 preexisting identities/images/labels/mounts were preserved, while 17 complete records stayed unchanged. Two unrelated infrastructure-monitor containers stopped before item 5 stop/removal; the event initiator is unavailable. No unrelated restart/restoration/cleanup was performed.

- Historical item 6: [owned cleanup and exact absence proof](../phase-3-item-6-20261003/resources/historical-cleanup-adjudication.json) completes archived exact-OID DBs, dedicated PG/anonymous volume and separately manifested frontend. The [original failed positional mount comparison](../phase-3-item-6-20261003/resources/historical-cleanup-result.json) remains retained: identical immutable mount objects appeared in reversed order. Separate exact-object comparison preserves all 22 other container identities and states.

- Fresh item 6: [cleanup result](../phase-3-item-6-20261003/new-harness-resources/result.json) completes owned API/worker/frontend, PG/Redis and exact anonymous volumes after zero-reference proof; 20 other records semantically match. The valid stop helper verifies the private DB marker and drops the owned DB before server removal. No independent live post-DROP OID query after removal is claimed. The secret-safe synthetic archive and redacted logs remain; private state/manifest/log directory remains for recovery.

- Preserve the old browser stack whose original private `/tmp` manifest/token is missing, shared development PostgreSQL and named volume, older Phase 2 and unrelated resources, including the stopped infrastructure-monitor containers. Missing ownership proof is not permission to infer ownership. Operational dotenv/preferences/paid gate and staged work were not activated or altered. Parent-owned closeout verification resources and their cleanup are outside this mapping task.

## Scope boundary

All P3-08–P3-14 software/synthetic acceptance claims now have current-source per-case evidence. No acceptance evidence gap remains within these seven rows. Live daily-use/provider behavior, real paid activation, recurring allowance and later phases remain separate authorization/evidence boundaries. Final independent review and root-owned resource disposition belong to the parent’s closeout record.
