# Independent verification of Phase 4A and Phase 4B

## 1. Verdict

**Phase 4A (P4-01–P4-09, P4-13, P4-14): supported for local/synthetic engineering acceptance.** I found no blocking source defect in the ownership, fencing, deadline, supervisor, child or recovery paths. The retained evidence is consistent with the claims.

**Phase 4B (P4-10–P4-12, P4-13, P4-14): supported for local/synthetic engineering acceptance.** The cache, limiter, dependency assembly, same-origin guard, static serving, wrapper and Compose source match the delivered-behaviour claims. The retained JUnit totals (451 / 44 / 48) are present with zero failures, errors or skips.

**I reran nothing.** I had Read/Glob/Grep only, so every conclusion is "source inspected" or "retained evidence inspected". I also could not hash files, so I could not confirm that the snapshot bytes match the recorded manifests; I checked the manifests against each other only.

Non-blocking issues to act on, most important first:

1. **The "preexisting" legacy test failure is very likely a Phase 4A regression, not something that predates Phase 4.** Neither phase's acceptance scope ran that test file. The impact is test-only, but the label in the 4B record is not justified. Details in F1.
2. **Unknown API paths on non-GET methods probably return 405 instead of 404 in personal mode.** This is inferred from routing semantics, not executed; only GET is tested. Details in F2.
3. **No real-browser mutation was performed with a configured API key.** The browser run blanked `API_KEY`. Details in E2.
4. **The three-way registered-Celery / CLI / UI parity was not repeated after 4B changed the runner assembly.** Details in E3.

## 2. Findings

### A. Source defects and observations

**F1 — Medium (evidence characterisation; test-only impact): legacy unit test broken by a Phase 4A edit and labelled "preexisting".**
- `apps/api/pipeline.py:90-91` imports `run_daily_pipeline_task` lazily inside `enqueue_pipeline_task`.
- `tests/unit/test_pipeline_runtime.py:645-656` monkeypatches `pipeline_api.run_daily_pipeline_task` as a module attribute, which no longer exists.
- The 4A baseline manifest (`personal-project-conversion/evidence/phase-4a-20261004/baseline-files.json:115`) records `apps/api/pipeline.py` as `08931add…`. The 4A accepted manifest (`phase4a-accepted-source-before.json:25`) records `f6cbf111…`, and `preservation-final.json:16` lists the file as changed in 4A.
- The test file hash `020d450f…` is identical from the 4A baseline through the 4B final manifest.
- The pre-4A dirty diff (`baseline-unstaged.diff`) does not touch that import.
- `test_pipeline_runtime` appears in no 4A or 4B JUnit file or command record.
- Consequence: `phase-4b.md` and `phase-4b-20261004/independent-review.md` call it "preexisting … match the baseline". That is true only against the 4B baseline, which is post-4A. Phase 4A's "0 unresolved failures" holds only for its selected suites.
- Limit: I could not read the pre-4A file content, so "introduced by 4A" is an inference from the hashes and the test's expectation.
- Check: `git show HEAD:apps/api/pipeline.py | grep -n run_daily_pipeline_task`, then run that single test.

**F2 — Low (inferred, not executed): unknown non-GET API paths likely change from 404 to 405 in personal mode.**
- `apps/api/personal_frontend.py:21` registers a GET-only catch-all `/{browser_path:path}` after all routers (`apps/api/main.py:86-101`).
- A POST/PUT/DELETE to an unknown `/api/...` path matches that route by path but not method, which normally yields 405.
- The response would still be the JSON envelope, never HTML, so the core P4-12 invariant holds.
- `tests/unit/test_personal_phase4b_api.py:179-186` and `same-origin-api-error.json` cover GET only.
- Check: with TestClient, POST `/api/v1/personal/unknown` under subprocess transport and under celery transport, and compare status codes.

**F3 — Low: idle control row keeps stale timing fields.**
- `release_control` (`services/personal/processing.py:173-178`) and `switch_processing_mode` (`:110-118`) leave `queued_at`, `started_at`, `graceful_deadline_at` and `hard_deadline_at` in place.
- `phase-4b-20261004/backup-restore-result.json:11-25` shows `active_run_id: null`, `mode: legacy` and populated deadlines.
- This is not an ownership hole: `verify_control` (`:142-170`) and the supervisor's deadline read (`services/personal/supervisor.py:70-87`) key on run id, token, generation and lease.
- `cli status` (`processing.py:304-331`) will show misleading instants for an idle workspace.

**F4 — Low: supervisor-side hard kill depends on a 10 Hz database poll.**
- `supervisor.py:197-210` polls through the pooled `SessionLocal` (`db/base.py:23-34`).
- If PostgreSQL hangs rather than refuses, one poll can block for the connect/statement timeout and delay the supervisor's kill.
- The child's own watchdog (`services/personal/deadlines.py:123-155`) is independent and is the one proven by the launcher-loss test.
- No retained evidence measures supervisor kill latency under a hung database.

**F5 — Low / operational: recovery after a hard app crash is CLI-only.**
- `require_idle` (`processing.py:93-100`) blocks any new date and any mode switch while a child lifetime is unconfirmed.
- The only caller of `reconcile_child_lifetimes` is `services/personal/cli.py:48-53`; there is no API or UI path.
- This is conservative and documented in `PHASE4B_SETUP.md`, but a browser-only user sees `processing_busy` until they run the CLI.

**F6 — Low: manual command edges.**
- `cli.py:117-118` maps a `None` result from `supervisor.wait` to exit code 0.
- Only `KeyboardInterrupt` gets bounded cleanup (`:119-123`). A SIGTERM to the CLI skips it; the child stays bounded by its own watchdog. The contract requires only Ctrl-C.

**F7 — Info.**
- `services/writer_mode.py:30-31` and `workers/alert_tasks.py:131-132` skip the fence for non-SQLAlchemy session doubles. Production `SessionLocal` always yields a real `Session`; unit tests using doubles prove nothing about the fence.
- Under Celery transport, saving settings still switches durable mode to personal when idle (`services/personal/workspace.py:45-63`). The "never selects a mode implicitly" claim is true for the subprocess wrapper only.
- The limiter starts empty but its capacity is a full minute's allowance (`services/llm/limiter.py:186-194`), so an idle runner can later burst. The 4B independent review acknowledges this.
- The 2/25/30/35 constants live in three places (`processing.py:22-25`, `deadlines.py:15-17`, `child.py:25`). They agree today.

### B. Evidence and provenance gaps

**E1 — Byte identity not verified by me.** What I could check is consistent:
- The twelve core 4A modules (`processing`, `deadlines`, `supervisor`, `child`, `runs`, `spending`, `coordinator`, `cli`, `writer_mode`, `workers/personal_tasks`, plus `runner` and `paid_runtime` as changed in 4B) have the expected hashes across manifests. The first ten are identical between the 4A accepted manifest and `actual-final-formatted-source-before.json`.
- The 4B 44-case gate ran on the final hashes of `runner`, `paid_runtime`, `local_runtime`, `cache`, `limiter`, `deps`, `health`, `main` and `middleware`. It differs from final only in `personal_frontend.py` and the wrapper/Compose port default.
- The 48-case gate already had the final wrapper and Compose hashes.
- The `personal_frontend.py` after-hash in `format-only-equivalence.json` (`f713e54c…`) matches the final manifest. I could not recompute the AST-equality claim.

**E2 — P4-10 browser run had `API_KEY` blank** (`stack-setup-result.json:5-12`, `verify.py:24-31`). Same-origin plus configured key is proven only by `test_personal_phase4b_api.py:203-209`. The UI does have key entry (`frontend/app/page-personal.jsx:93`). The wrapper preserves a key from `.env`, so a real user may hit this path untested in a browser.

**E3 — Three-way parity is 4A-epoch only.** `transport-parity-accepted.json` retains equality flags and one SHA, which I cannot recompute. `runner.py` and `paid_runtime.py` changed in 4B. The post-change Celery-delivered path is covered by one composed integration test in both 4B gates, not by a repeat of the actual worker/CLI/UI comparison.

**E4 — "Stop Redis" was satisfied by isolation, not by stopping.** `app-readiness.json:392,459` shows unrelated Redis containers still running on the host. Absence rests on blank URLs, a Python-level socket guard (`verify.py:543-563`), import-forbidding tests and source. The evidence states this boundary honestly, and the source supports it.

**E5 — Browser observations are textual.** No screenshots are retained (disclosed). I read `browser-brief.md` and viewed `brief-page-1.png`; the report, snapshot and run UUIDs match `browser-observations.json`. I did not open the PDF or page 2.

### C. Test limitations

- OS deadline tests scale 25/30/35 minutes to 2.5/3/3.5 seconds by patching `runs.*` in `tests/integration/_personal_runtime_fault_child.py:60-62`. They do call the real `child.main` (`:171-173`).
- Production values are asserted only with an authored clock (`tests/integration/test_personal_processing_control.py:97-113`).
- `test_real_child_ignores_graceful…` cannot tell a supervisor kill from a watchdog kill. The launcher-loss and bootstrap tests can. Their 4B-epoch reruns show the child exiting about 26 ms and about 11 ms after the original deadlines (properties in `phase-4b-20261004/phase4a-accepted.xml`).
- Limiter and cache tests use fake clocks and mock ledgers. The "fresh interpreter" test uses real processes at 6000 RPM with a 0.02 s Retry-After (`tests/unit/test_personal_phase4b_runtime.py:468-522`).
- All providers are synthetic.

### D. Documented out-of-scope limits (confirmed, not findings)

- Paid activation is off; the wrapper forces `PERSONAL_PAID_RUNTIME_ENABLED=false` (`scripts/personal-local.py:41-48`).
- No live feeds or models were used, and no notifications were sent.
- The browser still loads CDN assets.
- Phase 5 usefulness and public/live operation are unstarted and unverified.
- The repository as a whole is not green.

## 3. Case-by-case

Status key: **S** = source inspected by me; **E** = retained evidence inspected by me. Nothing was freshly rerun.

### Phase 4A

| ID | Claim | Checked | Conclusion | Basis |
| --- | --- | --- | --- | --- |
| P4-01 | Celery baseline matches CLI and UI managed child | `runner.py:204-354`, `workers/personal_tasks.py:820-853`, `supervisor.py:143-156`; `transport-parity-accepted.json`, `worker-scheduler-accepted-stopped.json` | Supported for the 4A epoch; not repeated after the 4B runner change (E3) | S + E (historical) |
| P4-02 | Two simultaneous starts give one run and one child | `runs.py:96`, `:135-147`; `apps/api/personal.py:754-781`; `concurrent-api-accepted-result.json` (both 202, same run id, generation 2, one child entry) | Supported | S + E (historical) |
| P4-03 | One global owner across dates and entry points; legacy guarded | `processing.py:93-100`, `runs.py:152-153`, `writer_mode.py:22-60`, guards in `workers/*_tasks.py`; `test_concurrent_different_dates…` in JUnit | Supported; I did not read every legacy task body | S + E |
| P4-04 | Stale delivery rejected after handoff expiry | `child.py:126-135`, `runs.py:296-298`, `processing.py:163-170`; `test_real_old_child_after_handoff_recovery…` | Supported; expiry uses an authored clock | S + E |
| P4-05 | Graceful deadline stops further work | `deadlines.py:21-22`, `:67-83`, `:172-188`; `coordinator.py:286`, `:328-330`, `:1016-1035`; `spending.py:396-401` | Supported | S + E (test names and JUnit only) |
| P4-06 | Watchdog enforces original hard deadline; no renewal | `child.py:45-61` (armed before DB imports), `:117-124`; `deadlines.py:142-155`; no code extends `lease_expires_at` after `runs.py:306`; launcher-loss and bootstrap properties | Supported at accelerated scale only | S + E |
| P4-07 | Stale business and terminal writes rejected; late accounting limited | `runs.py:326-381`; every coordinator and brief transaction begins with `lock_owned_run`; `spending.py:531-602` touches only the request row | Supported | S + E (JUnit) |
| P4-08 | Database loss stops dispatch; old token cannot resume | `spending.py:365-393`; `postgres-outage-result.json` (reservation $0.020 kept, $0.005 reconciled, generation 3) | Supported for the 4A epoch; `spending`, `runs` and `processing` hashes are unchanged since | S + E (historical) |
| P4-09 | Bounded shutdown, Ctrl-C, crash, restart | `supervisor.py:236-252`, `main.py:44-59`, `cli.py:119-123`; lifecycle properties (Ctrl-C about 0.61 s in the 4B rerun); `restart-proof.json` | Supported; SIGTERM to the CLI is not covered (F6) | S + E |
| P4-13 | Active switch rejected; idle switch durable | `processing.py:103-119`; `runner.py:48-58`; `apps/api/personal.py:430-444`; control tests in JUnit | Supported; stale control timestamps (F3) | S + E |
| P4-14 | Populated restore and legacy selection retain data | `phase-4a-20261004/backup-restore-result.json` cited; upgrade tests in JUnit | Supported by retained records; I did not read migration 0023 or the backup scripts | E |

### Phase 4B

| ID | Claim | Checked | Conclusion | Basis |
| --- | --- | --- | --- | --- |
| P4-10 | Only app and PostgreSQL persist; browser flow works; health says not required | `deps.py:57-65`, `:89-123`, `:292-330`; `health.py:44-50`; all Celery imports in `apps/api` are function-local; `infra/docker-compose.personal.yml`; `app-readiness.json:472-502`; `manual-cli-result.json`; exports | Supported within the isolated runtime (E2, E4, E5) | S + E (historical browser) |
| P4-11 | Empty-start pacing; bounded cache; durable spending | `cache.py:98-174` (256 entries, 16 MiB, key plus value UTF-8 bytes, TTL capped at 3600 s, oversize skipped); `limiter.py:162-232`; `local_runtime.py:57-116` (one lock held across each physical POST, Retry-After never extends the deadline); `paid_runtime.py:161-166`, `:370-386`; `restart-final-format-result.json` | Supported; pacing proof is synthetic-clock or very short real timings | S + E |
| P4-12 | Browser route loads; unknown API route stays a JSON error | `personal_frontend.py:17-37`; `middleware.py:58-67`, `:200-246`; frontend HTML `:31-43`; `same-origin-api-error.json` (404 `application/json`) | Supported for GET; non-GET status untested (F2) | S + E |
| P4-13 | As above, in the 4B epoch | 44-case JUnit; `phase-4b-20261004/backup-restore-result.json:11-29` | Supported | S + E |
| P4-14 | Checksummed restore to a disposable database; legacy selection keeps rows | Same file: 91 tables equal, only `personal_writer_mode` changed, read endpoints 200 | Supported by retained records; I viewed the first ~25 table fingerprints and the summary flags, not all 91 | E |

## 4. Targeted fresh actions

1. **Resolve F1.** Confirm the pre-4A import with the `git show` command above and run `tests/unit/test_pipeline_runtime.py::test_api_delivery_helper_uses_exact_pipeline_queue`. Then either update the test to patch `workers.pipeline_tasks.run_daily_pipeline_task`, or reword the 4B record to say the failure came from a Phase 4A edit.
2. **Resolve F2.** One TestClient check of POST, PUT and DELETE on `/api/v1/personal/unknown` in both transports. If the status differs, decide whether to accept 405 or add a method-agnostic 404 for `/api/`.
3. **Close E1.** Run `sha256` over the current files and compare with `actual-final-formatted-source-before.json`.
4. **Optional, E2.** One browser save through the app origin with `API_KEY` set and entered in the UI.

I do not ask for a rerun of the passed suites.

## 5. Method, scope and limits

**Method.** Read-only inspection with Read, Glob and Grep inside the snapshot. No commands, hashing, network, browser, Docker or writes.

**Source read in full.**
- `services/personal/`: `processing`, `deadlines`, `supervisor`, `child`, `runner`, `local_runtime`, `paid_runtime`, `runs`, `spending`, `coordinator`, `audit_repository`, `rss_runtime`, `app`, `cli`.
- `services/writer_mode.py`, `services/llm/cache.py`, `services/llm/limiter.py`, `services/nlp/embeddings.py`.
- `apps/api/`: `deps`, `health`, `main`, `middleware`, `personal_frontend`.
- `scripts/personal-local.py`, `infra/docker-compose.personal.yml`, `db/base.py`, `packages/providers/openai_embeddings.py`.

**Source read in part.** `apps/api/personal.py`, `apps/api/pipeline.py`, `services/personal/briefs.py`, `claims.py`, `workspace.py`, `workers/personal_tasks.py`, `report_tasks.py`, `ingestion_tasks.py`, `alert_tasks.py`, settings, the frontend origin guard.

**Tests read.** `test_personal_runtime_lifecycle.py`, `_personal_runtime_fault_child.py`, `test_personal_phase4b_runtime.py`, `test_personal_phase4b_review.py`, parts of `test_personal_processing_control.py` and `test_personal_phase4b_api.py`.

**Evidence read.** Both phase records, both setup guides, the contracts, both 4B reviews, the JUnit headers and case lists, the source manifests named in the request, and the readiness, restart, restore, parity, concurrency, outage, export and browser JSON.

**Not reviewed.** Migration 0023, `snapshots.py`, clustering, the orchestrator internals, the export endpoints, backup/restore scripts, the 4A fault-test bodies, frontend tests, `03-bounded-daily-use.md`, and most logs.

**Preservation.** I changed nothing. Paths are relative to the original repository. Historical 4A statements ("4B unstarted", Redis required) are treated as that epoch's record, not as current contradictions.
