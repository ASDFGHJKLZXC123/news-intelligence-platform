# Personal project assessment

Reviewed September 5, 2026 (America/Los_Angeles), against commit `7c7fceb`.

Conversion scope and phase order consolidated September 6: see [the conversion plan and readiness register](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/PERSONAL_PROJECT_CONVERSION_PLAN.md>) for the implementation order, unfinished-work mapping, concrete outputs, acceptance demonstrations, and outstanding decisions before each affected phase. Its subsequent ambiguity audit distinguishes the settled roadmap from implementation details still requiring specification. It supersedes the short conversion checklist below. All conversion phases remain not started.

**Recommendation:** turn the active product into a personal news research desk: chosen feeds and topics, grouped stories, authentic source links, saved items, and a daily brief. Preserve crisis forecasting as an optional research module. The existing backend is worth reusing; completing the everyday workflow should take priority over adding analytical breadth or rebuilding infrastructure.

This recommendation assumes the main goal is a useful, maintainable tool for one person. A portfolio benefits from the same complete workflow. If forecasting research is the main interest, the bounded research alternative below is more appropriate.

## Current progress

| Area | Assessment |
| --- | --- |
| Backend and database | Substantial implementation: ingestion, deduplication, clustering, entity linking, provider integrations, report generation, exports, APIs, job recovery, and 19 migrations. |
| Daily processing | A seven-stage descriptive coordinator exists with persistent run state and retry handling. It is manually triggered. Prediction and composite-alert stages are explicitly excluded. |
| Frontend | Broad visual prototype with tested API reads, but several central actions still use samples, keep temporary state, or have no implementation. |
| Analytical validation | Incomplete. Entity-linking thresholds have been applied, but the recorded final evaluation uses synthetic/automated labels. Clustering and composite alerts remain unverified; analogies remain evaluation-only. |
| Crisis forecasting | Heuristic research implementation with contracts and metric helpers. The repository does not establish trained, calibrated, out-of-sample forecasting performance. Default prediction reads are disabled. |
| Operations | Considerable local hardening exists. This review verified tests and clean database migrations, rather than sustained operation on live news. |
| Reproducibility | The local workspace passes its tests, but a tracked-files-only copy fails a test because a required evaluation document is absent from Git. |

The stage documents use different numbering and completion definitions. For example, the original plan still calls the coordinator missing, while the current code and README show it implemented. A single percentage-complete estimate would therefore be misleading. The clearest description is **a substantial backend, an incomplete personal-use interface, and unvalidated forecasting research**.

Evidence: [current README](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/README.md:7>), [pipeline graph](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/pipeline/contracts.py:24>), [validation outcomes](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/docs/worklogs/stage9-validation-and-tuning.md:9>), [v2 readiness](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/evaluation/stage9-v2/readiness.json:1>).

## Verification performed

| Check | Result |
| --- | --- |
| Python unit suite in this workspace | 4,022 passed; 141 integration tests deselected. |
| Disposable PostgreSQL integration suite | 141 passed, including the daily-pipeline integration test; blank database migrated through `0019`. Temporary database resources were removed and cleanup verified. |
| Frontend adapter, loader, and data-quality tests | 49 passed using simulated responses and static assertions. |
| Ruff and Docker Compose configuration | Passed. |
| Targeted test in a temporary copy of Git-tracked files | Failed: missing `docs/evaluation/stage9-validation-protocol.md`. |

The clean-copy reproduction used `git archive HEAD` and the existing Python environment, then ran `test_v2_protocol_identity_and_checked_in_scaffold_are_canonical`. This was a targeted reproduction, not a full fresh-install test.

No paid provider calls, live ingestion, or browser end-to-end workflow were exercised. Dependency vulnerability audit, current provider availability/prices, sustained capacity, and production deployment were not revalidated. No application containers were running when initially inspected. Passing fixture tests establishes software behavior, not news accuracy or forecasting ability.

September 6 clarification: the daily-pipeline integration test injects a brief stage that inserts a report directly. It does not verify that a live daytime update feeds the expected articles into production brief selection. The existing brief cutoff is 05:30 New York time, so the conversion needs an explicit personal brief coverage contract to fulfill the promised update-to-brief workflow.

## What makes the current scope unsuitable for a personal project

1. **The product promise is much larger than the evidence.** The original goal covers financial, macroeconomic, geopolitical, and supply-chain crises across multiple future horizons. The baseline converts a stress score into probabilities with fixed constants; ensemble weights are hand-set, and calibration defaults to an identity transform. These are useful experimental components, but calling the result a validated crisis-warning product would overstate progress. Narrow the user-facing promise to finding and explaining developments with sources. Preserve the forecast code and its existing disabled boundaries. [Baseline](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/crisis_model/baseline.py:87>), [calibration](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/crisis_model/calibration.py:15>).

2. **Too many products compete for attention.** Thirteen primary navigation destinations span news, risk, companies, industries, history, maps, alerts, reports, chat, and administration. Backend provider tasks import eleven contextual data clients in addition to the news/AI paths. Each adds setup, maintenance, and interpretation work. Start with one topic or company watchlist and 5–10 chosen feeds; keep other integrations dormant. These numbers are proposed defaults, not technical limits. [Navigation](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/shell.jsx:49>), [provider breadth](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/workers/provider_data_tasks.py:26>).

3. **Basic personal actions remain unfinished.** Ask AI selects canned answers. Reports lists hard-coded samples, and its creation/export/share buttons have no handlers. Watching an item changes an in-memory set rather than persistent personal state. Alert acknowledgement and job retry can show success messages without performing the action. Complete a small number of useful actions before expanding the interface. Some sample screens are labeled, so these should be described as prototypes rather than claimed backend failures. [Reports and Ask](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/page-misc.jsx:118>), [watch state](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/shell.jsx:9>), [alert action](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/page-alerts-admin.jsx:27>).

4. **The evidence experience undermines trust.** API mode appends fixture events and evidence to real results. The evidence drawer displays a generic invented excerpt and disables the original-source link even for real evidence. A personal research tool must make it easy to verify what it says. Separate explicit demo mode from real mode; show actual stored excerpts or say none is available; make authentic source links work. [Mixed content](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/api-adapter.js:1004>), [evidence drawer](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/frontend/app/main.jsx:24>).

5. **Operating it requires more machinery than the initial personal workflow needs.** The default stack has five services: API, worker, scheduler, PostgreSQL, and Redis. The full enrichment path also needs provider configuration, a registered embedding snapshot, and an explicitly installed language model for entity extraction. Preserve the current stack long enough to finish a real workflow, then introduce a simpler personal runtime. Do not discard working database migrations as the first simplification. [Compose](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/docker-compose.yml:4>), [embedding and entity prerequisites](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/packages/config/settings.py:131>), [manual model installation](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/requirements.txt:17>).

6. **Cost control does not yet represent the complete personal budget.** The configured $10 monthly budget belongs to the LLM orchestration path. Embeddings call their provider directly, and embedding jobs default to processing all missing items. That setting should not be presented as a cap on every external expense. Add daily article limits, a workload preview, and shared spending reservations/accounting for reasoning, embeddings, and retries. Retain the current safeguards while extending their coverage. [Budget configuration](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/packages/config/settings.py:160>), [embedding transport](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/packages/providers/openai_embeddings.py:143>), [unbounded default item limit](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/workers/embedding_tasks.py:116>).

7. **Completion depends on a research organization’s work.** Current validation requires independent reviewers and adjudication, including 150 entity tasks, 36 alert tasks, 40 analogy tasks, and at least 100 clustering pairs. Production release also carries deployment and operational requirements. These controls have reasons when making the original claims. A local descriptive reader can have separate completion criteria focused on source support, usability, reliability, and cost. This changes the product scope; it does not turn synthetic labels into human validation or open the forecast gate. [Review requirements](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/README.md:295>), [operational boundaries](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/docs/operations/stage10-runbook.md:3>).

8. **Another person cannot reproduce the local test result from Git alone.** The ignore rules exclude most documentation, including a protocol required by the tracked evaluation code. A clean-copy test fails with `missing immutable v1 artifact docs/evaluation/stage9-validation-protocol.md`. Publish the existing exact protocol bytes with a narrow ignore exception, preserving its frozen hash; then verify the clean checkout. Also reconcile public README links and install instructions. Dependency files mostly use version ranges despite metadata describing them as pinned, so reproducible dependency resolution deserves a separate cleanup. [.gitignore](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/.gitignore:37>), [required artifact](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/evaluation/stage9_v2.py:73>), [reproduced failing test](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/tests/unit/test_stage9_v2.py:133>).

## A more appropriate personal project

**Personal news research desk:** “Help me understand what changed in the topics I follow, show the original sources, and keep a brief I can revisit.”

The distinctive feature can be grouping multiple articles about the same development and preserving the evidence behind each summary. That is already technically interesting and makes use of existing work.

| Keep active | Simplify | Defer from the main product |
| --- | --- | --- |
| RSS ingestion, deduplication, event grouping | A small feed allowlist and one interest area | Broad global provider collection |
| Evidence links, provenance, honest missing-data states | Three screens: Today, Saved, Briefs | Global risk dashboards, forecast probabilities, model debates |
| Report generation and existing export backend | One daily brief and one reasoning route | Multiple report products and multi-vendor evaluation campaigns |
| Persistent saved items and a small watchlist | Simple entity aliases/manual correction where sufficient | Full identity enrichment and company-research expansion |
| Tests, recoverable runs, backups, budget accounting | Local single-user operation | Institutional deployment requirements as prerequisites for personal use |

Keep PostgreSQL and the current schema initially. JSONB, vector operations, and report advisory locking are already used. Although SQLite is well suited to local applications, converting this repository would be a separate migration effort. It becomes worth considering only if database setup remains a demonstrated obstacle after scope reduction. [SQLite guidance](https://www.sqlite.org/whentouse.html), [existing database models](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/db/models/core.py:139>), [report locking](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/reports/lifecycle.py:491>).

A later runtime could use an app process, PostgreSQL, and an on-demand processing command. The existing coordinator already invokes stages synchronously, making reuse plausible. This requires extracting reusable service functions, keeping durable run ownership/recovery, and replacing Redis-backed cache/rate limiting for single-process use. Removing containers alone would break dependencies. Celery remains useful for heavier work across processes or servers; simplification should follow the chosen operating needs. [Stage execution](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/workers/pipeline_stages.py:1>), [Redis dependencies](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/services/llm/runtime.py:79>), [FastAPI guidance](https://fastapi.tiangolo.com/tutorial/background-tasks/#caveat).

## Prioritized conversion plan

This table is a summary. [The expanded phase-by-phase specification](</Users/f8fq/coding projects/Unfinished/news-intelligence-platform/PERSONAL_PROJECT_CONVERSION_PLAN.md>) defines the deliverable, new versus reused work, success/failure demonstrations, and scope boundaries for every phase, including Phase 4A/4B.

| Priority | Work | Completion evidence |
| --- | --- | --- |
| 1 — Honest, reproducible foundation | Track required evaluation artifacts; reconcile public setup/status docs; create distinct demo and real modes; hide unsupported actions and predictions. | A clean checkout runs the documented checks. Real mode contains no fixture stories or invented excerpts. |
| 2 — One complete personal workflow | Connect real event details and source links; persist saved items; connect existing brief history/export APIs; show actual refresh, failure, and freshness state. | Fresh stories → grouped event → original source → saved item survives reload → real daily brief can be opened/exported. Verify this path in a browser. |
| 3 — Bounded daily use | Add a personal configuration, selected feeds, article limits, spending coverage, and a setup preflight. Make optional enrichments unnecessary for reading available news. | A source outage or AI failure leaves readable existing items and clear status; retries avoid duplicates; the total budget is observable and enforced across paid paths. |
| 4 — Reduce upkeep | Introduce the direct runner and appropriate local cache/limiter, then make the queue and scheduler optional. Retain existing database/data and experimental code. | The selected personal workflow runs with fewer required services and preserves retry/ownership behavior. |
| 5 — Establish usefulness | Use it regularly for a week, inspect a small real sample of groupings and summaries, and record relevance, source support, duplicate mistakes, cost, and time saved. | You can identify concrete value over reading the feeds directly. Keep only features that improve that result. |

These are proposed milestones, not a new claim that the existing pipeline already supports a minimal mode. Scheduling can follow successful manual use. Do not make a database rewrite, a new frontend framework, additional providers, or forecast validation prerequisites for the first complete reading workflow.

If the main goal is instead forecasting research, scope it to **one geography, one defined outcome, one forecast horizon, and one simple baseline**, with historical inputs available at each evaluation date and a reproducible held-out comparison. A well-documented negative result can finish that personal research project. Keep it separate from the descriptive app and preserve existing evaluation artifacts and holdout boundaries.

This assessment and its expanded plan are documentation only; the proposed conversion and identified fixes have not been implemented.
