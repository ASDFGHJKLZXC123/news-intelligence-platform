# Independent item-5 review

Date: October 3, 2026 (America/Los_Angeles).

**Judgment: PASS for the item-5 implementation and integration proof, with the broad preexisting-container state comparison failure explicitly retained.** No actionable source, test-coverage, accounting, or guard finding remains in this scoped review.

This reviewer independently read the current source, the original populated-upgrade test and its retained diff, commands, logs, JUnit records, manifests, and cleanup/event artifacts. The reviewer used read-only filesystem comparisons and did not rerun PostgreSQL tests, query databases, contact providers, or operate resources. Only this review document was written. Test execution and resource actions were performed by the item-5 verification agent.

## Source and proof review

- The runner clears provider/application credentials before application imports, sets `APP_ENV=test`, keeps the process paid gate false, and provides explicit owned database coordinates. Test Settings omit developer dotenv loading. The ordinary worker proof opens the gate only on isolated synthetic Settings and replaces provider transport with `httpx.MockTransport`.
- The owned resource checks require the exact container ID, name, owner label, image digest, loopback binding, and tmpfs storage without mounted volumes. Stage 6/7 helpers are restricted to literal loopback. Every observed suite CREATE/DROP DATABASE statement matches the ownership hook: the runner retains its created OID, verifies identity before DROP, and proves absence afterward. Role cleanup ties unique role names to a database created in the run, excludes initial roles, captures OIDs before DROP, and checks absence.
- The new HTTP API proof retains the production dependency/enqueuer and registered Celery task. Only broker delivery and provider transport are replaced. The production coordinator, serializers, deadline client, route identities, durable ledger, snapshots, and ownership/accounting checks execute. Five distinct physical requests retain exact bounds, usage, and a total synthetic charge of `0.00744` USD.
- Missing-route API execution retains readable raw content with no paid request or constructed provider transport. The additional guard cases reject an unfrozen scope, mismatched price identity, and rotated owner before dispatch, while retaining the original reservation.
- The [populated-upgrade diff](bounds-upgrade-change.diff) retains every prior preservation, legacy-cost, idempotency, and allowance-blocking assertion. It adds actual repeated spending API reads after 0020-to-head migration, precise metadata, unchanged retained rows, and no observed SQL writes. Production migration/downgrade guards were not changed.
- Existing meaningful cases remain in the complete gate for concurrent shared reservations across dates/API-worker paths, physical retry identity, uncertainty and restart obligations, idempotent reconciliation, month handoff, lower live ceilings, frozen scopes/snapshots, and ownership races. No failed assertion was dropped, test was skipped, acceptance condition relaxed, or production guard weakened by item 5.

## Executed evidence inspected

| Verification performed by the item-5 agent | Inspected result |
| --- | --- |
| Complete PostgreSQL integration marker gate | [238 passed; 0 failures, errors, or skips; 4,229 non-integration tests deselected](full-integration.log), corroborated by [JUnit](full-integration.xml) |
| New composed proofs plus populated-upgrade extension | [6 passed](new-proof.log); these targets overlap the complete gate and are not an additional distinct total |
| Directly related offline unit suites | [175 passed](unit.log) |
| Whole-repository Ruff, four-file formatting, staged and unstaged whitespace | [All successful](tooling.json) |

The JUnit file explicitly includes the formerly failing daily vertical slice, both historical Stage 6 hotness cases, and both Phase 3 retained-state downgrade-refusal cases. Earlier dependency and September 20 results were background records, not independently rerun totals.

The tested checkout is `codex/stage10-hardening` at HEAD `7c7fcebbeba9c7b9671d6eb38631c2625cc8b454`. All 592 regular source hashes in the full gate's before/after manifests match; protected hashes and the staged diff hash match too. An independent initial-to-tested comparison found only the two intended regular source changes:

- New composed test: `efe2ffeb1a8a2d4d8ba1b2918a870fb964f11f79fdbdea39d09129bd2cda96d3`.
- Extended populated-upgrade test: `31dfc50347597bcd3ce3ad3ff406da3991c7a592aab36ff5d4664d1d7890d5da`.

No production file changed. The reviewer independently compared all 307 retained historical-evidence hashes and found no drift. Operational dotenv, selected preferences, and staged index hashes remained unchanged during the gates. Exact identities are retained in [source before](full-integration-source-before.json), [source after](full-integration-source-after.json), and [gate result](full-integration-result.json).

## Cleanup and retained limitation

[New-proof resources](new-proof-resources.json) and [full-gate resources](full-integration-resources.json) retain 188 distinct disposable database lifecycles: 6 plus 182. All have the pre-DROP identity check and post-DROP absence. The three role lifecycles also retain OID/identity checks and absence. Database/role inventories were restored before the exact owned container `fad29e3f021a120416b5ff692009c2e1d23d47cbb7ba19ce9c727a866b4ec8ab` was removed.

The cleanup command then failed its broad preexisting-container comparison. [The failed assertion](cleanup-attempt.log) and [original false result](resource-cleanup.json) are preserved. Independent comparison confirmed that all 19 preexisting IDs, names, images, labels and mounts were preserved, while 17 complete records were unchanged. Two unrelated records changed from running to stopped, with corresponding port-state changes:

- `infra-monitor-demo-progress-v2-test-restart-relay`
- `infra-monitor-demo-progress-v2-test-restart-db`

[Docker events](resource-events.jsonl) record those stops at 23:37:02/03 UTC, before the owned stop/removal at 23:38:12 UTC. The event stream does not identify the initiator. The reviewed runner's resource mutations target only its exact owned ID; no unrelated restoration or cleanup was performed. [Adjudication](resource-cleanup-adjudication.json) correctly separates successful owned cleanup from the failed broad state comparison. A claim that every preexisting running state remained unchanged would be unsupported.

Real paid operation and selected runtime preferences remain inactive. Browser/old-resource work, global Phase 3 acceptance, and later phases are outside this review.
