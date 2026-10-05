# Authorized one-article live attempt — September 19, 2026

**Result: failed before paid processing. Phase 2 remains Verification pending.**

The user authorized “Run this test.” The attempt used the [reviewed one-article proposal](../phase-2-live-proposal-20260919/README.md): BBC's Japan interest-rate article, Gemini gemini-3.5-flash-lite, OpenAI text-embedding-3-small, no fallback, at most 10 physical paid requests, and a $0.25 total cap. Current official prices were independently rechecked unchanged before execution.

## Outcome

- Verification: 8cf55cfa-8162-436b-bb14-2430636fdd34.
- Run: b762e211-ee4a-4359-b660-4ec2feb8db7b.
- Config hash: c80d68585f9d29f3c2fd749fbbda55755151d5620f050b4f0bc98110922388d2.
- Run and ledger both failed. The only feed failed capture; zero articles, claims, snapshots or reports were produced.
- **Zero paid dispatches; $0 known cost, $0 reserved and $0 uncertain.**
- The command was executed exactly once. No replacement ledger, application retry or fallback was started.

## Cause and correction

The production RSS connector uses Python's default TLS trust store. On this Mac, its default certificate file was absent. A fresh read-only diagnostic reproduced CERTIFICATE_VERIFY_FAILED with the production connector and exact article filter.

Setting SSL_CERT_FILE for the diagnostic process to the existing virtual environment's certifi bundle made that same connector/filter successfully select exactly one approved article. Certificate and hostname verification stayed enabled. This was a public-RSS-only diagnostic, not a successful application retry or model proof. No model endpoint was called.

The [live setup guide](../../phase-2-live-smoke.md) now explicitly selects the existing certificate bundle for its process and calls for an RSS-only preflight before execution. No executable application code, global trust settings, shared database or provider credentials were changed.

## Retained evidence

- [Authorization](authorization.json), [authorized config](config.json), [fresh validation](authorized-validation.json).
- [Runtime preflight](runtime-preflight.json): dedicated migrated database, exact source row, empty business tables and provider configuration passed.
- [Command outcome](execution-result.json), [error log](execution.stderr.log).
- [Terminal ledger](live-smoke-8cf55cfa-8162-436b-bb14-2430636fdd34.json).
- [Database records and counts](database-evidence.json).
- [RSS/TLS diagnosis and successful feed-only check](rss-tls-diagnosis.json).
- [Independent review](independent-review.md).
- [Database dump](failed-database.dump), [archive listing](database-archive-list.txt), [retention hashes](retention.json).
- [Owned resource manifest](manifest.json), [cleanup verification](cleanup-verification.json).

The database dump and terminal ledger are retained both here and under /tmp/nip-phase2-live-20260919_8cf55cfa. The private state directory is retained as evidence.

## Verification and cleanup

Fresh checks: runtime/config validation, the actual one-shot application attempt, two public RSS diagnostics comparing trust settings, database/ledger inspection, and database archive readability. No unit/integration/frontend test suite was rerun in this live attempt; earlier passing suites and browser proof remain historical evidence.

The owned container was removed only after matching its full recorded ID and retaining its database. Its random port 63406 is closed. All seven pre-existing containers (including stopped containers) retain their original IDs, running states and start timestamps. No API, worker, Redis or frontend process was created for this attempt.

## Remaining acceptance

The browser download and Saved source wording gate remains passed. The only remaining Phase 2 gate is a **successful bounded real-feed/model run**, including supported claims, a published snapshot/report, reconciled ledger, retained outputs and cleanup evidence.

The failed attempt is terminal. The earlier one-attempt approval does not authorize a new full run. A user-approved retry can retain the same one-article scope, models and $0.25 cap, use the verified process trust setting, and receive a new verification ID and disposable database. Recheck article availability and prices before that attempt. No Phase 3 or recurring assisted operation is authorized.
