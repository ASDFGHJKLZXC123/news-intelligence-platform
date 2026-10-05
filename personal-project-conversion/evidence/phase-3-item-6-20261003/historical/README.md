# Historical Phase 3 browser consolidation

Consolidated October 3, 2026 from actual Chrome observations on September 20, 2026. These artifacts are **historical browser proof**, separate from fresh current-source item-6 captures. No historical browser action was rerun by this extraction.

The original browser used Real mode at `http://127.0.0.1:61410`, with the actual loopback API at `http://127.0.0.1:59751` and disposable PostgreSQL-backed records. Inputs were labeled synthetic RSS fixtures; frontend Demo was not used. Original page: `http://127.0.0.1:61410/SIGNAL%20-%20Intelligence%20Platform.html?api=http://127.0.0.1:59751`.

[Manifest](manifest.json) records exact source-session identity, UTC timestamps, function-output line/call IDs, retained artifact hashes and inspected fixture/review hashes. [Expected versus actual observations](expected-actual.json) contains the concrete historical UI values and their limitations.

## Recovered observations

| Check/action | Expected from the fixture/action | Actual browser value | Retained output |
| --- | --- | --- | --- |
| Real raw reading | Two admitted articles remain readable with optional AI disabled | Two raw cards; Disabled by profile; original source links and retained RSS snippets | [Initial Today](01-today-before-wording.txt) |
| Retained article detail | Preserve distinct source publication, capture and admission times | Agency policy article published September 20, 15:06:03 PDT; captured/admitted 16:06:03 PDT; exact retained summary and original URL | [Loaded detail](03-raw-detail-loaded.txt) |
| Initial settings | Read the fixture profile, limits and scope rules | Revision 3; daily/run/enrichment limits 2/2/2; one selected synthetic feed; disabled timezone field America/Los_Angeles | [Settings](04-settings-initial.txt) |
| Save and reload | Daily limit 1 and disabled source persist | Revision 4; daily 1; run/enrichment 2/2; source unchecked; 2 retained / 0 eligible / 2 disabled-source pending items | [Post-reload settings](05-settings-reload-disabled-feed.txt) |
| Re-enable source | Pending records remain and become eligible | Revision 5; 2 retained / 2 eligible / 0 disabled-source pending items; accurate settings applicability message | [Re-enabled source](06-settings-reenabled-feed.txt) |
| Frozen run after edits | Existing run retains its recorded profile revision | Today still names revision 3 and the original run identity | [Frozen run](07-today-frozen-run.txt) |
| Collection versus processing | Count retained records without implying completed enrichment | Settings: 4 observed, 2 pending, 2 admitted, 0 grouped; Today: 0 selected for enrichment; optional stages disabled | [Settings](04-settings-initial.txt), [Today](08-today-final-wording.txt) |
| Unknown publication time | Missing RSS publisher date stays unknown | Pending AI-note item says Publication time unknown | [Settings backlog](04-settings-initial.txt) |
| Disabled-grouping presentation | Do not label disabled grouping as a healthy quiet story result | Final Today says Grouping is disabled and retained RSS articles remain readable | [Final Today](08-today-final-wording.txt) |
| Historical zero spending/reset | Display zero allowance/obligations and UTC month/local reset | Ai disabled; 2026-09 UTC period; permitted/finalized/reserved/unresolved/remaining all $0; reset September 30, 17:00 PDT | [Settings](04-settings-initial.txt) |
| Secret-safe access control | Do not display tab-held access value as ordinary text | Local access key-set label and masked placeholder; provider-credential exclusion wording | [Settings](06-settings-reenabled-feed.txt) |
| Browser diagnostics | Retain observed diagnostics honestly | Earlier error reads empty; final warn/error read contains three CDN Babel-transformer warnings | [Detail errors](03-raw-detail-loaded-console.txt), [Today errors](07-today-frozen-run-console.txt), [Final logs](08-today-final-wording-console.txt) |

Native screenshots were decoded without editing from the original tool image payloads:

- [Article loading](02-raw-detail-loading.jpg): the screenshot does not prove loaded article detail.
- [Today before the wording correction](07-today-frozen-run.jpg): preserves the earlier No stories matched this update wording.
- [Today after the wording correction](08-today-final-wording.jpg): preserves the corrected disabled-grouping presentation.

The final Settings navigation output only says Reading settings. [That transient output](09-settings-navigation-loading.txt) is retained without claiming a final loaded Settings verification.

## Fixture and source limitations

The historical [seed record](../../phase-3-20260920/browser-seed.json) identifies run `1f658636-2151-45f0-be64-bf579a0703b3`, four captured items, two admissions and zero paid dispatches. The [seed script](../../../../scripts/seed-personal-phase3-browser.py) used `FakeRSSProvider`, a raw profile and explicit paid-provider/report rejection. The source was named Synthetic Phase 2 acceptance feed with URL `https://offline.personal.test/feed.xml`.

The fixture's publication ordering admitted the item titled Technology reading remains in backlog. Its retained fixture text describes an intended seed role; the actual browser classification is admitted raw. That fixture text is not counted as proof that this admitted article remained pending. Unknown publication time was observed on a pending item, not on an admitted raw detail page.

The original browser records do not pin all executable source hashes or retain a complete historical API/database value export. Manifest inspected-file hashes identify files at consolidation, rather than claiming that the same complete source revision ran on September 20. No general network audit, current-source browser pass, nonzero ledger proof or resource cleanup is inferred from these historical outputs.

Remaining fresh browser cases include interests/broader settings persistence and source validation; practical timezone restrictions; deterministic raw pagination and raw unknown-publication detail; grouped-card suppression/event linking; populated Saved/Briefs under blocked AI; capture bounds; distinct server-gate/configuration/allowance/capture/provider-failure states; and nonzero spending with older unresolved obligations against retained API/database values.

## Related reviews and later scope

[Independent intake review](../../phase-3-20260920/independent-intake-review.md) records corrections to capture/provider state separation, grouped versus completed-enrichment counts, title-or-summary search and disabled-grouping wording. [Independent ledger review](../../phase-3-20260920/independent-ledger-review.md) is source/concurrency/classifier/owned-loopback transport proof; it supplies no browser spending evidence.

The later [items 2/3 record](../../phase-3-items-2-3-20261003/README.md), [items 1/4 record](../../phase-3-items-1-4-20261003/README.md) and [item 5 record](../../phase-3-item-5-20261003/README.md) supersede older implementation-blocked wording within their respective scopes. Their backend/synthetic results do not replace fresh browser observations. Item 7 owns the stale global acceptance matrix and overall Phase 3 closure. Accepted Phase 2 browser/live records are separate and were not reopened.

## Reproduce the safe extraction

From the repository root:

```sh
.venv/bin/python personal-project-conversion/evidence/phase-3-item-6-20261003/historical/extract_historical.py
```

[The extraction script](extract_historical.py) verifies the original session SHA-256, selects only audited function-output records, retains exact DOM substrings and separate native console text, and decodes three native JPEG payloads. It does not copy whole session records, tool-call arguments or the launcher log. The original launcher log contains a throwaway access key and is intentionally excluded. [Consolidation checks](consolidation-check.json) retain artifact/hash/credential-exclusion/link validation.

All three native images were visually inspected during consolidation. No credentials appear in them. Historical files, production source, operational configuration and resources were not modified by this work.
