# Independent bounded intake and reader review

Reviewer: settings/frontend implementation agent, using the inherited model and effort. This agent did not implement the coordinator, snapshots, reader, runs, or bounds migrations reviewed here. Review date: September 20, 2026.

Scope inspected: `services/personal/coordinator.py`, `services/personal/snapshots.py`, `services/personal/reader.py`, `services/personal/runs.py`, `apps/api/personal_reading.py`, and their interaction with the reviewed settings transaction. This is a bounded source review, not a claim to have rerun the complete integration suite or provider proof.

## Findings reported to implementation owner

1. **P2: raw article state incorrectly becomes `provider_failed` for a capture-only failure.** `_article_item` changes every ungrouped article belonging to a failed or partially failed run to `provider_failed`. A run that intentionally disables AI but has one failed feed has `partial_capture` and zero paid dispatches; its article needs the retained disabled/deferred/configuration state plus the separate run error. Preserve explicit article processing state instead of deriving provider failure from the aggregate run result.
2. **P2: completed enrichment is equated with grouping.** `collection_status` initially returns `completed_enrichment=grouped`. Group membership can exist before claim preparation or report generation fails. Keep grouped count distinct and derive completed enrichment from durable terminal stage evidence, or omit the unsupported count.
3. **P2: raw search does not match the visible filter description.** Today describes search as “Title or summary”, but `raw_articles` applies its SQL text filter only to title. Include retained RSS summary in the same escaped search condition.

These findings were sent to the root implementation owner for correction. Follow-up acceptance must record the resulting code and relevant fresh regression checks; this initial review alone does not close them.

## Follow-up source verification

The reviewer inspected the corrections on September 20 after the owner reported them complete:

- `_article_item` now preserves disabled/deferred/configuration article states for aggregate run failures and only converts unresolved `selected` work to `enrichment_failed`; the run error remains separate. The new `test_partial_feed_failure_does_not_claim_a_disabled_provider_failed` regression explicitly checks retained `disabled_by_profile` state.
- `collection_status` now distinguishes grouped articles from completed enrichment. Its completed count additionally requires the processing run to reference a published report. The reader integration test asserts grouped content without a published report has zero completed enrichment.
- Raw search now applies the same escaped match to either retained title or RSS summary.

All three findings are resolved in inspected source. Final runtime acceptance remains pending the owner's complete regression run; this reviewer did not run additional PostgreSQL tests during the exclusive full-suite slot. Separately, the browser follow-up identified and corrected the empty grouped-story message: disabled and blocked grouping now have explicit unavailable wording while a successful quiet update retains “No stories matched this update”.

After that presentation correction, the full frontend test command passed 74 tests and refreshed `frontend.log`.

The additional summary-state follow-up was also inspected: the reader now labels a published processing-run report as `run_brief_available`, rather than claiming an article-specific summary exists for every admitted article. The final offline suite subsequently passed 4,192 unit tests and 74 frontend tests; see `final-offline-checks.md`. PostgreSQL acceptance remains owned by the final integration run.

## Boundaries inspected without a new finding

- The intake transaction serializes the shared pending/admission boundary, locks the run, checks the persisted local-day usage, and reads the current daily ceiling while holding the workspace lock. The transaction creates article linkage, charge state, and persisted admission order together.
- First-run creation and timezone edits serialize on the workspace row; historical run identities retain their own profile revision.
- New and enrichment membership are separate. The coordinator freezes them before downstream work and passes the selected enrichment set to embeddings, grouping, and snapshot construction.
- Transfer selection requires a previous succeeded processing run and an explicit durable transfer record, so active or failed retry scopes are not implicitly claimed by another date. Existing capture/admission timestamps remain unchanged.
- A retry of frozen scopes reuses membership rather than appending newly fetched articles. Snapshot rows are reused, and overlapping frozen ownership requires an explicit transfer.
- The raw reader depends on retained PostgreSQL data and does not initialize a provider or Redis. Grouped article detail remains addressable while ungrouped list results exclude group members.
- The reviewer found no additional definite locking or ownership defect in this bounded pass. This is not a proof against every possible race.

## Separate checks run by this reviewer

- 25 settings/contracts unit tests passed after settings, draft-source, and readiness changes.
- Three settings/API PostgreSQL tests and one populated bounds-upgrade test passed using owned disposable databases on the dedicated Phase 3 PostgreSQL service, with the helper's identity-checked cleanup. See `settings-upgrade-integration.log` (4 passed).
- The complete frontend test command passed 73 tests. See `frontend.log`.
- Scoped Ruff check and formatting completed for settings/API and the reviewer's focused tests.
- No paid provider request, ordinary runtime preference activation, shared development database mutation, or unrelated process cleanup was performed by this reviewer.
