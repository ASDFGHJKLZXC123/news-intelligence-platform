# Public repository snapshot

Prepared October 4, 2026 (America/Los_Angeles).

The repository includes the personal news desk implementation, additive migrations,
local runtime and recovery tools, unit/integration/frontend tests, conversion plans,
setup guides, Phase 5 preparation records, and selected acceptance evidence.

## Locally retained artifacts

Private environment files and credentials, database dumps and full-table snapshots,
raw SQL/DDL captures, raw reviewer transcripts and prompts, and local scheduler
state remain on the original machine and are excluded from version control.
Existing private planning-document exclusions remain in effect. No original
artifact was deleted or rewritten for publication.

Historical evidence can link to these locally retained files. Such links and hash
inventories describe the original acceptance record; the public repository alone
does not contain every raw proof. Published summaries and generated outputs keep
their original acceptance boundaries and dates.

## Publication checks

Fresh checks passed in the working copy and in an isolated clean Git checkout
containing exactly the staged public files:

- Python unit suite: **4,428 passed**, 284 integration tests deselected.
- Frontend suite: **77 passed**, zero failures.
- Whole-project Ruff lint and staged whitespace checks: passed.
- Credential scan: no live provider keys, private keys, JWTs, or actual configured
  secret values detected; documented local defaults and test fixtures were
  reviewed separately. All 28 images and three PDFs passed local text/OCR checks.

Tests used isolated test settings with paid processing disabled. The clean
checkout required Git metadata for the application's provenance tests; a plain
file archive could not satisfy those tests. The tested source, configuration,
and test files were unchanged when these results were added to this note.

The legacy pipeline queue test was updated to patch the task at its current lazy
import location. Its exact queue, arguments, and task-ID assertions are preserved.
Ruff excludes the archived evidence directory, which contains frozen historical
source and intentionally failing harness versions; application and test source
remain covered.

These checks do not repeat the historical PostgreSQL/browser/provider acceptance
runs or establish Phase 5 usefulness. Phase 5 remains in progress with actual
reading sessions and personal judgments pending. Publishing source code does not
activate live feeds, paid processing, or a hosted application.
