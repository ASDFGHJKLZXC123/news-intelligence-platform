# Stage 8 Worklog — Gold Datasets (cross-dataset governance, item 6)

Build source: `docs/implementation-order.md` §8 (Gold datasets) and
`docs/building_plan_gap_review.md` Top-10 action 5 ("three small gold datasets … they
unblock evaluation everywhere"). This log covers **bounded item 6 only**: cross-dataset
integrity, leakage, provenance, coverage, and the governance tests and documentation that
sit *across* the three assets. The individual schema/dataset contracts and their suites
were built in earlier item passes and are accepted as-is here.

## Objective and boundary

Audit what cross-dataset governance is already covered transitively by the per-dataset
suites, then add only stable, deterministic, offline tests that close genuine cross-asset
gaps — without duplicating the dozens of assertions the individual suites already own.

Write scope was exactly two new files:

- `tests/unit/test_stage8_gold_datasets.py`
- `docs/worklogs/stage8-gold-datasets.md`

No other file was created or edited. No production code, data, manifest, dependency
manifest, or lockfile changed. No database or network is used anywhere in the added work.

## Inventory of the three gold assets

All three load through **production** evaluation loaders, purely from committed JSON.

### Entity-linking gold v1 — `evaluation/gold/entity_linking/v1/`
Contract: `services/evaluation/entity_linking_gold.py` (schema `v1`, `en`).
- **200 mentions**, split train/development/final_holdout = **100 / 50 / 50**.
- **150 link + 50 NIL.** Positive links are ORG (133) and PRODUCT (17) only — the
  implemented company labels; NIL spans ORG (17), PRODUCT (9), PERSON (12), GPE (12).
- **27 catalog targets** across **11 countries**; assertions asserted/denied/speculative =
  116 / 39 / 45; every case tag present above its milestone minimum (aliases, tickers,
  acronyms, ambiguity, subsidiary/parent, geographic disambiguation, hard negatives, NIL).
- Provenance `original_synthetic`; adjudication is automated, with no human review and
  independent verification pending. Final holdout gated.

### Historical alert-episode gold v1 — `evaluation/gold/alert_episodes/v1/`
Contract: `services/evaluation/alert_episode_gold.py` (schema `v1`).
- **48 cases**, split **24 / 12 / 12**; **24 positive / 24 control**.
- Windows **2007-2009 / 2020 / 2023**, each exactly 16 cases (8 positive / 8 control), and
  each split covers each window with at least one positive and one control.
- Coverage: **5 risk types**, 48 distinct targets, 19 geographies, all four canonical
  horizons; one horizon per case, judged only inside that horizon's calendar-month bucket.
- Review state `automated_contract_validation_only`; `human_verified` and
  `independent_historical_verification` are both `false`. Final holdout gated.
- The Lehman record uses SEC Exhibit 99.1 dated 2008-09-10 for onset and the bankruptcy
  8-K for outcome (5-day lead, `actual_start_date` 2008-09-15); pinned by the per-dataset
  suite, not re-pinned here.

### Analogy gold (accepted pre-existing asset) — `db/seed/episodes/analogy_gold.json`
Contract: `services/analogies/corpus.py`.
- **40 labelled query→analogy pairs** over a 100-episode curated corpus; every target is a
  rankable **leaf** (never a parent arc) and lives in a family the pair searches.
- All 8 episode types covered; 22 counterexample pairs; 41 distinct expected episodes.
- Corpus review: 0 human sign-offs — every row is honestly reported as pending review.
- **Accepted, not rewritten.** The new tests reuse only its public loading/validation
  surfaces (`load_corpus_and_gold`, `EpisodeCorpus.parent_ids`, `GoldSet`,
  `unreviewed_episodes`, `quota_report`); none of its internals are duplicated.

> Note (contract location): the item-6 brief referenced
> `services/analogies/gold_set.py`; the analogy gold logic actually lives in
> `services/analogies/corpus.py` (there is no `gold_set.py`). No file was created to
> reconcile the name — the tests target the real module.

## Provenance and labelling policy

- **Entity** prose is original synthetic text; every mention carries
  `provenance_kind == "original_synthetic"` and no borrowed article URL/publisher.
- **Alert** prose is original paraphrase of sourced historical facts; onset/outcome/control
  citations resolve to authoritative https documents and each declares which side
  (onset/outcome/control) it may attest to, so provenance cannot be borrowed across the
  onset/outcome boundary.
- **Analogy** narratives are original prose with allowlisted institutional sources.
- No asset claims human authorship, human review, or independent/historical verification it
  did not perform. This is asserted both by the per-dataset manifest suites and, across all
  three at once, by `test_all_three_review_postures_are_honest`.

## Onset/outcome and look-ahead protections

- **Alert:** onset fields are strictly as-of — no hindsight phrase, no year after
  `evaluation_as_of`, no realised outcome date/class, and no source title. `label_available_on`
  sits on or after `evaluation_end`, which sits after `evaluation_as_of`, so a label could
  not have been known at prediction time. The Stage-9 hand-off DTO
  (`OutcomeEvaluationInputs.as_kwargs()`) carries outcome truth but **no threshold**.
- **Entity/Analogy:** query/onset text is guarded against hindsight and future-year leakage
  by the shared `find_hindsight_phrases` / `find_future_years` checks.

## Holdout governance for Stage 9

- Entity and alert both expose `TUNING_SPLITS == (train, development)`; `load_tuning_corpus`
  excludes `final_holdout` **by construction**, and `load_split(final_holdout)` refuses to
  load without an explicit `allow_holdout=True` opt-in.
- The cross-dataset suite loads **only the tuning slices** (train + development) for both
  split-based assets — its module fixtures, offline load, determinism, review-posture, alert
  hand-off, and label-inventory checks all run on `load_tuning_corpus`. It never opts into,
  parses, copies, validates, counts, or iterates a `final_holdout` record. The holdout is
  exercised **only as a rejected, gated access**: the loader raises when `final_holdout` is
  requested without `allow_holdout`, and that rejection is all this module asserts.
- An autouse tripwire (`_forbid_final_holdout_access`) spies on the file read path and on the
  split loaders' `allow_holdout` opt-in, so any accidental `load_corpus()`, direct read of a
  `final_holdout.json` file, or `allow_holdout=True` fails the offending test loudly. A
  meta-test (`test_the_final_holdout_guard_blocks_any_holdout_access`) proves the tripwire
  fires, and a C-level `open`-audit run confirms no `final_holdout.json` is opened at runtime
  by the module.
- Full-corpus holdout integrity (its exact counts, schema, per-window balance) remains owned
  by the per-dataset suites, which load the holdout deliberately for that audit.
- Analogy is an evaluation-only set with no train/dev/holdout split; it is never tuned on.

## Evaluation loader surfaces exercised (by this cross-dataset module)

- Entity: `load_tuning_corpus` (the working surface); `load_split(final_holdout)` only to
  assert its gated rejection; `load_corpus` only inside the meta-test, to prove the tripwire
  blocks a holdout-parsing load.
- Alert: `load_tuning_corpus` (the working surface); `load_split(final_holdout)` only for its
  gated rejection; `load_corpus` only inside the meta-test; `AlertEpisodeCase.evaluation_inputs()`
  → threshold-free `OutcomeEvaluationInputs`; `AlertEpisodeGoldCorpus.label_inventory()` →
  counts-only `WindowLabelInventory`.
- Analogy: `load_corpus_and_gold`, `EpisodeCorpus.parent_ids`/`by_id`, `GoldSet` pairs and
  `coverage`, `unreviewed_episodes`, `quota_report`.

The per-dataset suites remain the owners of `load_corpus` full audits, `load_manifest`,
`load_target_catalog`, and `GoldMention.to_stage2_inputs`; those are not restated here.

## Tests and validators added, incl. adversarial coverage

`tests/unit/test_stage8_gold_datasets.py` (17 test cases — 16 functions, one parametrized over
both split-based assets) covers, without restating the per-dataset suites:

- All three assets load together with the socket layer disarmed; the two split-based assets are
  loaded as **tuning slices only** (entity 150, alert 36, `includes_holdout is False`, split set
  = train/development), and the analogy asset loads its full accepted corpus/gold.
- Reports (`entity.report`, `alert.report`, `analogy.quota_report`) are deterministic across
  reloads and JSON-serializable — computed on the tuning slices for the split-based assets.
- Tuning loaders exclude the holdout **by construction**; the holdout is exercised only as a
  **gated rejection** (parametrized over both split-based assets), never as a count or content.
- An **autouse holdout tripwire** plus a meta-test prove the module cannot silently reload the
  holdout: `load_corpus()` (which would parse `final_holdout`) and `allow_holdout=True` both
  fail loudly, and no `final_holdout.json` is opened at runtime.
- **Adversarial, on temporary mutated copies of the committed train/development data (never the
  committed files, never the holdout — the `final_holdout.json` file is neither copied nor
  parsed):** the entity **tuning** loader rejects a leakage-group that straddles train and
  development; the alert **tuning** loader rejects an episode-group that straddles them; a NIL
  entity record that names a real catalog target is rejected. All go through the public
  `load_tuning_corpus` and carry actionable error text ("spans splits", "null
  expected_target_id").
- The alert Stage-9 hand-off withholds exactly the scorer's decision knobs
  (`prediction`, `prediction_id`, `threshold`); the label inventory is counts-only and its
  fields are disjoint from `WindowEvidence`'s metric fields, so a passing gate cannot be
  assembled from the gold set; the composite `ExperimentalGate` stays fail-closed while the
  single-signal carve-out stays released.
- The analogy asset is still exactly 40 pairs targeting live leaf episodes.
- Threshold governance (below).

Positive cross-split leakage on the committed data, exhaustive schema rejection tables, ORM
round-trips, per-window balance, and manifest-text honesty regexes remain owned by the
existing per-dataset suites and are intentionally not duplicated.

## Threshold governance (frozen production thresholds are Stage 9's)

The frozen production thresholds are clustering **0.82**, the linking bands **0.85 / 0.50**,
the analogy floor **0.60**, and the **0.50** alert decision boundary (`implementation-order.md`
§9; live constants `ACCEPT_THRESHOLD`, `ADJUDICATE_THRESHOLD`, `DEFAULT_MIN_SIMILARITY`, and
`evaluate_prediction`'s `threshold` default). No production threshold is selected, tuned, or
recalibrated in this stage; the gold sets exist so that **Stage 9 owns all threshold tuning**
and calibrates against them. These values are named here only as the boundaries Stage 9 will
calibrate the gold sets against.

The tests enforce this three ways: no committed Stage-8 gold document carries a
threshold-shaped JSON key; neither gold loader module defines a threshold attribute of its
own; and the live production constants are asserted intact. The structural key scan covers the
tuning-visible split files, the manifests/catalog, and the analogy asset, and deliberately
excludes the `final_holdout.json` files — this cross-dataset module never parses holdout
content, and the holdout's threshold-freedom (like the rest of its schema) is guaranteed by the
per-dataset `load_corpus` audits, which reject any unknown, hence any threshold-shaped, field. A
legitimate ADR/Stage-9 reference in prose is never mistaken for a tuning decision, and this
worklog is itself checked for any first-person tuning claim.

## Explicit exclusions

- No threshold tuning or recalibration (that is Stage 9).
- No non-experimental composite alert release; the `ExperimentalGate` remains fail-closed.
- No dependency, manifest, or lockfile change; no new dependency.
- No database, network, spaCy, or LLM use in any added code.
- No edit to any file outside the two-file write scope.

## Verification

Bounded item-6 checks were run and passed:

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_stage8_gold_datasets.py \
  tests/unit/test_entity_linking_gold_schema.py \
  tests/unit/test_entity_linking_gold_dataset.py \
  tests/unit/test_alert_episode_gold_schema.py \
  tests/unit/test_alert_episode_gold_dataset.py \
  tests/unit/test_analogy_gold_set.py
# result: 190 passed

.venv/bin/python -m ruff check \
  tests/unit/test_stage8_gold_datasets.py services/evaluation \
  tests/unit/test_entity_linking_gold_schema.py \
  tests/unit/test_entity_linking_gold_dataset.py \
  tests/unit/test_alert_episode_gold_schema.py \
  tests/unit/test_alert_episode_gold_dataset.py
# result: clean

git diff --check          # clean
git status --short --branch
git diff --cached --name-only   # empty (nothing staged)
test ! -e uv.lock          # holds (no lockfile created)
```

A targeted read-only check additionally ran the cross-dataset module under a C-level `open`
audit hook (`sys.addaudithook`), below any monkeypatch: 17 test cases passed and **zero**
`final_holdout.json` opens occurred at runtime — confirming the module loads only tuning
slices and never parses holdout content, even where it deliberately provokes (and blocks) a
holdout load in the tripwire meta-test.

Independent pre-commit verification then completed:

- Entity schema, coverage, target integrity, and leakage: **PASS**; 56 focused tests passed,
  all 200 records and 27 deterministic targets were audited, and no duplicate or cross-split
  leakage key was found.
- Alert historical accuracy, window/provenance coverage, and onset/outcome separation:
  **PASS after repair**. The broad Sonnet source audit identified the remaining Lehman onset
  citation mismatch; the corrected record was then independently checked by Opus 4.8 high
  against both exact SEC documents, including the five-day lead and calendar horizon.
- Accepted analogy asset: **PASS unchanged**; 135 relevant analogy/corpus/evaluation tests and
  the 17 cross-dataset tests passed, with all 40 pairs independently checked against the
  100-episode corpus.
- Loaders, validators, malformed/adversarial cases, and holdout governance: **PASS**; 190
  focused tests passed and an independent Python audit hook observed zero
  `final_holdout.json` opens from the cross-dataset module.
- Full non-integration suite: **3,426 passed, 135 integration tests deselected** in 26.05s.
- Repository Ruff (`ruff 0.15.17`): **PASS** with `All checks passed!`.
- Git scope: **PASS**; exactly 18 Stage-8 deliverables, no tracked drift, an empty index,
  synchronized `main`/`origin/main`, no leaked artifacts, and clean direct scans of all
  untracked files.

`pip-audit` was **not** run: dependency manifests and lockfiles are byte-identical to `HEAD`,
so the known pre-existing advisories are non-regression context only. No database or network
service was required by the implementation or offline test suites; network access was used
only by the dedicated historical-source verifiers.

## Final status

Stage 8 implementation and independent pre-commit verification are complete. Explicit staging
and the single Stage 8 commit remain; no push is authorized.
