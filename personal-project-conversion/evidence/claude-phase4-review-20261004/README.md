# Claude Code verification of Phase 4A and Phase 4B

Audit date: 2026-10-04 America/Los_Angeles (completed 2026-10-05 01:13:54 UTC).

Claude Code 2.1.289, using its configured default `claude-opus-5-5`, supports both phases for **local/synthetic engineering acceptance** and found no blocking source defect. Its full case-by-case verdict is retained verbatim in [claude-report.md](claude-report.md).

This was a read-only source and retained-evidence review. Claude did not execute tests, hash files, run services, or open a browser. Its report distinguishes current source inspection from historical execution evidence. It does not establish live/public usefulness, paid activation, or Phase 5 acceptance.

## Confirmed follow-up findings

Codex performed limited checks after Claude's report; these are separate from Claude's verdict. The results are retained in [followup-verification.json](followup-verification.json).

1. **Legacy test compatibility regression introduced in Phase 4A.** The freshly rerun `test_api_delivery_helper_uses_exact_pipeline_queue` fails because it patches an API-module task attribute that Phase 4A moved to a function-local import. Pre-4A bytes were reconstructed in memory and match the exact baseline hash; the unchanged test still expects the old attribute. Calling this preexisting relative to the 4B baseline is accurate, but implying it predates Phase 4 is incorrect. This check does not establish a previously executed passing baseline or a wrong production queue. See [provenance-adjudication.md](provenance-adjudication.md), [test log](legacy-test-rerun.log), and [JUnit](legacy-test-rerun.xml).
2. **Unknown mutation routes return a different JSON error status.** Eight fresh in-process requests confirmed that GET returns JSON 404 in both transports, while POST/PUT/DELETE return JSON 405 in subprocess mode and JSON 404 in Celery mode. All responses preserve the JSON error envelope. The probe made no socket connection, entered no application lifespan, and used no live database or service. See [unknown-route-probe.json](unknown-route-probe.json) and [probe log](unknown-route-probe.log).
3. **Current source byte identity checked locally.** All 599 non-Markdown entries in the final formatted source manifest match current bytes. Overall, 623/627 manifest entries match; the four differences are subsequent authorized Markdown status updates. All 1,219 copied snapshot files match current originals, including those documents. Both recorded formatting after-hashes match current bytes. Historical AST equivalence was not freshly recomputed because the before-format bytes are not retained. This does not rerun older PostgreSQL cases against final bytes.

No repairs were applied. The repository as a whole is not green. The non-blocking source observations F3–F7 remain in Claude's full report.

## Remaining evidence limits

- Configured-key authentication is covered by an inspected unit test; a real-browser mutation with a configured key remains unverified.
- Actual registered-worker/CLI/UI parity is from the Phase 4A epoch. Phase 4B retains a composed Celery integration check, but did not repeat the actual three-way comparison after runner assembly changed.
- Browser observations are textual; screenshots were not retained. Native exports and rendered PDF evidence remain available.
- Providers and timing acceptance are synthetic or accelerated. Browser CDN assets remain external. Paid project runtime is disabled; the project's live feed, model, and notification integrations were not used, and Phase 5 remains unstarted. The external Claude audit itself used the approved Anthropic account.

Claude did not request repetition of the passed acceptance suites. Codex freshly reran only the single legacy test and the eight in-process routing requests described above.

## Scope and preservation

The user explicitly approved the scoped source/evidence submission to Anthropic through the existing authenticated Claude Code account. The original automatic-review rejection and subsequent approval are retained in [approval-scope.json](approval-scope.json).

Claude reviewed an exact temporary copy containing 1,219 project/source/evidence files. Private environment files, Git state, authentication files, runtime caches, private key files, symlinks, and unrelated evidence folders were excluded. Claude initialized only Read, Glob, and Grep, with no command, write, MCP, browser, or customization tools. Its 126 observed tool calls targeted only the snapshot. One grep pattern was rejected because look-around was unsupported; it was not an access-boundary violation. No independent operating-system confinement probe was performed.

[final-preservation.json](final-preservation.json) confirms that all 1,994 original baseline files and Git HEAD, branch, index, and staged diff are unchanged. There are no added, missing, or changed original paths outside this new audit folder. The exact temporary snapshot was removed. The audit changed no selected preferences, environment files, databases, or shared services.

The exact prompt, command, snapshot hashes, event stream, completion result, and preparation review are retained alongside this record. Historical Phase 4A/4B evidence and Claude's report were left unchanged.
