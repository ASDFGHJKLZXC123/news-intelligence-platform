# Item 6 browser fixture controller

`fixture-controller.py` is scoped evidence tooling. It does not change application source or operational `.env` settings. `fixture-initial.json` is the first retained output from the current application services. These retained service/database outputs support browser expectations; they are not browser observation proof.

Before every scenario, the controller loads the existing disposable launcher manifest, requires `database.owned=true`, a recorded exact OID and database owner role, loopback host with a nondefault PostgreSQL port, the exact manifest database name, and an exact `personal_offline_harness_ownership.owner_token` match. The retained JSON projects only name, OID, role, host/port, marker hash, and manifest hash. It excludes database credentials, throwaway API access credentials, and the marker token itself.

Each process sets `APP_ENV=test`, clears the four supported paid-provider environment credentials, and sets `PERSONAL_PAID_RUNTIME_ENABLED=false` before application imports. RSS providers are `FakeRSSProvider` or a deterministic raised exception. The brief uses `OfflinePersonalFixture` embeddings/composition/grounding. Raw processing dispatches are forbidden by assertion. Python socket transport to any non-loopback address also fails closed. No real RSS, model, article-body, official-price-page, or export destination is contacted.

The root's pre-run browser setting edits and explicitly added draft source were preserved. The controller updates only the original launcher source's synthetic name/feed identity and adds the second synthetic source. It selects those two synthetic sources for its run profiles, resets interests and bounds deliberately, and restores `America/Los_Angeles` before the first run. The separate UI-added draft remains stored and unselected.

## Initial provenance and expectations

- A run at execution UTC time minus three days uses the actual coordinator to capture one labeled synthetic article, embed/group it, prepare supported claims, freeze the brief snapshot, and publish an offline brief. The actual repository save method retains the grouped event in Saved.
- A second actual coordinator run at execution UTC time minus two days captures raw RSS only with run admission limit 26 and daily limit 100. The technology input has 501 fixture entries: 18 unique URLs followed by repetitions of its first URL. The production bounded adapter observes entry 501, processes only 500, retains 18 unique candidates, and records `entry_limit` plus `total_entries_known=false`. The policy input has 18 unique entries. No remote feed total is inferred.
- Both source inputs have an article with publication time `null`. Other publication timestamps are explicitly authored fixture timestamps before capture. Each first item's input summary exceeds 2,000 characters; the production adapter retains exactly 2,000 and records summary truncation.
- Initial stored collection is 37 observed, 10 pending, 27 admitted, one grouped, and one completed enrichment. Default raw reading excludes the grouped article and exposes 26 standalone articles, paginated 20 + 6 in the production reader's timestamp/UUID order. Initial backlog is 10 eligible and zero disabled-source records. Removing either selected source in browser Settings must preserve its pending records while changing eligibility.
- The initial active profile is raw/AI disabled. It retains the labeled offline route. Earlier published brief/Saved records remain readable independently of active optional processing.

## Running later scenarios

Run from the repository root using its `.venv/bin/python`. Each output must be a new `.json` filename directly inside this scoped evidence directory. Existing output files are never overwritten.

```text
.venv/bin/python personal-project-conversion/evidence/phase-3-item-6-20261003/fixture-controller.py \
  --manifest /tmp/nip-personal-phase2-offline-p3_item6_20261003_b/manifest.json \
  --scenario SCENARIO \
  --output personal-project-conversion/evidence/phase-3-item-6-20261003/NEW-OUTPUT.json
```

| Scenario | Mutation and expected state |
|---|---|
| `summary` | Read-only retained service/database snapshot, including API pages of 20 plus 6, browser pages of 12 plus 12 plus 2, and per-article IDs/titles/URLs/sources/times/states/event links in all raw articles including grouped. |
| `capture_failure` | One-shot yesterday run; both synthetic selected feeds raise a deterministic error before optional processing. Actual coordinator retains `all_feeds_failed`, feed failure receipts, and failed run. |
| `provider_failure` | One-shot current-day run; up to ten existing pending articles are admitted, empty synthetic feeds succeed, and deterministic embedding raises before any transport. This admits the full remaining fixture backlog after capture failure, making the unknown-publication and truncated-summary fixtures readable. Actual coordinator retains `personal_workflow_failed`, failed workflow and `provider_failed` selected capture. |
| `spending` | One-shot insertion of five explicitly synthetic retained monetary obligations; selects valid synthetic live route metadata, assisted/AI enabled and $1.00 allowance with operational paid gate still off. Exact summary is finalized $0.20, current reserved $0.15, all-period unresolved $0.30, remaining $0.65. |
| `reset_settings` | New revision, raw/AI disabled, no allowance, valid synthetic route metadata so the route editor can show its fields. |
| `missing_config` | New assisted/AI-enabled revision with empty route and no allowance; expected optional-processing configuration missing. |
| `gate_off` | New assisted/AI-enabled revision with valid synthetic route and $1.00 allowance; actual server runtime gate and missing real credentials remain blocking. |
| `allowance` | New assisted/AI-enabled revision with valid synthetic route and $0.00 allowance; expected allowance reached. |

The synthetic route uses OpenAI-supported adapter identities and validates the current public route schema. Its `synthetic-item6-*` model identities and `synthetic-item6-prices-not-live` revision are deliberately invented. Prices are $1,000 per million tokens solely to create simple fixture arithmetic; they are not recommendations or claims about real prices. The official pricing URL validates the schema shape and is never fetched.

The spending scenario inserts modeled statuses rather than sending a provider request. Current reconciled $0.20, current reserved $0.10, current uncertain $0.05, older uncertain $0.15, and older finalized $0.90 all carry `synthetic_browser_fixture` evidence and zero real provider dispatches. Current reserved includes reserved and uncertain obligations; unresolved includes all periods and both reserved/uncertain; old finalized is excluded from the current month. Remaining is `1.00 - 0.20 - (0.10 + 0.05) = 0.65`, matching the current implementation. Prior-period unresolved remains disclosed separately. Every result includes current UTC accounting period, local next-reset timestamp, and the public route metadata the reader actually returns.

No scenario mutates prior terminal run JSON, frozen profiles, user/shared databases, unrelated services, or retained earlier monetary records. No cleanup is performed by this controller; the parent assignment retains and verifies owned-resource cleanup separately.
