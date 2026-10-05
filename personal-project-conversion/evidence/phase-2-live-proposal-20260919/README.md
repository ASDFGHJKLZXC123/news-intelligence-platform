# Proposed one-article Phase 2 test

Status: **prepared and locally validated; live execution is not authorized or started.**

Use BBC Business's [Japan raises interest rate to new 31-year high to curb rising prices](https://www.bbc.co.uk/news/articles/cqn74jeek06no), published September 18, 2026. Public RSS lookup confirmed this exact item. The app consumes its headline and short RSS summary; the full article was not retrieved.

The earlier flight-disruption suggestion produces zero candidates under the existing conservative claim rules. This replacement produces one candidate in the local source check. This is an article-selection correction; application code and claim rules were not changed.

## The proposed run

1. Provision a fresh owned disposable database and validate isolation before enabling this config. Create only the reserved BBC source row; preserve the shared development database and saved four-feed preferences.
2. Read the real BBC feed, accepting only this one canonical article URL. Stop if it is absent or duplicated; do not substitute another article.
3. Use OpenAI text-embedding-3-small for embeddings and Gemini gemini-3.5-flash-lite for generation and checking, with minimal thinking and no fallback.
4. Aim to produce one cited brief, inspect it and its Markdown/PDF outputs, and retain the captured input, run/report identities, spending ledger and provider usage.
5. Stop and preserve evidence on failure. Remove only owned resources. No unattended new attempts or recurring profile activation.

## Bounds and prices

The already approved allowance is **$0.25 total for this single attempt**. This proposal allows at most **10 paid HTTP requests**: one embedding and up to nine Gemini requests, including checks and retries. It still processes only **one article**. Actual calls may be fewer.

[Google Standard pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite): $0.30 per million input tokens and $2.50 per million output tokens, including thinking.

[OpenAI Standard embedding price](https://developers.openai.com/api/docs/models/text-embedding-3-small): $0.02 per million input tokens.

At the configured maximum 8,192 input / 4,096 output tokens, the total allocated conservative reservation bound is **$0.11444224**, below the approved $0.25 ceiling. This is a cap calculation, not a predicted bill. Uncertain usage retains its reservation; every retry consumes the same attempt's allocation. Recheck official prices immediately before execution and stop if they require changing the approved proposal.

## Evidence and limits

- [Inactive exact config](config.json), verification ID 8cf55cfa-8162-436b-bb14-2430636fdd34.
- [RSS selection and local claim check](source-selection.json).
- [Fresh CLI validation](validation.json): validation only; no RSS/model requests or ledger creation by the runner.
- [Fresh local checks](offline-checks.json): authorization remains blocked and the allocation is below the allowance.
- Independent Gemini documentation/adapter review found no confirmed incompatibility. The [stable v1 endpoint](https://ai.google.dev/gemini-api/docs/api-versions) and [model support](https://ai.google.dev/gemini-api/docs/interactions-overview#supported-models--agents) are documented, although the generated v1 model enum omits the exact model. Real provider credential/model acceptance remains untested.

No test suite was rerun for this documentation/config preparation. No runtime was started and no database was connected, so no runtime cleanup was needed. Existing browser evidence and shared working-tree changes are preserved.

## Next action

The user can authorize **this exact one-article attempt** by saying “Run this test.” The allowance is already decided. Final execution approval is still required by the user's earlier no-live-execution instruction.

After approval, complete the disposable runtime preflight before setting authorization true, record the approval timestamp and resulting config hash, and execute this verification ID once. Never execute this inactive draft. Phase 2 stays **Verification pending** until the bounded real-feed/model proof passes with its ledger and outputs retained. The browser download and Saved wording gate has already passed.
