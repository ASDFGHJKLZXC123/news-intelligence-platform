# Phase 2 personal workflow implementation

**Status: Complete — software/offline, actual browser download/Saved wording and authorized bounded live gates passed.** Updated September 20, 2026. Phase 1 remains complete; Phases 3–5 have not started.

Implementation was orchestrated with GPT-5.6 Sol at xhigh effort for backend and a second bounded frontend/harness assignment. A fresh GPT-6 Astra agent at high effort independently reviewed the work. The root orchestrator performed migration, fixed-copy regression, actual queue/browser checks and evidence reconciliation. That initial offline implementation work used no production database, live RSS source or paid model. The separately authorized September 20 proof below used real RSS/model calls only in its disposable runtime; the shared development database remains untouched.

## Implemented behavior

The personal workspace now has explicit owner binding, immutable profile revisions and one logical update per Los Angeles calendar date. The API and Celery worker retain capture, admission, enrichment scope, candidate observations, ownership tokens, attempt history and frozen brief inputs. Retries preserve their original run/date and completed work. Duplicate requests return the existing result.

Selected feed items pass through bounded capture and admission, complete-word interest rules, scoped embedding/grouping and deterministic ranking. The new claim producer creates exact source-supported Claim → ClaimEvidence → article EvidenceItem links. The actual shared composer, grounding/copyright checks and report lifecycle publish the immutable personal brief. Quiet, partial and failed outcomes remain distinct. Source attribution is evidence of what a source says, not independent verification of its truth.

Today, source detail, Saved and Briefs use actual personal endpoints. Saves are confirmed by the server and survive restart; unavailable targets remain visible and removable. Brief history, report-scoped citations and Markdown/PDF exports preserve the selected report and snapshot identity. Legacy reports and personal reports remain separate.

The additive 0020 migration preserves legacy content and ownership. Writer-mode guards cover legacy entrypoints and manual report queue/retry behavior. Independent review verified that existing standalone legacy claims and preexisting support/contradiction relations stay readable while newly personal-created support is excluded from legacy readers.

The [offline launcher](../phase-2-offline-harness.md) provisions owned disposable PostgreSQL/Redis resources with synthetic inputs, blank provider credentials and no Beat scheduler. The separate [live smoke guide](../phase-2-live-smoke.md) defines a guarded fixed-capture verification route. Ordinary assisted live API/Celery execution fails closed until the application-wide spending controls are implemented; it cannot silently bypass the dedicated verification ledger.

## Verification retained

- [Migration proof](phase-2-migration-final-verification.json): normal 0019 → 0020 upgrades with zero, one and two owners, unchanged prior rows across 75 tables, explicit setup behavior, owned database cleanup.
- [Coordinator review](phase-2-coordinator-fixed-review.log): nine independent cases covering fresh generation, pending/admission limits, fair ordering, midnight accounting, seven committed observations and frozen retry without repeated embedding.
- [Worker review](phase-2-worker-fixed-review.log): raw route avoids AI, frozen route wins over mutable settings, corrected embedding identity, sanitized setup errors, stale-delivery protection and published recovery.
- [Report API/export review](phase-2-report-api-fixed-review.log): exact published identity, owner isolation, frozen bytes after source mutation, shared canonical claims and rejected foreign/unpublished IDs.
- [Legacy guard review](phase-2-legacy-guard-fixed-review.log), [entrypoint matrix](phase-2-legacy-guard-matrix-review.log) and [manual report preservation](phase-2-manual-preserved-pair-fixed-review.log): provider/write fences, durable queue lifecycle, automatic retry and preserved historical evidence relations.
- [Final actual offline queue proof](phase-2-offline-delivery-verification.json): empty article/event/claim/report tables reach one event, two supported claims, a snapshot and a published report through API → Redis → Celery. Four model calls are explicitly scripted/offline. Duplicate start adds no work; corrected embedding version matches the frozen route. The launcher exited before a separate verification process completed the queue flow. Cleanup was idempotent and preserved unrelated resources.
- [Browser persistence proof](phase-2-browser-persistence-verification.json): owner choice, missing-target removal, update/duplicate behavior, source inspection, save, actual process restart, reload/Saved, failed unauthorized removal, confirmed removal, exact brief and citation. Direct exports retained identical bytes after restart, and both PDF pages were visually inspected.
- [Independent live software review](phase-2-live-review-final.md): 14 accounting cases, eight actual worker/coordinator cases, the API retry regression and additional identity/transport cases pass. Mismatched ledgers reject before database access; uncertainty preserves output identities while recording verification as blocked; ordinary live retry cannot mutate or enqueue. All physical HTTP was fake. No remaining finding exists in this bounded scope.
- [Final full check](phase-2-final-check.log): **4,085 Python unit, 176 fresh PostgreSQL integration and 71 frontend tests passed**, along with configuration validation, Ruff, structural checks, verified test-resource cleanup and dependency audit. The [656-file snapshot](phase-2-final-delivery-manifest.json) stayed fixed for that historical full run. The later personal-composition source correction passed 84 focused unit and 16 integration tests; the full 4,085/176/71 suite was inspected, not rerun after that correction. Earlier failed checkpoints and their corrections remain retained.

The complete [acceptance matrix](phase-2-acceptance.md) maps P2-01–P2-17 and CORE-01 to evidence. The [orchestration record](phase-2-orchestration.md) preserves failed intermediate checks and exact correction/review hashes. Tests reused the existing repository virtual environment; this phase does not claim a fresh dependency installation.

The frozen Stage 9 protocol remains unchanged at SHA-256 494f97fb5357ecda8d35a296567fde0d9c503be58257ea003df2bd4625fda3af.

## September 19 browser gate completed

A fresh owned synthetic harness in the shared current checkout produced a published report through the real API/Redis/Celery flow. Chrome UI clicks downloaded the exact selected report as Markdown and PDF; Chrome marked both Done. The actual files were retained, their browser origin metadata matched the report UUID, and both matched the stored report rendering byte for byte. All sections/citations and both PDF pages were inspected. Saved → Open displayed current coverage without implying a selected daily update. No scoped application defect was found and no executable source was changed.

The [concrete browser evidence](phase-2-browser-20260919/README.md) records report/snapshot/run identities, files, hashes, observed labels, focused reruns, independent inspection and owned cleanup. Fresh checks passed **43 focused Python unit tests, 1 fresh disposable-PostgreSQL export integration test and all 71 frontend tests**. The historical full 4,085/176 regression and audit were inspected, not rerun in this continuation.

## Closure and remaining scope

**No Phase 2 acceptance gates remain.** The [corrected authorized one-article proof](phase-2-live-corrected-20260920/README.md) captured the exact BBC Japan rates article, produced its supported claim/evidence chain, passed both real semantic grounding checks, and published an intelligible cited brief. Five dispatches reconciled at $0.00224186 with $0 uncertain. Exact Markdown/PDF outputs, both-page visual inspection, independent review, immutable ledger and database dump are retained. Owned cleanup passed.

The selected four feeds and models remain saved preferences, not an active ordinary runtime profile. No generation fallback, recurring allowance or additional paid attempt is authorized. Phase 3–5 work has not started.

The earlier [browser harness cleanup](phase-2-browser-cleanup-verification.json) remains historical evidence. The new [September 19 cleanup record](phase-2-browser-20260919/cleanup-verification.json) records only this continuation's owned resources. The shared development database and preexisting September 18 harness are outside this task's cleanup ownership.

The live verification software is limited to one fresh disposable run and does not resume an interrupted live attempt. Its ledger and published output must be retained for investigation; a new attempt requires separate authorization. It does not establish ordinary daily-use cost control or real-world reliability. Settings, RSS-only reading, shared daily/monthly spending controls, service simplification and human usefulness trials remain in their later phases.

## One-article live proposal prepared — September 19

The [inactive exact proposal](phase-2-live-proposal-20260919/README.md) passed fresh local CLI validation and independent config/budget review. The replacement BBC Japan interest-rate article yields one claim candidate; the earlier flight suggestion yields none. Official current prices and a maximum of nine Gemini requests plus one embedding request give a conservative reservation allocation of $0.11444224 within the approved $0.25 attempt allowance. No suite was rerun, no runtime was started, no database was connected and no model calls were made for this preparation. At proposal time, concrete execution authorization and the successful bounded live proof remained the only acceptance gate; the corrected attempt below subsequently closed it.

## Authorized live attempt — September 19

The [one-shot attempt](phase-2-live-20260919/README.md) failed during BBC TLS verification before model processing. Its ledger has zero dispatches and zero cost/reservations. The exact feed passed a fresh diagnostic with the existing certifi CA bundle selected for that process; the setup guide now records this correction. No application source change or suite rerun was made. The failed ledger/database evidence are retained and the owned container was removed; all existing containers were preserved. At that checkpoint, a newly authorized successful bounded attempt remained required.

## Authorized retry and scoped offline correction — September 20

The [fresh authorized retry](phase-2-live-retry-20260920/README.md) passed exact RSS capture, embedding, grouping, claim preparation and snapshot creation. Five paid requests reconciled at $0.00362026 with no uncertain charges. Both composition sections failed their word limits after their allowed retries, and the report correctly remained unpublished. The inherited market/risk template and sparse-evidence length policy were corrected and passed 84 unit plus 16 independent integration checks [offline](phase-2-personal-composition-20260920/README.md). That checkpoint did not authorize a further paid run. The failed ledger/database are retained and owned live resources were cleaned up.

## Corrected authorized live proof — September 20

The user then explicitly requested “Run the corrected test.” The [fresh one-shot proof](phase-2-live-corrected-20260920/README.md) passed using the same BBC article, Gemini gemini-3.5-flash-lite for two composition and two semantic-check requests, OpenAI text-embedding-3-small for one embedding request, no fallback, and the unchanged $0.25 cap. The recorded cost was $0.00224186; cumulative conservative reservations were $0.0464456; uncertainty was zero.

The real coordinator began with only the selected Source and no article/claim/report rows. It produced one article, one event, one supported claim/evidence link, one immutable personal_descriptive.v1 snapshot and published report d9dec7c9-28ce-414a-bd49-03f87872c95f. Root inspected the cited retained title, substantive generated prose, exact-report Markdown/PDF identity and both PDF pages. A separate reviewer recomputed ledger accounting and audited the source-to-publication chain. The existing actual Chrome download proof remains separate and accepted.

No executable source changed during this attempt. The preceding correction's 84 unit and 16 integration results and historical full/browser results were inspected, not rerun. Fresh checks here cover live flow, exact-report exports, ledger integrity/arithmetic and cleanup. The owned database was dumped and removed, port 61264 closed, and all seven preexisting container identities/start/running states stayed unchanged. Phase 2 and CORE-01 are complete; ordinary runtime activation remains off.
