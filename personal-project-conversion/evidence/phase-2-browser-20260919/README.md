# Phase 2 browser gate — September 19, 2026

**Browser gate: passed. Phase 2: Verification pending for separately authorized live proof.**

This continuation used the shared current checkout, preserved its uncommitted implementation and preference choices, and launched a new owned synthetic environment `browser_20260919_5fa7`. The earlier locked-Mac record was checked afresh: real browser interaction was available and all required browser observations completed.

## What passed

- The real browser requested the offline update; API → Redis → Celery produced a published report with one captured article, one grouped event, two supported claims and four scripted model records at zero cost.
- Saved → Open displayed **“These sources show current coverage for this saved story. No daily update is selected.”** The source row read **“Current event coverage”**. The view omitted the run-specific article count and did not label the source as used in a selected brief.
- Briefs → Open this version displayed exact report `0539fc3a-a716-4f88-8647-d780de20ca82`, snapshot `55301cb1-e7d6-4198-9337-e03ff5af6a52` and run `da3288e6-efbd-4497-8f18-2adfb512c888`.
- Actual **Markdown** and **PDF** link clicks in Chrome completed downloads. Native Chrome showed **3.6 KB • Done** and **18.9 KB • Done**, with both filenames containing the selected report UUID. Show in Finder located the actual downloaded files; Finder moved only these files into this evidence folder.
- Both retained downloads preserve macOS browser-origin metadata naming the exact report export URL. Both matched the selected stored report's renderer byte for byte. This comparison supplements the completed-browser-download proof; direct API export bytes were not used as download proof.
- The downloaded PDF has two pages. All report sections, identities, coverage and cited source were checked in extracted text and visually on both pages; no clipping, overlap or missing content was found.

No scoped application defect was found. No executable source or machine-readable preference was changed. Seven existing status/evidence Markdown documents were reconciled; prior uncommitted work was preserved. The existing frontend loaded its normal public JS/CSS/fonts; “offline” describes the synthetic feed/model inputs, not an air-gapped browser.

## Evidence

- [Browser observations and exact UI wording](browser-observations.json)
- [Selected-report browser snapshot](brief-snapshot.txt)
- [Download identity, byte comparisons, origin metadata and hashes](download-verification.json)
- [Actual downloaded Markdown](personal-news-brief-2026-09-19-v1-0539fc3a-a716-4f88-8647-d780de20ca82.md)
- [Actual downloaded PDF](personal-news-brief-2026-09-19-v1-0539fc3a-a716-4f88-8647-d780de20ca82.pdf)
- [PDF page 1](downloaded-pdf-page-1.png), [PDF page 2](downloaded-pdf-page-2.png), [extracted text](downloaded-pdf-text.txt)
- [API log](api.log), [scripted worker delivery log](worker.log)
- [Independent bounded review](independent-review.md)
- [Baseline of the current checkout and existing containers](baseline.json)
- [Owned harness manifest before cleanup](manifest.json), [manifest after cleanup](manifest-after-cleanup.json)
- [Cleanup log](cleanup.log), [cleanup verification](cleanup-verification.json)

The browser download files are 3,666 and 19,386 bytes. SHA-256:
- Markdown: `10eeaabf353298c2270d794c9d20090a88407ce9b5550af97d0608684c47e404`
- PDF: `1ca3349113e3465f7e0c38db59dd1183dfb42424af3b7953cc2b68af7f0af1a5`

## Fresh tests versus inspected history

| Check | This continuation |
| --- | --- |
| Focused export/harness unit checks | **43 passed**, rerun; [log](focused-python.log). |
| All frontend tests | **71 passed**, rerun; [log](frontend-tests.log). |
| Exact published report integration | **1 passed**, rerun in its own migrated disposable database on this task's PostgreSQL container; [log](focused-integration.log). |
| Historical complete regression | **4,085 unit / 176 integration / 71 frontend**, configuration, lint, structural gate and dependency audit: retained/inspected, **not rerun as a full gate** here. |
| Executable-source drift | Independent comparison with all 656 historical delivery paths found only documentation differences. Baseline-to-final verification also confirms no executable-source or preference-JSON change. |
| Independent review | Recomputed downloaded-file hashes and origin metadata; checked content/identity, both PDF images, source behavior and logs. Did not rerun tests or repeat browser driving. |

Focused command scope: `tests/unit/test_personal_exports.py`, `test_report_export.py`, `test_report_export_api.py`, `test_personal_offline_harness.py`; all `frontend/app/*.test.js`; and `tests/integration/test_personal_brief_api.py`. The integration test freshly covers a published version remaining readable/exportable despite newer failed/generating versions, mutable source changes and wrong/foreign IDs.

## Cleanup and preservation

Manifest-checked cleanup passed twice. The owned database was dropped, both owned containers were removed, API/worker/frontend PIDs are absent, and the API/frontend ports are available again. The shared development PostgreSQL and all other three baseline containers remain running with the same IDs/start times. No connection to or mutation of the shared development database was performed. The older September 18 harness is outside this continuation's ownership and was preserved.

The owned DevTools page and Chrome application tab were closed. The Mac relocked only after all browser acceptance observations; final native-window cleanup was unavailable. The task-created Chrome Download History tab `952903099` and Finder evidence window may remain. They run no task services. Evidence and private state-directory logs/manifest are intentionally retained.

The branch and HEAD are unchanged. All original implementation files and `config/selected-preferences.json` retain their initial hashes.

## Exact remaining acceptance gate

**One acceptance gate remains: the separate bounded live feed/model proof.** Resolve a concrete capture of one to three real RSS articles, compatible exact provider/model routes, current official prices, an explicit allowance no greater than $0.25, and authorization for that exact run. Only then may the guarded production flow execute; retain its dispatch ledger/accounting, supported claims, frozen snapshot, report identities and final outcome.

The four feed selections, empty inclusion/exclusion lists, Gemini `gemini-3.5-flash-lite` generation/checking, OpenAI `text-embedding-3-small` embeddings and no generation fallback remain saved preferences only. **Spending is undecided, allowance is unset, live execution is unauthorized, and preferences are not applied to runtime.** No live feeds/models, paid calls, production smoke, live profile activation, Phase 3 work or unrelated research occurred.
