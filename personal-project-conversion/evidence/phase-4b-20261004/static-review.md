# Phase 4B API and static frontend review

**Result: no material correctness finding in the inspected API/static frontend change.** This review is independent of those source changes. The reviewer implemented the local runtime portion of Phase 4B earlier and does not present this note as an independent review of that implementation. No source files or tests were changed during this review.

## Fresh verification

The following read-only test command completed with **75 passed in 2.82 seconds**, exit status 0:

```text
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -s -p no:cacheprovider \
  tests/unit/test_personal_phase4b_api.py \
  tests/unit/test_api_key_middleware.py \
  tests/unit/test_health.py \
  tests/unit/test_personal_runtime_readiness.py
```

Tests use TestClient, mocked dependencies, and synthetic configuration. This result establishes local API behavior; it does not establish rendered browser interactions, a persisted screenshot, populated restore, app restart preservation, or live provider/feed behavior.

The root agent's existing API evidence was also inspected: `api-unit.xml` records 48 tests, zero failures, and zero errors across Phase 4B API, Phase 4A API, legacy health, and API-key middleware suites. `frontend.log` records 77 tests passed with no failures, including explicit tab-scoped API keys, visible failed mutations, evidence/report identity, legacy prototype inclusion, and the credential-free loopback URL override. These are inspected root results, not reviewer reruns.

## Reviewed boundaries

| Boundary | Assessment |
| --- | --- |
| Unknown API requests retain API errors | API routers register before the browser catch-all. `personal_frontend.py:27-29` rejects unsupported browser paths rather than returning the shell. Fresh tests verify JSON 404 envelopes for `/api/unknown`, `/api/v1/personal/unknown`, `/uploads/private`, and a missing `/app` asset. Existing validation/error handlers remain registered. |
| Static assets are isolated | The sole static mount is `/app` rooted at `frontend/app`; repository root, uploads, evidence, databases, and source captures are not mounted. The shell reader uses one fixed entry file. Installed Starlette `StaticFiles` defaults to `follow_symlink=False` and its `lookup_path` resolves real paths and checks `commonpath`, preventing escape from that directory. These containment details were inspected in the installed dependency. |
| Browser API uses the application origin | The injected head setup sets `SIGNAL_PERSONAL_APP` and `SIGNAL_API_BASE=window.location.origin` before the adapter/data loader runs. The legacy loopback-only query override is conditional on the personal flag being absent, so an app-served shell keeps the actual application origin. A root-relative base makes nested browser routes load the same assets. |
| Protected same-origin calls keep authentication | `_personal_same_origin` requires selected personal/subprocess mode, a loopback client, a loopback/localhost request Host, and exact origin equality with the request base URL. Different ports, foreign origins, hostile Host values, and nonlocal clients are rejected in the fresh tests. This allowance only passes the origin gate: a configured API key still needs the constant-time key check, and a valid key does not authorize an unapproved browser origin. |
| No new wildcard CORS allowance | The app retains its explicit configured origin list and `allow_credentials=False`. This change introduces no `*` origin or credential override. Non-browser requests with no Origin retain the existing API-key/local-loopback rule. |
| Legacy behavior is conditional | The browser mount, local launcher initialization, and launcher shutdown only select the subprocess transport. Other transports retain their previous health/probe branch. Legacy health, remote/no-key rejection, production/no-key rejection, valid/invalid keys, internal-read protection, and visibly disabled/missing live capabilities passed the fresh suites. Personal stored-content reads remain distinct from live-capability readiness. |
| Disabled service assembly | Source and fresh API import/health tests show that selected personal subprocess mode reports Redis, Celery, and Beat as `not_required_in_personal_mode` and does not import or construct them through app import or health. The existing launcher config checks still require loopback binding, one app worker, and personal processing mode. |

## Review limits

The frontend retains its existing external React, ReactDOM, Babel, Cesium, and font dependencies. This is consistent with serving the existing application, but backend network isolation does not prove an entirely offline browser. The root agent's actual browser observations must remain separate from these TestClient/source results.

The source review and fresh tests do not independently prove backup/restore, active/idle mode-switch behavior, preservation of prior evidence/preferences, or the final process/service inventory. Those gates retain their actual evidence requirements; this note does not change them.

No CUA tool calls or UI experiments were performed during this review. A screenshot emitted as a native CUA image can remain inspectable in the conversation, but that is not proof of an evidence-folder file. For a durable file, use an available native Save/Export action directed to the workspace evidence directory, or have the existing download explicitly moved there through the authorized native file UI. If macOS TCC prevents reading the downloaded file, record that limitation until an accessible file is actually verified; do not bypass the CUA filesystem restriction or describe an unreadable download as a checked artifact.
