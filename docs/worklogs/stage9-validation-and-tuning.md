# Stage 9 Worklog — Validation and Tuning

Date: 2026-07-18
Protocol: `stage9-validation.v1`
Scope: deterministic, offline validation and threshold governance over the Stage 8 gold assets

## Outcome

Stage 9 has a written and implemented validation protocol, tuning-only domain reports, a frozen
parameter/manifest pair, and one completed one-time entity-linking final report. Entity linking is
the only calibrated domain and the only domain whose selected values were applied to production.
Analogy remains evaluation-only. Clustering and composite alerts remain `not_verifiable`; neither
status is a proxy for a passing result.

No dependency was added or changed, and the Stage 9 evaluation paths require no database.

## Protocol and audit inventory

The protocol is `docs/evaluation/stage9-validation-protocol.md`. It fixes the lifecycle before final
evaluation: enumerate on train, select exactly one policy on development using a declared objective,
freeze and hash the protocol/reports/parameters, and report the final holdout once. A poor final
result cannot be retuned against the same holdout; another attempt requires a new protocol version
and a new unseen holdout.

The audit inventory is:

- shared deterministic primitives: `services/evaluation/calibration.py`;
- domain evaluators: `clustering_calibration.py`, `entity_linking_calibration.py`,
  `analogy_validation.py`, and `alert_validation.py` under `services/evaluation/`;
- freeze/apply and one-time final machinery: `services/evaluation/stage9_freeze.py`,
  `services/evaluation/entity_linking_final.py`, and `services/evaluation/apply_entity_linking.py`;
- development reports: `evaluation/stage9/development/{clustering,entity-linking,analogy,alerts}.json`;
- frozen artifacts: `evaluation/stage9/frozen/{parameters,manifest}.json`;
- entity final artifacts: `evaluation/stage9/final/entity-linking.json` and
  `entity-linking.unseal-receipt.json`;
- implementation audit trails: `docs/implementation-logs/stage9/`.

All reports use canonical sorted JSON with a trailing newline, exact-byte SHA-256 input hashes,
stable ordering, Decimal grids, no wall-clock input, no randomness, and overwrite refusal where an
artifact is intended to be immutable or one-time.

## Declared grids, objectives, and tie-breaks

### Clustering

- Intended threshold grid: `0.700` through `0.950` inclusive by `0.005` (51 points).
- Eligibility: nonzero predicted positives and precision at least `0.90`.
- Objective: maximize recall, then F1, then proximity to live `0.80`, then prefer the higher
  threshold.
- Result: `not_verifiable`; no genuine labeled same-event/different-event pair asset exists, so the
  grid was recorded but not executed and no threshold was selected.

### Entity linking

- Fixed weight set: `adr0005-stage2-initial.v1`; signal weights were not tuned.
- Grid: accept and adjudicate each range from `0.000` through `1.000` by `0.005`, constrained to
  `0 <= adjudicate < accept <= 1` (20,100 policies).
- Train eligibility: at least one automatic accept, auto-accept precision at least `0.95`, and NIL
  recall at least `0.80`.
- Development objective: maximize auto-accept precision, correct auto-link recall, routed-link
  recall, and NIL recall; then minimize adjudication rate and total distance from `0.850 / 0.500`;
  then prefer higher accept and higher adjudicate thresholds.
- Selected bands: accept `0.070`, adjudicate `0.000`.

### Analogy

- Floor fixed at `0.60`; the 40 pairs are unsplit and have no negative/no-reliable-analogy labels.
- Metrics are descriptive only: hit@1/3/5, recall@5, MRR, abstention, and threshold diagnostics.
- Result: `evaluation_only`; no grid was swept and no selection or reranker-quality claim was made.

### Alerts

- Conditional boundary grid: `0.10` through `0.90` by `0.01` (81 points), usable only with genuine
  outcome-blind candidate and baseline prediction artifacts.
- Candidate eligibility requires at least one alert and at least one true positive. The objective is
  precision, then mean true-positive lead time, recall, proximity to `0.50`, then the higher
  threshold.
- The composite release gate must strictly beat the baseline on both precision and mean TP lead
  time in every required window (`2007-2009`, `2020`, `2023`). Ties, missing windows, missing labels,
  no-alert evidence, or no-TP evidence fail closed.
- Result: `not_verifiable`. The evidence tuple is empty, the composite remains experimental and
  blocked in all three windows, and the explicitly separate single-signal carve-out remains
  released.

## Development and frozen outcomes

Development reports record these statuses:

- clustering: `not_verifiable`, live `0.80` unchanged;
- entity linking: `calibrated`, selected `0.070 / 0.000` from 3,240 train-eligible policies;
- analogy: `evaluation_only`, floor `0.60` unchanged;
- alerts: `not_verifiable`, boundary `0.50` unchanged, composite unreleased, single-signal released.

The checked-in frozen manifest records the following development report hashes:

- clustering: `fed869dfc76d16c48add89f362f60d19855e90035eb20e0f0e775cc33e69ba96`;
- entity linking: `3ee276f80a4c615dae1567402d5d8eca05dde2bab48ddd7c3bf22b21cd0131db`;
- analogy: `18f4331f7aecfa60ced44f55215125780c97cb50fc46373418ec26e84a7500ec`;
- alerts: `631e5c759c14be169205cb688fe65d411ce656cc8af115c92bb6ea695afca0cb`.

It also records frozen parameters hash
`6c1e5651bb7f6ed8ec892246b76888edca8c468b4e22eac5430e2e33915cac51`
and protocol hash
`494f97fb5357ecda8d35a296567fde0d9c503be58257ea003df2bd4625fda3af`.
The entity-linking bands were then applied under policy version
`entity-linking-bands.stage9-validation.v1`; the weight set remained unchanged.

## One-time entity-linking final result

The entity final holdout was scored once at the already-frozen `0.070 / 0.000` bands. The 50-case
report records:

- auto-accept precision `1.0` (33 correct accepts, zero wrong-target or NIL accepts);
- correct auto-link recall `0.868421`;
- routed-link recall `1.0`;
- NIL recall `0.833333` (10 of 12);
- adjudication rate `0.14` (7 of 50);
- candidate recall and top-target recall `1.0`.

The result did not select or change a policy. The split is now spent, and the report records
`no_retuning=true` and `evaluation_status=final_reported_once`.

This result is limited by its labels: they are synthetic/automated, original-synthetic English
ORG/PRODUCT positives only, with no human adjudication and no coreference. It measures agreement
with automated labels, not human-validated ground truth. The stage-3 LLM adjudicator was also not
exercised; routed-link recall only establishes that the expected target reached the deterministic
candidate route.

## Governance caveat

The tuning and freeze implementations exclude sealed holdouts by construction, and the one-time
entity command is the sole entity path allowed to opt in after hash verification and explicit
acknowledgement. During post-freeze verification, a verifier broad search surfaced only split-marker
and path snippets referring to the holdout. No holdout labels or outcomes were used for selection,
retuning, or any production decision. This search exposure should nevertheless be retained in the
audit record as a governance caveat, not silently omitted.

## Roadmap and live-value mismatches

The roadmap names clustering `0.82`, while the implemented live default and Stage 9 frozen effective
value remain `0.80`; `0.82` is informational only until verifiable labeled pairs support a change.
The original entity-linking roadmap/live bands were `0.85 / 0.50`; Stage 9 legitimately changed
them to frozen and production-applied `0.070 / 0.000` under the versioned band policy. Analogy
remains live/frozen at `0.60`, and the alert decision boundary remains `0.50`; neither was tuned.

## Verification and closeout

- Focused Stage 9 verification: **431 passed**.
- Independent verifier runs: **127 passed**, **122 passed**, and **162 passed**.
- Repository-wide non-integration suite after the post-unseal test repair: **3,716 passed, 135
  deselected** in 378.94 seconds.
- Repository-wide Ruff: **PASS** (`All checks passed!`).
- Diff whitespace/error check: **PASS**.
- Evaluation remained offline: no database and no network-dependent scoring path.
- Dependencies are unchanged.

The first repository-wide run exposed one stale test assumption: it expected the canonical final
report never to exist. The authorized one-time evaluation had correctly created the report and
receipt. The single repair snapshots both artifacts before an unacknowledged CLI call and proves
that neither existence nor bytes change; its narrow file passed **33 tests**, and the full suite
then passed as recorded above.

Git scope and cached-name review are performed immediately before the single Stage 9 commit. Stage 9
is intentionally not pushed.
