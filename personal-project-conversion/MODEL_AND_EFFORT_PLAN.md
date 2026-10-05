# Coding-agent model and reasoning-effort plan

Recommended September 6, 2026 for execution of the [personal conversion plan](README.md).

**Status: Phases 1 and 2 are complete; the Phase 2 continuation used the user-requested GPT-6 Astra with Ultra effort and inherited bounded reviewers. Phase 3–5 assignments remain recommendations only.** These choices concern the coding agents that would build/review the project. They do not select the news application's embedding or summarization provider, enable paid application processing, or schedule expansion work.

The assignments below are engineering judgments based on this repository's tasks, not measured model benchmarks on this project. Recheck available models/settings at execution time and record the actual model/effort in each phase's completion evidence.

## Recommended assignments

Use one continuing orchestrator on `gpt-6-astra`, with the phase-specific effort below. It owns shared behavior contracts, division of work, integration, resolving conflicting findings, and phase acceptance. Delegated agents receive bounded responsibilities; assigning multiple agents does not make shared schema or transaction changes independent.

| Phase | Orchestrator model / effort | Main implementation model / effort | Separate review model / effort |
| --- | --- | --- | --- |
| 1 — Foundation | Continuing root orchestrator | GPT-5.6 Sol / `xhigh` (user-selected, applied) | GPT-6 Astra / `high` (user-selected, applied) |
| 2 — Personal workflow | Continuing root orchestrator | GPT-5.6 Sol / `xhigh` (user-selected, applied) | Fresh GPT-6 Astra / `high` (user-selected, applied) |
| 3 — Bounded daily use | GPT-6 Astra / `xhigh` | GPT-6 Astra / `high` for the spending ledger; GPT-5.6 Sol / `high` for intake/settings/raw reading | GPT-6 Astra / `xhigh` |
| 4A — On-demand processing | GPT-6 Astra / `xhigh` | GPT-6 Astra / `high` for runner, supervision and ownership integration | GPT-6 Astra / `xhigh` |
| 4B — Simpler local operation | GPT-6 Astra / `high` | GPT-5.6 Sol / `high` | GPT-6 Astra / `high` |
| 5 — Validation and release | GPT-6 Astra / `high` | GPT-5.6 Sol / `high` for sampling/reliability analysis; GPT-5.6 Terra / `medium` for bounded evidence tabulation/documentation | GPT-6 Astra / `high` for grouping/source-support and release-evidence review |

Exact model identifiers: `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`. A separate reviewer means a different agent from the implementation author, working from the actual requirements, changes and evidence. It does not require a different model family and does not constitute independent human review.

## Why and how to delegate each phase

**Phase 1:** The work is bounded but requires careful separation of real/demo state, authentic source rendering and exact preservation of a frozen artifact. Split repository/setup verification from UI/data-mode work after the adapter contract is settled. For the September 6 execution, the user selected Sol at xhigh effort for implementation and a separate Astra reviewer at high for state transitions, source URLs, artifact integrity and the clean-delivery proof. Two bounded Sol implementers handled frontend and setup; root integrated and verified the result. The [completion record](evidence/phase-1.md) records actual outcomes. Terra at medium may handle link inventories or documentation edits with a fixed brief, not decide the real/demo behavior.

**Phase 2:** This phase crosses database, API, frontend, run identity, snapshots and report evidence. For the September 8 execution, the user selected GPT-5.6 Sol at xhigh effort for implementation and a fresh GPT-6 Astra agent at high effort for independent review. Two Sol implementers owned separate areas during that implementation: the original handled coordinator/worker/runtime, schema and legacy writer protection; the second handled frontend, the separate personal brief/evidence/export read APIs, and the reusable offline launcher. The second also owned the new bounded live-verification harness, while the original owned its worker/API integration and fail-closed eligibility. Both used xhigh effort. The continuing root agent orchestrated shared schema/API decisions, [CORE-01](CORE_FOLLOW_UPS.md), integration and acceptance. Actual work and evidence are recorded in the [orchestration record](evidence/phase-2-orchestration.md). The concluding browser/live continuation and scoped correction used the user-requested GPT-6 Astra at Ultra effort with inherited bounded reviewers; all Phase 2 acceptance gates passed. Avoid competing edits to the shared schema/run pipeline. The separate Astra review must inspect populated upgrades, retries, immutable sources, report identity and production briefing without preloaded claims. A higher effort setting does not substitute for this live/offline evidence.

**Phase 3:** Incorrect accounting or ownership can allow extra requests, lose pending items, or retry the same work under a new allowance. Give the ledger/reservation implementation to Astra at high; split intake/backlog and settings/raw-reading work to Sol at high after shared transaction interfaces are settled. Astra at xhigh orchestrates and reviews concurrent reservation, uncertain charges, rollback, month rollover, scope transfer and every paid path, including CORE-01 processing.

**Phase 4A:** This has the highest concentration of execution-lifecycle risk: delayed children, superseded writers, in-flight requests, database loss and deadline enforcement. Astra at high implements the runner/ownership work, while the xhigh orchestrator fixes token/state/lock-order contracts and integrates changes. A separate reviewer can develop fault cases while implementation proceeds. Keep edits to common ownership helpers coordinated. Review must prove stale writes are rejected in the business transaction, not just in final status updates.

**Phase 4B:** The hardest ownership work should already be proven in 4A. Sol at high handles local cache/limiter assembly, optional-dependency initialization, static serving and startup configuration. Independent tasks can verify service inventory and backup/restore. Astra at high reviews that reduced infrastructure preserves spending, data and recovery behavior. Terra at medium can edit the verified setup guide; it should not design database restoration or downgrade behavior.

**Phase 5:** Sol at high builds reproducible sample selection and analyzes run/cost/reliability evidence. Terra at medium can organize predetermined log fields and format the release record without inventing judgments. Astra at high checks grouping, all factual claims in sampled summaries, conflicting findings and the release decision. The user still supplies personal relevance and continued-use judgments. No model can replace the actual observation sessions or turn its own labels into independent human review.

## Effort and escalation policy

- `medium`: bounded organization, documentation and mechanically checkable tasks with clear inputs and acceptance criteria.
- `high`: the default for substantial implementation and review.
- `xhigh`: selected for the orchestrator/reviewer when several contracts interact, and for unresolved claim/evidence or concurrency design. Routine work within that phase can still use its assigned high/medium worker.
- `max`: not a default for any phase. Consider only for a specific unresolved problem after a reproducible failure or review disagreement shows what additional reasoning must resolve. First narrow the question and gather missing evidence; do not repeatedly escalate without learning.

These are starting assignments, not guarantees that greater effort improves every result. Record concrete defects, verification time and unnecessary rework before adjusting the policy. Model selection does not replace tests, browser demonstrations, a populated-data migration check or source inspection.

Use up to three independent delegates only when the tasks can actually proceed in parallel under the current four-agent limit. Do not keep every worker active merely to fill available slots. Review starts from concrete changes and acceptance evidence; the orchestrator resolves findings and verifies the combined result before closing a phase.

## Basis for the recommendation

The current Codex task tools expose all three recommended models and the high/xhigh settings used here. Official OpenAI documentation describes [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra) as the strongest model for complex end-to-end work and lists high/xhigh reasoning support. That supports choosing it for orchestration and difficult integration, while the phase-specific allocation remains my judgment.

[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) is documented for complex professional work and supports the recommended effort levels. [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra) is documented as balancing capability and cost, supporting its limited role on straightforward delegated work. Their public API specifications do not establish a total Codex project cost or the best effort for this repository; neither has been measured here.

The earlier conversion acceptance cases and the potential-expansion backlog are unchanged by this staffing recommendation.
