# Authorized retry — September 20, 2026

**Live result: failed composition acceptance. Phase 2 remains Verification pending.**

The user authorized “Retry this test.” This was one fresh attempt of the same BBC Japan interest-rate article with Gemini gemini-3.5-flash-lite and OpenAI text-embedding-3-small, no fallback, at most nine Gemini requests plus one embedding, and a $0.25 total cap. The first failed attempt remains unchanged.

## What passed

The production RSS connector selected exactly the approved article with certificate and hostname verification enabled using the existing certifi bundle. A fresh isolated database passed migration, source identity and empty-business-table preflight. The real embedding request succeeded, the article was grouped, one source-supported claim was prepared, and an immutable snapshot was frozen.

Both real providers accepted the selected models. All five paid dispatches reconciled: one embedding plus four Gemini composition requests, including each section's word-budget retry. **Recorded cost: $0.00362026; uncertain charges: $0.** Total conservative reservations were $0.0460505; those reservations are not additional charges. The earlier attempt cost $0.

## Why the live gate failed

| Generated section | First output | Budget retry | Required range at execution |
| --- | ---: | ---: | ---: |
| Executive summary | 35 words | 126 words | 96–120 |
| Article section | 82 words | 69 words | 120–180 |

Both sections failed their word ranges after the allowed retry and were replaced with budget_unmet notes. The personal run became partially_failed; the report and ledger failed. No published report or Markdown/PDF export was produced.

The database's gate_outcome pass does not establish that the discarded prose was grounded: all four Gemini requests were composition calls. The prose was omitted before the semantic grounding stage. Some raw output also invented alert/risk stability and quiet volatility absent from the sole source claim; it must not be presented as a verified brief.

The inherited executive prompt asked for an actionable US-market lede, alerts and risk movement, although the personal snapshot supplies none of that material. Its rigid legacy minimum lengths also pressure sparse RSS evidence into verbose prose. This prompted a separate [offline personal-composition correction](../phase-2-personal-composition-20260920/README.md). That correction does not retroactively change this failed attempt.

## Identities and evidence

- Verification: 98db64a8-cc44-4984-a1f4-181ba5803fe1.
- Run: 321902cf-f088-4e07-b6f1-6ef734444228.
- Snapshot: 78fefdbe-f92f-4b68-954e-5dfa6ede40df.
- Failed report: 8d0533bd-e7f4-4fdd-b466-2fc8e5dbd119.
- Config hash: f1d76dd2ec8ebd862a2aa895ae5804ed7f7db1ad7ccca71d9dffc8150900f9aa.

[Authorization](authorization.json), [exact config](config.json), [RSS preflight](rss-preflight.json), [runtime preflight](runtime-preflight.json), [command result](execution-result.json), [failure log](execution.stderr.log), [terminal ledger](live-smoke-98db64a8-cc44-4984-a1f4-181ba5803fe1.json), [workflow diagnosis](workflow-diagnosis.json), [database records](database-evidence.json), and [fresh offline word-count replay](offline-output-checks.json) are retained.

The [database dump](retry-database.dump), [archive listing](database-archive-list.txt), and [retention hashes](retention.json) preserve the attempt for investigation. The terminal ledger and dump are also retained in the private state directory /tmp/nip-phase2-live-20260920_retry_98db64a8.

## Cleanup and verification boundary

[Cleanup verification](cleanup-verification.json) confirms removal of the exact owned container and closure of port 50369. All seven pre-existing containers retained their IDs, start times and running states. No API, worker, Redis, frontend or scheduled process was started for this live attempt. Shared development data and the first attempt's evidence are preserved.

The live attempt and deterministic output-count replay were freshly executed. No regression suite was rerun as part of the live attempt itself; tests for the subsequent scoped offline correction are recorded separately. No further paid attempt has been made.

The browser download/Saved wording gate remains passed. The remaining acceptance gate is a newly authorized successful bounded real-feed/model run using the corrected personal behavior, with supported prose, a published immutable report, reconciled ledger, inspected outputs and owned cleanup. Phase 3 and ordinary recurring assisted operation remain unstarted.
