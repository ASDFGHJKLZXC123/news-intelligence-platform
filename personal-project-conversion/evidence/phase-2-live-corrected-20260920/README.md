# Corrected one-article live proof — September 20, 2026

**Result: passed.** The user explicitly authorized “Run the corrected test” at recorded time 2026-09-20T07:44:59Z. One fresh disposable run used the same exact BBC article, selected models, no fallback, and unchanged $0.25 cap. The run published a substantive source-supported brief and reconciled every paid dispatch. This closes the live portion of Phase 2 and CORE-01 alongside the separately retained [genuine browser download/Saved proof](../phase-2-browser-20260919/README.md).

## Exact scope and identities

- Article: **Japan raises interest rate to new 31-year high to curb rising prices**, BBC Business, published 2026-09-18T07:14:57Z.
- Canonical URL: https://www.bbc.co.uk/news/articles/cqn74jeek06no?at_campaign=rss&at_medium=RSS
- Capture: bbc-japan-rates-1; source b88fbe3b-1b26-4d35-b8a7-dee8ad0faf9e.
- Verification: dec8277e-7e6e-4d27-ba01-2a7f4b44e921.
- Run: e78cacb3-001c-4bb8-b278-7579ceff4693.
- Snapshot: 9b2d5fad-2938-4861-9725-a998488c8fa6.
- Published report: d9dec7c9-28ce-414a-bd49-03f87872c95f, version 1, brief date September 20.
- Config hash: a398cc803e147527249077896fc16147732372be5ba230cbffac401b1194390d.
- Snapshot input hash: fa255ba05c051facc1bbc873f9c9a8d71c20c8a247e417282fba1241baa27e94.
- Ledger hash: ac6d9b35521e838a53bc1912077c83a14b9e2bc5d7a3dae3531b2e1a4aecd29a.

[Authorization](authorization.json), [config](config.json), [fresh public RSS preflight](rss-preflight.json), [runtime preflight](runtime-preflight.json), and [validation](authorized-validation.json) retain the exact boundary. The feed wrapper admitted only the selected URL. The existing certifi trust bundle was selected only for these processes; certificate and hostname verification stayed enabled.

## Production result and spending

The migrated disposable database initially contained only the approved source. The production personal coordinator performed real capture, admission, embedding, grouping, deterministic claim preparation, snapshotting, composition, semantic grounding, copyright checks and publication. No claim links or report were preloaded. The frozen snapshot uses personal_descriptive.v1.

| Dispatch | Route and purpose | Input / output tokens | Recorded cost USD |
| --- | --- | --- | --- |
| 1 | OpenAI text-embedding-3-small — one article | 33 / 0 | 0.00000066 |
| 2 | Gemini gemini-3.5-flash-lite — executive summary | 665 / 143 | 0.000557 |
| 3 | Gemini gemini-3.5-flash-lite — event summary | 663 / 154 | 0.0005839 |
| 4 | Same Gemini route — executive semantic grounding | 445 / 178 | 0.0005785 |
| 5 | Same Gemini route — event semantic grounding | 456 / 154 | 0.0005218 |

**Total: $0.00224186; uncertain charges: $0; cumulative reservations: $0.0464456.** All five requests reconciled within the 10-dispatch, 8,192-input, 4,096-output and $0.25 limits. Both generated sections passed on their first composition and grounding attempts; no fallback, budget retry or regeneration was needed. The full configured worst-case allocation remained $0.11444224.

Standard prices were independently checked at 2026-09-20T07:49:11Z: [Gemini $0.30 input / $2.50 output per million](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite), [OpenAI embedding $0.02 input per million](https://developers.openai.com/api/docs/models/text-embedding-3-small). Gemini output usage includes thinking. Costs are returned provider usage multiplied by these prices, not invoice reconciliation.

The [immutable ledger](live-smoke-dec8277e-7e6e-4d27-ba01-2a7f4b44e921.json), [arithmetic/hash verification](accounting-verification.json), [execution result](execution-result.json), and [database rows/model outputs](database-evidence.json) retain the evidence. Total recorded cost across the initial zero-dispatch failure, failed composition retry and successful corrected attempt is $0.00586212.

## Supported content and exact exports

The database contains one captured article revision, one claim, one supports relation, one article EvidenceItem, one frozen snapshot, and one published report with five sections. Claim d657a01e-97c4-5983-89d3-8ca9f3bead31 is supported by the exact retained BBC headline. The executive and event paragraphs describe Japan raising interest rates to a 31-year high to address rising prices. Both real semantic checks returned supported; root also compared the prose directly with the frozen source input.

- [Published Markdown](personal-news-brief-2026-09-20-v1-d9dec7c9-28ce-414a-bd49-03f87872c95f.md).
- [Published PDF](personal-news-brief-2026-09-20-v1-d9dec7c9-28ce-414a-bd49-03f87872c95f.pdf).
- [Export identity, hashes and source inspection](export-verification.json).

Exports came from the production workspace-scoped published-document repository and exact-report renderers. Repeated rendering produced identical bytes. Root visually inspected both PDF pages: readable text, no clipping/overlap, correct source and identities, final disclaimer retained. Poppler rendered the pages; because pdftotext was unavailable, bundled pypdf supplied text extraction.

These are live-report renderer exports, **not new browser-download evidence**. The required actual Chrome Markdown/PDF clicks and Saved wording already passed in the separate linked browser record; this continuation did not rerun that browser gate.

The evidence scope is headline/RSS-summary material, not full article retrieval or independent confirmation of BBC's factual accuracy. The summary is short because only one eligible factual claim exists. Static legacy comparison/data-quality notes and the canonical disclaimer remain visible; this result does not establish predictive analysis, broad coverage, routine-use readiness or personal usefulness.

## Code and verification boundary

The preceding [personal-composition correction](../phase-2-personal-composition-20260920/README.md) removed market/risk assumptions from generated personal prose, allowed source-supported brevity while retaining hard ceilings, froze the personal policy in snapshots, and rejected failed/over-budget grounding regeneration. That correction passed 84 focused unit and 16 disposable PostgreSQL integration tests plus scoped lint and independent review.

This continuation made **no executable-source changes**. The [eight tested file hashes](implementation-files.json) were checked unchanged before the live attempt. At final verification all seven executable/test files remain unchanged; the phase specification's status paragraphs were updated to record completion. Tests were **inspected, not rerun** for this corrected live execution. The historical full 4,085 unit / 176 integration / 71 frontend run and the later browser checks remain evidence at their recorded revisions; they are not claimed as fresh full-suite results after the composition change. The fresh work here is the real feed/provider flow, ledger/hash arithmetic, exact-report output review and cleanup. See [independent acceptance review](independent-review.md) and [final check](final-check.json).

## Retention, cleanup and remaining gates

The [database dump](corrected-database.dump) and its readable archive listing, raw rows, ledger, config and outputs are retained. [Retention metadata](retention.json) records hashes. The temporary state directory is /tmp/nip-phase2-live-20260920_corrected_dec8277e.

The [cleanup record](cleanup-verification.json) confirms removal of owned container b5f9473d8e074e49b48a1653254972f3bc3dc8020525a16413caa4fc8eabaa0a and closure of port 61264. All seven preexisting containers retain the same identities, start times and running states. No API, worker, Redis or scheduler was started for this direct bounded proof. The shared development database and uncommitted implementation were preserved.

**Remaining Phase 2 acceptance gates: none.** Ordinary runtime preferences remain unapplied, no recurring allowance is approved, and no further paid attempt is authorized. Phase 3–5 work has not started. Earlier failures and their ledgers remain unchanged historical evidence.
