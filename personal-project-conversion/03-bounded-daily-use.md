# Phase 3 — Bounded daily use

**Status: complete for local/synthetic engineering acceptance.** Phases 1 and 2 are complete. The [authoritative Phase 3 acceptance record](evidence/phase-3.md) maps all fourteen passed criteria, current proof and retained historical status. Ordinary paid activation remains off; Phase 4A is complete for local/synthetic engineering acceptance with [a 101-case regression refresh](evidence/phase-4a.md). Phase 4B/5 remain unstarted. Read the [shared contracts](00-shared-contracts.md) first.

## Finished outcome

You can choose a small source list, understand what was collected or deferred, set a permitted workload, and see the application's recorded AI spending. News remains readable when grouping or brief generation is unavailable. Retries, competing requests, and restarts cannot silently reset allowances.

The default technical profile permits up to 10 enabled feeds, 100 newly admitted articles per local calendar day, 100 newly admitted articles per logical run, and a separate maximum of 100 articles selected for enrichment per logical run. These are initial implementation defaults that can be edited in settings. The [selected preferences](SELECTED_PREFERENCES.md) record four approved feeds, empty interest phrases, Gemini generation/checking and OpenAI embeddings, with no fallback. Those preferences remain saved and unapplied to ordinary runtime. No recurring monthly allowance or additional paid run is authorized. Live AI stays disabled. The completed Phase 2 proof supplies separate real-provider evidence; Phase 3 boundary verification uses synthetic providers. **$10/month is an optional example, not an authorized allowance or a cost estimate.**

## Implement in this order

1. Persist and validate personal settings, including a revision used by each run. Add feature-specific setup results.
2. Extend Phase 2's durable capture/admission records into a bounded pending queue and atomic article allowances.
3. Propagate the frozen article processing scope through embeddings, clustering, and brief selection; prevent whole-backlog processing.
4. Put every paid dispatch behind the same durable reservation/reconciliation service.
5. Add raw-article reading, processing-status presentation, and settings/backlog/spending displays.
6. Demonstrate the boundary, retry, restart, and failure cases below against PostgreSQL and production application components.

## Settings and time contract

| Setting | Rule |
| --- | --- |
| Workspace | One existing personal workspace; reuse the identity and ownership rules from Phase 2. |
| Timezone | Persist `America/Los_Angeles` as the initial workspace timezone. Render dates and reset instants using its actual offset, including daylight saving time. |
| Sources | An explicit enable allowlist, initially capped at 10 feeds. Disabling a source stops its capture and new admission; it does not delete its pending records or articles. Re-enabling makes its pending records eligible again. |
| Interest | Use the deterministic selection in the shared contract to filter Today and brief inputs. Capture and admission cover the selected feeds independently of interest phrases. Do not add another relevance model. |
| Article allowance | Initially 100 newly admitted unique articles per workspace local calendar day. Derive the day from server-recorded admission time, never a supplied processing/report date. |
| Run admission size | At most 100 newly admitted articles for one logical run, shared by all attempts. Store the chosen ceiling in the run's configuration revision. |
| Run enrichment size | A separate maximum of 100 admitted articles selected for enrichment per logical run, shared by all attempts. Previously admitted raw articles can occupy this scope without reducing the new-admission ceiling. |
| Paid work | Explicit enabled state, configured provider/model routes, finite token/request limits, and a permitted monthly USD allowance. Disabled paid processing has a zero ceiling. The per-run spending ceiling defaults to the explicitly configured monthly allowance unless a lower per-run ceiling is configured, and every request remains constrained by the shared month's remaining allowance. A separate per-run input is not required. Missing required values blocks paid work while raw reading remains available. |
| Spending month | UTC calendar month at request dispatch. This preserves the existing spending aggregation calendar. Show its next reset as a local timestamp and label the UTC accounting period. |
| Changes during a run | Existing runs keep their feed/selection/model/configuration snapshot. A lower live spending ceiling applies immediately to new dispatches. A lower daily admission ceiling applies immediately to new admissions; prior usage remains recorded. Raising a ceiling requires an explicit settings change. |

Setup may choose a different IANA timezone before the first run. Changing it after the first run requires an explicit future migration and is outside the first release. The settings API rejects such changes with a clear explanation. A timezone edit cannot make stored runs acquire different identities or move historical spending/admissions into different buckets.

Example: an unfinished September 6 run resumes at 00:05 on September 7 in Los Angeles. Its logical run identity, admission ceiling, and enrichment ceiling remain September 6's. Any admission still legally permitted before its intake freeze is charged to September 7's calendar-day allowance. An already admitted article is never charged an admission unit again. A request dispatched on October 1 at 00:01 UTC belongs to October's spending ledger even though Los Angeles still displays September 30.

## Bounded capture and durable deferral

Reuse the production canonical URL and hash logic in `services/ingestion/normalize.py`. Deduplicate across existing articles, pending candidates, feed duplicates, and repeats within one capture. Do not treat a tracking-parameter variant as a new article. Preserve the first retained record; a repeated URL is not permission to overwrite a published brief's input snapshot.

Each enabled feed capture has a durable receipt: workspace/run, source, attempt, capture start/end, observed counts, status, and bounds reached. Retain pending candidates before articles rotate out of the remote feed. A candidate stores its canonical identity, source identity, first-seen capture/time, publication timestamp when actually supplied, bounded title/summary, original URL, and field-truncation indicators. These are retained RSS fields, not downloaded publisher pages.

The capture limits are concrete:

- Read at most **2 MiB of response bytes per feed request** and process at most **500 feed entries**. A response above the byte limit is marked too large and yields no unverified partial XML; do not label it a complete capture. The entry limit applies to parseable input and names truncation explicitly.
- Titles are limited to **512 characters** and retained snippets to **2,000 characters**, or an existing stricter source-storage permission. Preserve truncation indicators. Bound other external fields using the existing schema limits and reject unsupported/invalid URLs rather than silently corrupting them.
- Keep at most **2,000 pending candidate records** for the workspace. Admitting a candidate atomically transfers it to an article/admission record; the pending slot becomes available. This is a state transition, not deletion of an article or source record.
- At pending capacity, pause further candidate capture/admission input and show `collection_paused_pending_capacity`. Existing pending admission may still drain the queue. Do not automatically delete the oldest pending record, pretend the remote feed is exhausted, or invent a deferred count for records never observed.
- Missing/invalid publisher dates remain unknown in the reader and ordering. Adapt the current provider's fallback-to-current-time behavior so a capture timestamp cannot masquerade as a publisher timestamp.

A new run drains eligible pending candidates before requesting fresh feed captures. Among active feeds, use round robin by stable source ID in ascending order; select one candidate from each eligible source, then repeat. Within one source, select the earliest first-seen capture batch first, newest known publication time within that batch next, then canonical URL and candidate ID ascending; unknown publication times follow known times in the same batch. Persist the admission order and resume it after a crash. A source represented by duplicate URLs cannot consume additional slots.

The transaction that admits a candidate must lock the relevant allowance/run scope, confirm remaining daily and run capacity, ensure canonical uniqueness, create or reuse the article, link it to the logical run, increment the daily count only for a genuinely new admission, and complete its pending state transition atomically. A conflict or rollback spends no admission unit. The full intake stage freezes before any paid call. A retry after freeze may resume downstream processing but may not append candidates to that run.

The backlog display distinguishes **observed candidates**, **pending/deferred candidates**, **admitted articles**, and **completed enrichment**. It reports exact counts only for durable records. “20 admitted; 10 pending” is valid after observing 30 new records within all bounds. “20 processed; 10 deferred” is invalid when only intake succeeded or the response was truncated before its size was known.

## Processing profiles and raw reading

The personal AI profile is **RSS capture/admission → article embeddings → clustering → brief**. Entity linking, event embeddings, historical analogies, and forecasting are disabled by default, with durable `disabled_by_profile` outcomes. Intentionally disabled stages do not fail the run. A required enabled stage that fails or is blocked produces the accurate partial/failed outcome; it is not converted to a successful skip. The raw profile runs RSS capture/admission only.

Freeze two separate typed sets before any paid/enrichment work: newly admitted article IDs and selected enrichment article IDs. All enabled downstream stages consume the enrichment set. They must not silently select every unembedded article or every event in the database. The shared snapshot contract still controls event accumulation, brief source membership, and retry reuse.

Add a paginated raw-article read using retained RSS fields: title, source, actual publication time or unknown, capture/admission time, bounded snippet, original URL, and grouping/summary state. It must work without an embedding model, model credentials, Redis-dependent AI runtime initialization, or an existing brief. Existing events, saves, and published briefs remain readable.

When an admitted raw article becomes grouped, Today shows the grouped event as its primary presentation and keeps the article available inside event details. It does not show a second independent “new article” card for the same record by default. Opening an existing article URL still works and identifies its event. Failed enrichment never causes duplicate admission or lost raw content.

Existing admitted raw articles may enter a later AI run only through an explicit transfer record and frozen enrichment scope. Select eligible previously admitted raw articles from currently enabled feeds first, by admission time then article UUID ascending, and then newly admitted articles in their recorded admission order, up to the separate enrichment ceiling of 100. Only intentionally disabled or budget-deferred raw articles from terminal runs are transferable, plus the explicit capacity deferral below. Never steal articles from an eligible failed-run retry scope; exhausted hard failures stay visible as raw articles with their error, without an implicit new paid retry. Recheck and claim transfer eligibility atomically when freezing the new enrichment set. Preserve the article's original publication/admission dates and show that it came from the existing backlog.

New admission still operates up to its own run/day ceiling when the enrichment set fills with older raw articles. New admitted articles left outside enrichment are readable immediately with `deferred_enrichment_capacity`; this intentional capacity deferral is transferable on a later assisted run, using the same recorded eligibility rules as budget deferral. Raw mode admits normally and selects zero articles for enrichment. No background catch-up job or unrestricted backlog embedding is introduced. A model call that is actually repeated consumes its own spending reservation; the article does not consume a second intake unit merely because its enrichment is retried.

## Shared paid-request ledger

The current completed-LLM-run monthly sum is insufficient for concurrent admission, embedding costs, in-flight requests, and unknown outcomes. Extend it with a workspace-wide durable reservation ledger in PostgreSQL. Use fixed-precision USD values, never binary floats for allowance decisions.

Every billable path uses this sequence, including reasoning, article embeddings, model/snapshot verification probes, enabled fallbacks, and provider retries:

1. Resolve the exact configured provider, model/snapshot, price-table revision, maximum input/output tokens, and finite request deadline. Persist the resolved route; no hidden vendor fallback is enabled by default. Unknown pricing or an unbounded maximum refuses dispatch.
2. Lock the workspace's applicable spending bucket and run reservation records. Count completed actual costs plus unreleased reservations. Atomically reserve the conservative maximum only if both run and month ceilings allow it. The effective run ceiling is the explicitly configured monthly allowance, or a configured lower per-run ceiling; disabled paid processing has a zero ceiling. This run ceiling never creates credit beyond the shared month's remaining allowance, and all attempts retain their logical run's accumulated charges/reservations.
3. Persist request identity and `dispatching` before network dispatch. A confirmed pre-dispatch cancellation releases its reservation. Once dispatch may have occurred, an ambiguous result is charged conservatively as pending reconciliation.
4. On a trustworthy result, persist provider request identity and usage, reconcile actual cost against the price revision, and release only the unused part. Confirmed provider non-billable errors can release their reservation. A timeout, connection loss after dispatch, or crash does not prove no charge.
5. Reconcile uncertain requests using recorded provider usage/receipt evidence when available. Without evidence, retain the reservation and show the unresolved amount; do not automatically expire it into free allowance. A retry creates a new request identity/reservation unless the provider guarantees replay of the same billed request.

Serialize request-dispatch accounting at the UTC month boundary: a reservation created in the previous month but not dispatched must be revalidated/moved into the current dispatch month under the same workspace lock before sending. Persist the authoritative dispatch-attempt timestamp immediately before sending; a crash in that handoff is an uncertain request in that bucket, not zero cost. Once dispatched, usage remains in that bucket even when reconciliation occurs next month.

Pricing is an implementation input to verify against the selected provider's official pricing when enabling that route, not a hard-coded assertion in this plan. The application allowance covers requests through this application. Record known prior current-month application usage when upgrading; an existing usage record whose cost cannot be established must be displayed as unreconciled and block enabling paid work until resolved. A previously configured sample budget or a populated API key alone is not permission to enable paid work.

The implemented ordinary v2 worker also requires the separate server setting `PERSONAL_PAID_RUNTIME_ENABLED`, which defaults to false and remains unset in this checkout. Persisted profile settings and credentials cannot open it. With otherwise valid AI configuration and allowance, a closed gate reports `paid_runtime_disabled` and performs bounded raw intake; those raw articles remain eligible for explicit enrichment transfer on a later assisted run. Readiness inspects the exact stored route without constructing providers. An open server gate still requires the frozen profile's explicit AI enablement, finite allowance, exact priced routes, credentials, and the durable pre-dispatch guard. Synthetic tests supply this gate only in isolated test Settings objects; real activation and recurring spending require separate authorization.

Budget exhaustion stops additional paid requests. Raw intake can still proceed within its separate article and capture limits. The reader shows remaining allowance, finalized cost, unresolved reservations, accounting period, and next reset. It must distinguish “AI disabled,” “configuration missing,” “allowance reached,” and “provider failed.”

## API and data obligations

Add or extend contracts in the existing API style; endpoint naming may follow repository conventions, but the following fields and behavior are required:

| Contract | Required behavior |
| --- | --- |
| Settings read/update | Validated profile revision, selected sources/interest, timezone, intake/run ceilings, AI enablement, configured route identifiers, spending ceiling, and whether changes apply now or to the next run. Secrets remain environment-only and are not returned. |
| Setup/readiness | Per-feature ready/disabled/blocked result and reason; raw reader readiness is independent of optional AI readiness. |
| Run/status | Stable run identity and snapshot/config revisions; observed/admitted/pending/enriched counts; bounded/truncated capture indicators; disabled/blocked/failed stage reasons. |
| Backlog | Paginated retained pending metadata and exact retained count; distinguish disabled-source pending items from currently eligible ones. |
| Raw articles | Bounded pagination, deterministic ordering, authentic source fields, grouping state/event link, errors/empty states. |
| Spending | Current UTC period, local reset instant, finalized USD cost, reserved/unresolved USD, remaining permitted USD, route/price revision metadata without credentials. |

`GET /api/v1/personal/spending` preserves the aggregate money/reset fields above and adds two credential-free metadata fields:

- `current_configuration` names the active `profile_revision_id`, numeric `profile_revision`, `route_mode`, and `routes`. Each route exposes only `role`, `provider`, `model`, `model_version`, and `price_revision`. No active profile is represented by null revision/mode values and an empty route list. A configured route can be shown even while AI is disabled; the existing `status` controls availability.
- `accounted_routes` identifies the frozen routes contributing to current-period finalized charges or reservations, plus unresolved paid requests retained from earlier periods. Entries carry `source` (`paid_request` or `legacy_usage`), UTC `accounting_period`, and the same route identifiers. They are deduplicated and sorted by period, source, role, provider, model, model version, and price revision, preserving different price revisions within one month. Older finalized requests, cancelled reservations, and confirmed non-billable requests contribute no current obligation and are omitted. Current-period legacy audits disclose their retained provider/model; role, model version, and price revision remain null because those facts were not authoritatively recorded.

Current settings never relabel historical charges. Arbitrary stored settings/route extras, credentials, provider request IDs, receipts, and evidence are not returned. Reading this metadata performs no import or reconciliation writes.

Add database uniqueness and locking invariants for canonical pending/admitted identity, one admission charge per article, run membership, and request identity. Keep receipts, allowances, reservations, and published snapshots durable across app restarts. Preserve existing article/event/report/watch-entry data. Changes are additive migrations; rollback disables new processing and uses the same retained database. Do not automatically downgrade/drop the new records or delete data to obtain a passing rollback.

## Acceptance evidence

| ID | Input/action | Required result |
| --- | --- | --- |
| P3-01 | Daily/run admission and enrichment ceilings 20; capture 30 distinct articles within the response/queue bounds. | Exactly 20 new admissions and 10 retained pending records; frozen enrichment scope has at most 20 articles. UI does not call pending or merely admitted records fully processed. |
| P3-02 | Replay captures, tracking-URL variants, duplicate feed items, and the same retry concurrently. | No duplicate article, admission charge, or run membership. A rolled-back admission does not reduce remaining allowance. |
| P3-03 | Feed A has 20 pending, B has 2, ceiling 4. | A1, B1, A2, B2 are selected using recorded within-feed order. Restarting does not reorder already admitted records. |
| P3-04 | Pending candidate disappears from its remote RSS feed before the next run. | Its retained content remains eligible; feed rotation does not delete it. Disabling/re-enabling its feed preserves it. |
| P3-05 | Oversized response, 501 entries, or full 2,000-record pending queue. | Each relevant bound produces an accurate capture status. No over-cap record insertion or fabricated total/deferred count occurs. |
| P3-06 | Retry spans local midnight; a second request supplies an old report date. | Run allowance/freeze persist; only genuinely new pre-freeze admissions use their actual local admission day. Caller dates cannot reset either allowance. |
| P3-07 | AI disabled, allowance exhausted, missing model configuration, or one feed fails. | Available raw articles and prior saved/brief data remain readable. Status distinguishes the cause; no disabled path dispatches a paid probe. |
| P3-08 | Remaining budget $0.03; two requests each need a $0.02 reservation. | Only one can reserve/dispatch. The other is blocked before dispatch, across both API/worker and different run dates. |
| P3-09 | Crash/timeout immediately after paid dispatch, followed by retry and restart. | First reservation remains unresolved; retry needs additional allowance. Known usage reconciles without double counting; unknown cost cannot become zero. |
| P3-10 | Reserve across a UTC month boundary, dispatch next month, complete later. | Request uses its dispatch-month bucket; local displayed reset is correct; no free request appears between buckets. |
| P3-11 | Populated upgrade with prior usage, saved events, reports, and ungrouped articles. | Records retain their identities. Known spend is imported once; unknown historical spend is disclosed before paid enablement. No unrestricted catch-up processing starts. |
| P3-12 | Process the same frozen fixture with default profile, raw profile, and a required-stage failure. | Disabled stages are explicitly disabled; failure remains partial/failed; successful default brief uses only the persisted eligible snapshot. |
| P3-13 | 100 transferable old raw articles and 100 fresh unique articles, with all default ceilings. | Admit 100 fresh articles, select the 100 eligible old articles for enrichment, and retain fresh articles as readable capacity-deferred raw records. No second admission charge for old records. Raw mode admits fresh records but selects zero enrichment IDs. |
| P3-14 | Old raw record belongs to a failed run that is still eligible for retry, or to an exhausted hard failure. | Later runs cannot silently take or retry it. Its existing raw content and failure reason remain visible. |

Use small configurable limits in tests to prove boundaries. Do not purchase model work simply to exercise a limit. Deterministic paid-provider adapters prove accounting; a configured live smoke run from Phase 2 supplies separate provider evidence. Record expected/actual counts, ledger states, browser states, and migration results in the phase evidence file before marking complete.

## Existing starting points and remaining boundary

- `services/ingestion/http_provider.py`, `service.py`, and `normalize.py`: live capture, current direct-to-article insertion, and canonical deduplication.
- `workers/ingestion_tasks.py`, `embedding_tasks.py`, `clustering_tasks.py`, and `pipeline_stages.py`: replace implicit/global backlog selection with the frozen personal scope.
- `services/pipeline/contracts.py`, `coordinator.py`, `sqlalchemy_store.py`, and `db/models/pipeline.py`: profile outcomes, run scope, receipts, and lifecycle.
- `services/llm/repository.py`, `orchestrator.py`, `pricing.py`, `runtime.py`, and `packages/config/settings.py`: extend completed-call accounting into shared durable reservations and explicit route configuration.
- `apps/api/intelligence.py`, `pipeline.py`, `deps.py`, and `frontend/app/`: personal settings, raw reading, spending, and accurate status presentation.

This phase still permits the original runtime services. Reducing them belongs to Phase 4. It does not add automatic scheduling, automatic deletion, full publisher-page archives, new paid providers, a new relevance model, or same-day successful reruns.
