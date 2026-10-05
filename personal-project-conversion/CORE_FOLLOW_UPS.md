# Required core follow-ups

Recorded September 6, 2026 during the functional-impact review. These are required gaps in the promised conversion workflow, not optional items in the [potential-expansion backlog](potential-expansions/README.md).

## CORE-01 — Prepare supported factual claims from fresh articles

**Status: Complete for Phase 2, September 20, 2026.** The producer passed independent offline checks, the genuine browser workflow/export gate, and the [authorized real-feed/model proof](evidence/phase-2-live-corrected-20260920/README.md).

**Original September 6 finding:** The report context loader reads supportive claim/evidence relationships through `Event → EventArticle → Article → EvidenceItem → ClaimEvidence → Claim`. The composer permits citations to those claim identities and degrades a section when no citable claims exist. Targeted searches of production services, workers, scripts, APIs and packages did not identify the production step that creates this claim/evidence chain from fresh RSS articles. This is a review finding, not a claim that every possible runtime path has been exercised.

Existing source evidence: `services/reports/context_repository.py` describes the linked-only read and performs the inner joins; `services/reports/composition.py` describes the no-citable-claim behavior; `services/reports/lifecycle.py` requires actual Claim identities at publication. Entity linking in `workers/entity_linking_tasks.py` extracts/links entity mentions and must not be assumed to supply factual claim/evidence records.

**Why it matters:** Reusing the existing composer and enabling RSS → embeddings → clustering does not by itself prove that new articles can produce useful cited summaries. An exported report consisting only of missing-evidence notes cannot satisfy the promised ordinary non-quiet briefing workflow. This appears to be a preexisting integration gap, not damage caused by disabling optional entity enrichment.

**Required Phase 2 work:**

1. Identify an existing compatible production producer if one exists, or implement explicit claim/evidence preparation from the permitted retained article inputs in the personal run. Record its concrete location and contract.
2. Specify its supported claim form, actual retained evidence, source-span/revision linkage, validation, idempotent identity, retry behavior and bounded resource use before implementation. The original plan's generic references to extraction/context preparation do not settle these details.
3. Persist only supported links into the run's eligible source scope and freeze their identities/source revisions with the brief inputs. No fabricated citation identities, unsupported claim promotion or evidence-check bypass is allowed.
4. Account for any model calls, probes or retries within the existing Phase 2 live-smoke bounds and later Phase 3 ledger. Reuse the same processing ownership rules and distinguish unsupported content from system failure.

**Acceptance evidence:** Start with a disposable database containing no preloaded claims or claim-evidence links. Capture a small permitted real-source example containing a clear reportable factual statement. Run production intake, grouping, claim/evidence preparation and report generation. Demonstrate at least one substantive source-supported event summary, open its exact cited source input, and export the same report version. A fixture that inserts a ready-made report or manually supplies the claim links does not prove this integration.

Also exercise missing/insufficient evidence, changed article text, retries and duplicate inputs. Unsupported material must abstain or fail honestly; repeated processing must not duplicate evidence or change a published version. Existing source-support tests and a bounded live proof remain separately identified.

**Closure:** Name the implemented producer, record its behavior and affected schema/API contracts in Phase 2, and attach the actual verification results. Until then this is an explicit open core requirement. Completing optional expansions cannot substitute for it.

**Implemented producer and current proof (September 9):** [services/personal/claims.py](../services/personal/claims.py) prepares bounded, deterministic factual excerpts from the retained title/RSS summary of eligible observation articles. Exact source spans and hashes are validated; source support is not independent fact verification. Stable claim/evidence identities, supports-only links with unknown confidence, source revision provenance and ownership checks feed the immutable brief snapshot. The actual coordinator and registered worker invoke this producer before the shared composer, grounding, copyright and publication gates.

[Independent coordinator review](evidence/phase-2-coordinator-fixed-review.log) covers fresh preparation, bounded larger captures and frozen retry. The [actual offline API/Redis/Celery run](evidence/phase-2-worker-browser-run.json) began with zero Article/Event/Claim/ClaimEvidence/Report rows and produced one article, one event, two supported claims and a published report with substantive scripted provider output through the real composer. No claim links or reports were preloaded. Exact citation and Markdown/PDF output were fetched and the PDF visually inspected. The [exact report API review](evidence/phase-2-report-api-fixed-review.log) verifies frozen source stability and shared canonical claims.

The September 9 record closed the missing producer implementation gap while leaving live/browser acceptance pending. The September 19 genuine Chrome workflow/download evidence and September 20 corrected live proof now close those requirements. The real coordinator created a supported claim from the retained BBC Japan interest-rate headline and published substantive prose after two successful real semantic checks. Its exact cited input, same-version Markdown/PDF, reconciled $0.00224186 ledger and owned cleanup are retained. This establishes the bounded source-to-brief integration, not independent factual truth or human usefulness.
