# Personal project conversion plan

Revised September 6, 2026. Plan version: **personal-conversion.v1**.

**Phases 1–3 are complete. Phases 4A and 4B are complete for local/synthetic engineering acceptance with ordinary paid activation off; Phase 5 is in progress — raw-reading trial preparation, with model spending off.** See the [Phase 4B acceptance record](evidence/phase-4b.md), [two-service setup/recovery guide](PHASE4B_SETUP.md), and historical [Phase 4A runtime evidence](evidence/phase-4a.md). See the [verified Phase 1 completion record](evidence/phase-1.md) and [Phase 2 implementation and verification record](evidence/phase-2.md). Phase 2 software/offline checks, the September 19 browser download/Saved wording gate, and the September 20 authorized corrected live proof passed. [Live result, ledger and outputs](evidence/phase-2-live-corrected-20260920/README.md) close the remaining gate. This package replaces the earlier roadmap and closes its engineering decision register with explicit behavior, boundaries, examples, and acceptance cases. It is the authoritative conversion plan. The historical assessment and earlier roadmap are retained under `reference/` for context, not as competing instructions.

**Functional-impact review follow-up:** The [potential-expansion backlog](potential-expansions/README.md) records the capabilities narrowed or deferred by this conversion. The same review identified [CORE-01: fresh-article claim/evidence preparation](CORE_FOLLOW_UPS.md), a Phase 2 producer now accepted with independent offline review and a real-feed/model proof from empty business tables. The earlier D1–D8 decisions remain documented; their closure must not be read as proving this subsequently identified dependency complete.

Start the conversion with Phase 1. There is no preliminary requirement to finish the original broad platform. Keep the existing database, frontend technology, reusable backend services, stored data, and research artifacts. Complete unfinished capabilities when they serve the personal workflow; defer the rest.

## The finished product

A local **personal news research desk**: “Help me understand developments in the topics I follow, show the original sources, and keep a brief I can revisit.”

One workspace and one local owner use three main views:

- **Today:** selected grouped stories, available collected articles, real source links, collection coverage, and processing status.
- **Saved:** persistent saved events, including an honest unavailable state if an event can no longer be loaded.
- **Briefs:** descriptive briefs from recorded inputs, claim/source evidence, history, and Markdown/PDF exports.

The daily journey is: request the day's update → inspect stories and sources → save useful events → read/export the brief. Reloading the screen reads stored results. A successful daily update cannot be repeated on the same personal date; failed runs have explicit recovery rules. Manual daily use includes weekends. Automatic scheduling and continuous intraday refresh are future extensions.

Forecast probabilities, broad risk dashboards, general Ask AI, maps, model debates, broad company research, external notifications, public accounts/hosting, new providers, and a database/frontend rewrite are outside this conversion. Existing experimental code and disabled prediction boundaries remain preserved.

## Read the package

See the [public repository snapshot](PUBLICATION.md) for publication checks and
the raw acceptance artifacts retained locally.

| File | Purpose |
| --- | --- |
| [Shared behavior contracts](00-shared-contracts.md) | The rules every phase must follow: selection, time, ownership, snapshots, allowances, retries, runtime, and evaluation. Read first. |
| [Selected personal preferences](SELECTED_PREFERENCES.md) | User-approved feeds, empty interest filters, Gemini generation and OpenAI embeddings; a $0.25 single-verification cap approved, the bounded proof passed, ordinary runtime activation remains off. |
| [Phase 1 — Foundation](01-foundation.md) | Reproducible setup, real/demo separation, authentic evidence, truthful controls. |
| [Phase 2 — Personal workflow](02-personal-workflow.md) | Data upgrades, Today/Saved/Briefs, daily processing, source inspection, persistent saves, brief evidence/export. |
| [Phase 3 — Bounded daily use](03-bounded-daily-use.md) | Settings, collection/admission limits, retained backlog, raw reading, complete paid-request accounting. |
| [Phase 4 — Local runtime](04-local-runtime.md) | Separate 4A and 4B milestones for supervised on-demand processing and fewer persistent services. |
| [Phase 5 — Validation and release](05-validation-and-release.md) | Actual-use trial, reproducible quality samples, personal usefulness, and release evidence. |
| [Potential expansions](potential-expansions/README.md) | Every functional trade-off from the subsequent review, recorded as an optional future candidate with outcomes, dependencies and completion evidence. |
| [Required core follow-ups](CORE_FOLLOW_UPS.md) | Required core work and its closure evidence; CORE-01 passed the Phase 2 live proof. |
| [Coding-agent models and effort](MODEL_AND_EFFORT_PLAN.md) | Recommended orchestrator, implementation and review assignments for each phase; recommendations only, not applied model settings. |
| [Historical assessment](reference/project-assessment.md) | Original source-based findings and the earlier verification results. |
| [Superseded roadmap](reference/previous-roadmap.md) | The previous roadmap, including the formerly unresolved D1–D8 register. Retained as history only. |

The shared contracts control cross-phase behavior. Each phase document supplies implementation steps, existing source targets, expected failure/recovery behavior, concrete acceptance cases, and the evidence required to finish. If implementation uncovers a contradiction, revise the affected contract and acceptance case together; do not silently choose a conflicting behavior or expand scope.

## Current project status

The reviewed application revision is `7c7fceb` from August 7, 2026. The repository already has substantial ingestion, storage, grouping, report, API, and recovery code. The main gaps are the complete personal interface, authentic evidence presentation, personal selection/time behavior, comprehensive limits, simplified operation, and real-use validation. Forecasting remains unvalidated research.

The September 5 review recorded 4,022 Python unit tests, 141 disposable-database integration tests, and 49 frontend tests passing, plus lint and configuration checks. A targeted test in a copy of Git-tracked files failed because a required evaluation protocol was missing. Those are historical verification results, not tests rerun while writing this package. They do not establish a complete live browser workflow or sustained use.

| Milestone | Plan status | Application status | Evidence required to close |
| --- | --- | --- | --- |
| Phase 1 | Written | Complete | [Clean delivery, real/demo/evidence checks and separate review passed](evidence/phase-1.md). |
| Phase 2 | Written | Complete | [Software/offline, browser and bounded live gates passed](evidence/phase-2.md); exact published outputs, ledger and cleanup retained. |
| Phase 3 | Written | Complete | [All P3-01–P3-14 accepted with mapped local/synthetic proof and independent review; ordinary paid activation remains off](evidence/phase-3.md). |
| Phase 4A | Written | Complete | [All eleven required local/synthetic runtime cases passed with actual child/UI/CLI proof, independent review and exact-owned cleanup](evidence/phase-4a.md). |
| Phase 4B | Written | Complete | [All five required local/synthetic cases passed with actual browser/CLI, bounded local cache/pacing, populated restart/restore, independent review, and exact-owned cleanup/preservation](evidence/phase-4b.md). |
| Phase 5 | Written | In progress | [Trial preparation and empty baseline/reading records; actual-use and quality evidence pending](evidence/phase-5.md). |

## Execution order and outcome at every stage

**Sequence: Phase 1 → Phase 2 → Phase 3 → Phase 4A → Phase 4B → Phase 5.** Independent backend preparation may overlap earlier work, but a later milestone does not close before its prerequisites. No calendar completion estimate is asserted before implementation and live setup establish the effort.

| Stage | Work to finish | What you have when it is done | Proof of completion |
| --- | --- | --- | --- |
| **1 — Trustworthy foundation** | Deliver the exact missing evaluation artifact without altering its frozen hash; repair setup/documentation; separate explicit read-only demo from real mode; remove fabricated excerpts and false action success; constrain navigation. | An installable application whose displayed data and limitations can be trusted. Core features still being connected are clearly unavailable. | Fresh delivered-file setup and blank-database migration; real empty/error/stale states; working authentic source URLs; no fixture leakage. See P1 acceptance cases. |
| **2 — First usable personal version** | Add compatible workspace/run/report records; connect event members/sources; persist event save/unsave; implement once-per-date start/status/retry; apply deterministic interests/ranking; freeze source inputs; connect actual briefs, evidence, history, exports. | You can complete the daily reading-to-brief journey, keep saves after restart, and inspect what each summary used. Existing services remain required. | Real browser journey, populated-database upgrade, same-date/retry/overnight cases, immutable input checks, and a small controlled provider/feed proof when its configuration is available. See P2 cases and live limits. |
| **3 — Routine use with limits** | Add settings/readiness; bound capture and new admissions; retain pending items; separate new intake from old raw enrichment; cover every paid dispatch with durable spending reservations; support RSS-only reading. | You can choose feeds, see exact retained backlog and recorded spending, and read available news when AI is disabled, unavailable, or out of allowance. | Boundary/concurrency/restart tests; no double admission or hidden paid path; accurate raw/deferred/failed states; old raw work cannot block permitted fresh intake. See P3 cases. |
| **4A — On-demand processing** | Extract the reusable runner; use the same supervisor for API/command launches; enforce one database-controlled writer, deadlines, and rejection of superseded writes; preserve recovery. | The daily workflow works with the persistent Celery worker and scheduler stopped. Redis remains a temporary dependency. | UI and command runs; competing/stale worker rejection; launch failure, cancellation, crash, and recovery demonstrations. See 4A closure and P4 cases. |
| **4B — Simpler local operation** | Replace personal Redis dependencies with bounded local cache/throughput control; keep money/ownership in PostgreSQL; serve frontend and API together; document startup, shutdown, backup/restore, and mode switching. | Only the application and PostgreSQL run persistently; an update starts a temporary managed processor. | Full workflow with Redis/Celery/Beat absent; correct health/status; preserved limits; verified populated backup/restore and safe runtime rollback. See 4B closure and P4 cases. |
| **5 — Evaluated personal release** | Use the finished release in actual daily sessions; select quality samples deterministically; inspect grouping/source support; obtain personal relevance/usefulness judgments; record costs and upkeep. | A documented release with evidence of whether it helps you, known limitations, and a reproducible setup. | Seven actual sessions; 30 distinct event groups; 10 event summaries with every factual claim checked; explicit reliability/relevance/coherence targets; ordinary-reading comparison and your continued-use judgment. Extend observation if volume is insufficient. See P5 cases. |

**Phase 2 delivers the first functional personal version. Phase 3 makes it suitable for routine use with explicit limits. Phase 4 reduces upkeep. Phase 5 establishes usefulness.** A failure to meet a phase's evidence requirements leaves that phase incomplete, even if much of its code is written.

## What happens to every unfinished area

| Existing unfinished area | Finish before conversion? | Final treatment |
| --- | --- | --- |
| Missing tracked artifact, setup, broken documentation | No separate preliminary project. | Fix first, inside Phase 1. Preserve frozen evaluation inputs. |
| Mixed sample/real content, invented evidence, false success messages | No separate preliminary project. | Fix in Phase 1 before presenting a working personal workflow. |
| Event detail, member articles, original sources, stale/read status | No. | Finish in Phase 2; preserve honest Phase 1 error states throughout. |
| Persistent saved items and local ownership | No. | Finish in Phase 2 with populated-data migration checks. |
| Daily processing controls and brief coverage | No. | Finish identity/start/status/retry/selection/snapshots in Phase 2, allowances in Phase 3, runtime in Phase 4. |
| Brief screen, actual content, evidence, history, export | No. | Finish in Phase 2 using the existing backend where its contract fits. |
| Personal feeds, workload bounds, deferred articles, comprehensive cost controls | No. | Use a tightly bounded proof in Phase 2; finish reusable settings and general enforcement in Phase 3. |
| Grouping quality | Do not finish the original evaluation program first. | Fix workflow-blocking defects in Phase 2; measure and correct personal-feed quality in Phase 5. |
| Entity-linking, historical analogy, composite-alert validation | No. | Disabled/deferred from the personal default. Preserve their code, artifacts, and existing validation restrictions. |
| Forecast training, calibration, prediction release gates | No. | Outside this conversion. Personal-reader success does not validate or enable forecasts. |
| General chat, dashboards/maps, broad provider/company expansion, notifications | No. | Remove from personal navigation and defer. Do not finish prototype actions merely because their screens exist. |
| Service simplification, backup/restore, sustainable local setup | No. | Verify initial setup in Phase 1; reduce services and demonstrate recovery in Phase 4. |
| Public deployment and institutional operational requirements | No. | Public operation is outside scope; keep controls needed for local data, recovery, and spending. |
| Actual relevance, source support, reliability, value | Cannot be established by finishing old code first. | Evaluate the converted release in Phase 5, keeping failures and missing evidence visible. |

## Shared decisions now fixed

These are documented implementation defaults, not hidden claims about user preferences. The full algorithms and edge cases live in [the shared contracts](00-shared-contracts.md).

- **Interests and ordering:** explicit include/exclude phrases; excludes win; complete-word matching; selected feeds control intake. Rank matching groups by source count, recorded hotness, publication recency, then stable ID. Today can show all qualifying groups; the brief selects at most five.
- **Time and coverage:** initial personal calendar is `America/Los_Angeles`, selectable before first run and fixed afterward in v1. Server-computed daily identities include weekends. Overnight retries retain the original identity. Show actual capture/backlog coverage instead of promising a full publication day.
- **Data preservation:** additive migrations; separate personal reports from legacy reports; explicit local-owner binding; idempotent saves; no automatic reassignment/deletion. Keep published brief versions and their actual retained source inputs immutable.
- **Work scope and failure:** record captured, newly admitted, and enrichment-selected items separately. Freeze processing inputs before paid work; retries reuse them. Intentional deferrals remain distinct from unexpected failures. Old failed-run work is not silently retried under a new date.
- **Limits and money:** initial limits are 10 active feeds, 100 new articles/run and local day, and 100 enrichments/run; capture/pending storage is also bounded. Paid work defaults to zero until enabled with configured allowance/routes. Account for embeddings, reasoning, probes, retries, and uncertain requests before more dispatches. Spending months use UTC and display local reset times.
- **Operation and evaluation:** supervised temporary processing, one durable writer across entry points, finite deadlines and stale-write rejection; then an actual-use trial with a reproducible sample, all-claim citation checks, and the user's own relevance/usefulness judgments.

## Inputs still required for live use

| Input | How to resolve it | What can proceed meanwhile |
| --- | --- | --- |
| Actual feed URLs and interest phrases | Selected September 18: BBC Business, Ars Technology, MIT AI and Federal Reserve monetary policy, with empty include/exclude lists. See [approved preferences](SELECTED_PREFERENCES.md). These choices are saved, not applied to runtime; the exact one-article BBC proof identity is retained in the [live attempt](evidence/phase-2-live-20260919/README.md). | All foundation work and deterministic offline workflow tests. |
| Provider/model routes | Selected Gemini `gemini-3.5-flash-lite` for generation/checking and OpenAI `text-embedding-3-small` for embeddings; no generation fallback selected. The corrected September 20 proof successfully published a supported brief using both selected providers/models. Recheck prices before any separately authorized future run. | Offline integration and configuration work. |
| Permitted spending | **$0.25 cap approved; the authorized corrected proof passed at $0.00224186 with no uncertain charges.** Earlier attempts cost $0.00362026 combined. No further attempt is authorized. No recurring budget is approved. The selected-preferences record has no runtime loader; approving this cap does not activate paid processing. | Offline checks and browser verification with synthetic inputs. |
| Existing local owner, only if ambiguous | Reuse an established binding; follow the documented zero/one-user rule; offer explicit selection when multiple existing users remain. | Reading, migration analysis, and independent implementation. |
| Personal trial judgments | The user records relevance and continued-use feedback during the actual trial. | Engineering reliability, coherence, and claim/source checks; do not invent personal judgments. |

No unanswered runtime architecture, relevance algorithm, retry policy, sampling method, or data-ownership algorithm is left to an implicit assumption. Ordinary internal naming/refactoring choices remain with the implementer. Unknown real feed/model behavior will be measured through the specified checks, not presumed successful.

## Progress and completion records

For each phase, record the changed revision/files, implemented behavior, migrations/data checks, acceptance IDs with expected and actual results, browser observations where applicable, and remaining limitations. Create records under `evidence/phase-1.md`, `evidence/phase-2.md`, `evidence/phase-3.md`, `evidence/phase-4a.md`, `evidence/phase-4b.md`, and `evidence/phase-5.md` as implementation proceeds. The [Phase 1 record](evidence/phase-1.md) is complete; [Phase 2 evidence](evidence/phase-2.md) records its completed software/offline, browser and authorized live gates. [Phase 3](evidence/phase-3.md) is complete for local/synthetic engineering acceptance; Phase 4A is complete for local/synthetic engineering acceptance. Phase 4B is complete for local/synthetic engineering acceptance with [all five required cases and preservation/cleanup](evidence/phase-4b.md). Phase 5 is in progress — raw-reading trial preparation, with model spending off.

Use explicit states: **Not started → In progress → Verification pending → Complete**. A dependency awaiting a real input is recorded by name alongside the state. Offline checks cannot be substituted for a requested live proof; live output cannot substitute for a user's usefulness judgment. Update this index only when the phase evidence supports the change. Phase 3's authoritative acceptance table is `evidence/phase-3.md`; behavior changes must update affected criteria and evidence in the same change, retaining prior proof as dated history.

The original plan package was documentation only. Phase 1 execution began September 6, 2026; implementation and verification are recorded in its evidence file as they complete.
