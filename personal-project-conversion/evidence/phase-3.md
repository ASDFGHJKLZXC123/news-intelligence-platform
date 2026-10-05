# Phase 3 acceptance — Phase 4A regression refresh

Status: **Complete for local/synthetic engineering acceptance; refreshed October 4, 2026.** All fourteen original Phase 3 outcomes remain accepted under the new global ownership runtime. [Current 101-case refresh](phase-4a-20261004/phase3-accepted.xml) passed with no failures/errors/skips, source drift, protected-file change or database/role inventory change. [Phase 4A evidence](phase-4a.md) explains valid ownership fixture/expectation adaptations, retains the failed first refresh and maps the new runtime requirements. Ordinary paid activation is off; selected preferences and monthly allowances remain unapplied. Phase 4B/5 remain unstarted.

The October 3 authoritative record and its fourteen mapped criteria are retained below as dated history and [archived byte-identically](phase-4a-20261004/phase-3-accepted-before-phase4a.md). Its historical statement that Phase 4A had not started is superseded by the current Phase 4A record. Original criteria, costs, source identities, admission bounds and attempt ceilings are preserved.

<details>
<summary>October 3 accepted Phase 3 record, retained unchanged</summary>

# Phase 3 acceptance and verification

Status: **Complete — local/synthetic engineering acceptance, October 3, 2026 (America/Los_Angeles).** All fourteen requirements below have explicit current evidence and independent review. Phases 1–2 and CORE-01 remain complete. Ordinary paid runtime remains off; saved preferences are unapplied and no recurring spending allowance has been authorized. Phase 4A, Phase 4B and Phase 5 remain not started.

This is the authoritative current Phase 3 status. The original pending matrix and earlier scoped logs are retained as dated history below and in the byte-identical archive. They no longer describe current acceptance.

## Current acceptance

| Acceptance | Status | Actual result | Exact proof |
| --- | --- | --- | --- |
| P3-01 | PASS | 30 observed; exactly 20 admitted, 10 pending and 20 frozen enrichment IDs. Intake and completed enrichment remain distinct in the reader. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-01); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-02 | PASS | Canonical duplicates spend no extra charge; rollback spends none. Two simultaneous HTTP retries yield one new attempt/token/delivery and preserve frozen membership and charges. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-02); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-03 | PASS | A20/B2 with ceiling 4 admits A1, B1, A2, B2; fresh sessions retain the same IDs and ordinals. Older batches and known/unknown publication order are explicit. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-03); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-04 | PASS | Retained candidates survive remote rotation and source disable/re-enable, then admit with original source/content and unknown publication date preserved. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-04); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-05 | PASS | 2 MiB response, 501-entry and 2,000-pending boundaries retain accurate statuses/counts and no over-cap insertion or fabricated totals. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-05); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-06 | PASS | Midnight retry preserves logical run limits; new admissions charge their actual server-local day. Old caller dates cannot create a second same-day run or reopen intake. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-06); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-07 | PASS | Disabled, missing, exhausted and failed processing stay distinguishable; available raw articles, Saved and prior Brief remain readable, with zero disabled-path paid probes. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-01-07.md#p3-07); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-08 | PASS | Two concurrent $0.02 requests under $0.03 shared credit permit exactly one dispatch. API-created obligations and the registered worker use the same durable boundary. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-08); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-09 | PASS | Missing completion/timeout retains uncertain obligations; physical retries need distinct credit. Fresh ledger reconstruction and idempotent receipts preserve known and unknown costs. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-09); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-10 | PASS | Reservation handoff revalidates the UTC dispatch month; later completion stays charged there. Local reset and current/all-period disclosures match retained browser/accounting proof. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-10); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-11 | PASS | Populated 0020-to-0022 upgrade retains raw/save/report/run identities; known usage imports once, unknown usage blocks enablement and no unrestricted catch-up starts. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-11); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-12 | PASS | One authored RSS hash drives default/raw/required-failure modes. Default publishes only its persisted eligible snapshot; raw selects zero enrichment; required failure persists failed stage/error and readable content. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-12); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-13 | PASS | 100 old raw plus 100 fresh: 100 fresh admissions, 100 old enrichment IDs, fresh capacity-deferred raw records and no second old admission charge. Raw selects zero enrichment. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-13); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |
| P3-14 | PASS | Retryable/exhausted failed ownership cannot transfer silently; explicit retries preserve scope and the old raw content/failure remains readable. | [Mapped assertions](phase-3-closeout-20261003/acceptance-map-08-14.md#p3-14); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |

Acceptance totals: **14 PASS, 0 pending, 0 open recorded failures.** Original failures remain retained; the real failure-stage persistence defect is fixed and verified, and the publication oracle now checks the app's actual published report/snapshot rather than an invented status key.

## Verification and limits

The closeout refreshed **100 personal PostgreSQL integration cases: 95 existing and five new cases**, all passing with no skips/errors/failures. The five new cases close concurrent-retry, caller-old-date and matched-fixture default/raw/failure gaps. Independent execution reran those same five cases. Their original first gate (two failures/six passes), preceding five-case gate and final runs are retained separately. Repeated checks do not create extra distinct coverage.

The earlier item-5 **238 integration / 175 related unit** and item-6 **50 frontend** results are inspected retained evidence, not freshly rerun here. The one-line coordinator correction supersedes the earlier coordinator hash; every mapped personal integration case was freshly rerun against the corrected source. Item-6 frontend source and native Chrome proof still match their retained verification. Historical September results remain historical. [Commands, JUnit, source identities, native provenance, corrections and cleanup](phase-3-closeout-20261003/README.md).

The production HTTP handlers, registered task, coordinator, PostgreSQL invariants and guarded serializers/adapters execute with scripted broker delivery, feed/provider transport and clocks. Ledger/session reconstruction proves durable recovery; an actual broker/worker service restart, operating-system crash, real billed month transition and personal usefulness are not inferred. Native Chrome proof loaded existing CDN prerequisites; no completely isolated browser-network claim is made. This shared dirty checkout is not a clean tracked release.

[Independent review](phase-3-closeout-20261003/independent-review.md), [owned resource cleanup](phase-3-closeout-20261003/resource-cleanup.json) and [final preservation/status checks](phase-3-closeout-20261003/final-check.json) retain the disposition. Current cleanup removes only the fresh exact-owned tmpfs PostgreSQL container, restores its database/role inventory and preserves all twenty preexisting container records. Earlier broad comparison failures and their separate adjudications remain visible; the historical stack missing its original ownership manifest remains preserved.

## Status maintenance and next dependency

The phase plan, conversion index and root pointers show this phase-level status and link here. Any later behavior change must identify affected P3 criteria and update their evidence/status in the same change. Keep prior results dated; mark invalidated or missing proof pending with its exact gap. Suite totals alone cannot promote acceptance.

The next building-plan milestone is **Phase 4A**, replacing always-running Celery worker/scheduler transport with a supervised on-demand child while retaining Redis initially. Its concurrency, ownership, deadlines and recovery evidence belongs to Phase 4; Phase 4B and Phase 5 follow separately. No Phase 4/5 implementation or live activation occurred in this closeout.

## Historical status and scoped logs

The following original record is preserved unchanged. Its pending totals and assignment-specific open-work statements describe their recorded time and are superseded by the current acceptance above. [Byte-identical archive and provenance](phase-3-closeout-20261003/historical-record-provenance.json).

<details>
<summary>Original Phase 3 status and dated scoped records</summary>

# Phase 3 implementation and verification

Status: **implementation in progress**. Started September 20, 2026 in the shared checkout, preserving the uncommitted Phase 1/2 implementation and evidence. Phase 2 and CORE-01 remain complete. No ordinary runtime preferences or recurring spending allowance have been activated. No paid provider calls are authorized or needed for this work.

Implementation order: settings/readiness; retained collection and atomic admission; frozen enrichment and explicit transfer; durable paid-request reservations; raw reading and settings/backlog/spending UI; acceptance and independent review.

| Acceptance | Status | Evidence |
| --- | --- | --- |
| P3-01 bounded admission and retained pending | Pending | Implementation underway |
| P3-02 duplicate/concurrent/rollback invariants | Pending | Not yet run |
| P3-03 persistent round-robin order | Pending | Not yet run |
| P3-04 retained rotation and source toggles | Pending | Not yet run |
| P3-05 byte/entry/pending bounds | Pending | Not yet run |
| P3-06 local midnight and immutable run limits | Pending | Not yet run |
| P3-07 readable raw and distinct blocking causes | Pending | Not yet run |
| P3-08 shared concurrent paid reservations | Pending | Not yet run |
| P3-09 uncertain outcomes, retry and restart | Pending | Not yet run |
| P3-10 dispatch month and reset | Pending | Not yet run |
| P3-11 additive populated upgrade | Pending | Not yet run |
| P3-12 profiles, frozen inputs and failures | Pending | Not yet run |
| P3-13 old backlog separate from fresh intake | Pending | Not yet run |
| P3-14 failed-run retry ownership | Pending | Not yet run |

Acceptance totals: 0 passed, 14 pending, 0 recorded failures. This is a work log, not a completion claim. Historical Phase 2 test results were inspected and are not fresh Phase 3 coverage. Disposable verification resource identities, actual test logs, browser evidence, cleanup and independent review will be retained here as performed.


## October 3, 2026: scoped remaining-work items 2 and 3

**Item 2 (spending API metadata): complete.** The existing read-only spending response now separates current profile/route identifiers from retained accounting routes and their price revisions. Historical paid routes are never relabeled by current settings; legacy model/price facts that were not recorded remain null. Existing money, status, UTC-period and local-reset semantics are preserved. No secrets, receipt data or arbitrary stored extras are exposed. The additive response is documented in the Phase 3 API contract.

**Item 3 (daily-pipeline fixture migration head): complete.** The fixture resolves and verifies the actual single repository Alembic head instead of asserting stale `0020`, preserving its disposable isolation, cleanup, descriptive workflow and replay/idempotency checks. Fresh PostgreSQL execution reached `0022_personal_spending` and passed the previously blocked test.

Fresh scoped checks: **17 PostgreSQL integration tests passed**, **55 offline spending/settings unit tests passed**, whole-repository Ruff and Git whitespace checks passed. All final-run disposable databases were identified by unique name/OID, verified at the current head, and removed; existing resource inventories and unrelated checkout files were preserved. Independent scoped spending and fixture reviews found no actionable issues. [Changes, exact test results, resource identities, cleanup and preservation evidence](phase-3-items-2-3-20261003/README.md).

This closes only remaining-work checklist items 2 and 3; it does not close the global P3-01–P3-14 acceptance rows above. Items 1 and 4–7 remain outside this assignment. Ordinary live AI workers and recurring spending/preferences remain disabled/unactivated, no live feeds/providers were contacted, historical Stage 6 isolation changes were not attempted, and Phase 4/5 were not started. The ledger retention/downgrade guard remains intact. Historical full-suite results remain historical.

## October 3, 2026: scoped remaining-work items 1 and 4

**Item 1 (ordinary AI-worker integration): complete, synthetic verification passed; real activation remains off.** The ordinary registered v2 worker now composes the existing production coordinator with one durable spending ledger and the guarded embedding/generation/checking adapters. Exact frozen routes/prices/token bounds/deadlines, ownership and scopes, shared ceilings, physical retry accounting and retained uncertainty remain enforced. The separate server gate defaults off; configured routes/credentials alone cannot activate it. Readiness reports `paid_runtime_disabled`, raw collection remains available, and intentionally disabled raw articles transfer explicitly to a later assisted run. Independent review found that the new reason initially omitted transfer eligibility; a fresh failing test, narrow correction and independent rerun closed that finding.

**Item 4 (historical hotness migration-test isolation): complete.** The two named tests now own separate disposable databases at the real 0015/0016 boundary, with explicit subprocess test mode/target and identity checks. Their preservation/index/reversibility assertions and the other five Stage 6 tests remain meaningful. Production migrations and the paid-ledger retention/downgrade guard are unchanged. The September 20 rejection records remain historical evidence; the fresh human assignment authorized these two implementations.

Fresh scoped checks: **59 distinct PostgreSQL integration targets passed** (50 directly related worker/accounting/settings/coordinator/ownership/brief-API checks, 7 Stage 6 checks, 2 existing retention guards), **92 offline unit tests passed**, scoped Ruff/formatting and repository whitespace passed. Independent worker review additionally reran 76 unit checks and 3 identified disposable-database cases; migration isolation received separate independent review. All 97 databases from the assignment's failing baselines, final checks and reviews were dropped, the exact owned PostgreSQL container was removed, and all 17 preexisting containers remained unchanged. [Changes, expected/actual results, review, preservation and cleanup](phase-3-items-1-4-20261003/README.md).

This closes only checklist items 1 and 4. It supersedes their prior unavailable/blocked implementation status, without reopening Phases 1–2/CORE-01 or taking over items 2–3. The original global acceptance matrix above is still stale and has not been audited by these scoped assignments; these checks do not declare P3-01–P3-14 globally accepted. Items 5–7 remain open. No real feeds/providers were contacted, article content was not exported externally, no paid runtime/preferences or recurring allowance were activated, and Phase 4/5 remain unstarted.

## October 3, 2026: scoped remaining-work item 5

**Item 5 (final combined integration verification): PASS for software/synthetic integration.** The complete serial owned-PostgreSQL gate freshly passed **238 integration tests**, with zero errors/failures/skips; **175 directly related offline units** and whole-repository Ruff, scoped formatting and staged/unstaged whitespace passed. The three former daily-pipeline/Stage 6 failures, both retained-state downgrade guards, populated upgrades and role hardening all executed successfully. Six focused new proof cases passed separately and overlap the full-gate total. Earlier dependency/September totals were inspected and are not added to these counts.

The new assertions cover actual API enqueue → registered ordinary worker → durable ledger → production serialized embedding/generation/checking with synthetic transport, exact identities/bounds/cost; missing-route raw reading with no dispatch; unfrozen/mismatched/stale dispatch rejection; and repeated read-only spending metadata after a populated upgrade. Existing concurrency, uncertainty/restart/reconciliation, physical retry, month, frozen snapshot/route and lower-live-ceiling cases were reused. No production correction or weakened assertion/guard was required. Independent scoped review found no unresolved issue.

All 188 fixture databases and three test roles were identified and removed, and the exact new owned container was removed. The original broad preservation comparison failed because two unrelated infrastructure-monitor containers stopped before our cleanup; all preexisting identities/images/labels/mounts and the other 17 complete records were preserved. The failed comparison and read-only lifecycle evidence remain explicit, and no unrelated resources were restored or cleaned. Source, operational dotenv/preferences, staged work and historical evidence stayed preserved. [Exact commands, results, proof additions, source hashes, independent review and cleanup observation](phase-3-item-5-20261003/README.md).

This closes only checklist item 5. The historical global acceptance matrix above remains unchanged; item 6 browser/old-resource verification and item 7 global closeout remain open. Phases 1–2/CORE-01 remain complete, real paid activation and recurring spending/preferences remain inactive, and Phase 4/5 have not started.

## October 3, 2026: scoped remaining-work item 6

**Item 6 (browser verification, historical consolidation and resource disposition): complete for the authorized local/synthetic scope.** Fresh actual Chrome observations verify persistent settings/source limits, distinct collection/backlog counts, all 26 raw articles across 12/12/2 pages with stable order, known/unknown publication and storage truncation, grouped direct access, distinct capture/provider failures, prior Saved/Brief readability, nonzero modeled spending/reset/model metadata and separate AI blocking causes. September 20 browser evidence is retained separately with source/line/hash provenance and loading/pre-wording qualifications.

Independent review found failed updates incorrectly used the healthy quiet grouped-empty message. Test-first correction adds the truthful unavailable warning and narrowly refreshes the JSX script cache key. **50 affected frontend tests passed**, with an independent rerun of the same 50 and fresh Chrome rechecks of both failures and prior Saved/Brief. The evidence extractor's multiline secret guard also passes four meaningful cases after a failing baseline. Item 5's integration/unit results were inspected, not rerun or added.

Exact owned old test databases/container/anonymous volume/frontend are archived and removed, with absence proof. The new browser harness is archived and removed after capture. The older browser stack missing its original private ownership manifest remains preserved, as do shared/Phase 2/unrelated resources. Failed broad mount-order comparison and its separate identity-preservation adjudication remain explicit. [Fresh expected/actual browser proof, correction, historical provenance, independent review, preservation and cleanup](phase-3-item-6-20261003/README.md).

This closes only checklist item 6. Item 7/global P3-01–P3-14 closeout remains open; the historical matrix above is unchanged. Phases 1–2/CORE-01 remain complete, ordinary paid runtime/preferences and recurring allowance remain inactive, no live feeds/providers or external article exports were used, and Phase 4/5 remain unstarted.

</details>

</details>
