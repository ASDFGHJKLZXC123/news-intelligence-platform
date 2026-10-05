# Independent Phase 5 preparation review

Review date: October 4, 2026, America/Los_Angeles. Reviewer: a separate inherited Codex agent, not an independent human reviewer.

**Result: no remaining blocking defect found in the prepared raw-reading trial tooling. Phase 5 acceptance remains insufficient evidence: no actual sessions, baseline observations, quality reviews, or usefulness judgments were supplied by this review.**

## Scope and entry evidence

Read the Phase 5 acceptance contract, shared contracts, model/effort recommendations, Phase 4B acceptance, and retained Phase 4 review/preservation records. Before Phase 5 edits, freshly compared the retained source hashes: all 1,219 Claude review snapshot entries matched; the final Phase 4B manifest matched all 599 non-Markdown files and had only its four already documented Markdown status differences. This was a byte comparison and evidence inspection, not a rerun of earlier acceptance suites.

Independently reviewed `trial_records.py`, `trial_sampling.py`, `trial_capture.py`, the trial CLI, their focused tests, and the trial guide/status record. The reviewer changed no implementation, configuration, historical evidence, Git controls, database, service, feed, or provider. This new review file is the only retained reviewer-authored project change.

## Fresh verification

Reran the three Phase 5 unit files with the project interpreter, pytest cache disabled, bytecode writes disabled, paid activation disabled, blank provider/API keys, and infrastructure addresses pointed at unused loopback port 9. **99 tests passed in 1.77 seconds**: 15 records, 61 sampling, and 23 capture cases. These use synthetic rows, mocked transactions/transports, authored clocks, and temporary local records. No real database capture, feed collection, model dispatch, browser reading, or human quality review occurred.

An additional independent synthetic capture-to-sampler probe confirmed:

- All 27 frozen Today groups survive capture and sampling while the brief snapshot selects only five events; permitted source material remains attached.
- A published report whose immutable material cannot be read produces `summary_population_complete: false`; this blocks downstream summary selection even when that report supplies zero extractable units. Its session-linked finding remains inspectable.
- The same logical run on two processing dates is refused, including nested captured run identity.
- Seven recorded sessions with no verifiable logical run identity remain insufficient reliability evidence.

The four reviewed implementation files had identical hashes before and after the successful probe. An earlier probe fixture accidentally retained a top-level run identity when testing missing identity and was correctly refused; the fixture was corrected and the probe rerun.

Finally inspected the actual prepared trial `fe79058e-52c3-4d1d-b9f0-3f7cf8af2efb`: it is raw, paid runtime is disabled, no runtime binding exists, all three baseline dates are blank, no closed-session files exist, the release decision is insufficient evidence, and actual-session/user-judgment counts are zero. Every recorded application source hash matches current bytes.

## Findings resolved before this verdict

The review identified and the authors corrected duplicate/future baseline acceptance, missing/invalid reading timestamps, insufficient exercise records, capture-time exporter identity mislabeled as application provenance, missing pretrial runtime/profile binding, retrospective session admission, duplicate logical-run counting, capture/sampler material-shape disagreement, dropped real stage vocabulary, nonterminal session omission, and missing whole-report completeness propagation. The sample CLI now uses ordered sequence numbers and predecessor hashes, and requires the latest retained manifest instead of accepting an arbitrary older sample file.

Runtime binding is created exclusively, records the tool's `bound_at`, and requires raw/paid-disabled observations with the candidate source identity and actual profile. Session registration requires start time after both trial creation and binding, capture during reading, the bound profile/calendar/migration, and explicit user observations. This enforces the recording protocol; it cannot independently authenticate the user's reading or launch observations.

Frozen group selection remains independent of wording and judgments. Missing retained sources remain visible on selected units. Original seven-session and extension reliability records are separate, and frozen selections cannot be silently replaced. Missing publication chronology or an incomplete captured summary population blocks new summary selection while preserving earlier frozen selections.

## Remaining acceptance limits

- The intended runtime/database/profile has not been activated or bound during preparation, and the actual read-only PostgreSQL export path remains unverified against a real database.
- Three ordinary-reading baseline sessions, seven real distinct-date sessions, retained failures/recovery, costs/data-preservation checks, and the user's continued-use judgment remain required.
- Raw articles are outside the group/summary populations. Raw reading cannot close the 30-group relevance/coherence or ten-summary factual-support targets.
- `Report` lacks an immutable publication timestamp. Capture preserves null publication time and blocks earliest-version summary selection; creation/update timestamps are not substituted.
- Earlier overwritten attempt outcomes and prior Saved-data loss cannot be reconstructed from a final database capture. Independent attempt captures and session observations remain necessary.
- A future profile change needs a new identified relevance/usefulness trial. The prior $0.25 one-shot verification allowance provides no recurring authorization.

## Reviewed implementation identity

| File | SHA-256 |
| --- | --- |
| `services/personal/trial_capture.py` | `b84e6c0d2cb2536f5a150bfd8a66e242f2cc75d4935434c0f7ef1ab712a27405` |
| `services/personal/trial_sampling.py` | `ab5751f4a5a9af206b006830cb055c145b825e2990f80f4a6ada0473c6e152fa` |
| `services/personal/trial_records.py` | `6188a2a129c8cc9ffc43f74542a157f3f73dfa0c6e2b60623afe5e73fcf8f2ae` |
| `scripts/personal-trial.py` | `522072b276664640afacd9479736eb2f374fcbe237b8fa380dee3f9d9fc86522` |
