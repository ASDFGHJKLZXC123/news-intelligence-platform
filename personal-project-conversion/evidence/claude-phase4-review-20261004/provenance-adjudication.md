# F1 provenance adjudication

An independent Codex subagent inspected the original source and retained baseline evidence without writing files or executing tests. Codex separately reran the single test after Claude's report.

The pre-4A `apps/api/pipeline.py` was reconstructed entirely in memory from the recorded HEAD plus the retained pre-4A unstaged patch. Its SHA-256 exactly matches `phase-4a-20261004/baseline-files.json`:

`08931add4a411690b7ae0154e14d8c082a83f957d87d26fd2fbdf9fc9c98b2b8`

The reconstructed baseline eagerly imports `run_daily_pipeline_task` at module scope, line 27. Current source imports it only within `enqueue_pipeline_task`, at lines 90–91, and matches the Phase 4A accepted hash:

`f6cbf1116ed91e1ba29e488d2db1218420d212311c98045b776d04cb1b070519`

Evidence relative to `personal-project-conversion/evidence/`:

- `phase-4a-20261004/baseline.json:2` identifies the unchanged HEAD.
- `phase-4a-20261004/baseline-staged.diff` changes only `.gitignore` and the Stage 9 validation document.
- `phase-4a-20261004/baseline-unstaged.diff:521–532` retains the eager task import as context.
- `phase-4a-20261004/baseline-files.json:115` records the exact reconstructed API file hash.
- `phase-4a-20261004/baseline-files.json:1396` and `phase-4a-20261004/phase4a-accepted-source-before.json:561` record the unchanged test hash; current test bytes also match.
- Current `tests/unit/test_pipeline_runtime.py:656` patches the removed API-module attribute.
- `phase-4b-20261004/independent-review.md:11` accurately says unchanged against the 4B baseline, which already contains the mismatch.
- `phase-4b.md:50` uses “preexisting” without that narrower qualification.

Conclusion: the specific test/source compatibility mismatch arose during Phase 4A and was carried into the Phase 4B baseline. The fresh single-test run reproduces the expected `AttributeError`. This establishes source provenance and current failure, not a previously executed green baseline. No production task-delivery failure was demonstrated, and no source or historical phase record was changed.
