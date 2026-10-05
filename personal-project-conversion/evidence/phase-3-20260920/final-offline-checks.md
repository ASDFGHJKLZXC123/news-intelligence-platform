# Final offline checks

Executed serially on September 20, 2026 in the shared checkout. No PostgreSQL suite was started by this reviewer during these checks. Every command explicitly set `APP_ENV=test` and cleared `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, and `DEEPSEEK_API_KEY`; Python commands also set `PYTHONDONTWRITEBYTECODE=1`.

| Check | Command after the environment assignments | Fresh result | Retained output |
| --- | --- | --- | --- |
| Complete unit suite | `.venv/bin/pytest -q -p no:cacheprovider tests/unit` | 4,192 passed in 22.14 seconds | `final-unit.log` |
| Complete frontend suite | `node --test frontend/app/*.test.js` | 74 passed; no failures or skips | `final-frontend.log` |
| Whole Python lint, no fixes | `.venv/bin/ruff check --no-cache .` | One I001 import-order issue in `apps/api/main.py` | `final-python-lint.log` |

The lint issue concerns the newly added personal reader route import: `personal_briefs` must precede `personal_reading`. It was reported to the implementation owner without applying an automatic fix. A later lint-only rerun can close it; the unit and frontend commands do not need repeating for an import-order-only correction.

The rejected historical hotness-test changes remain unapplied. Their exact approval-review reasons and affected paths are recorded in `test-isolation-blocker.md`. These offline results do not replace the separately owned final PostgreSQL or browser evidence and do not imply all Phase 3 acceptance gates passed.
