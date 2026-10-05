# Personal composition correction — September 20, 2026

**Scoped implementation complete; offline checks passed. Live verification remains pending.**

The [authorized live retry](../phase-2-live-retry-20260920/README.md) exposed personal RSS evidence being sent through a market/risk-oriented prompt with rigid legacy minimum lengths. That attempt remains failed and immutable. The correction below made no additional paid requests.

## Behavior

New personal snapshots freeze composition_policy=personal_descriptive.v1 inside their hashed payload. Personal executive/event prompts use the frozen citable source assertions, preserve attribution/uncertainty, and do not receive market-opening instructions, risk/alert data, hotness/risk metadata or prior-market narratives. Distinct personal prompt names and version identify the new behavior in audit/cache records.

Personal prose may remain short when evidence is sparse. Targets of 60/90 words are guidance only; the hard executive/event ceilings remain 120/180 words. Empty or persistently overlong output still degrades after at most one budget retry. The one-word formatting floor is not a substantive-quality acceptance standard: CORE-01 still requires an intelligible supported summary.

The same immutable policy applies to initial composition, budget retry and grounding regeneration. Historical snapshots without a policy keep legacy behavior; unknown/malformed policies fail before model calls. Published recovery and old snapshot/report contents remain unchanged.

An additional personal lifecycle guard prevents publication when grounding regeneration itself fails composition or misses its word budget. Source/evidence scope, schema and citation validation, semantic grounding, copyright thresholds, finite retries and the existing publication checks remain enforced. Legacy prompts/budgets, model routes, migrations and schema envelopes are unchanged.

## Implementation

- [Policy contracts](../../../services/reports/contracts.py), [shared composer](../../../services/reports/composition.py), [personal prompt builders](../../../services/reports/prompts.py).
- [Snapshot freezing](../../../services/personal/snapshots.py) and [personal generation/publication](../../../services/personal/briefs.py).
- [Policy boundary unit tests](../../../tests/unit/test_personal_composition_policy.py), [extended generation integration tests](../../../tests/integration/test_personal_brief_generation.py).
- [Updated Phase 2 specification](../../02-personal-workflow.md).

No original-platform research or Phase 3 work was added.

## Fresh verification

- **84 unit tests passed**, executed by the implementation agent: personal policy, legacy report budgets/prompts/composition, grounding and copyright. [Exact record](focused-unit-verification.json). Root inspected this current result rather than rerunning the same unit suite. Two intermediate failures were new test-fixture/assertion mistakes; final checks passed after test corrections without application changes.
- **16 integration tests passed**, independently run by root against a new owned PostgreSQL container with every model credential blank. They cover personal generation/publication, new/old/unknown snapshot policy identities, failed or over-budget grounding regeneration, frozen claim scope, brief API and export behavior. [Full log](focused-integration.log) and [environment/result](focused-integration.json).
- Scoped Ruff and git diff checks passed. Full historical regression suites and frontend/browser checks were not rerun; the previously accepted browser gate remains recorded separately.
- Independent code review is recorded in [review evidence](independent-review.md).

These are offline scripted-provider checks; they do not establish current real model output quality.

## Cleanup and remaining gate

[Cleanup verification](cleanup-verification.json) confirms zero leaked test databases, removal of the exact owned integration container, closure of port 55152, and unchanged IDs/start times/running states for all seven pre-existing containers. No API/worker/Redis/frontend process was created. The prior live database dumps and ledgers remain preserved.

Phase 2 stays **Verification pending**. The only remaining acceptance gate is a newly authorized bounded real-feed/model attempt that produces an intelligible supported published brief, reconciled ledger and inspected exact-report outputs. The live retry spent $0.00362026 with no uncertain charges; this correction added no paid calls. Another paid attempt has not been authorized or started.
