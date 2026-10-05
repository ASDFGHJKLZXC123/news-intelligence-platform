# Personal project conversion plan — scope and implementation readiness

Scope and phase order consolidated September 6, 2026; implementation readiness audited the same day. This document replaces the shorter conversion checklists in the conversation and [PERSONAL_PROJECT_REVIEW.md](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/PERSONAL_PROJECT_REVIEW.md>). It defines the conversion roadmap and acceptance goals, but is not yet a complete implementation specification for every phase. The existing research artifacts and disabled forecasting boundaries are preserved.

**Implementation status: all five conversion phases are not started.** The application remains at commit `7c7fceb` (August 7). The previous review added documentation only. Today's work clarifies the plan and checks its assumptions against the source; it does not implement the conversion.

**Execution decision:** begin the conversion with Phase 1. There is no separate requirement to finish the original platform first. Complete existing gaps when they serve the chosen reader/brief workflow; defer broader forecasting and platform capabilities. Keep the database and reusable backend services, finish the personal experience, and then simplify how it runs.

**Readiness:** Phase 1's bounded repository and data-display fixes can begin. Before implementing later product behavior, resolve the relevant entries in the decision register below and record their examples/tests. This is engineering specification work, not a requirement to ask the user to approve routine implementation details. The word "final" in earlier summaries referred too broadly to implementation readiness; scope and sequence were more settled than the detailed behavior.

## Decisions, defaults, and remaining specification work

This section takes precedence over less-specific wording later in the roadmap. An example value or earlier assistant recommendation is not a personal preference supplied by the user.

**Scope retained from the conversion discussion:** one local user, Today/Saved/Briefs, source-supported descriptive news, persistent saved events, manual daily processing, reuse of the database/backend, and forecasting outside this conversion.

**Explicit implementation defaults:**

- Keep the existing frontend/backend technology for the first working flow. A framework rewrite is not included.
- Hide deferred features from personal navigation; direct navigation to a deferred personal screen displays unavailable status. Keep demo content explicitly separated and read-only; never use it as an error fallback.
- The first personal release exposes daily collection, eligible failure retry, and reading/export of report history. A separate user-triggered regeneration action is deferred. Existing backend generation/versioning can be reused internally without making every operation a new UI feature.
- A saved entry refers to an event's stable identity and current available data; it is not a frozen article archive. Keep the saved entry visible when the target is unavailable. Do not automatically delete, reassign, or hide preexisting saves during local-owner setup.
- Retain the bounded source inputs actually used by a personal brief, within existing source-storage permissions, plus their hashes/versions. A content hash alone is not a reconstructable source snapshot. New material means a named new input snapshot; existing published content remains immutable.
- Apply allowances using server-recorded admission/request times, never a caller-supplied report date. A historical date or retry cannot create a fresh spending allowance. Exact reset calendars still require the explicit time contract below.
- Do not add automatic deletion of existing data, extra providers, a new relevance model, public sharing, or scheduling to solve an underspecified detail.

**Suggested values, not supplied user choices:** 5–10 feeds, one interest area, 100 new articles/day, $10/month, and the Phase 5 numerical quality targets. Actual topic/feed preferences and a live spending ceiling have not been provided in the conversation. Keep these fields explicit in configuration and settle the applicable values before personal live use; labeled offline fixtures can be used meanwhile. Existing configured preferences should be checked before asking again.

| Decision | What must be specified to remove material ambiguity | Resolve before | Who resolves it |
| --- | --- | --- | --- |
| D1 — Interest and selection | Define whether matching uses existing text/company/category filters; where the filter applies; exact brief ordering and tie-breaks; handling of missing scores; first-run/missed-day coverage. "Relevant" and "coverage/hotness" alone are insufficient. Use examples where reasonable ranking rules would choose different events. | Phase 2 personal selection; actual profile before live use | Engineer defines deterministic behavior; user supplies interests or existing configuration does. |
| D2 — Dates and retries | Name the personal calendar, default date, weekend behavior, midnight-crossing retry rule, historical-date access, and intake/billing reset calendars. Existing processing uses New York dates, the current user environment is Los Angeles, and spending aggregation currently uses UTC months. Do not silently equate these. Fix the logical run identity across attempts and show actual coverage/reset times. | Phase 2C; allowance extensions before Phase 3 | Engineer records a coherent default and migration implications; user need only specify a different preferred routine if desired. |
| D3 — Data identity and upgrades | Define the personal-versus-legacy report discriminator, date/version uniqueness, list/latest/export selection and migration. Define idempotent workspace-owner creation, treatment of existing owners/saves, and unavailable or regrouped event targets. Test a populated database, not only a blank one. | Phase 2 schema and saved-item writes | Engineer; ask only if actual existing records leave an ownership choice that cannot be inferred safely. |
| D4 — Snapshot lifecycle | Fix exactly when candidates, article membership, source inputs and final selection freeze; what a failed pre-snapshot run may add; whether a retry reuses the frozen inputs; how late evidence enters a later snapshot. Record the bounded persisted content and reproducible input identity. | Phase 2D | Engineer. |
| D5 — Deferred and raw articles | Define durable storage of deferred candidates so RSS rotation does not lose them, selection order/feed fairness, next-run eligibility, storage bounds, and how raw items later become grouped without double charging/display. Distinguish collected candidates, admitted articles, and paid enrichment. | Phase 3 | Engineer; do not assume permission for automatic deletion. |
| D6 — Paid work and limits | Record actual configured providers/models, enabled enrichments, fallback behavior, finite smoke-test limits, price source/version, maximum reservations, reconciliation and uncertain outcomes. The sample $10 value is neither measured usage nor authorization for a paid test. | Live portion of Phase 2 proof; general ledger before Phase 3 | Engineer defines enforcement; configured/user-specified spending and credentials control live use. |
| D7 — Runtime and ownership | Choose the actual launcher, process lifetime, deadline/lease values and one lock used by every supported entry point. Define behavior when switching modes and preventing a superseded process from writing events/reports after ownership loss. Replace the current "deadline or renewal" alternative with a selected design and tests. | Phase 4A | Engineer. |
| D8 — Trial evaluation | Select samples deterministically across trial days before judging quality; retain failed cases; name who judges grouping/source support; check all factual claims in the sampled summaries. Define routine retry versus developer intervention, baseline reading measurements, and the treatment of low-volume days. | Phase 5 | Engineer defines the repeatable method; user supplies personal relevance/usefulness judgments. |

An unresolved decision does not stop unrelated phases. For example, the feed topic does not block fixing the missing Git artifact, and runtime launcher design does not block source-link rendering.

**Small implementation specification required at each phase entry:** list the affected files/contracts; pin the observable rules and configuration values; define request/response and state transitions where they change; explain schema/data compatibility and recovery; provide concrete input/expected-output examples and tests; mark remaining user-dependent fields explicitly. Keep routine internal naming/refactoring decisions with the implementer. Do not turn these specifications into another broad platform-design project.

**A phase closes on recorded evidence:** expected outputs, test results, browser demonstrations where applicable, migration/recovery results, and declared limitations. If a material behavior is still phrased as "A or B," the relevant decision remains open. No new user-facing feature, destructive data change, paid allowance, or quality claim should be inferred merely to make a phase look complete.

| Existing unfinished area | Final disposition |
| --- | --- |
| News collection setup and limits | Configure the small verification feed scope in Phase 2; deliver reusable personal settings and enforced allowances in Phase 3. |
| Grouping quality | Fix defects that prevent the Phase 2 workflow; assess real-feed relevance/grouping in Phase 5. Do not require completion of the original institutional evaluation program for the descriptive reader. |
| Reading interface | Remove sample/live confusion and false content/actions in Phase 1; finish details, sources and updates in Phase 2. |
| Saved items | Complete persistent event save/unsave in Phase 2. |
| Daily briefs | Complete actual UI/export and the explicit personal coverage/selection contract in Phase 2. |
| Daily processing | Wire once-per-date start/status/retry in Phase 2; enforce shared allowances in Phase 3; simplify execution in Phase 4. |
| Forecasting | Defer from this conversion. Preserve research code and current disabled prediction boundaries. |
| Installation | Fix reproducibility in Phase 1; reduce persistent service requirements in Phase 4. |

## 1. What exists today

| Capability | Current implementation | Remaining gap for personal use |
| --- | --- | --- |
| Collect RSS news | Fetching, normalization, stored articles, duplicate prevention and source records exist. | No small personal setup flow or consistently enforced daily intake allowance. |
| Group stories | Embeddings, clustering, event persistence and read APIs exist. | Quality on real personal feeds remains unestablished; embeddings are a required dependency for new grouping. |
| View events | Real lists, filtering, pagination and a detail API exist. | Frontend mostly uses its initial list snapshot. Detail response lacks the member-article/source list needed to verify a fresh event. |
| Inspect evidence | Published descriptive report claims have an evidence endpoint with source links and bounded snippets. | Frontend drops snippet fields, invents representative prose and disables links. Event sources must also be accessible before a report exists. |
| Save an event | Database watch entries, uniqueness rules and a list API exist. | Save/delete APIs and consistent local ownership are missing. Frontend changes are temporary and disconnected from the list. |
| Generate/read/export briefs | Real report generation, evidence checks, persistence, versions and Markdown/PDF export exist. | Reports UI is sample content. Existing market-day selection does not necessarily include news collected by a daytime run. |
| Run a daily update | Durable start/status/retry APIs and a seven-stage coordinator exist. | UI controls are unwired. A successful date is deliberately not processed again; this is not an anytime news-refresh API. |
| Operate locally | Five-service setup, migrations, recovery controls and runbooks exist. | No minimal personal runtime. Redis is used for queues, prompt caching and rate limiting. |
| Forecast crises | Research calculations, contracts and evaluation helpers exist. | No established forecasting validity; predictions and composite alerts remain outside the daily process. |

The latest verification, from the September 5 review, was 4,022 Python unit tests passed, 141 disposable-database integration tests passed, and 49 frontend tests passed; lint and Compose configuration also passed. A targeted test in a temporary Git-tracked-only copy failed because an evaluation protocol is not tracked. Source status was rechecked today; unchanged suites were not rerun.

These checks have limits: the daily-pipeline integration test injects a brief stage that inserts a report directly. Separate tests cover real report components, but this does not prove that a live daytime update produces a correctly scoped brief. No complete live browser workflow or week of operation has been demonstrated in this review.

## 2. The finished personal product

The product serves one person, one selected interest area, and a small feed list. Its main screens are:

- **Today:** collected stories, grouped events where available, source access, filters, update status and timestamps.
- **Saved:** persistent saved events that remain available after a browser or application restart.
- **Briefs:** actual generated briefs, their coverage and citations, previous versions, and exports.

The initial daily workflow is: run the day's update → inspect grouped stories and their sources → save useful events → read and export the resulting brief. A display reload reads stored data. The daily update runs once per supported date, with explicit recovery of failed runs. Multiple successful collection runs within one date and automatic scheduling are future extensions, not hidden promises in the first release.

Forecast probabilities, general Ask AI, global maps, model debates, broad company-research expansion and external notification channels are outside this conversion's completion criteria. Their existing code can be retained separately. PostgreSQL and existing data stay in place.

## 3. Outcome at each phase

| Phase | Deliverable when complete | What the user can do | Current status |
| --- | --- | --- | --- |
| 1 | Reproducible, honest application foundation | Install the documented project, distinguish demo from real data, and see accurate availability/error states. | Not started |
| 2 | Functional personal reader and briefing workflow | Run a daily update, inspect sources, save events permanently, and read/export a brief for that update. | Not started |
| 3 | Bounded personal-use profile | Choose feeds and limits, understand spending and backlog, and read available news when AI fails or is disabled. | Not started |
| 4A | Queue-free processing option | Run the same workflow without an always-running Celery worker or scheduler; Redis remains temporarily. | Not started |
| 4B | Lightweight local runtime | Start the application and PostgreSQL; processing runs on demand, with Redis optional. | Not started |
| 5 | Evaluated personal release | See evidence of reliability, relevance, source support, cost and usefulness from actual use. | Not started |

**Phase 2 is the first functional personal MVP. Phase 3 makes it suitable for routine use with explicit limits. Phase 4 reduces upkeep. Phase 5 establishes whether it actually helps the user.**

## 4. Phase 1 — Reproducible, honest foundation

**Outcome:** someone using only the delivered repository can follow its setup instructions, run the documented checks, and understand which displayed information is real.

**Deliverables**

1. Make required evaluation documentation available in version control, starting with the existing exact bytes of `docs/evaluation/stage9-validation-protocol.md`. Preserve the frozen hash. Audit other required local-only files and broken public documentation links.
2. Document and verify a clean installation, including the declared dependency versions, database migrations, frontend launch, and which capabilities need provider keys or extra models. Record the verified dependency resolution.
3. Introduce explicit demo and real-data modes. Real mode never appends sample events/evidence or switches to samples after an API failure.
4. Render actual available snippets, or a clear missing-excerpt state. Enable authentic HTTP(S) source links. Display data freshness and failures accurately.
5. Limit normal navigation to the personal workflow. Hide deferred analytical screens in personal mode and disable unfinished in-scope actions with an accurate unavailable state until connected. A success message must correspond to a successful operation.

**Acceptance demonstration**

- Install into a fresh environment from the delivered files and apply all migrations to a blank temporary database; run the documented offline tests and database checks.
- The previously reproduced missing-protocol test passes in a tracked-files-only copy.
- Real mode with an empty database shows empty states, not sample stories. Disconnect the API and see an error or stale-content state rather than examples.
- Explicit demo mode is visibly labeled. Existing evidence with a source URL opens that URL; missing evidence never produces invented quotations.

**Boundary:** this phase makes the foundation reliable to install and interpret. Persistent saving and the complete daily workflow arrive in Phase 2.

## 5. Phase 2 — Complete the personal workflow

**Outcome:** a user can process a day's news, inspect its actual sources, save useful events, and read/export a brief that corresponds to the selected news from that update.

### 2A. Read a story and verify its sources

Reuse event list/filter APIs and fetch event details when opened, including from Saved when the event is absent from the current Today page. Add a bounded event-to-article/source read using the existing event/article/source relationships. Display article title, publisher, available publication time, retained snippet and original URL without requiring a published brief first.

For report citations, retain existing evidence-access rules and load evidence for the selected report/version, rather than only the latest brief. Implement updates to frontend state after successful operations; the existing boot-only snapshot is insufficient.

**Pass condition:** open a stored grouped event in a database with no report, inspect its constituent articles and open an original source. Filtering to no matches produces an empty result. An unavailable detail or missing snippet is described accurately.

### 2B. Save and unsave persistently

Reuse the watch-entry table with event targets. Add save/delete operations, target validation and idempotent behavior. Establish a single local owner that satisfies the existing user relationship; this is local workspace identity, not an account/signup product. Use the same owner on reads and writes. Connect Today, event detail and Saved to this persistent state.

**Pass condition:** save an event twice, reload the browser and restart the app. Exactly one saved entry remains. Unsave it, reload, and confirm removal. A failed write displays an error and does not falsely retain success state. Saved events remain openable outside the current Today filter.

### 2C. Run an update and recover from failure

Connect the existing process/status/retry APIs to visible actions and status polling. Present queued, running, succeeded, partially failed and failed states. Show processed date, last successful update, available terminal stage results, and the actual reason when retry is unavailable. Extend the status response with server-computed retry eligibility/reason so the frontend does not guess the attempt allowance or safe-to-rerun policy. Do not invent percentage progress or per-stage live progress the backend does not expose.

Keep the initial once-per-date contract explicit:

- **Reload display** rereads stored data and incurs no collection or AI work.
- **Run daily update** starts the supported processing date once.
- Repeating a succeeded date reports **Already processed**.
- **Retry** is available only for eligible failed/partially failed runs or expired ownership, within the retry allowance.

**Pass condition:** a queued action eventually reflects its real terminal result and reloads available items. Repeated clicks do not create duplicate processing. A controlled failure is inspectable and an eligible retry preserves prior saved items and reports.

### 2D. Brief the selected news and revisit it

Replace sample Reports with real list/detail/history and wire Markdown/PDF exports. Reuse report generation, citation checks, immutable published versions and failure handling.

Add an explicit personal brief input contract after closing D1–D4. After collection/grouping, use the durable event IDs accumulated across all attempts of the logical daily run, not just events newly created by its last retry. Freeze their associated article membership, the candidate/selected IDs, an as-of timestamp and coverage metadata. Retain the actual bounded source inputs or resolvable immutable source revisions, plus hashes, needed to inspect the exact material used; hashes alone do not reconstruct it. The brief describes those selected inputs, with the displayed date/time and source coverage matching that snapshot. A retry must identify its snapshot; changed source inputs require a named new snapshot under the specified lifecycle. Preserve the old coverage semantics of already published market-day reports. A separate personal UI regeneration action is deferred; existing versioned history remains readable/exportable.

The intended personal selection is up to five qualifying grouped events from that run snapshot, without the legacy institutional hotness floor of 40, which could empty a small feed. The exact relevance predicate, ranking fields/order, missing-value handling and ties must be fixed in D1; the phrase "coverage/hotness" is not itself an algorithm. Preserve evidence requirements and quiet-period behavior; ranking is a reading aid, not a claim of crisis severity. Persist the selected IDs and input identity so the selection can be inspected and reconstructed; fresh model generation is not promised to produce byte-identical prose.

**Pass condition:** a representative article collected after 05:30 New York time forms an eligible event, appears in the personal run's selected inputs, and can appear in that update's brief. Each tested claim resolves to its supporting evidence. UI and exported content match the selected published version. A failed generation leaves the previous published version accessible. A quiet update produces an honest no-qualifying-events result.

**Proof for the complete phase:** exercise the whole browser flow against real application/database components and representative captured feeds, using deterministic providers for regression tests; also record a bounded live-provider smoke run. For that smoke run, use a disposable database with a fixed small, explicitly listed subset of real feed captures, no scheduler and no unrelated pending backlog. A verification harness must cap total live dispatches, per-call input/output tokens and reserved run spending across embeddings, verification probes, reasoning, fallbacks and retries; it refuses further calls at the cap and records usage. This is a one-run verification boundary, not Phase 3's reusable daily/monthly allowance implementation. Tests must call production report selection/generation rather than insert a report as the integration shortcut does today. Record actual coverage, output and cost, and distinguish captured-feed regression evidence, live-provider evidence and the later real-feed trial.

**Boundary:** the existing full stack may still be needed, and initial setup can still use documented configuration. Personal settings, enforced allowances, and AI-independent access to newly ingested raw articles are Phase 3 work. General chat, rich note editing, arbitrary same-day reprocessing and public sharing are not needed to finish Phase 2.

## 6. Phase 3 — Bounded daily use and graceful failure

**Outcome:** the user controls the selected sources and workload, sees how much processing costs, and can keep reading available news when optional AI work is unavailable.

**Deliverables**

1. A small personal configuration: enable/disable selected feeds, choose the interest filter, set article/spending allowances, and see enabled enrichments. Proposed initial defaults are 5–10 feeds, at most 100 newly admitted unique articles per day, and a $10 monthly application AI allowance. These are suggested starting settings, not measured requirements or a cost estimate.
2. A setup/preflight result specific to the selected features: database ready, feeds configured, required credentials/models present, and remaining allowance. Raw reading must not require credentials for disabled features.
3. A bounded input selection applied after duplicate detection. Ingestion and downstream embedding/grouping must share the selected article scope instead of processing the entire backlog. Show processed/deferred counts; retries share the same daily allowance. Intake admission and spending reservations must be atomic across concurrent dates, requests and worker/application entry points; a per-date job lock alone is insufficient to enforce a shared allowance.
4. Durable spending reservations and usage records for reasoning, embeddings, snapshot-verification probes, fallbacks and retries. Estimate a conservative maximum before each request, reconcile usage afterward and account conservatively for uncertain outcomes. Unknown pricing cannot silently count as zero. The application allowance covers this application's requests, not spending by other programs on the same provider account; estimated charges need reconciliation with provider usage.
5. A raw-article reading endpoint/view using existing retained RSS fields. If embeddings are disabled or fail, new articles remain readable as ungrouped items. Stored events, saved entries and prior briefs remain available while the local app/database run. This does not download full publisher pages for offline reading.
6. Explicit execution-profile semantics for disabled enrichments. The current coordinator requires seven runners and rejects a stage directly returning skipped; intentional feature disabling must be represented deliberately. Preserve independent entity/analogy failure handling and existing prediction exclusions.

**Acceptance demonstration**

- Set an allowance of 20 and present 30 new unique articles: admit/process at most 20 and account for 10 deferred. Retrying does not reset the allowance or duplicate records.
- Fail one feed while another succeeds: successful items remain available and the failed source is named accurately.
- Disable AI or exhaust its allowance: no new paid request starts, including embedding probes/retries; new raw articles and stored reading material remain accessible with accurate grouping/summary status.
- Restart the app: daily allowances, spending records, saved events and report history remain intact.
- A proposed paid request whose maximum reservation exceeds the remaining allowance is refused before network dispatch. An uncertain charged request is not erased from accounting merely because the job failed.

**Boundary:** a missing AI summary is acceptable; fabricated content or a false successful update is not. The application still may use the original background services until Phase 4.

## 7. Phase 4 — Reduce required services

**Outcome:** the same bounded workflow works with fewer always-running components while preserving data, spending and recovery behavior. This is a runtime refactor; deleting containers alone would break the current app.

### 4A. Direct processing with Redis retained temporarily

Extract reusable processing functions from Celery wrappers. Add a manual command using the durable lifecycle, plus an application dispatch path that invokes the same processing without queuing to an absent Celery worker. Keep explicit dates, error records and retry rules. Add a single active processing lock across dates and entry points. Replace Celery's execution deadline with a direct-runner deadline compatible with the database ownership lease, or an explicitly verified ownership-renewal policy, before declaring this step complete. Redis may still provide caching/rate limiting at this intermediate point.

**Pass condition:** with the Celery worker and scheduler stopped, the documented command and app action can complete the bounded daily workflow or show a recoverable partial outcome. The UI reads the same durable status. Existing fixtures produce equivalent selected inputs and persisted results.

### 4B. Make Redis optional in personal mode

Assemble the local cache/rate limiter, keep monetary reservations and counters in PostgreSQL, and retain the single active processing lock and deadline/recovery controls from 4A. A local limiter alone cannot protect against two independently launched runs. Adapt health checks, API dispatch and cleanup to the personal mode. Serve the frontend from the application if claiming one reader/API service.

The target is **two persistent services: application and PostgreSQL**, with a temporary processing process when an update runs. No permanently running Celery worker, scheduler or Redis is required for that mode. Preserve the larger runtime as an optional mode and retain the existing database/schema.

**Acceptance demonstration**

- Stop Redis, Celery worker and scheduler. Start the application and PostgreSQL using the documented personal launch path; the selected capabilities report healthy.
- Complete a daily update, source inspection, saving and valid brief/export through the same user workflow.
- Start two competing updates, including different dates: only one acquires processing ownership.
- Stop a run during processing and recover explicitly after the appropriate timeout/lease; a late old process cannot overwrite the newer result.
- App restarts preserve spending/allowances and user data. Back up and restore to a disposable database and recover saved events and report history.

**Boundary:** a running local application/database is still required. Mobile synchronization, hosted availability, a database-engine migration and automatic scheduling are separate choices.

## 8. Phase 5 — Demonstrate personal usefulness

**Outcome:** a release assessment based on real use, with explicit pass/fail findings and an issue list, rather than another declaration that implementation is complete.

Run the selected profile for seven actual daily sessions. Record each run's success/partial/failure outcome, article/event counts, coverage timestamps, expenses, recovery steps and time spent reading. Review at least 30 real event groups and 10 brief event summaries, extending the observation period if the chosen feeds do not supply enough material. A quiet day is acceptable and should not be filled with examples.

**Proposed acceptance targets**

- At least six of seven daily sessions complete without developer intervention; any failed session has an understandable, recoverable result. No saved-data loss or hidden allowance reset occurs.
- At least 80% of the reviewed event groups are relevant to the user's selected interest; the user supplies the relevance judgments.
- At least 90% of reviewed groups describe one coherent development. Record incorrect merges/splits rather than quietly adjusting the sample.
- Every factual claim checked in the sampled summaries is supported by its cited material. Any unsupported claim blocks this quality check until the defect is fixed and affected cases are retested.
- Processing respects the configured scope and allowance. Reading effort/cost observations show concrete usefulness relative to the user's ordinary feed-reading routine; if they do not, simplify or revise the product before expanding it.

These are proposed personal acceptance targets on a small sample, not guarantees of unseen accuracy, statistically established model performance, or validation of financial forecasts.

**Deliverables:** a dated trial log, reviewed examples with corrections, observed spending, remaining issues, a concise architecture/setup guide and a reproducible demonstration using clearly labeled captured/sample data. Where source material cannot be redistributed, keep the demonstration to appropriate metadata and permitted fixtures.

Seven days of real use and personal relevance judgments cannot be manufactured by an automated test run. If this evidence is missing, Phase 5 remains incomplete even if every software test passes.

## 9. Execution order and implementation boundaries

The default order is 1 → 2A/2B/2C/2D → 3 → 4A → 4B → 5. Some frontend/backend work within Phase 2 can proceed in parallel once response and mutation contracts are fixed. The week of observation can start after Phase 3, but changes made during Phase 4 still need regression verification before declaring the final release complete.

Phase 2 contains the largest amount of new product behavior: event sources, save mutations, frontend updates and personal brief selection. Phase 4 is the principal backend refactor. Neither is a small UI-only adjustment. Calendar estimates should follow the agreed scope and first implementation breakdown rather than assume these stages take equal time.

| Work area | Existing starting points |
| --- | --- |
| Phase 1 reproducibility | [.gitignore](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/.gitignore:37>), [immutable evaluation dependency](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/evaluation/stage9_v2.py:73>), [README](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/README.md:37>) |
| Phase 1/2 frontend | [adapter](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/api-adapter.js:382>), [loader](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/data.js:1>), [evidence drawer](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/main.jsx:5>), [Reports prototype](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/page-misc.jsx:118>) |
| Phase 2 reads/saving | [event API](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/apps/api/intelligence.py:2196>), [watch-entry model](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/db/models/core.py:2196>) |
| Phase 2 update/brief coverage | [process API](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/apps/api/pipeline.py:147>), [existing cutoff](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/reports/window.py:16>), [selection query](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/reports/repository.py:171>), [brief adapter](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/workers/pipeline_stages.py:297>) |
| Phase 3 scope/allowances | [stage execution](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/workers/pipeline_stages.py:201>), [embedding defaults](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/workers/embedding_tasks.py:116>), [spending records](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/llm/repository.py:103>) |
| Phase 4 runtime | [coordinator](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/pipeline/coordinator.py:1>), [lifecycle store](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/pipeline/sqlalchemy_store.py:1>), [production model runtime](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/llm/runtime.py:17>), [health](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/apps/api/health.py:1>) |
| Verification limitation to address | [injected integration brief stage](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/tests/integration/test_daily_pipeline_vertical_slice.py:405>) |

No phase is marked complete by this document. Implementation, phase-specific tests, live evidence where needed and the recorded acceptance demonstrations are the work that changes those statuses.
