# Potential expansions after personal conversion

Recorded September 6, 2026, following the review of functionality affected by the conversion plan.

**Status: recorded candidates; none selected, scheduled, or implemented.** This backlog preserves the capabilities and trade-offs discussed with the user. It does not activate features, authorize spending, or add every item to the current conversion's completion requirements. The [conversion plan](../README.md) and [shared contracts](../00-shared-contracts.md) remain the baseline until an expansion is deliberately adopted.

Entries distinguish implemented capabilities that personal mode would deactivate, deliberate behavior changes, and new enhancements. A proposed future approach is not a finalized implementation decision. Dependencies describe what must exist before an expansion can be verified; they are not a new phase schedule.

The fresh-article claim/evidence gap is recorded separately as [required core follow-up](../CORE_FOLLOW_UPS.md). It must be addressed in Phase 2 and must not be deferred as an optional expansion.

## Coverage of the functionality review

| Review point | Expansion entry | Relationship to the conversion |
| --- | --- | --- |
| Narrower news collection | EXP-01 — Broader collection | Expand verified source/workload coverage. |
| Grouping still depends on article embeddings and available budget | EXP-02 — More flexible grouping | Enhance a retained feature; ordinary grouping is not being removed. |
| Automatic company/entity linking disabled | EXP-03 — Entity enrichment | Restore an implemented optional analysis stage. |
| Historical comparisons disabled | EXP-04 — Historical comparisons | Restore event embeddings and analogy processing. |
| Keyword relevance and different ranking | EXP-05 — Richer relevance and prioritization | Extend/replace the deliberately simplified selection rules. |
| Narrower brief context | EXP-06 — Broader brief context | Add bounded background and contrary evidence. |
| Once-per-date updates and no regenerate action | EXP-07 — Intraday updates and regeneration | Add explicit run/version behavior beyond daily v1. |
| Existing automation inactive in personal mode | EXP-08 — Unattended operation | Reintroduce selected scheduled operations under personal controls. |
| RSS-only fallback lacks new AI analysis | EXP-09 — More capable operation without paid AI | Explore additional offline/local reading and summary capabilities. |
| Forecasting distinction | EXP-10 — Forecasting research | Future research, not restoration of a validated production capability. |

## EXP-01 — Broader source coverage and collection capacity

**Baseline/change:** Personal v1 starts with at most 10 active feeds, 100 new articles per run/day, 100 enrichments per run, and bounded response/pending storage. Those editable limits narrow the coverage of the broader platform. Retained pending articles survive feed rotation; items never captured can still disappear remotely.

**Potential expansion:** Increase the verified feed and article capacity, improve capture fairness for feeds that rotate rapidly, and provide clearer per-source freshness and backlog controls. Raising a limit within an already verified range may be ordinary configuration; capacity beyond that range needs evidence before becoming a supported setting.

**Outcome:** More of the user's chosen news can be collected and processed within a known resource envelope, with visible delays and coverage gaps.

**Dependencies/trade-offs:** Phase 3 admission, pending-queue and spending controls; Phase 4 execution deadlines; measured storage, processing time and provider usage. More collection does not guarantee historical recovery. A true backfill feature would require an appropriate retained or archival source rather than pretending current RSS contains missing history.

**Completion evidence:** Exercise the proposed larger workload across uneven feeds, restarts and a full queue; demonstrate fair capture, exact retained counts, no duplicate admission, intact spending limits, and accurate missing-coverage messages.

## EXP-02 — More flexible grouping with less external-model dependence

**Baseline/change:** Article embeddings and clustering remain part of the assisted personal core. They require a compatible model route and available allowance. Disabling event embeddings for analogies does not remove article grouping.

**Potential expansion:** Evaluate a local article-embedding route or a bounded simpler grouping fallback. These are candidate approaches to choose after quality/resource comparison, not two features automatically committed for implementation.

**Outcome:** Related articles can potentially be grouped when the usual external embedding route is unavailable or unsuitable.

**Dependencies/trade-offs:** Preserve model/snapshot identity, clustering scope, saved-event identities and retry ownership. A different model can change grouping quality and event membership. Local computation can require downloads, memory and processing time; it is not inherently costless or equally accurate.

**Completion evidence:** Compare candidate grouping against the assisted baseline on the same retained inputs; inspect incorrect merges and duplicate splits; record latency/resources; verify fallback labels, event/source access, migration compatibility and no hidden paid request.

## EXP-03 — Optional company and entity enrichment

**Baseline/change:** Personal v1 disables automatic entity linking. Existing normalized links remain stored, but newly processed events will not receive the same company/entity relationships and related descriptive exposure enrichment. This is an implemented service capability, not merely a placeholder screen.

**Potential expansion:** Restore entity linking as a selectable stage for chosen topics or companies, and expose useful linked-entity navigation where supported by real results.

**Outcome:** The user can follow the same company/entity across differently worded articles and inspect the relationships attached to an event.

**Dependencies/trade-offs:** Existing entity-linking services, identity reference data, required extraction models, quality thresholds, and personal spending/run controls. Existing links need freshness indicators; new linking must not silently rewrite historical report evidence or another owner's saves.

**Completion evidence:** Review real names, aliases, ambiguous mentions and incorrect matches; show provenance and unavailable states; demonstrate optional-stage failure without losing core reading or a correctly supported brief. Record required setup and processing cost.

## EXP-04 — Optional historical comparisons

**Baseline/change:** Personal runs do not create event embeddings or retrieve/rank historical analogies by default. Article grouping remains separate and active in assisted mode.

**Potential expansion:** Restore event embeddings and historical-episode retrieval/ranking as an optional analysis stage, with a bounded number of evidence-linked parallels.

**Outcome:** A reader can inspect earlier episodes that may help explain a current development, including relevant differences and contrary examples.

**Dependencies/trade-offs:** Existing analogy services and evaluation boundaries, usable historical episodes, compatible event-embedding snapshots, and shared request accounting. A similarity result is not evidence that the same outcome will recur.

**Completion evidence:** Inspect useful and misleading parallels on retained real examples; preserve episode dates, source references, limitations and counterexamples; demonstrate an honest no-reliable-comparison result. Do not open forecasting gates as a side effect.

## EXP-05 — Richer relevance and configurable prioritization

**Baseline/change:** Personal v1 uses explicit keyword phrases, then orders qualifying groups by feed-source count, hotness, publication time and stable ID. It removes the legacy hotness floor and risk-weighted ordering. Interests filter Today/Briefs, not capture/admission. Both legacy top-event selection and the proposed personal brief already use a five-event limit; five itself is not a newly introduced loss.

**Potential expansion:** Evaluate synonym/semantic matching, company-aware interests after EXP-03, and explicit ranking choices such as recency, source coverage or supported descriptive impact. A larger brief selection could be a further configurable extension if useful.

**Outcome:** Relevant developments expressed indirectly can be surfaced, and prioritization can better reflect the user's reading purpose.

**Dependencies/trade-offs:** A representative relevance sample and the user's judgments, reproducible selection versions, finite processing cost, and source-quality information. Descriptive risk indicators must be labeled separately from validated forecast probabilities; predictive ranking requires the relevant research/release requirements in EXP-10.

**Completion evidence:** Compare missed relevant stories and irrelevant inclusions against keyword v1 using the same population; show why a story matched/ranked; record all model/rule changes in run snapshots. Changing selection must not mutate older briefs.

## EXP-06 — Broader brief context with background and contrary evidence

**Baseline/change:** Personal briefs use only matching articles within their run's frozen enrichment scope. Older or unmatched event members can remain visible in event detail while being excluded from the brief. This can omit useful chronology, qualifications or counterevidence.

**Potential expansion:** Add a bounded context-selection step for relevant background, corrections and contrary evidence from other available event members. Specify how it interacts with interest exclusions before implementation; do not silently ignore the user's filters.

**Outcome:** A brief can explain what changed and represent conflicting accounts while making the age and purpose of each added source clear.

**Dependencies/trade-offs:** The required claim/evidence preparation in CORE-01, immutable source observations, explicit source/time bounds, and grounding checks. Additional context raises processing cost and risks mixing old information with current developments.

**Completion evidence:** Use examples containing a prior event, a new update, a correction and a contradictory account. Demonstrate correct chronology, attribution, inclusion reasons, conflict handling and all-claim source support. Reopening/exporting an older report must retain its original context.

## EXP-07 — Multiple daily updates and explicit brief regeneration

**Baseline/change:** Personal v1 permits one successful run per local date, explicit eligible failure retries, and read-only display reloads. It has no standalone regenerate action. Later breaking stories and profile edits do not enter a completed day's frozen inputs.

**Potential expansion:** Support multiple explicit updates within one day and a named regeneration action. Specify two distinct operations: retry/recompose using preserved inputs, and create a new version from newly captured or newly selected inputs. Never disguise new paid work as a free display reload.

**Outcome:** The user can incorporate later developments or a changed interest profile without waiting for the next calendar date, while retaining previous reports and their evidence.

**Dependencies/trade-offs:** New run/version identities beyond unique workspace/date, migration of selectors, durable input snapshots, admission/spending periods based on real timestamps, and one processing owner. More frequent requests increase processing and spending; no new allowance is granted merely by creating another run ID.

**Completion evidence:** Process two same-date updates with late-arriving news, retry one, regenerate from both unchanged and new inputs, and cross midnight. Show distinct identities, no duplicate admission, unchanged prior versions, correct citations/exports, and enforced shared allowances.

## EXP-08 — Optional unattended updates and notifications

**Baseline/change:** Personal mode is manual and rejects incompatible legacy writers. Existing scheduled daily briefs, identity refreshes and pending-notification redelivery are real implemented paths that do not continue alongside personal processing.

**Potential expansion:** Selectively introduce personal scheduling for collection/briefs, reference-identity refresh where EXP-03 needs it, and explicitly configured notifications/redelivery. Record these as separate enablement choices; enabling one must not enable the others. More than one successful update per date depends on EXP-07.

**Outcome:** The selected workflow can run at a chosen time and report useful results or meaningful failures without manual initiation.

**Dependencies/trade-offs:** Personal ownership, source scope, spending, cancellation and mode controls; explicit timezone/missed-run behavior; deduplication of notification delivery; configured destinations and authorization for external messages. Simply restarting the old scheduler is not a compatible implementation. Always-on or wake-up operation adds maintenance and resource demands.

**Completion evidence:** Demonstrate missed schedules, offline periods, daylight-saving changes, competing manual starts, duplicate triggers and provider failure. Verify at-most-once logical work where specified, honest retry/delivery status, no notification without configured authorization, and no allowance bypass by scheduled jobs.

## EXP-09 — More capable operation without paid AI

**Baseline/change:** RSS-only mode preserves article reading, source links, existing saves and published briefs. It does not produce new assisted groupings or generated summaries. A completed raw run is not equivalent to delivery of an AI brief.

**Potential expansion:** Evaluate local summary generation or a clearly labeled extractive digest built from permitted retained source text. Reuse EXP-02 if grouping is needed. Choose and validate an approach rather than assuming a local model is available or that excerpts equal verified generated analysis.

**Outcome:** A user with no paid route can potentially obtain a structured daily reading aid while keeping original-source access.

**Dependencies/trade-offs:** CORE-01 for any feature promising claim-supported generated summaries; storage/content limits; model installation/resources if local generation is chosen. Keep raw articles usable when the optional local processing fails. No automatic switch to a paid provider is permitted.

**Completion evidence:** Run with paid routes unavailable and confirm zero paid dispatches; inspect factual support, truncation and labels; measure machine resources and latency; distinguish original quotations, extractive selections and generated text in the UI/export.

## EXP-10 — Forecasting and composite-alert research

**Baseline/change:** The current daily pipeline already excludes crisis predictions, predictive rollups and composite alerts. Their research code does not establish validated forecasting performance. The conversion retains these restrictions, so this entry is not a promise to restore an already validated product.

**Potential expansion:** Pursue a separate bounded research question with one defined outcome, geography/domain, horizon and baseline. Preserve existing evaluation artifacts and release gates. Integrating any resulting output into the personal reader is a later decision.

**Outcome:** A reproducible assessment of whether the selected forecasting approach adds value over its baseline; a well-supported negative result can finish the research work.

**Dependencies/trade-offs:** Appropriate historical data, time-correct inputs, out-of-sample evaluation, calibration and the applicable existing review/release requirements. Operational notification capability in EXP-08 does not establish predictive validity.

**Completion evidence:** Reproducible held-out results, baseline comparison, error/calibration analysis and documented limitations. Research completion and authorization to present predictions are separate outcomes; no gate opens merely because the personal reader passes its trial.

## Rules for adopting an expansion

1. Identify the user benefit and select the relevant entry. All entries currently remain potential; no priority or delivery date has been assigned.
2. Specify the chosen behavior, configuration, migration and acceptance evidence before implementation. Update affected shared contracts explicitly. Retained optional code is not automatically ready for personal-mode activation.
3. Preserve existing articles, events, relationships, saved entries, report versions, source snapshots, run history and cost reservations. Use additive migrations; never silently rewrite published material or delete history to enable an expansion.
4. Keep every processing path under the supported ownership, mode, input-scope and spending controls. Preserve the simpler reader when an optional capability is disabled or fails. New paid or external-message behavior requires the actual applicable configuration/authorization.
5. Verify with relevant retained examples and actual operation, recording limitations and resources. A supported rollback uses an idle mode switch or a separate validated backup; it does not permit destructive migration or competing unguarded legacy writers.

The register is documentation only. Core conversion implementation, expansion implementation, live tests, paid requests and external notifications have not been performed by saving it.
