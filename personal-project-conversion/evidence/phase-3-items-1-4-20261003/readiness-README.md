# Item 1: inactive-by-default activation and readiness slice

Date: October 3, 2026 (America/Los_Angeles). Scope: the delegated activation/readiness part of remaining-work item 1. The fresh human assignment authorizes inactive-by-default ordinary-worker implementation; it does not authorize runtime activation, provider calls, article export, recurring spending, or live feeds. Historical September 20 automatic-review records remain unchanged.

## Implemented behavior

- `packages/config/settings.py` adds `personal_paid_runtime_enabled: bool = False`. Credentials, saved profile preferences, and historical verification allowances do not enable this server gate.
- `services/personal/paid_runtime.py:runtime_readiness` requires the gate to be exactly `True` before inspecting execution configuration. Absent runtime/gate and false/truthy substitutes return `paid_runtime_disabled`. An open gate still requires the exact supported two-role route, immutable model/version/price identity, finite prices/token bounds/deadline, generation and OpenAI embedding credentials, and supported explicit Gemini thinking configuration. No providers or transports are initialized by readiness.
- `services/personal/settings.py:feature_readiness` uses that helper for v2 live profiles with the original stored route. Public redaction cannot hide unsupported stored route extras or fallbacks and produce a false ready result. Paid-runtime inactivity is reported as disabled; missing configuration is blocked. Profile AI-disable/zero-allowance reasons retain their existing precedence.
- Raw collection remains independent of optional AI readiness. Raw reader and saved/brief readability remain ready. The offline fixture and legacy v1 live boundary retain their previous behavior.
- Existing API settings/status code already invokes feature readiness; its remaining hard-coded `execution_route_unavailable` branches apply to legacy v1 profiles. No API change was needed in this slice.

## Fresh test-first verification

The new `tests/unit/test_personal_runtime_readiness.py` file contains 37 parameterized checks. Its autouse fixture rejects HTTP-client request calls and paid-provider builder calls. All route/price data and credentials supplied inside test Settings objects are synthetic; environment provider credentials were cleared, `APP_ENV=test`, and environment activation remained false.

| Check | Expected | Actual | Retained result |
| --- | --- | --- | --- |
| Initial new activation/readiness tests before production edits | Missing gate/readiness behavior fails | 24 failed, 10 passed | [readiness-red.log](readiness-red.log) |
| First corrected activation/readiness plus existing settings/spending tests | Pass | 89 passed in 0.80 seconds | [readiness-green.log](readiness-green.log) |
| Additional exact-stored-route tests before correction | Redacted extras incorrectly appear ready | 3 failed, 34 deselected | [readiness-exact-route-red.log](readiness-exact-route-red.log) |
| Final readiness/settings/spending unit checks | Pass | 92 passed in 0.94 seconds: 37 new readiness, 16 settings, 39 spending | [readiness-final-unit.log](readiness-final-unit.log) |
| Final focused Ruff | Pass | All checks passed | [readiness-final-ruff.log](readiness-final-ruff.log) |
| Final formatting check | Pass | Four files already formatted | [readiness-final-format.log](readiness-final-format.log) |
| Final Git whitespace check | Pass | Exit 0, empty output | [readiness-final-whitespace.log](readiness-final-whitespace.log) |

Final command: `.venv/bin/pytest -p no:cacheprovider tests/unit/test_personal_runtime_readiness.py tests/unit/test_personal_settings.py tests/unit/test_personal_spending.py -q` with `APP_ENV=test`, `PERSONAL_PAID_RUNTIME_ENABLED=false`, `PYTHONDONTWRITEBYTECODE=1`, and empty `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`. Ruff check and format used `--no-cache` on the three changed production files and the new focused test file. The sandbox cannot create pytest temporary captures; the authorized check ran through ordinary approval review with synthetic transport guards. No automatic approval-review rejection occurred for this slice.

## Preservation and limits

Edits were localized to the new server field, runtime-readiness helper, feature-readiness branch, and a separate new unit-test file. Existing staged/unstaged checkout changes were preserved. Paid adapter builders, spending summaries, production migrations, daily-pipeline fixtures, real environment files, saved preferences, and historical evidence were not edited by this slice.

No database/container/service resource was created, accessed, migrated, or removed by this readiness verification. Unit transport checks use synthetic HTTP transport only. No external provider/feed was contacted, no article content was sent externally, no real runtime was activated, and no recurring allowance was approved. There are no owned database or service resources requiring cleanup from this slice.

These checks establish activation/readiness behavior and component regressions. Ordinary-worker composition and PostgreSQL accounting proof belong to the parent item's separate integration record; this slice does not declare item 1 or Phase 3 globally accepted.
