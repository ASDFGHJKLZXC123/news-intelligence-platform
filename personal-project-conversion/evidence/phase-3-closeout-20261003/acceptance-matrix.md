# Phase 3 acceptance and verification

Status: **Complete — local/synthetic engineering acceptance, October 3, 2026 (America/Los_Angeles).** All fourteen requirements below have explicit current evidence and independent review. Phases 1–2 and CORE-01 remain complete. Ordinary paid runtime remains off; saved preferences are unapplied and no recurring spending allowance has been authorized. Phase 4A, Phase 4B and Phase 5 remain not started.

This is the authoritative current Phase 3 status. The original pending matrix and earlier scoped logs are retained as dated history below and in the byte-identical archive. They no longer describe current acceptance.

## Current acceptance

| Acceptance | Status | Actual result | Exact proof |
| --- | --- | --- | --- |
| P3-01 | PASS | 30 observed; exactly 20 admitted, 10 pending and 20 frozen enrichment IDs. Intake and completed enrichment remain distinct in the reader. | [Mapped assertions](acceptance-map-01-07.md#p3-01); [current per-case proof](acceptance-matrix.json) |
| P3-02 | PASS | Canonical duplicates spend no extra charge; rollback spends none. Two simultaneous HTTP retries yield one new attempt/token/delivery and preserve frozen membership and charges. | [Mapped assertions](acceptance-map-01-07.md#p3-02); [current per-case proof](acceptance-matrix.json) |
| P3-03 | PASS | A20/B2 with ceiling 4 admits A1, B1, A2, B2; fresh sessions retain the same IDs and ordinals. Older batches and known/unknown publication order are explicit. | [Mapped assertions](acceptance-map-01-07.md#p3-03); [current per-case proof](acceptance-matrix.json) |
| P3-04 | PASS | Retained candidates survive remote rotation and source disable/re-enable, then admit with original source/content and unknown publication date preserved. | [Mapped assertions](acceptance-map-01-07.md#p3-04); [current per-case proof](acceptance-matrix.json) |
| P3-05 | PASS | 2 MiB response, 501-entry and 2,000-pending boundaries retain accurate statuses/counts and no over-cap insertion or fabricated totals. | [Mapped assertions](acceptance-map-01-07.md#p3-05); [current per-case proof](acceptance-matrix.json) |
| P3-06 | PASS | Midnight retry preserves logical run limits; new admissions charge their actual server-local day. Old caller dates cannot create a second same-day run or reopen intake. | [Mapped assertions](acceptance-map-01-07.md#p3-06); [current per-case proof](acceptance-matrix.json) |
| P3-07 | PASS | Disabled, missing, exhausted and failed processing stay distinguishable; available raw articles, Saved and prior Brief remain readable, with zero disabled-path paid probes. | [Mapped assertions](acceptance-map-01-07.md#p3-07); [current per-case proof](acceptance-matrix.json) |
| P3-08 | PASS | Two concurrent $0.02 requests under $0.03 shared credit permit exactly one dispatch. API-created obligations and the registered worker use the same durable boundary. | [Mapped assertions](acceptance-map-08-14.md#p3-08); [current per-case proof](acceptance-matrix.json) |
| P3-09 | PASS | Missing completion/timeout retains uncertain obligations; physical retries need distinct credit. Fresh ledger reconstruction and idempotent receipts preserve known and unknown costs. | [Mapped assertions](acceptance-map-08-14.md#p3-09); [current per-case proof](acceptance-matrix.json) |
| P3-10 | PASS | Reservation handoff revalidates the UTC dispatch month; later completion stays charged there. Local reset and current/all-period disclosures match retained browser/accounting proof. | [Mapped assertions](acceptance-map-08-14.md#p3-10); [current per-case proof](acceptance-matrix.json) |
| P3-11 | PASS | Populated 0020-to-0022 upgrade retains raw/save/report/run identities; known usage imports once, unknown usage blocks enablement and no unrestricted catch-up starts. | [Mapped assertions](acceptance-map-08-14.md#p3-11); [current per-case proof](acceptance-matrix.json) |
| P3-12 | PASS | One authored RSS hash drives default/raw/required-failure modes. Default publishes only its persisted eligible snapshot; raw selects zero enrichment; required failure persists failed stage/error and readable content. | [Mapped assertions](acceptance-map-08-14.md#p3-12); [current per-case proof](acceptance-matrix.json) |
| P3-13 | PASS | 100 old raw plus 100 fresh: 100 fresh admissions, 100 old enrichment IDs, fresh capacity-deferred raw records and no second old admission charge. Raw selects zero enrichment. | [Mapped assertions](acceptance-map-08-14.md#p3-13); [current per-case proof](acceptance-matrix.json) |
| P3-14 | PASS | Retryable/exhausted failed ownership cannot transfer silently; explicit retries preserve scope and the old raw content/failure remains readable. | [Mapped assertions](acceptance-map-08-14.md#p3-14); [current per-case proof](acceptance-matrix.json) |

See [the current acceptance JSON](acceptance-matrix.json) for every exact expected/actual claim, JUnit node and test-source hash.
