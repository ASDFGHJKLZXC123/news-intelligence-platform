# Phase 2 orchestration and independent verification

Started September 8, 2026 (America/Los_Angeles). **Phase 2: In progress.** This is a progress record, not acceptance closure. The [acceptance tracker](phase-2-acceptance.md) maps each required user-visible outcome to its current evidence and remaining proof.

## Requested roles

The continuing root agent orchestrates scope and combined acceptance. The user requested GPT-5.6 Sol at xhigh effort for implementation and a fresh GPT-6 Astra agent at high effort for independent review. Both delegates were launched with those explicit settings and no inherited conversation history. These are coding-agent assignments; they do not configure or authorize the news application's paid providers.

## Preserved starting point

The checkout is on `codex/stage10-hardening`, with uncommitted Phase 1 implementation and conversion planning. No reset, commit, publication, or production-data migration was performed. A temporary 535-file SHA-256 baseline inventory records the starting delivery set separately from new Phase 2 changes.

The frozen evaluation protocol was independently rechecked: SHA-256 `494f97fb5357ecda8d35a296567fde0d9c503be58257ea003df2bd4625fda3af`.

Fresh starting checks, before Phase 2 source changes:

| Check | Result |
| --- | --- |
| Python unit suite | 4,022 passed; 141 integration cases deselected; 20.04 seconds |
| Frontend suite | 58 passed; no failures |
| Blank database migration and integration suite | Migrated to existing head 0019; 141 passed; 4,022 unit cases deselected; 34.82 seconds |
| Disposable resources | Test containers, volume, network and image alias removed; cleanup verified |

These establish the starting baseline only. They do not validate new Phase 2 code.

## Initial independent review

Astra identified required safeguards around legacy report selectors, frozen source context, Los Angeles daily identities and midnight retries, per-transaction ownership checks, explicit disabled stages, and a production CORE-01 producer. Its acceptance plan includes populated migrations, concurrent starts/saves, failure recovery, immutable inputs and candidate observations, substantive production composition, bounded API reads, and the complete browser journey.

The proposed CORE-01 extractive producer is subject to review: lexical predicates or numbers do not establish factual truth. Preserve source attribution, negation and uncertainty or abstain. Immutable source revisions and spans must coexist with the one-per-article canonical evidence record. Existing composition, grounding, consistency and copyright checks remain required. No CORE-01 implementation or acceptance approval is implied by this preliminary contract review.

## Live-input boundary

A credential-safe inventory found existing provider routes/keys, but no personal settings or explicit personal live-test spending allowance in `.env` or exported environment settings. Credentials were not printed or copied. Existing keys and legacy default budgets are not authorization for paid verification. No live feed/provider calls or production writers have been started. Required bounded live proof remains pending; offline implementation and disposable verification continue.

## Pending closure

New-code regression results, blank/populated migration acceptance, reviewed backend/frontend behavior, browser observations, exact delivered source hashes, and the bounded authorized live proof will be recorded as they become available. Phase 2 remains incomplete until its authoritative closure requirements are met.


## First backend slice: verified progress

This slice adds the schema, protected workspace setup/owner binding, personal event/source reads and persistent save/unsave behavior. It does **not** complete daily processing, CORE-01, snapshots, brief generation, frontend integration, browser acceptance or live verification.

The orchestrator tested a fixed proposed-delivery copy while implementation continued. The [interim delivery inventory](phase-2-m1-delivery-manifest.json) pins its source bytes; new run/claim draft modules in that copy are not approved merely because unrelated regression tests pass. No dependency reinstall is claimed: these checks used the existing Python 3.13 environment. Phase 1 browser files were preserved at this checkpoint.

| Evidence | Actual result and boundary |
| --- | --- |
| [Full offline unit regression](phase-2-m1-unit.log) | 4,026 passed; 144 integration cases deselected, 21.71 seconds, on the earlier first-slice fixed copy. |
| [Full database regression after migration correction](phase-2-m1-integration.log) | 144 passed; 4,026 unit cases deselected, 45.56 seconds. Blank database migrated to 0020; disposable resources removed. |
| Lint | Whole first-slice copy passed Ruff with cache disabled. Initial cache creation was denied by the read-only sandbox; no lint rule was weakened. |
| Orchestrator pure-rule probes | Nine matching cases, three invalid-token cases, three Los Angeles date boundaries, and P2-07 ranking passed. Includes NFKC/casefold, exclusion precedence and title/summary boundary cases. |
| Orchestrator API/session probes | Ambiguous owner with another user having no saves, explicit binding repeat/conflict, duplicate save and unsave, source-independent event detail, invalid IDs/limits and wrong API key all behaved as required. Legacy sentinel users/saves/reports/sections remained exactly unchanged. This is not a browser or process-restart proof. |
| [Astra owner/read/source probes](phase-2-m1-owner-review.log) | Eight concurrent first setups produced one owner/workspace/profile; competing preloaded owner choices produced one binding and one conflict. Bounded source reads, malformed URLs, scope attribution, invalid run scope and unavailable saved entries passed. These first-use concurrency fixtures deliberately cleared the former migration bootstrap and therefore are component evidence, not normal-upgrade proof. |
| [Legacy duplicate-spelling save correction](phase-2-m1-legacy-save-review.log) | Same-owner lowercase/uppercase UUID rows were both removed by one successful unsave; other-owner entries stayed intact. |
| [Astra ordinary upgrade/setup proof](phase-2-m1-upgrade-review.log) | Seven actual 0019-to-0020 cases, without clearing migrated data: zero/one/two existing users, explicit owner choice, nonexistent owner and malformed owner configuration. Legacy data was preserved; active legacy mode stayed active; missing setup was an honest read-only response; invalid setup made no writes; explicit configuration won. Blank downgrade worked, while post-setup downgrade refused and preserved data. |

The migration/setup proof pins migration SHA-256 `f904d8ec342f6288183d22d1fc1888bdf399756d6f7ffb02c12fa132f0eee390`. An intermediate migration created a workspace during upgrade while refusing any populated downgrade; the full regression exposed three failures and seven setup errors. The schema-only correction restored all 144 integration cases without relaxing the protection for actual personal data. The reviewer also found and verified fixes for save construction, read-triggered initialization, owner concurrency, invalid run scope and duplicate legacy UUID spellings.

The orchestrator and reviewer removed their first-slice databases/containers. No production database, live feed or paid provider was used. Phase 2 remains **In progress** while the remaining workflow is implemented and independently tested.


## Run, claim and snapshot slice: review in progress

Sol's focused handoff reported 9 combined personal database integration tests and 19 pure personal tests passing. Astra then ran independent adversarial probes using actual PostgreSQL and production-style `Session(autoflush=False)`; the [interim review log](phase-2-m2-interim-review.log) pins tested source hashes. Passing cases include concurrent daily identity, Los Angeles retry identity and attempt limit, stale ownership and writer-mode rejection, eight fresh supported claim/evidence chains with no preloaded claims, exact decimal/abbreviation/denial spans, seven retained terminal observations, snapshot retry immutability, and rejection of corrupt claim spans, hashes and support links. These are component proofs, not a generated-brief or browser acceptance claim.

Independent review remains open on three reproduced issues: altered observation source text was accepted despite its pinned revision, a new date could reclaim exhausted failed work, and [Today reads could drift to a newer observation](phase-2-m2-interim-today.log) instead of the frozen snapshot's observation. Sol received all three reproductions. Final acceptance requires fixes and reruns, along with the remaining composition, worker, frontend and browser workflow.

Setup configuration is also being corrected so migration alone preserves legacy writer behavior and personal feeds require an explicit profile. These ongoing migration changes supersede the first-slice hash for future acceptance; the earlier logs remain valid only for their stated checkpoint.


### M2 independent fix results and full regression

Astra's [exact populated core reruns](phase-2-m2-core-review.log) rejected altered observation source text and exhausted-run transfer. Its [separately stable Today rerun](phase-2-m2-today-review.log) proved that a persisted second observation exists while Today still renders the snapshot-pinned first revision and membership. Snapshot source hash: `b2db224bae70054f6e98db719e9ce19a027d0877814e947920a1ae68eb3be491`; Today repository hash: `1701a1810edca32eedd5fe6e7fbdd72843fc92241f8fd4400b236d6c1d016fa4`. The core log explicitly identifies a concurrent repository-only change; Today was verified separately with stable before/after hashes. Review infrastructure was removed.

The orchestrator then froze a [559-file checkpoint](phase-2-m2-delivery-manifest.json), verified no source changed during copying and rechecked the frozen protocol. Whole-copy Ruff passed. The [19 focused pure tests](phase-2-m2-unit.log) passed in 0.49 seconds. A read-only test-runner attempt could not create its capture temporary file; the permitted temporary-file rerun succeeded.

The [full fresh database suite](phase-2-m2-integration-interim.log) reported **149 passed, 1 failed, 4,041 deselected in 60.20 seconds**. All disposable resources were cleaned. The remaining failure exposed a related source-detail issue: `qualifies_for_brief` still consulted the latest observation, incorrectly returning false when a newer revision removed membership that remains pinned in the existing snapshot. Sol received this exact regression for `sources` and `event_detail`; this full checkpoint is not yet approved. Latest normal upgrade and explicit profile activation are undergoing independent re-verification because migration/setup behavior changed after M1.


### Explicit profile and source-detail corrections

The [normal migration/profile review](phase-2-m2-profile-review.log) exercised ordinary 0019 seeding followed by 0020 upgrade, without clearing personal/migration state. Zero/one/multiple owners, explicit second-owner configuration, legacy mode until explicit profile activation, active legacy work, immutable revisions, one/ten selected sources, 70 invalid payload cases and API-key rejection were checked. That interim review found tokenless phrases accepted and ambiguous-owner setup readiness inconsistent; it does not claim those initial cases passed.

The [final targeted rerun](phase-2-m2-final-fix-review.log) verified the corrections on stable source bytes with production-style `Session(autoflush=False)`: tokenless setup and profile edits return 422 without writes, unresolved owner readiness agrees as false, valid bound-owner raw setup agrees as true, and source/detail qualification stays pinned to observation 1 after an actual retry persists observation 2. Current additional members remain visibly in the full enrichment scope but do not become snapshot-qualified. The earlier full-suite source-detail failure is therefore independently corrected; a revised fixed-copy full regression is running.


### M2 checkpoint regression passed

The [revised 564-file fixed copy](phase-2-m2-revised-delivery-manifest.json) had no source drift during copying and retained the frozen protocol hash. Its [fresh database regression](phase-2-m2-integration.log) passed **150 integration cases, with 4,041 deselected, in 57.07 seconds**. Containers, volumes, networks and test image aliases were removed and cleanup verified. Together with the independent exact reruns, this closes the reviewed workspace/save/run/claim/snapshot component checkpoint. It does not close CORE-01's substantive generated-brief acceptance, actual worker delivery, the browser journey, exact exports or the live gate.


## Generated-brief slice: independent review in progress

Sol's first composition integration test uses fresh synthetic Article/Event input with zero initial claims, claim links or reports. The personal producer builds the supportive evidence chain; the snapshot feeds the actual shared composer, LLM orchestrator, grounding/copyright checks and report lifecycle, with source-specific scripted responses only at provider I/O. This is a composition-slice proof, not actual RSS ingestion, grouping, worker delivery or live model validation. Generic word-count filler in the first draft fixture was replaced with source-specific prose before the reported passing handoff.

The [early independent component review](phase-2-generation-early-review.md) reproduced failed capture becoming quiet success and swallowed composition failure becoming successful processing. Subsequent fixes connect capture accounting and separate report publication safety from run processing status. The orchestrator also required published-report recovery, sanitized errors, durable failure marking after session acquisition faults and preservation of missing hotness values.

Astra's [nine-case disposable database review](phase-2-generation-interim-review.log) used stable source hashes and production-style sessions. Eight case assertions passed, including substantive fresh claims through the real orchestrator, quiet output with zero dispatch, all-feed failure rejection, failed-provider then healthy-version recovery, no-call recovery of a healthy published report, durable sanitized session failure, stale ownership and an autocommitting-factory rejection. One failed case loses partial-capture status after publication followed by terminal failure. Ordinary partial-capture retry also recomposed unnecessarily. Both are with Sol: any published partial-capture output must retain its snapshot-derived limitation and be reused with the same UUID/sections and no additional composition calls. A failed nonpublished composition attempt may produce a new version on explicit retry. The public error must agree with the report's actual publication status.

Generation acceptance remains open. No paid provider, live feed, production database or scheduler has been used for these checks.


## Parallel frontend implementation

After the M2 backend checkpoint passed and generation work had a concrete implementation, the orchestrator split frontend work into a second GPT-5.6 Sol agent at xhigh effort, also started without inherited conversation history. The original Sol retains backend, migrations, worker/coordinator and backend tests; the additional Sol owns frontend code and frontend tests. Astra at high remains the independent reviewer. Root owns combined acceptance and the acceptance/evidence record. This uses the model plan's maximum of three independent delegates without competing edits to the shared backend. Full browser acceptance remains pending.


### M3 generation checkpoint passed

Astra's [targeted final generation rerun](phase-2-generation-fixed-review.log) passed the three affected cases with stable generator SHA-256 `4a9d29b67892050f4a3ebc301256a547287cc34795d2a8f04e849a7b74b25213`. Both ordinary partial-capture retry and publication-followed-by-terminal-fault recovery preserve the same report UUID and sections, make no new composition calls, and retain partial status from immutable snapshot coverage. Failed-provider reporting is accurate and healthy retry produces the next nonfailed version against the same snapshot. Six unaffected cases passed at the earlier stable checkpoint; the record does not claim all nine independent cases ran again at the final hash. All reviewer resources were removed.

The orchestrator froze a [572-file delivery checkpoint](phase-2-m3-delivery-manifest.json) without concurrent source drift; its generator hash matches the final independent review and its evaluation protocol hash remains exact. Whole-copy Ruff passed. The [full offline unit regression](phase-2-m3-unit.log) passed **4,041 cases, 156 deselected, in 23.32 seconds**. The [fresh database regression](phase-2-m3-integration.log) passed **156 cases, 4,041 deselected, in 59.85 seconds**; its disposable containers, volumes, network and image alias were removed and cleanup verified. The existing Python environment was reused; no fresh dependency installation is claimed.

This closes the reviewed composition/generation component milestone. Actual RSS capture/admission, scoped embedding/grouping, coordinator and worker delivery, personal brief/citation/export API integration, frontend browser acceptance and the bounded live gate remain pending. Phase 2 is still **In progress**.


## Today read-selection review

Astra independently exercised the actual personal events/runs APIs in six disposable database cases, with stable source hashes. The [interim review](phase-2-today-read-interim.log) passed five cases: yesterday remains dated and readable with a separate newer queued/running/failed status; healthy published quiet output supersedes older stories; and a later failure without readable output preserves the dated quiet result. One case failed: an all-feed-failed run whose empty snapshot exists but whose generator correctly rejects publication displaced yesterday, because snapshot existence alone counted as readable. The correction and exact independent rerun remain pending. This is backend read-selection evidence, not browser or full capture acceptance.

The orchestrator also reproduced an existing middleware issue relevant to the new empty-body start endpoint: a local loopback request with no configured key and an untrusted browser Origin reached a harmless sentinel POST handler. No database or queue was used. The backend delegate is adding an allowed-origin check while preserving existing API-key and no-Origin local CLI behavior; verification remains pending.


### Dependency check at the M3 checkpoint

The project dependency audit ran against the fixed M3 dependency declarations and returned exit code 0: [no known vulnerabilities found](phase-2-m3-dependency-audit.log). This is the current advisory result for those declarations, not a guarantee against unknown vulnerabilities.

- `requirements-dev.txt` SHA-256 `adb1121ee6e662f646b8b922da83371aee2fb5e3d5e89f20bd61433ebdbdb593`; current checkout matches: True.
- `requirements.txt` SHA-256 `68cef3209445bc3bf3488a4b08600d933a51cf7adc557cd07c057d3089f73a6e`; current checkout matches: True.


### Today read-selection and browser-origin fixes verified

Astra reran all six exact actual-API timeline cases in a fresh migrated disposable database: [all passed](phase-2-today-read-final.log). Failed capture with an empty rejected snapshot no longer hides older readable results; queued/running/failed neighbors and healthy quiet supersession retain correct dates and status. Relevant source hashes were stable before/after: repository `fc80b72727549666ab0608ed70305b6cb30c0e70143efe30db1c3db62af3c68f`, API `8d6386131274d94553ef1121d10d7e864ab637867f9019a68c654c969c0c47bb`, generator `4a9d29b67892050f4a3ebc301256a547287cc34795d2a8f04e849a7b74b25213`. The test used the evolving migration but is not a new independent populated-migration acceptance claim. Owned databases/container/volume were removed and absence verified.

The orchestrator independently reran the middleware sentinel check after the fix (middleware SHA-256 `020749628c64e423a4ed8457f10b773b358083cee8401aad3d271f35f9ac1dcb`): untrusted and `null` origins both returned 403 with zero handler calls; both configured local frontend origins and a no-Origin local CLI-style request returned 200. No actual database or queue was used.

Early browser observations on the actively edited frontend verified the real API-unavailable state and explicit demo-to-real sample clearing. These are preliminary boundary checks, not acceptance of the unfinished new screens or full application journey. The PDF skill was loaded for eventual read-only export rendering and visual inspection; no PDF has yet been validated.


## Capture adapter and frontend interim checks

The orchestrator independently exercised the actual HTTP RSS adapter with only network I/O replaced by in-memory responses. [Seven stable-source boundary cases passed](phase-2-rss-boundary-review.log): unsupported Atom, XML error pages and RSS without a channel are rejected; valid empty RSS succeeds; 500 entries and unknown publication times are retained honestly; 501 entries and a response above 2 MiB fail within the configured bounds. No network was used. These adapter checks do not replace coordinator/worker or live acceptance.

The [independent frontend Node rerun](phase-2-frontend-interim.log) passed 67 tests. The accompanying [frontend hashes](phase-2-frontend-interim-hashes.json) were collected immediately after that run, rather than in a fixed-copy before/after audit. Tests include client/poller logic and static source contracts; they do not prove React/browser state transitions or saved persistence. CUA checks caught and verified corrections for misleading first-use/empty guidance on an unavailable API and stale stylesheet caching. The full data-backed journey is still pending.

After the frontend checkpoint, its Sol delegate also received ownership of separate personal brief read/evidence/export API modules and router registration. The original Sol retains coordinator/worker/runtime, migrations/models and legacy writer protection. Both remain at xhigh effort; Astra independently reviews, and root owns combined acceptance.


## M4 coordinator regression checkpoint

The actual coordinator now has focused integration evidence beginning with zero Article, Event, Claim, ClaimEvidence and Report rows. External RSS/embedding/model I/O uses labeled offline fixtures; production capture/admission, scoped embedding/grouping, source-claim preparation, immutable snapshot, shared composition/grounding and publication execute. Other focused cases cover same-run failed-feed retry, a three-admission/four-receipt limit with pending retention and feed round-robin order, explicit raw-stage disablement, and seven grouping observations committed together before a forced later claim-stage failure, followed by frozen-scope retry with no RSS refetch. This is application-service integration, not actual Celery delivery, browser acceptance or live-provider proof.

The orchestrator froze a [585-file checkpoint](phase-2-m4-delivery-manifest.json), verified no source additions/changes during copying and the exact frozen protocol hash. Coordinator SHA-256 is `abb1249f307238768522f4dff7e50f5775d12f8815c0d46c5a20f71056c6ff64`; generator is now `aa46b0f689b4275ea37af6001fec97cbc418da78d159c1cbeb8f9f7ea11935ba` after paused-coverage validation changes, so previous generator review hashes are not relabeled as current. Whole-copy Ruff passed. [Full offline unit regression](phase-2-m4-unit.log): **4,047 passed, 160 deselected, 22.52 seconds**. [Fresh database regression](phase-2-m4-integration.log): **160 passed, 4,047 deselected, 70.14 seconds**. Disposable test resources were removed and cleanup verified.

Independent D6/coordinator stress review remains in progress. Astra also identified frontend issues with retry availability for a displayed failed run, stale polls after filter changes, and late saves after navigation; fixes and exact reruns are pending. Passing regression therefore does not approve the unfinished worker/report API/frontend slice.


## Independent coordinator and frontend stress review: fixes pending

The [first seven-case D6 review](phase-2-coordinator-interim-review.log) ran at stable coordinator `abb1249f...` and passed six case assertions. Positive evidence includes fresh actual coordinator publication with four scripted provider calls, unmatched admission with global-backlog exclusion, a 2,000-pending ceiling and fair bounded admission, actual Los Angeles admission-day accounting through midnight, seven atomic observations and frozen retry without repeated embedding, and failed capture followed by healthy quiet output without model calls. A duplicate at pending capacity could skip a new candidate despite a free slot. Production-style `autoflush=False` also exposed missing grouping-stage telemetry after a nested ownership-lock refresh; default autoflush tests had masked it.

The [five additional cases](phase-2-coordinator-extra-interim-review.log) used a later stable coordinator checkpoint and found three failures: published recovery accessed a nonexistent report timestamp, raw mode misreported partial feed failure as succeeded, and a permitted larger capture exceeded the claim-candidate ceiling after grouping, leaving frozen retries unable to proceed. Canonical URL reuse without repeated admission and ownership restrictions passed. The [admission-order probe](phase-2-coordinator-order-interim-review.log) found that rebuilding an earlier admission batch using tied timestamps and random IDs changes which articles fill enrichment slots. That last run used a previously imported coordinator while source files changed; it is an interim reproduction, not final stable verification, and requires an exact rerun after correction. Six fixes are with Sol. No production database, live RSS or paid provider was used; owned databases were cleaned between cases, with the reviewer container retained briefly for reruns.

The [independent frontend review](phase-2-frontend-interim-review.log) reproduced stale filter/poll and late save/navigation state using actual handler code in a minimal hook scheduler with stable hashes, and identified missing Retry for a failed run that is itself the displayed readable result. This is not a React runtime or CUA interaction proof. Related report identity/mode cleanup and citation-response guards are under review and correction.

## Independent coordinator, frontend races and report API corrections verified

The [nine exact coordinator reruns](phase-2-coordinator-fixed-review.log) passed with production-style sessions and stable source hashes: coordinator 34554dd7e81a5e336d09e011b532e66e247fd23b844760c82062d8b340752a2a, generator aa46b0f689b4275ea37af6001fec97cbc418da78d159c1cbeb8f9f7ea11935ba. These close the six issues in the preceding interim section, including duplicate handling at capacity, grouping telemetry, published recovery, raw partial capture status, 60/100 article claim bounds and retained admission order. Paused coverage also passed. No independent D6 blocker remains.

The [exact frontend handler reruns](phase-2-frontend-fixed-review.log) no longer reproduce the two stale poll/save races; Retry is rendered for a displayed failed run. This remains handler-level evidence rather than the full React/browser journey. Subsequent text-only changes are identified separately from those reviewed hashes.

The [initial exact report API review](phase-2-report-api-baseline-review.log) passed published-only identity, owner isolation, privacy, immutable source mutation and coverage checks. A two-article shared canonical claim initially broke detail and exports. The [final independent rerun](phase-2-report-api-fixed-review.log) passed both immutable-report and shared-claim cases, retaining two distinct supports, correct evidence counts and both Markdown URLs. Current source changes do not alter evidence or Markdown/PDF bytes, and conflicting frozen claim text remains rejected. Stable report API hash: da5424f30bdfa79a4d66bf7f27be7633504a2b6e1db390eed8a733d16c2af953; export hash: 07374f66159088ac241e6678f24fbdbe7ade147f7b6c3a9d64198ccc41362de9. Reviewer databases and container were [removed and absence verified](phase-2-report-api-cleanup.log).

An initial two-page PDF component export was visually inspected without clipping or overlap. Missing coverage fields were then corrected to show “Not recorded” rather than invented zero values; final browser-generated export rendering remains pending.

The first root-owned API/Redis/worker browser harness started against a new empty synthetic database but stopped during readiness. Browser workspace GET exposed a recent nonexistent last_successful_run repository call. The API fix and visible partial-fetch error handling are pending; startup cleanup removed only the root-owned database, Redis container and child processes. No complete browser or queue workflow is claimed yet.

## Actual offline API, Redis and Celery delivery passed; browser sequence in progress

The root-owned second harness started with zero Article, Event, Claim, ClaimEvidence and Report rows in a uniquely named disposable database. Its profile used a labeled synthetic RSS fixture with a local controlled article page, and the complete immutable offline generation/embedding route. All configured provider key fields were cleared. The worker consumed only the pipeline queue; no Beat scheduler ran.

Through CUA, the browser required explicit owner choice despite one owner having zero saves. An unauthenticated mutation did not bind the owner. After setting a throwaway local key, the chosen owner was persisted. The retained missing saved target kept its label and removal succeeded; the legacy all-owner/type read view was visible.

A browser update POST returned 202, displayed queued, and was [received and executed by Celery through Redis](phase-2-worker-delivery.log). Polling reached succeeded. The [independent database and HTTP readback](phase-2-worker-browser-run.json) confirms one capture/admission, one scoped event/observation, two exact source-supported claims, one snapshot and one published report, with four explicitly offline LLM audit rows. The brief date is September 9 in Los Angeles even though the article publication is September 8. No old cutoff or same-day publication requirement excluded it. A repeated browser update POST returned 200, retained the same single run, and dispatched no second task.

Run UUID: fda6c6b9-d734-4a61-aea3-c972981943eb. Report UUID: 50cf2f44-9282-4bde-a145-60f2de5aede0, version 1. Snapshot UUID: a75f0934-75b7-466c-8bf9-980446121499. The running worker, fixture runtime, API and coordinator source hashes matched the stable handoff. The frontend remained under bounded polish and requires reloading before final browser verification.

CUA opened the grouped story and source drawer before entering Briefs, showing the retained excerpt and source membership. Clicking its original-source link succeeded, but the next browser inventory was blocked by the Mac lock. The user was asked to unlock it; no alternate control path was used. Save/restart persistence, browser citation/export interaction and final polished-state checks remain pending.

Direct HTTP fetched the same exact report detail, citation, Markdown and PDF. Both rendered PDF pages were visually inspected and have readable text/URLs with no clipping or overlap. Markdown SHA-256: 089d9e28628053debeed7d6acdbfc5b20555dcfb92fc5d4926e36df8298fc694. PDF SHA-256: 928f10c13a561345b316b3f3d33e46f7cc3b2140121ffdc51a148dc7337ef8b4. This is output from actual queued generation with scripted external provider I/O, not a live model/feed result.

The checked-in reusable harness remains under correction after root review found path quoting, masked database URL, early-cleanup traversal and queue-readiness issues. Root's separate browser harness does not use those scripts.

## Latest migration and frontend checkpoint; worker configuration review

The [latest normal upgrade checks](phase-2-migration-final-verification.json) ran 0019 migrations, then ordinary 0020 upgrades, in three uniquely named disposable databases with zero, one and two owners. Every preexisting row in 75 tables remained identical across upgrade (the 8,500-row baseline includes database reference data). The populated case retained its two legacy saves and other sentinels. Migration created no workspace/profile automatically; explicit setup honored each owner-selection rule and retained legacy writer mode. Source hashes were stable, and all three databases were removed and absence verified.

The [41-file frozen frontend checkpoint](phase-2-frontend-final-manifest.json) had no source drift during copying. Its [70 Node tests passed](phase-2-frontend-final.log). This checkpoint includes combined workspace/event error handling, human-readable coverage, visible duplicate-start status and honest absent source claim-count handling. Final CUA checks remain blocked by the locked Mac.

Astra's [direct worker review](phase-2-worker-interim-review.log) and [additional cases](phase-2-worker-extra-interim-review.log) passed durable sanitized setup failures, old-token and competing running-delivery protection, frozen profile selection and partial published-report retry with no additional composition. Two blockers remain under correction: API-shaped raw profiles with an empty retained model route enter live setup, and offline embedding metadata persists version v1 rather than the frozen route's fixture-v1. The successful earlier queued run therefore proves delivery/generation but does not approve embedding route metadata. These exact fixes require independent reruns.

### Independent worker configuration fixes verified

The [final worker rerun](phase-2-worker-fixed-review.log) passed seven database cases plus a pure embedding-result identity assertion with stable worker b6c025a605711282e86fb5c325b86e47990b1016e564443d2aee0274e47c30f1 and fixture runtime c809834defcfef9ba957b3b128e21d5ef2bea0d654dfb58c9d0a01199006dbb4. Raw profiles with absent, empty or retained assisted route settings all perform intake with zero AI/embedding/report rows or AI/Redis factories. The frozen assisted route wins over mutable active profile and global settings, and persisted embedding model/version plus the provider result identity now match it. Setup/stale-delivery and partial-report recovery cases also pass. Test databases were removed; the review container is temporarily retained for legacy guard work. This closes the bounded runtime findings, not the pending full browser or live gates.

To preserve independent parallel work, the second Sol agent now owns the reusable offline launcher scripts, their safety tests and their standalone setup guide as well as the completed frontend/report read slice. The original Sol retains legacy writer/read isolation and bounded live-test software. Root owns integrated acceptance; Astra remains the separate reviewer.

## Legacy writer guards and reusable offline queue verification

The [five main independent legacy guard cases](phase-2-legacy-guard-fixed-review.log), [nine direct entrypoint matrix](phase-2-legacy-guard-matrix-review.log), and [two alert lock-release reruns](phase-2-alert-fence-fixed-review.log) passed at their recorded stable hashes. A stale ORM mode value is refreshed under the singleton lock; queued legacy work blocks activation; an actual provider call and its write retain the fence; direct entrypoints reject personal mode before provider/business calls. Interim [eager provider construction](phase-2-legacy-guard-interim-review.log) and [invalid-timestamp lock leak](phase-2-alert-fence-interim-review.log) findings were corrected and independently rerun. Manual report queue lifecycle and legacy read isolation are a separate gate.

The [manual report review](phase-2-manual-report-interim-review.log) passed queue durability before broker delivery, sanitized broker-failure recovery, actual Celery automatic retry from failed version 1 to published version 2, duplicate suppression, and the full generation transaction fence. It found two additional issues still under correction: a queued date can be consumed with a different task date, and a shared canonical claim can expose personal-created support through a legacy evidence reader. These are not waived by the passing base guard checks.

Root independently ran the checked-in offline launcher in new owned PostgreSQL/Redis containers on random loopback ports. The [stable successful verification](phase-2-offline-launcher-verification.json) begins with zero article/event/claim/support/report/run/capture/snapshot/model rows and follows actual API → Redis → Celery delivery to one event, two supported claims, one immutable report, and four labeled offline model calls. Run 7d194b95-ed9f-4ba3-8085-eb41e99972ff, report cc485f62-bf85-4f2c-98b8-03a7af1ec089 and snapshot 9ac0f902-de22-4cbc-b621-11323e7109ba retain the same complete route; the stored embedding version is fixture-v1. Duplicate start returned the same run without additional business/model rows. Direct report detail/citations/Markdown/PDF all succeeded. [Worker log](phase-2-offline-launcher-worker.log) and [idempotent cleanup log](phase-2-offline-launcher-cleanup.log) are retained; both stop calls succeeded, owned containers/database were removed, and unrelated containers remained.

An [earlier root verifier](phase-2-offline-launcher-interim-verification.json) wrongly assumed only one profile revision; application execution and cleanup succeeded, but its all-revisions scalar query failed. Root corrected the verifier to select the run's frozen profile revision and ran the fresh successful case above. This was a verifier defect, not an application profile defect.

The second Sol now owns new bounded live-smoke software, tests and guide in addition to frontend/report reads and the offline launcher. The original Sol retains worker/API integration and fail-closed eligibility plus manual report compatibility. No live source/provider call or spending has been authorized or performed.

## Browser save, restart and citation continuation

The Mac briefly became accessible. Root reloaded the latest frontend, saved the generated story through CUA, and directly verified the owner-specific persisted row. After an identity-checked restart of the disposable API and worker, page reload retained Saved state and the Saved list retained the title. A removal with the cleared tab key returned 401 and kept the confirmed item visible with an error. Opening from Saved displayed current event source coverage and its retained excerpt independently of a run. Authorized removal then returned 204, showed the empty state, and left zero saved rows.

CUA opened the published brief by exact report version and followed its report-scoped citation. The [persistence verification](phase-2-browser-persistence-verification.json) and [API trace](phase-2-browser-persistence-api.log) retain these observations. After restart, direct Markdown/PDF bytes still match the earlier exact report hashes.

The browser export links were clicked, but no matching HTTP requests were observed before the next inventory reported the Mac locked again. Browser export acceptance therefore remains pending. A minor unscoped Saved-detail label also remains under review: it should not present zero articles as if an update were selected when no run is selected. The older browser dataset's original embedding version mismatch remains identified above; the corrected runtime is independently proven by the new checked-in launcher run.

## Combined regression checkpoint: legacy fixtures and access correction pending

Root froze a [633-file checkpoint](phase-2-combined-interim-manifest.json) with stable copy hashes and the unchanged validation protocol. The [required combined check](phase-2-combined-interim-check.log) passed configuration validation, Ruff, 4,063 Python unit tests, 71 frontend tests and the structural service-kernel gate. Fresh database integration finished with 160 passed and nine failed; all owned containers, volumes and networks were removed and cleanup verified. The dependency audit was not reached because the combined target stopped at integration.

Seven failures come from an older identity fixture that creates only seven identity tables and omits the new writer-mode dependencies. One report API fixture similarly lacks the personal claim-preparation table. These fixtures must exercise the current real schema without bypassing the guards. The ninth failure is a compatibility defect: with Gate G open, an untouched legacy claim and evidence without a report previously remained readable; the new always-require-published-report predicate returns 404. Sol is restoring that accepted legacy read while excluding personal-only claims and newly personal-created support pairs. Astra will independently test both the preserved standalone legacy case and the mixed-support negative case.

The running copy itself was fixed. Only report task/API source changed in the working tree during this checkpoint as the separately reviewed corrections progressed, so this result is interim rather than final delivery approval.

## Independent manual report and legacy evidence fixes verified

The [three exact manual report reruns](phase-2-manual-report-fixed-review.log) passed: a mismatched task date leaves the original Job queued at attempt 1 with no error/report/provider call; a standalone untouched legacy claim remains readable under Gate G with no Report fixture added; personal-only claims and newly personal-created support are excluded while distinct historical support remains. The separate queue, broker-failure, automatic retry, duplicate and full-generation fence checks remain retained above.

An [additional preservation probe](phase-2-manual-preserved-pair-interim-review.log) found that excluding by claim/evidence IDs alone also hid a preexisting contradictory relation when personal preparation added supports. The [final exact rerun](phase-2-manual-preserved-pair-fixed-review.log) now preserves both preexisting supports and contradicts and excludes only the newly personal-created supports relation. Actual Celery automatic retry still produces failed version 1 then published version 2 with Job attempt 2; a terminal duplicate makes no generation call. Stable final API hash prefix 7e471337, report worker feee2691, claim producer 2fcc0a48; full hashes are in the logs. No manual/report/read-isolation finding remains; final combined regression is still required.

## Preliminary bounded live guard findings

The [independent fake-HTTP live guard review](phase-2-live-guard-interim-review.log) reproduced four issues before any real provider use: a false authorization flag still allowed direct guarded dispatch, a multiple-output request reserved only one output, a terminal ledger allowed a new dispatch, and finite long-decimal prices could round a reservation downward at the allowance boundary. Sol is correcting these; final stable reruns remain pending. Basic invalid/unknown/body/output bounds, concurrent reservation count/route limits and retained uncertainty across reopening passed.

Root's existing-parent permission finding was corrected in the new ledger implementation and is covered by a dedicated test; no host parent directory was altered by root. Ordinary live API/Celery execution now fails closed outside the dedicated guarded smoke entrypoint. The dedicated worker integration and all-route verification remain in progress. No live feed/model call, real expenditure or live pass is claimed.

## Independent bounded live accounting fixes verified

The [second guard review](phase-2-live-guard-second-review.log) closed the four preliminary findings but found one more issue: the real OpenAI adapter's normalized missing output usage could clear a physically uncertain reservation. That issue was corrected rather than treated as zero cost.

The [final 14-case accounting review](phase-2-live-accounting-fixed-review.log) passes with stable module SHA-256 3a6f042d0afc4b0d57c1842e2142f855f08f0014b23ad0d82ced6034ce383f6f and CLI 69e755a6e0dbd9853e88c864cf864758985d9f6a568412dd982016e980990dc1. Cases include authorization, request multiplicity, terminal guards, decimal bounds, concurrent count limits, uncertain restart reservations, partial usage through real OpenAI/Gemini adapters, exception paths, and existing-parent permissions. All HTTP responses were fake; no actual paid dispatch occurred. This closes the accounting component only. The dedicated worker/coordinator integration remains a separate review gate.

The reusable offline launcher also reached readiness but lost child processes when the launching tool exited. The earlier root verification kept its parent process alive through cleanup and therefore did not prove launcher detachment. Sol is replacing the launch mechanism and will provide a separate-call survival check before final queue verification.

## Final offline launcher: separate-call survival and queue proof

Root's [fresh final verification](phase-2-offline-final-verification.json) first ran the checked-in launcher to a successful return, allowed that tool process to exit, and then used a separate process/tool call for API → Redis → Celery execution. Both services survived the launcher's exit. The new disposable database began with zero article/event/claim/support/report/run/capture/observation/snapshot/model rows.

Run 90ccab09-14df-430d-b36d-8d1d70ff3e7f produced report 668f36fb-ee51-4d81-89e5-cf30ff4396aa with two source-supported claims and four labeled offline model calls. Duplicate start retained one run and unchanged business/model counts. Exact report/citation/Markdown/PDF endpoints passed; profile, snapshot and persisted embedding route/version match. Source hashes stayed unchanged throughout.

The [worker log](phase-2-offline-final-worker.log) proves actual broker delivery and terminal publication. [Cleanup](phase-2-offline-final-cleanup.log) passed twice, removed the recorded owned database/containers, and preserved all bystander containers. This closes the launcher detachment finding and verifies the corrected checked-in reusable harness independently. The previously successful parent-held run remains retained as earlier evidence.

## Combined green checkpoint; final worker precision review pending

Root's stable [646-file delivery checkpoint](phase-2-combined-green-checkpoint-manifest.json) passed the [complete required check](phase-2-combined-green-checkpoint.log): configuration validation, Ruff, **4,084 unit tests**, **71 frontend tests**, the structural kernel gate, **174 fresh PostgreSQL integration tests**, cleanup verification and dependency audit with no known vulnerabilities. The frozen protocol hash remained exact. No executable source changed during this run; the recorded concurrent changes are documentation and newly retained evidence only.

After that run, the worker's duplicate provider/model price check was tightened to compare exact Decimal values before converting legacy informational settings to floats. The independent live worker review and an exact-source final regression remain pending. This successful checkpoint is retained without relabeling its worker hash as the newer source.

The final live setup guide was also corrected and exercised on a separate owned disposable database: source insertion uses psql variable interpolation through stdin, Bash-specific input syntax is explicit, cleanup requires a matching nonempty container ID, and unsupported probes and interrupted-run limitations are stated. Its placeholder feed URL was never fetched. The guide's owned database/container and port were removed.

## Independent dedicated live worker review: identity and outcome fixes pending

The [eight-case worker review](phase-2-live-worker-interim-review.log) used real HTTP adapters and production coordinator/composer behavior behind fake HTTP and retained stable worker 8b4382ad... / accounting module 3a6f042d... hashes. Seven cases passed: substantive fresh output with five physical dispatches, primary 503/fallback reservation correspondence, uncertain timeout held across fallback, oversized embedding blocked with zero POSTs, preflight rejection before factories, fresh Claim/Evidence contamination rejection, and ordinary registered-worker live rejection. Bound-ledger reopening and a new ledger against an already used database also stopped before I/O.

One case found that valid config A could be paired with independently valid ledger B. It published under A while recording the accounting outcome under B. A config/verification equality check before database setup or I/O is required.

The uncertain fallback case correctly refuses ledger success after publishing substantive content, but its database run still appears succeeded and its blocked outcome omits the already-created report/snapshot IDs. A follow-up correction must preserve valid immutable output while explicitly persisting that live verification is blocked and retaining those output identities. This is an evidence/status defect, not an allowance bypass. Both fixes are with Sol and require exact independent reruns.

The [actual API follow-up](phase-2-live-api-interim-review.log) confirms ordinary live start rejects with zero broker calls, but found that retrying a failed frozen-live run returns 202, increments its attempt and enqueues before the worker rejects it. No paid I/O occurs, but this contradicts the dedicated fresh-only verification boundary and consumes recovery state unnecessarily. Sol is closing ordinary live retry and its advertised eligibility before mutation/enqueue; raw/offline retry must remain available.

## Browser resources removed; final UI gate remains explicit

The Mac remained locked at the last CUA check. Root retained the browser evidence and [independently verified cleanup](phase-2-browser-cleanup-verification.json): recorded API/worker/source process IDs absent, owned Redis container absent, exact synthetic database absent, and the static server stopped. The final browser export/wording check will need a new disposable harness after unlock. No bystander database or reviewer container was removed.

## Independent dedicated live integration findings closed

The [final eight worker reruns](phase-2-live-worker-fixed-review.log) and [actual API rerun](phase-2-live-api-fixed-review.log) pass with stable worker da6e53be..., runs f42c0809..., personal API 5ef6cb6a..., and guard 64f94431... hashes. The exact full hashes and observations are retained in each log.

Mismatched verification/config identities now reject before even opening a database session or constructing RSS/HTTP clients. [Additional identity checks](phase-2-live-identity-review.log) also reject the same verification UUID with a different parsed configuration at the worker, guarded HTTP client and embedding builder boundaries.

A timeout followed by fallback can preserve valid published content, but the final live_smoke stage explicitly records verification_blocked, the immutable ledger records blocked/accounting_incomplete, and both retain the actual capture/snapshot/report IDs. An ordinary live start and retry both return 409 with zero broker calls. Retry eligibility remains false even after the active profile switches to raw because the failed run's frozen profile controls its behavior. Attempt, original error and ownership token remain unchanged.

The [14 accounting cases rerun](phase-2-live-accounting-final-review.log) pass at current guard 64f94431..., and the [additional transport/preflight cases](phase-2-live-boundaries-review.log) pass exact Decimal prices, missing Gemini thinking configuration, custom fallback endpoint and non-followed redirect behavior. These are real adapters/coordinator with fake physical HTTP, not live provider evidence. No concrete bounded-review finding remains. Root's final exact-source full regression is still running.

## Final delivered-source verification and phase status

The [final stable 656-file copy](phase-2-final-delivery-manifest.json) passed the [complete check](phase-2-final-check.log): configuration, Ruff, **4,085 unit tests** (24.37 seconds), **71 frontend tests**, structural kernel gate, **176 fresh PostgreSQL integration tests** (93.85 seconds), verified disposal of test containers/volumes/network, and dependency audit with no known vulnerabilities. Executable sources did not change during the run; only new review logs and this documentation changed. The existing repository virtual environment was reused. No fresh-install or live-provider claim is made.

Root reran the corrected delivery through the [separate-process offline launcher proof](phase-2-offline-delivery-verification.json). The launcher exited before verification began; the services survived. New empty business tables produced run 0d1d8ca8-1727-4281-9b9f-1873db943455, snapshot 786a0a59-d717-4dac-b933-6ad045916c6d and report 465287ba-9c2e-47c2-962f-a11aa7fef2aa. Actual [Redis/Celery delivery](phase-2-offline-delivery-worker.log), two supported claims, four labeled offline model calls, duplicate suppression, frozen embedding/profile/snapshot identity and exact report/citation/Markdown/PDF endpoints all passed with no source drift. [Cleanup](phase-2-offline-delivery-cleanup.log) passed twice and preserved bystanders.

The [independent final live software review](phase-2-live-review-final.md) has no remaining finding in its bounded scope. The reviewer removed its owned database/container after verifying no test databases remained. Root and the second Sol also removed their owned runtime resources; the preexisting shared development PostgreSQL on 55432 was preserved.

**Phase 2 is Verification pending.** Its software, offline queue flow, migration checks and independent review are complete. Browser export downloads and the final Saved wording check remain pending after the Mac unlocks. Required real RSS/model verification remains pending exact user-selected capture/routes/current prices and explicit spending authorization. No live or paid dispatch occurred, and no Phase 3–5 implementation was started.


## September 19 browser continuation

The new task stayed in the shared current checkout and preserved all uncommitted implementation/preferences. Fresh browser availability superseded the historical locked-Mac blocker. An owned offline harness produced report 0539fc3a-a716-4f88-8647-d780de20ca82; actual Chrome link clicks downloaded Markdown and PDF, both marked Done. Original files, exact byte/identity checks, all-page PDF inspection and the final Saved source wording passed. No executable-source defect or change was needed.

Fresh focused checks passed 43 unit, 1 disposable-PostgreSQL exact-report integration and 71 frontend tests. A separate bounded reviewer inspected source drift and the retained evidence. Full-suite results above remain historical. The [continuation evidence](phase-2-browser-20260919/README.md) distinguishes fresh checks, inspection and owned cleanup. Only the separately authorized live proof remains open: spending is undecided, the preference record remains inactive, and no live feed/model execution or Phase 3 work occurred. **Phase 2 remains Verification pending.**

## September 20 final acceptance

The [corrected one-article live proof](phase-2-live-corrected-20260920/README.md), explicitly authorized by “Run the corrected test,” passed with the selected Gemini/OpenAI models and unchanged $0.25 cap. One embedding, two composition and two semantic-grounding requests reconciled at $0.00224186 with no uncertainty. The exact supported published report, inspected Markdown/PDF, immutable ledger, database dump and independent review are retained. Owned cleanup preserved all seven preexisting containers.

The correction preceding this proof changed five production files, one existing integration test and a new unit-test file; 84 focused unit and 16 integration checks passed. The historical full 4,085/176/71 gate was not rerun after that correction. The corrected live attempt changed no executable source, and the browser UI/export wiring remains unchanged from the accepted genuine Chrome gate.

Phase 2 and CORE-01 are Complete. No Phase 2 acceptance gate remains. The saved ordinary runtime profile remains inactive, no recurring spending is authorized, and Phase 3–5 work has not started. Earlier dated pending/failure entries are preserved as history.
