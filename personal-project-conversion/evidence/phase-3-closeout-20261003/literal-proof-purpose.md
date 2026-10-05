# Narrow literal acceptance proof

Three literal acceptance input gaps; execution belongs to the root-owned identity-checked disposable PostgreSQL harness.

The one new reusable integration file supplies the exact inputs that prior compositional proof omitted. Each case keeps actual application stages and database behavior in use; scripted broker/provider responses cannot establish live model or running broker service behavior.

## C1 / P3-02

- `tests/integration/test_personal_phase3_closeout.py::test_two_concurrent_http_retries_rotate_one_attempt_and_preserve_frozen_admissions`

- Two actual simultaneous HTTP retries yield one 202 and one 409, one production enqueue/scripted broker delivery and only attempt 2 with a rotated token.
- Frozen profile, date, membership, capture/admission records and two charges remain unchanged; stale ownership fails.
- Actual coordinator resumes frozen raw scope without recapture or provider/report dispatch; one pending item remains.

## C2 / P3-06

- `tests/integration/test_personal_phase3_closeout.py::test_http_old_caller_dates_cannot_backdate_run_or_reopen_same_day_admission`

- Fixed server instant independently converts to the expected America/Los_Angeles calendar date.
- First actual POST captures three, admits two and retains one pending; second POST supplies four old caller date fields yet returns the same succeeded run/date/attempt.
- One run, one job, one delivery, two articles, two charged admissions and unchanged frozen membership/records prove no renewed daily credit.

## C3 / P3-12

- `tests/integration/test_personal_phase3_closeout.py::test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes[default]`
- `tests/integration/test_personal_phase3_closeout.py::test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes[raw]`
- `tests/integration/test_personal_phase3_closeout.py::test_same_authored_rss_fixture_preserves_v2_scope_across_processing_modes[stage_failure]`

- Every mode reconstructs identical authored RSS fields and checks the shared input hash; v2 profiles freeze two admitted articles before enrichment.
- Default uses actual offline embedding/grouping/claim/snapshot/report publication. A live profile edit after freeze cannot change eligible agency membership or the pinned original profile; snapshot hash is independently recomputed.
- Actual brief detail/evidence APIs cite only persisted eligible agency sources and remain byte-identical after current Article title/summary/URL mutation.
- Raw mode selects zero enrichment, persists explicit disabled outcomes and readable raw content, and rejects any embedding/report dispatch.
- Required embedding failure occurs after scope freeze, remains failed with explicit workflow/provider failure, no snapshot/report, and both raw records readable.

## Shared input and correction history

The three C3 modes share canonical authored RSS SHA-256 `f1fddce95550b567f275aa2986bd3a06440fe015089b82093db8a2edd3a78f07`. Canonical JSON of authored RSS guid/title/summary/url/published_at/source/provider_name/source_refs/evidence_refs. Generated database/run/profile UUIDs differ per mode and are excluded.

Two failed and six passed; default expected an unwritten success-stage key, while workflow failure JSON was truly lost before refreshed ownership checking.

Assert actual published Report plus run.result published/pass/report/snapshot identity and four implemented successful stage outcomes. Only session.flush() after workflow failed stage assignment and before finish_run; required failed-stage assertion remains intact.

The failed gate and exact pre-edit bytes/diffs remain retained. The root ran all personal integration modules after the concrete coordinator change: personal-final retains 100 passing individual cases, exit zero, unchanged protected files/resources and no source drift. The acceptance map uses exact relevant node outcomes and matching source hashes. The independent rerun is recorded separately; overlapping totals are never added.

The root expands the glob and supplies the exact owned resource, cleared credentials, paid gate false and a database-only socket guard; exact executed arguments belong to its retained command artifact.
