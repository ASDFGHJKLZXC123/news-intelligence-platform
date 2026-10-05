# Phase 5 — Validate usefulness and finish the personal release

**Implementation status: In progress — trial preparation began October 4, 2026 (America/Los_Angeles).** The user selected raw reading first with model spending off and requested a new ordinary-reading baseline log. No actual trial sessions, human judgments, or quality scores have been performed. The acceptance requirements below remain unchanged. Use the [trial guide](PHASE5_TRIAL_GUIDE.md) and [Phase 5 evidence record](evidence/phase-5.md). Original contract revised September 6, 2026. See the [package index](README.md) for scope and phase order and the [shared contracts](00-shared-contracts.md) for cross-phase behavior.

## Outcome and entry conditions

A dated release assessment states what worked during actual personal use, whether the news was useful, what it cost, and what remains wrong. It includes reproducible examples and a setup guide for the finished personal runtime.

Begin a release trial after the functional workflow, allowances, and simplified runtime pass their earlier phase checks. Exploratory reading can begin earlier, but it is not silently substituted for this release candidate's evidence. Record the application revision, database migration state, profile revision, enabled features, providers/models, price-table version, and configured allowances before the trial. Do not expose credentials in the record.

These numerical targets are explicit proposed engineering acceptance defaults, not preferences supplied by the user or established statistical guarantees. They measure this personal reader, not financial forecasting.

## Trial record

Use seven actual reading sessions on seven distinct processing dates in the persisted personal calendar, initially `America/Los_Angeles` under the shared contract. Start with consecutive calendar dates where practical. A missed session is recorded as missed; do not manufacture one from a background or retrospective test. Extend the calendar period until seven actual sessions exist, while retaining the gaps in the log. Quiet days with genuine no-news results count as actual sessions if the user opens the application and the supported workflow completes honestly. A retry across midnight still belongs to its original run/session for reliability counting; it does not produce a second trial session or grant a second successful run for that date.

Each session records:

- Session ID, calendar date, start/end time, application revision, and frozen profile reference.
- Logical processing run and attempt IDs, requested date, actual collection/coverage times, terminal outcome, and eligible retry actions taken.
- Feed successes/failures, candidate/admitted/deferred article counts, grouped/raw item counts, and generated brief ID/version or the exact no-brief reason.
- New reservations, settled and uncertain estimated charges, remaining allowance, and any enforced refusal.
- Whether source inspection, saving/reopening, brief reading, and export were exercised, including failures and preserved data.
- User reading time, useful stories saved/noted, sources opened, and any maintenance/developer intervention.
- Links to failure records and corrections. Never remove a session because it produced poor news or failed.

Record raw outcomes independently from whether a retry later succeeds. An ordinary retry means the user uses the documented interface or command once an eligible failure appears, without editing data, changing code, restarting individual internals manually, or receiving custom repair instructions. Such a retry can remain a session without developer intervention; log its count and additional time. Unsupported code, database, or process repair is developer intervention. Starting/stopping the application through its documented launcher is ordinary operation.

A session “completes” when its configured workflow reaches a valid supported outcome and available material can be read. An honest quiet result or intentionally disabled enrichment is valid. The documented raw/limited mode also counts when a limit leaves usable current articles and the screen clearly explains the omitted work; record it separately from full briefing success. A feed error with usable current results, clear source failure and a documented recovery action can count as a usable session without developer intervention, while its actual partially failed backend state stays unchanged. An unexpected failure outside those supported outcomes, a brief missing despite being enabled and funded without a recorded reason, a lost saved item, or a stuck run is not a completed session merely because some older news is readable. A budget refusal is never counted as successful delivery of a brief that was not generated.

## Deterministic sample selection

Create the sample manifest from the recorded run/report snapshots before the engineer or user judges quality. Sample selection must not inspect titles, apparent relevance, grouping quality, or summary wording.

1. Assign a fixed trial identifier when the record is created. Record `sampling_method = sha256-round-robin-v1`.
2. At each session's close, record all qualifying grouped event candidates available for its run, including every result page and inconvenient or failed-quality examples. This is the full Today candidate population, not only the at-most-five events selected for each brief or the cards the user clicked. Raw ungrouped articles, legacy history outside the trial, and nonmatching events reached through other filters are outside this population. Each qualifying event's stable ID appears once, assigned to its earliest eligible trial session. Retain that session's frozen in-scope member article IDs and permitted evidence fields for review; if only an incomplete pre-snapshot group was available, record its observed membership explicitly instead of pretending it has a completed brief snapshot. Document exclusions by structural reason, never by apparent quality.
3. Within each session's group population, sort by the hexadecimal SHA-256 of the UTF-8 string `trial_id + "|group|" + event_id`; break any hash tie by stable event ID in ascending order.
4. Visit sessions from oldest to newest in rounds, taking one remaining group from each nonempty session queue. Stop at 30 distinct groups. Empty day queues are skipped without replacing the day with synthetic material.
5. Build the brief-summary population separately from successfully published personal brief versions available at each trial session's close. A unit is one selected event's rendered summary, its heading, and its complete associated claim/citation material; include any report introduction/conclusion claims attributed to that event. Use the earliest published trial version that includes an event, ordered by publication timestamp, numeric version, then report UUID, with at most one unit per stable event ID. Preserve report ID/version, snapshot ID, event ID, and source references. A failed attempt, quiet-period message, raw article, report-wide heading without an event summary, or repeated version of the same event is not another summary unit.
6. Apply the same day-balanced round-robin selection to 10 summary units, sorting within each session by SHA-256 of `trial_id + "|summary|" + report_id + "|" + version + "|" + event_id`, with the full unit identity as the final tie-break.
7. If the seven sessions supply fewer than 30 groups or 10 summaries, freeze the selected units so far and append more actual sessions. Add units from new session queues with the same method until both minimums are reached. Never replace already selected units because they appear difficult to judge. Report reliability for the original seven sessions and the extension separately.

The manifest must preserve original membership/source input snapshots, not only current database event rows. Later regrouping or report changes must not silently alter the material evaluated. Nonselected population IDs and selection order remain inspectable so someone can reproduce the sample. Missing retained evidence is an “unverifiable” finding, not grounds to silently draw a different sample.

The five-event brief limit is compatible with this trial: seven successful runs can provide at most 35 selected-event summary occurrences, and ten distinct summary units need at least two sufficiently populated brief dates. The 30-group sample is drawn from the uncapped qualifying Today candidate list, so it does not require 30 generated summaries. Repeated event identities, quiet dates, raw-only mode, failed generation, or insufficient permitted spend may reduce either population; extend actual observation or leave evidence insufficient. Never raise spending, change interests, or regenerate the same date solely to fill the sample.

## Who judges what

| Check | Judge and method | Recorded result |
| --- | --- | --- |
| Personal relevance | The user reviews the 30 sampled events against the recorded interest profile. Ask whether the event is useful for that interest; do not infer the answer from clicks, saves, embeddings, or the model's opinion. | Relevant / not relevant / not judged, plus an optional reason. |
| Group coherence | The engineer inspects the frozen in-scope member articles for each of the 30 sampled Today groups. A coherent group describes the same specific development or explicitly connected updates, rather than merely sharing a company, region, or broad topic. A one-article group may be coherent; it is not evidence of successful multi-article clustering. | Coherent / incorrect merge / unverifiable, with article IDs and an explanation. Report singleton and multi-article counts separately. Record apparent duplicate splits and misleading additional current members in event detail as separate defects; this percentage does not measure recall or all historical event memberships. |
| Factual support | The engineer enumerates every factual claim in each of the 10 selected event summaries, including headings and numerical assertions, and compares it with the actual cited source inputs. A citation must support the assertion's subject, event, time, number, and qualifications. | Supported / contradicted / unsupported / unverifiable for every claim, with citation IDs and rationale. Plausibility or an uncited external page is not citation support. |
| Reliability and limits | The engineer reads run/allowance records and observes failures, recovery, data persistence, and cost reconciliation. | Reproducible findings tied to session/run IDs. |
| Usefulness and continued use | The user compares the application with ordinary reading and states whether they would continue using it and why. | The user's own conclusion; never a fabricated testimonial or automatic quality label. |

An automated check may assist by finding missing citations, broken identities, or duplicate IDs. It cannot substitute for the human relevance judgment or silently label the source-support review as completed. Coherence and source-support findings are engineer-recorded assessments, not an independent human review unless a separate human reviewer actually performs it.

## Baseline and value comparison

Before the trial, record three ordinary news-reading sessions using the user's existing approach and the same broad interest area. Log date, approximate minutes spent, useful stories found, sources opened, and duplicate coverage noticed. If the user already has dated comparable notes, those may supply the baseline; otherwise collect the observations. A recollection may be labeled contextual feedback but is not a timed baseline.

During the seven application sessions, record the same fields plus maintenance time and application costs. Compare median reading time and useful-story counts, and include the user's description of anything the briefs or grouping made easier. This is a small practical comparison across different news days, not a controlled causal study. Do not impose an invented percentage time-saving target. Completion requires concrete user-reported value and willingness to continue; increased maintenance or cost must be visible in that judgment.

## Acceptance targets

| ID | Requirement | Passing evidence |
| --- | --- | --- |
| P5-01 | Seven actual daily sessions, with at least six completing without developer intervention. | Dated logs covering all seven; routine retries and developer repairs counted explicitly. Any failed session has an understandable outcome and demonstrated recovery, with unresolved material failures listed. |
| P5-02 | At least 80% personal relevance on 30 sampled groups. | At least 24 “relevant” judgments supplied by the user. All 30 have judgments; “not judged” cannot be dropped from the denominator to pass. |
| P5-03 | At least 90% group coherence on 30 sampled groups. | At least 27 coherent groups; every sampled group reviewed with retained evidence. Unverifiable items are reported and cannot count as coherent. |
| P5-04 | Every inspected factual claim in all 10 sampled event summaries is supported by its cited material. | Complete claim inventory with no unresolved contradicted, unsupported, or unverifiable claim. Do not merely check one claim per summary. |
| P5-05 | No saved-data loss, duplicate charge admission, hidden allowance reset, or uncapped paid dispatch. | Session and durable-ledger checks; any uncertain provider charges remain conservatively accounted for and disclosed. |
| P5-06 | Concrete usefulness compared with ordinary reading. | Three-session baseline, seven-session reading log, observed cost and upkeep, and the user's own continued-use judgment. |
| P5-07 | Reproducible release and limitations. | Setup/runtime guide verified from delivered files, a clearly labeled captured-data demonstration, completed phase records, and a prioritized remaining-issues list. |

## Corrections, insufficient evidence, and release decision

Keep the original sample, original scores, failures, and release revision. If a defect is fixed, create a correction record with the changed revision and recheck every affected sample unit; never replace a failing sample with an easier event. Display original and corrected results separately. A correction affecting runtime reliability requires a new seven-session reliability window; a source-rendering or summary-generation correction requires renewed review of the affected claims/outputs and appropriate regression checks. A changed interest/feed profile requires a newly identified relevance/usefulness trial because it changes what is being evaluated.

The release assessment has one of three states:

- **Pass:** each acceptance target has evidence, relevant corrections are verified, and no material known defect undermines the core workflow or spending/data controls.
- **Needs correction:** adequate evidence exists but a target fails or a material defect is unresolved. Record the next fix and repeat only the evidence affected by that fix, subject to the rules above.
- **Insufficient evidence:** too few sessions/groups/summaries, missing retained sources, absent baseline, or missing user judgments prevents a conclusion. Keep Phase 5 incomplete and name the missing evidence. Do not treat silence or elapsed time as a user judgment.

## Deliverables and source targets

Produce a trial manifest, session log, deterministic sample manifest, group/relevance/claim reviews, cost observations, correction log, release assessment, setup guide, and a remaining-issues list. These can be concise Markdown/CSV/JSON records; no new dashboard or research application is required. Keep copyrighted source redistribution within the existing retained-source permissions; the public demonstration may use permitted captures and metadata with clear labeling.

Existing implementation evidence comes from the personal run/report snapshots delivered in Phase 2, the durable admission/cost records delivered in Phase 3, and the launch/recovery behavior delivered in Phase 4. Verified source starting points are `services/pipeline/`, `services/reports/`, `services/llm/repository.py`, `apps/api/pipeline.py`, `apps/api/intelligence.py`, `db/models/core.py`, `tests/integration/test_daily_pipeline_vertical_slice.py`, and `README.md`. Record the final implemented schema/paths in the trial manifest rather than assuming the current schemas already contain the required evidence.

This phase adds no scheduling, external messaging, public hosting, forecasting validation, or broad provider expansion. A seven-day trial and personal judgment remain real work; passing software tests alone cannot close it.
