# Phase 4A verification artifacts — October 4, 2026

[Authoritative acceptance](../phase-4a.md) maps all eleven required cases and states proof limits. [Independent review](independent-review-accepted.md) distinguishes reviewer execution from inspection. [Setup/recovery](../../PHASE4A_SETUP.md) documents the implemented runtime.

Current gates are `phase4a-accepted` (44), `phase3-accepted` (101), `accepted-units` (384), and `frontend-final` (77). Each retains command, result, log and source records; integration gates retain JUnit and exact-owned database/role cleanup. Four historical integration cases overlap the two current integration gates. `actual-accepted-*`, `transport-parity-accepted.json`, `concurrent-api-accepted-result.json`, `browser-accepted-retry.jpg/txt`, and `worker-scheduler-accepted-stopped.json` are the final actual runtime epoch.

`postgres-outage-result.json`, `backup-restore-result.json` and `restart-proof.json` retain actual infrastructure/persistence proof with synthetic contents. They are retained component experiments, supplemented by the final migration/ownership/OS/regression gates; their earlier whole-source snapshots are not represented as the final accepted runtime epoch. `resource-cleanup.json`, `stack-cleanup.json`, `outage-cleanup.json` and `preservation-final.json` define cleanup/preservation.

Earlier red/intermediate runs, the pre-final actual epochs, and `closeout-draft.md` are dated developmental history. They retain defects, harness corrections and source-drift distinctions and do not replace current accepted gates. The original Phase 3 acceptance is archived byte-identically. No live paid activation or Phase 4B/5 work occurred.
