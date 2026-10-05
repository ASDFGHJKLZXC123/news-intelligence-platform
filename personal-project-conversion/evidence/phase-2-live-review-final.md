# Independent Phase 2 bounded live software review

Scope: production PostgreSQL sessions with autoflush=False; actual RSS-I/O-to-coordinator-to-CORE01-to-shared-composition and production OpenAI HTTP adapters, with all RSS/provider transport replaced by independent fixtures. No real network, live feed, paid provider, production database, or browser calls. This is software/component proof only; it does not close Phase 2 or the live acceptance gate.

Final results: eight dedicated-worker scenarios PASS, actual API start/retry scenario PASS, fourteen accounting adversaries PASS. Four additional preflight/redirect scenarios and two identity variants passed at their separately recorded stable hashes. No remaining concrete finding in this bounded review.

The actual independent harbor story starts with zero Article/Event/Claim/EvidenceItem/ClaimEvidence/Report/LLMRun, creates fresh source-supported claims, and publishes two substantive source-specific sections through five counted physical fake HTTP calls. Primary 503 to configured fallback uses nine counted calls, all reconciled. Primary timeout to fallback retains published output but records verification_blocked/accounting_incomplete with exact capture/snapshot/report IDs; uncertain reservations remain held and restart is refused. Oversize embedding dispatch is rejected before any physical POST.

Configuration/ledger mismatch is rejected before SessionLocal, settings, RSS, or HTTP construction, including same verification ID with a different parsed configuration. Bound-ledger restart and a new ledger against the existing database are rejected. Standalone preexisting Claim and EvidenceItem independently prevent fresh-database execution. Ordinary live start and retry return409 with zero enqueues; after active profile switches to raw, frozen-live retry remains ineligible and preserves attempt/error/token.

All source edits were by implementers. Reviewer modified only /tmp scratch artifacts and owned disposable infrastructure.

## Evidence

- /tmp/nip-p2-live-worker-fixed-review-c47e.log (SHA256 `c536a92777876bbd57d432f5227fc27d42941b46e71c7653662cf8716f55166b`)
- /tmp/nip-p2-live-api-fixed-review-c47e.log (SHA256 `876787833fb94ca91ef7c5e5376582e0152fdb604246547de5146e4cecf8b1cf`)
- /tmp/nip-p2-live-usage-final-review-b652.log (SHA256 `a0e6807e196d6da5bb727161bcbc78dd1a4011d86c7062779988942ec1bd202d`)
- /tmp/nip-p2-live-boundaries-review-c47e.log (SHA256 `fc94f0e811cdb3de51d896aedfd2eb199d98e47f5ec5745956a66803fbdf932e`)
- /tmp/nip-p2-live-identity-review-c47e.log (SHA256 `341c21a2ae8cd20340756e498ae7fb633d616d47dab634b6a1c7fbc5d5c177ab`)

## Final source hashes

- `workers/personal_tasks.py`: `da6e53bebd244501800f62a8c46587b25d366b3e86f7a8cb43469d92e790f143`
- `services/personal/live_smoke.py`: `64f94431bddfb7a639a2e70f5513fa440370534e24fae40bd7639e55ccd36d7d`
- `services/personal/runs.py`: `f42c080999fa7d7eeb8882d2c046c1cf078fa67b00830cd2c6c5f5c5b30782e5`
- `apps/api/personal.py`: `5ef6cb6a4ca606f628bf3088d57640b0418a1370620a39282e47de1335b18109`
- `services/personal/coordinator.py`: `34554dd7e81a5e336d09e011b532e66e247fd23b844760c82062d8b340752a2a`
- `services/llm/http_providers.py`: `4e9cb5b496f8b5a4192699503e05477447c069b2128308e21d1b3bbb4747929a`
- `services/personal/briefs.py`: `aa46b0f689b4275ea37af6001fec97cbc418da78d159c1cbeb8f9f7ea11935ba`
- `db/migrations/versions/0020_personal_news_desk.py`: `0f9d96e46aadc0db4d903dd7a0fa927bc7cc09f9869d2c2fffbb475b9f2f12eb`

## Cleanup

At 2026-09-09T19:35:40.598463+00:00, maintenance-only lookup found no nip_* review databases. Removed owned container `nip-p2-worker-review-74cd` (exact identity checked); subsequent Docker inspection confirmed it absent. Root/backend resources were untouched.
