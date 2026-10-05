# Phase 2 — Complete the personal workflow

**Implementation status: Complete, September 20, 2026.** Initial implementation used GPT-5.6 Sol at xhigh effort and independent GPT-6 Astra review at high effort; the concluding verification used the user-requested GPT-6 Astra at Ultra effort. See the [Phase 2 completion evidence](evidence/phase-2.md) and [orchestration history](evidence/phase-2-orchestration.md). This remains the implementation specification. Read [the shared contracts](00-shared-contracts.md) first; they control cross-phase dates, identity, scope, money, and compatibility. This phase follows [Phase 1](01-foundation.md) and precedes the unstarted [Phase 3](03-bounded-daily-use.md).

**Required follow-up from the functional-impact review:** [CORE-01](CORE_FOLLOW_UPS.md) is implemented and accepted. The production producer prepares supported claims from retained source spans; the corrected real-feed/model proof began without preloaded claims and published a substantive cited summary. Its exact report exports, ledger and cleanup are retained. A report containing only missing-evidence notes would not satisfy the ordinary non-quiet workflow.

## Finished outcome

The first usable personal MVP has three screens: **Today, Saved, and Briefs**. The user can request one daily update, open a grouped story and its original sources, save that event permanently, and read/export the brief built from that run's selected news. A restart preserves saves, processing results, input snapshots, and published briefs.

This phase retains the existing application technology and background services. A documented configuration supplies the initial feed/interest profile; the reusable settings interface, shared allowances, and raw-article fallback arrive in Phase 3. Forecasting, general chat, automatic scheduling, continuous intraday collection, note editing, and a separate regenerate button are outside this phase.

## Entry conditions and implementation order

Phase 1's installation, real/demo separation, authentic evidence display, and navigation changes must be verified before the complete browser demonstration. Backend work can begin against a disposable database while independent Phase 1 work finishes.

Implement in this order:

1. Add personal workspace, profile revision, logical-run, admission, snapshot, and report linkage contracts and migrations.
2. Finish event/source reads and Today navigation.
3. Add persistent save/unsave and Saved navigation.
4. Add personal run creation, status, retry, and exact coverage reporting.
5. Add personal selection and snapshot generation; connect Briefs, citations, history, and exports.
6. Verify the complete flow using real application components, then conduct the bounded live smoke test if live settings authorize it.

The initial feed IDs, interest phrases, live provider/model routes, and authorized live spending are configuration inputs. Inspect existing configuration first. If absent, use labeled offline fixtures while the user-dependent live inputs remain unset; do not infer a topic or treat a suggested budget as authorization.

## 2A. Establish personal identity without changing legacy meaning

### Workspace and owner

Create one persistent personal workspace with a UUID, bound local owner UUID, calendar timezone, and active profile revision. Use `America/Los_Angeles` as the explicit setup default from this environment. Persist it rather than reading the machine's current timezone on each run. Once a run exists, ordinary settings cannot change the workspace timezone.

Owner binding is idempotent:

- Reuse an existing recorded personal-workspace binding on subsequent startup/migration. Do not let a newly supplied configuration silently override it.
- At first setup, an explicitly configured existing owner wins after validation. Report the binding in the setup/upgrade summary.
- Without a binding/configured owner and with exactly one existing user, bind that user and report the mapping. The decision counts all existing users, including users without saved entries. Do not alter account fields or move entries.
- With no users, create a reserved local profile once. Its required email field uses a reserved `.invalid` address derived from the workspace UUID and is labeled as an internal local identifier, not a contact or signup.
- With multiple existing users and no binding/configured owner, setup displays the users with entry counts and requires an explicit choice before personal save/unsave writes. Do not infer the owner from which user has saves, guess, merge, reassign, or delete entries. Unrelated read-only checks can continue.

All preexisting saved entries remain accessible through a clearly labeled legacy saved-items view, including non-event targets and entries of owners other than the personal binding. Personal Saved uses only event targets belonging to the bound owner. No email is sent and no external account is created.

### Personal processing and report records

Add personal records alongside legacy records. Reuse the existing `Job` state machinery and processing services through a personal identity adapter; do not pass a personal date into the legacy New York daily-pipeline identity helper and claim they are equivalent.

The schema must enforce these invariants:

| Record | Required durable identity and content |
| --- | --- |
| Personal workspace | Workspace UUID, owner UUID (nullable only while explicit setup selection is pending), timezone, active profile revision, setup/migration provenance. |
| Profile revision | Immutable selected feed IDs, include/exclude phrases, relevant execution settings, schema revision, creation timestamp. New edits create a new revision for later runs. |
| Personal run | Run UUID, workspace UUID, local run date, profile revision, job linkage, actual capture timestamps, separately frozen newly admitted and enrichment article IDs, accumulated event IDs, snapshot/report links. Unique `(workspace_id, local_date)`. |
| Capture/admission records | Durable captured item identity and retained RSS input, source/fetch receipt, article linkage, deduplication identity, pending/admitted state, and the run that admitted the article. Scope is recorded before paid processing. Phase 3 extends the same records for backlog limits and fairness. |
| Run-event observation revisions | Immutable observation for each committed run grouping result: event/candidate identity, qualifying in-scope members, actual retained source-input revisions, profile/ranking inputs and observation time. Preserve nonsummarized candidates and observations from incomplete/failed runs. A terminal review manifest pins each candidate's last committed revision for the trial. |
| Brief input snapshot | Immutable run/profile identity; candidates, ranking values, selected events and member article IDs; bounded source text actually supplied; admission/enrichment identities, hashes and source references; capture/publication coverage; exact model/route and input-contract identifiers. |
| Personal report linkage | Report UUID to workspace, run, and input snapshot. Personal report version allocation is isolated by workspace/date. |

Use the job key `personal-daily:{workspaceUUID}:{localDate}`. A retry retains the same logical run and job key; its attempt number and delivery/ownership token change. A new day's run is a new logical run.

Personal reports use `report_type=personal_daily_brief`. Keep legacy report types, New York cutoffs, published rows, and existing identifiers unchanged. Adjust the current non-null-`brief_date` uniqueness index so legacy `(report_type, brief_date, version)` uniqueness continues to apply to legacy types, while personal uniqueness is `(workspace_id, brief_date, version)` in the personal namespace. Migration must validate linked personal rows and preserve legacy rows; it must not renumber old versions.

Legacy report list/latest/date/export routes exclude personal reports. Personal routes include only reports linked to the current workspace and the personal type. Report UUID is the identity for opening/exporting a particular version. Never resolve a personal export through an unqualified date-only query.

## 2B. Read and verify stories

Today defaults to the latest personal run with readable stored results and displays that run's date and actual capture times. A newer run that is still processing or failed without readable results appears in a separate status panel; it does not erase older readable news or relabel that news with the newer run date. If the displayed result is from an earlier date, label it as the latest available update. An optional run selector opens previous personal run results. An initial empty workspace shows the setup/update state.

Readable results include available qualifying groups from partial/running work, marked as incomplete, and a successfully prepared quiet snapshot with zero qualifying groups. A newer healthy quiet result displays its empty/quiet state instead of leaving older stories on screen as if they were new. A complete capture failure alone is not a quiet result. Show the latest successful update timestamp independently from the displayed result's date and any newer run's status.

Use the full deterministic ranking below for the list, without the brief's five-event limit; once its snapshot exists, retain its recorded candidate order. Before that snapshot exists, rank the currently available in-scope groups and label results as processing/incomplete.

The server provides filtered/paginated data. The browser refetches detail when an event opens, including when reached from Saved or an older run. It must not depend on the initial Today list containing that event.

Each detail presents:

- Current event title and available descriptive fields, saved state, and grouping/freshness status.
- Individual article title, publisher, known publication timestamp, available retained excerpt, original HTTP(S) URL, and source/article identity.
- For an event reached from a run, which members belong to that run's frozen enrichment scope. Other current event members may be shown separately as additional coverage; they are not silently added to that run's brief context.
- Honest missing states for missing publisher/time/excerpt/URL. A missing excerpt does not generate representative prose.

Source reads use the existing event/article/source relationships and do not require a published report. Use bounded reads with real totals/pagination rather than silently truncating membership. Article/source sort is known publication time descending, missing times last, then article UUID ascending. Original URLs are validated as HTTP(S); render source text as text, not executable markup.

The main list offers a text filter and a saved-only filter. The text filter applies the same normalization/token phrase matching described below to current event titles and retained member article titles/summaries; an empty query leaves the list unchanged. Filters narrow the server count, changing a filter resets pagination, and zero matches shows an empty state. There is no crisis-risk filter in the personal screen.

## 2C. Save and unsave persistently

Reuse `watchlist_items` with `item_type=event` and the stable event UUID as `item_id`. Resolve the owner on the server from the persisted workspace binding; callers do not choose a user ID for personal writes.

- `PUT` is idempotent: saving an existing valid event twice returns one saved entry. The database uniqueness constraint is the final defense against concurrent clicks.
- `DELETE` is idempotent: removing an absent entry succeeds without creating or modifying another entry.
- Validate the event for a new save. An unknown event returns not found; do not create a new phantom target.
- A previously saved event whose target is now unavailable remains listed with its retained label, saved timestamp, and an unavailable status. It can still be unsaved.
- Saving points to the event's stable identity and current data. It does not promise frozen event membership, an offline publisher archive, or automatic transfer to another event if grouping changes.
- Saved sorts by saved timestamp descending, then saved-entry UUID. Refreshing, filtering, and restarting do not change ownership or erase state.

While a mutation is pending, prevent duplicate UI requests and show pending status. Update visible saved state after the server confirms success. On failure, preserve the last confirmed state and display an error with retry; no success toast is emitted.

## 2D. Daily processing, scope, and recovery

### What each action means

| Action | Exact behavior |
| --- | --- |
| Reload display | Read stored data/status only. No RSS fetch, embedding, model call, or new processing run. |
| Run daily update | Server derives today's date in the persisted personal timezone, including weekends. Request has no arbitrary processing date. Create/enqueue one run, or return the existing queued/running/succeeded run for that date. |
| Retry | Address an existing run UUID. Resume only if the server declares it eligible. Keep its original local date, profile revision, frozen scopes, and snapshot if one exists—even after midnight. |
| Open previous run | Read-only history. It does not replay old feeds or grant historical processing. |

A succeeded date returns **Already processed** on another start request. A failed/partially failed run requires the explicit retry route rather than quietly restarting from the start button. Retain the existing three-attempt maximum and ownership rules in this phase; return eligibility and a reason from the server. Phase 4 changes execution transport while preserving this product contract.

Current captures and already durable pending items are the input sources. There is no automatic replay for missed days, promise of a complete 24-hour interval, or retrieval of entries already lost from an RSS feed. Display capture start/end, oldest/newest known publication timestamps, number of unknown publication times, and feed failures. Publication timestamps do not define run identity or spending resets.

### Scope freeze and retries

1. Persist the logical run and immutable profile revision before capture starts.
2. Persist each captured item/receipt and deduplicate it. Before admission freezes, a retry may complete failed free captures; successful receipts and candidates remain durable. Pending selection obeys the shared contract and becomes the Phase 3 bounded backlog selection without rewriting this lifecycle.
3. Freeze separate article sets for newly admitted input and enrichment work before enrichment or paid dispatch. The enrichment set may contain previously admitted ungrouped articles selected from the durable backlog, as well as newly admitted articles. All embeddings, grouping, extraction, and report preparation receive the explicit enrichment set; no stage processes the entire database backlog by default. Phase 3 enforces distinct admission and enrichment allowances; the fixed Phase 2 smoke capture uses the same at-most-three IDs for both sets.
4. A retry after admission freezes does not add newly fetched articles. It resumes unfinished work in its frozen enrichment set. Feed failures that cannot be completed without changing that scope remain visible for the next daily capture. Durable pending items stay pending.
5. Accumulate event IDs associated with its frozen enrichment work across attempts, including events updated rather than created. With each committed grouping result, persist an immutable run-event observation containing current candidate membership, retained source input/revision references, matching/ranking inputs, and observation time. Later membership changes append a revision rather than overwriting that evidence. Retain observations for all qualifying candidates, including those outside the top five and those produced before a run fails. Do not replace the stored event set with only the latest attempt's newly created events.
6. After grouping/context preparation, freeze the personal brief snapshot before the first composition request. Persist candidate/selected membership and source content in one consistent database view, referencing pinned observation revisions. Every composition retry reads that exact snapshot. At each terminal outcome, pin the latest committed observation revision per candidate in a review manifest; this permits Phase 5 to review a failed run's available groups even when composition/report-snapshot preparation never completed.
7. Late articles or later membership/text changes enter a later daily run's new snapshot. They do not silently change an existing snapshot or published report. There is no standalone user-triggered regeneration in this release.

Capture and admission depend on selected feeds and their bounds, not on interest phrases. Preserve admitted articles that do not match; matching filters the personal Today/brief results. Phase 3 can expose those retained articles through its raw-reading filter without recapturing or charging another admission.

If a report is already published and a later stage/terminal write fails, recovery links/reuses the published report instead of composing another copy. Before publication, failed generation can record another internal version against the same frozen snapshot. Published content is immutable; version history remains accessible.

### Personal execution profiles

The assisted profile enables RSS intake, article embeddings, clustering, and descriptive brief generation. Entity linking, event embeddings, and analogies are disabled by default. The raw profile enables intake/reading only; its finished raw-reading interface arrives in Phase 3. Use explicit disabled/deferred stage outcomes in the coordinator rather than treating a deliberately absent runner as an unexpected failure or abusing dependency-skip behavior.

Intentionally disabled, budget-deferred, or enrichment-capacity-deferred work yields completed processing with explicit limitations and transferable raw items. Phase 3 implements these deferral controls. Unexpected processing/evidence failures remain failed or partially failed. Another date cannot claim an active run's or retry-eligible failed run's frozen work. Retry exhaustion leaves raw material readable with the final error; it does not silently schedule another paid attempt under a new date.

Only one configured processing mode can write the shared dataset. Reject incompatible legacy/personal writer starts; permit legacy read/history. Mode switches require no queued/running writer. All personal stage writes preserve ownership checks inside the write transaction; adding personal APIs must not bypass the current ownership protection. Phase 4 changes the transport and tests the full shared deadline/lock contract.

### Status presented to the user

Return the actual `queued`, `running`, `succeeded`, `partially_failed`, or `failed` state; attempt/max-attempts; timestamps; last terminal stage results/counts; available event/snapshot/report IDs; sanitized failure codes/messages; and server-computed retry eligibility/reason. While active, poll every two seconds with one request in flight; stop on terminal state and refetch on focus/reload. A transient status-read failure preserves last known state as stale and does not imply the processing job failed.

Do not invent a percentage progress meter or stage detail the backend does not record. A failed feed with useful processed items is distinguishable from a wholly failed update. Zero matching news is distinguishable from missing data caused by failure.

## 2E. Exact personal selection and reproducible brief inputs

### Interest matching

The initial profile has explicit selected feed IDs plus at most 20 include and 20 exclude keyword phrases, each at most 80 characters. Normalize each phrase and article title/RSS summary with Unicode NFKC and casefold; tokenize into maximal Unicode letter/digit runs, treating punctuation/underscores as separators. A phrase matches a contiguous token sequence within either title or summary; do not match across the title/summary boundary. Reject configured phrases that normalize to zero tokens. Phrase limits and invalid values produce configuration validation errors rather than silent truncation.

At least one include phrase must match; an empty include list means all articles from selected feeds. Any matching exclude phrase wins. There is no semantic relevance model, automatic synonym expansion, substring matching inside tokens, or requirement that entity linking succeeds.

An event qualifies if it is linked to at least one article in its frozen enrichment set that matches that run's frozen profile. Brief input includes only matching in-scope members, even if the current event also has older or unrelated members.

### Ranking and selection

Rank qualifying events by this complete order, using values retained in the snapshot:

1. Distinct configured `Source.id` values among matching members in the frozen enrichment scope, descending. Label this as feed-source coverage, not independent publisher count: different feeds can share a publisher. Missing source identity contributes zero.
2. Event hotness score, descending; null scores last.
3. Newest known publication timestamp among matching in-scope members, descending; an event with no known publication timestamp sorts last at this comparison.
4. Event UUID, ascending.

Select up to five. Do not apply the legacy hotness floor of 40. Feed-source count/hotness order prioritizes reading; it is not a crisis severity or probability assertion. Record the ranking key for all candidates so selection is inspectable.

### Snapshot content and publication

Reuse existing bounded evidence excerpts and the exact text bounds enforced by `services/reports/context.py` and its repository. Persist the actual bounded text supplied to composition, not only article pointers/hashes. Record source URL/publisher/publication metadata, matched article IDs, claim/evidence references, truncation flags, selected/candidate IDs and ranking values, profile revision, capture times, preparation time, and model/route/input-contract identifiers. Also retain the normalized title/summary inputs used for interest matching under the capture-storage bounds.

Adapt the generation entry point to consume this personal snapshot. Do not reuse the legacy 05:30 New York event-window query as personal selection. Reuse composition, grounding, consistency/copyright checks, descriptive content policy, immutable published lifecycle, and export rendering where their contracts apply. Every claim admitted to personal context must resolve through supportive evidence to the selected matching article scope. Existing output authorization and prediction gates remain in force.

New snapshots freeze `composition_policy=personal_descriptive.v1` inside the hashed input payload. This policy writes source-attributed descriptions from the frozen citable claims only; personal executive/event prompts receive no market-opening instructions, risk/alert tables, hotness/risk metadata, or prior-market narrative. Missing observations mean unknown, never stability or an all-clear. Explain significance only when a citable claim supports it. Separate personal prompt names and a `personal_descriptive.v1` prompt version identify this behavior in model audit records. The same immutable policy applies to initial composition, the one budget-feedback retry, and any grounding regeneration. Snapshots that predate the policy field retain `legacy_daily_brief.v1`; unknown or malformed identities fail before model dispatch. Existing snapshots and published reports are not rewritten.

Personal length targets guide concision: about 60 words for the executive summary and 90 for each event when the supported information warrants it. They are not minimums. A short supported description is valid for sparse evidence; do not repeat a claim or add boilerplate to fill space. Hard ceilings remain 120 and 180 words respectively across all blocks, using the shared deterministic word counter. Prose must be non-empty (a one-word formatting floor is not a substantive-quality judgment); absent evidence and abstention retain explicit missing/degraded behavior. Persistent empty/over-limit output gets at most one budget retry and fails personal processing. Failed or budget-missed composition during grounding regeneration also prevents personal publication. Schema validation, citation whitelists, supportive source links, semantic grounding, copyright thresholds, and the publication gate remain enforced. The live CORE-01 proof still requires an intelligible substantive supported summary, not merely a positive word count. Legacy narrative budgets and prompts remain unchanged.

An update with no qualifying events has a deterministic published quiet brief containing coverage and data-quality information, with no fabricated summary and no unnecessary composition call. It has a normal report UUID and can be read/exported. Where qualifying events exist but supporting evidence is insufficient, preserve the existing honest abstention/degraded-content behavior and mark that limitation. A processing failure cannot be relabeled as a successful quiet day.

Briefs lists personal reports by local date descending and version descending. Published reports open by UUID; versions retain their status and input snapshot linkage. A failed latest attempt does not displace the previous published report. Failed/generating versions have status/error information but do not export as published content. Do not promise byte-identical text from a fresh model call; reproducibility here means identifiable preserved inputs and immutable stored outputs.

Citation drawers read the selected report/version, not the latest global report. Markdown/PDF exports use that exact published report UUID and snapshot attribution, and match the displayed title, coverage, sections, and citations. Missing or unauthorized reports return not found; there is no fallback to a different date/version.

## API contract to implement

These are new personal routes; the existing APIs are starting points to reuse internally, not claims that the new routes already exist. All mutations follow the application's existing local binding, authentication/key, and request-protection middleware. The plan does not add an exposed public service or bypass current checks.

| Route | Required behavior |
| --- | --- |
| `GET /api/v1/personal/workspace` | Setup state, workspace/timezone/profile identity, owner-selection requirement where applicable, latest run, and available actions. Never return credentials. |
| `PUT /api/v1/personal/workspace/owner` | Body contains an existing `owner_id`. Validate and bind atomically; 200 for that already-bound owner, 409 for a different owner after binding with an explicit future-migration requirement. GET workspace supplies safe existing-owner choices during ambiguous setup. |
| `GET /api/v1/personal/events` | Current-workspace personal run scope, optional run UUID/text/saved filter, bounded limit/offset, actual filtered total, current saved state, freshness metadata. Default latest run with readable stored results; report a newer run's status separately. |
| `GET /api/v1/personal/events/{event_id}` | Current event detail independent of current Today page, saved state, membership totals, and descriptive availability. Optional run UUID gives in-scope attribution. |
| `GET /api/v1/personal/events/{event_id}/sources` | Bounded paginated article/source rows, actual total, known/missing fields, membership attribution for an optional run UUID. |
| `GET /api/v1/personal/saved` | Bound owner's saved event entries, including unavailable targets; limit/offset and total. |
| `PUT /api/v1/personal/saved/{event_id}` | Idempotent save. Return confirmed saved entry/state; 404 for a new unknown target. |
| `DELETE /api/v1/personal/saved/{event_id}` | Idempotent unsave; 204 after confirmed absence. |
| `POST /api/v1/personal/runs` | Empty request body; no date override. 202 for newly queued/active work, 200 for existing success, 409 with run/status link when explicit retry is required. |
| `GET /api/v1/personal/runs` | Read-only bounded run history, newest local date first; default 50, maximum 100. |
| `GET /api/v1/personal/runs/{run_id}` | Durable state, coverage, counts, result/error, available outputs, retry eligibility/reason. 404 outside this workspace. |
| `POST /api/v1/personal/runs/{run_id}/retry` | Explicit eligible recovery; 202 when queued, 409 with reason otherwise. Never accepts a changed date/profile/scope. |
| `GET /api/v1/personal/briefs` | Bounded personal report/version history with status; default 50, maximum 100. |
| `GET /api/v1/personal/briefs/{report_id}` | Selected personal version, immutable content when published, snapshot/coverage and citations. |
| `GET /api/v1/personal/briefs/{report_id}/claims/{claim_id}/evidence` | Check workspace, published descriptive policy, and claim membership in that exact report; then return bounded evidence through existing evidence repository logic. |
| `GET /api/v1/personal/briefs/{report_id}/export.md` and `/export.pdf` | Export only this published, authorized personal version; otherwise 404. |

Use the new report-scoped personal evidence route for citation drawers. It reuses the existing authorized evidence repository and serialization after checking claim membership in the selected published personal report. Do not widen the global endpoint's read permissions. All new list endpoints use default 50/max 100 unless specified above, validate inverted/invalid ranges as 422, and use stable ordering. Bad UUIDs/configuration return validation errors, not empty-success responses.

## Concrete examples and acceptance checks

| ID | Case | Input/action | Required result |
| --- | --- | --- | --- |
| P2-01 | Calendar and cutoff | Sunday, September 6, 2026 at 14:00 Los Angeles; a current capture contains a matching story published after 05:30 New York. | Local run date is September 6; weekend is accepted; the article can qualify. Personal selection never excludes it solely because of the legacy cutoff. |
| P2-02 | Midnight retry | September 6 run fails; retry at 00:05 Los Angeles on September 7. | Same September 6 run/job/profile and frozen scopes; incremented attempt. It is not a September 7 run and creates no independent allowance. |
| P2-03 | Successful duplicate | Two start requests for the same date; second request again after success. | Exactly one logical run. Second active request returns its status; later request says Already processed. |
| P2-04 | Latest readable result | Yesterday has readable events; today's run fails before producing any. | Yesterday's dated events remain available; today's failure is shown separately. A later healthy quiet result shows quiet/empty, not yesterday's stories relabeled as today. |
| P2-05 | Interest rules | Include `interest rate`, exclude `opinion`; title `INTEREST-RATE decision`, summary contains `Opinion`. | Exclude wins. `interest-rate decision` without the exclusion matches; `interested rates` does not. Empty include accepts selected-feed articles subject to exclusions. |
| P2-06 | Scope isolation | An event has one matching in-scope enrichment article and two older/unmatched members. | Event qualifies, but the two other members do not enter feed-source-count ranking or brief context. Detail identifies additional current coverage separately. |
| P2-07 | Ranking | Event A: 3 configured source IDs, hotness 10; B: 2 source IDs, hotness 99; C: 3 source IDs, null hotness. | A ranks before C, C before B. Equal later fields resolve by UUID. A score below 40 does not disqualify an event. |
| P2-08 | Frozen input | Capture/admit article A, freeze snapshot, then update A's stored summary and attach article B to the event before retry. | Retry composition reads the saved A snapshot and excludes B. A later daily snapshot may include new material. |
| P2-09 | Review evidence before composition | Group seven qualifying events, then fail before report-snapshot preparation. | All seven committed candidate observations and their source inputs remain reviewable through the terminal manifest, including the two that would fall outside a five-event brief. No successful report is required to preserve that evidence. |
| P2-10 | Sources before briefs | Database has an event and articles but no report. | Open detail and source URL successfully; missing excerpt shows missing state. |
| P2-11 | Saved persistence | Save twice, reload, restart, open Saved, unsave twice. | One entry after saves and restart; none after deletes; failures never falsely claim a write succeeded. |
| P2-12 | Missing saved target | Existing saved entry points to an unavailable event. | Entry remains visible and removable; no silent reassignment or deletion. |
| P2-13 | Populated migration | Existing legacy daily reports and multiple owners' saved entries are present. | No content/version/ownership changes. Ambiguous personal owner blocks personal save/unsave until binding; setup selection and other independent work remain available. Legacy and personal lists do not mix. |
| P2-14 | Owner without saves | Two users exist, but only one has saved entries; no workspace binding/configured owner exists. | Setup still requires explicit owner selection. Saved-item counts do not implicitly choose the owner. |
| P2-15 | Admission versus interest | Two selected-feed articles are eligible for admission; one matches the profile and one does not. | Both can be admitted within the limits. Only matching in-scope material qualifies for personal Today/brief; Phase 3 can show the other article in raw reading. |
| P2-16 | Export identity | Open an earlier published personal report; a newer attempt has failed. | UI, evidence, and both exports refer to the earlier report UUID; failed version cannot export or replace it. |
| P2-17 | Quiet versus failed | Healthy capture has no matching events; separately, all feeds fail. | First is honest quiet coverage; second names capture failure. Neither inserts sample stories. |

Use unit tests for the deterministic date/matching/ranking/snapshot rules; database integration tests for uniqueness, admission/retry persistence, populated migration, report identity and saves; and browser checks for the full user path/error states. Use production selection/generation code in the complete-flow integration test. The existing integration shortcut that inserts a report directly does not satisfy this phase.

## Bounded live smoke verification

The live smoke test is a separate evidence requirement after deterministic/captured-feed checks pass. Use a disposable database, no scheduler, no unrelated backlog, and an explicitly listed fixed capture containing at most three real RSS articles. Use the actual configured provider/model route only when a user-configured live ceiling authorizes that route. Paid dispatch defaults to zero until a live route and authorized live ceiling are configured. The limits below are technical ceilings, not authorization to spend.

The harness must reserve and count every billable request, including embeddings, extraction/reasoning, model-verification probes, fallbacks, and retries:

- At most 20 billable dispatches in the entire smoke run.
- At most $0.25 in cumulative conservative reservations, or the lower actual authorized configured cap.
- Each reasoning/probe request: at most 8,192 input tokens and 4,096 requested output tokens.
- Each embedding dispatch: at most 8,192 total input tokens and no more than the three captured articles in its batch.
- Reject oversized requests before dispatch. Retain an uncertain dispatch's reservation; retries consume another dispatch/reservation. Unknown price or an unbounded paid route prevents that route from running. No automatic increase in any ceiling.

Record provider/model, price-source version, exact caps, sanitized input identities, usage/reservations, capture/snapshot/report IDs, and outcome. If a cap prevents completion, record a bounded failure and investigate; do not label the live proof passed or silently widen the test. The real runtime's grouping/selection/report construction must execute—replacing it with a prewritten brief invalidates this proof.

The Phase 3 application-wide daily/monthly ledger is separate work. This one-run harness establishes a finite verification boundary while Phase 2 is developed.

## Files and service boundaries to use

| Existing area | Planned work |
| --- | --- |
| `db/models/core.py`, `db/models/pipeline.py`, migrations | Add personal linkage/records and uniqueness; preserve users, watches, legacy jobs/reports. |
| `apps/api/intelligence.py`, `apps/api/pipeline.py` | Reuse repository/serialization/auth patterns; add personal routers and report-scoped reads. |
| `services/pipeline/contracts.py`, `services/pipeline/sqlalchemy_store.py`, `workers/pipeline_stages.py` | Introduce personal identity/scope adapters; carry frozen article/event/snapshot scope through retries; retain existing ownership protection. |
| `services/reports/selection.py`, `repository.py`, `context.py`, `context_repository.py`, `generation.py`, `lifecycle.py`, `exports.py` | Add personal snapshot selection/generation path and version namespace; reuse evidence/grounding/export behavior where applicable. |
| `frontend/app/api-adapter.js`, `data.js`, `page-events.jsx`, `page-event-detail.jsx`, `page-misc.jsx`, `main.jsx`, `shell.jsx` | Actual personal reads/mutations and refreshing; Today/Saved/Briefs; coverage/status/errors and persistent saved state. |
| `tests/integration/test_daily_pipeline_vertical_slice.py` and new personal integration tests | Preserve legacy coverage and add a complete personal flow that does not inject a report row as its generation stage. |

## Closure evidence

Record migration results for both blank and populated disposable databases; relevant unit/integration/frontend results; the browser sequence from update through source inspection, save, restart, brief, and export; failure/retry/quiet-day examples; and the bounded live smoke outcome. Link evidence in the package status record. Declare any remaining limitation explicitly.

Phase 2 closes only when the entire personal workflow is verified and the required live proof is available. If live configuration/authorization is absent, software and offline verification may finish, but the live-evidence item remains pending. This does not prevent independent documentation or Phase 3 design work.
