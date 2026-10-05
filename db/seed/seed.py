"""Database seed entrypoint.

Stage 5 gives this script its first real job: the curated historical-episode corpus
(``db/seed/episodes/``) and its labelled retrieval gold set. Everything else in the platform is
ingested; the episodes are *written*, and this is where they are loaded.

Two modes, and the split matters. ``--validate-only`` loads and checks the corpus and gold set with
no database, no API key and no network -- the mode an author runs after editing a row, and the mode
CI can run anywhere. The default mode seeds, which needs both a database and an embeddings key, and
says so plainly if the key is absent rather than failing three layers down inside an HTTP client.

Seeding is also a *gate*, not just a load. The spec's curation workflow is "LLM drafts -> human
verifies onset/outcome separation + dates + sources -> committed via seed script", and this script
is the last arrow: it refuses to publish an episode whose ``review`` block does not record a real
human sign-off. Offline validation is not that sign-off and cannot stand in for it -- it catches a
malformed row, never a fluent and confidently wrong one. ``--allow-unreviewed`` loads the drafts
anyway for a development database, says so in the log and in the summary, and is the only way past.

The gate runs before the database, the API key and the provider are touched, and the order is the
point: with an unreviewed corpus the seed is blocked no matter what the environment holds, so an
operator must hear *that* -- the blocker they have to act on -- and not a connection error from a
database the run was never going to reach.

The transaction boundary is here, at the CLI, and nowhere else: the corpus is one unit of work, so a
failure part-way through leaves the table as it was rather than half-updated. The seeder itself only
flushes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

from db.base import SessionLocal
from db.seed.episode_seed import (
    EpisodeReviewGateError,
    EpisodeSeedSummary,
    enforce_review_gate,
    seed_episode_corpus,
)
from packages.config.logging import configure_logging, get_logger
from packages.config.settings import get_settings
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE, OpenAIEmbeddingProvider
from services.analogies.corpus import (
    CorpusValidationError,
    load_corpus_and_gold,
    quota_report,
    unreviewed_episodes,
)
from services.nlp.embeddings import build_embedding_provider
from services.nlp.snapshot_registry import SnapshotVerification, verify_snapshot

logger = get_logger("db.seed")

_NO_API_KEY = (
    "OPENAI_API_KEY is not set, so the curated episodes cannot be embedded and must not be "
    "inserted with a placeholder vector. Set OPENAI_API_KEY to seed, or run "
    "`python -m db.seed.seed --validate-only` to check the corpus offline."
)


def validate_only() -> dict[str, object]:
    """Load and validate the corpus and gold set. No database, no network, no API key."""
    corpus, gold = load_corpus_and_gold()
    report = quota_report(corpus, gold)
    logger.info(
        "corpus validated", extra={"episodes": corpus.quotas.total, "pairs": len(gold.pairs)}
    )
    return report


def build_provider() -> OpenAIEmbeddingProvider:
    """The production embeddings adapter, with an actionable failure when the key is missing."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit(_NO_API_KEY)
    return build_embedding_provider(settings)


def _verify_live_snapshot(
    provider: OpenAIEmbeddingProvider,
) -> SnapshotVerification | None:
    """Replay fixed probes around the seed so a batch cannot span hosted-alias drift."""

    if not isinstance(provider, OpenAIEmbeddingProvider):
        return None
    settings = get_settings()
    if not settings.embedding_require_registered_snapshot:
        return None
    return verify_snapshot(provider, provider.model_version)


def seed_episodes(
    *, batch_size: int = MAX_EMBEDDING_BATCH_SIZE, allow_unreviewed: bool = False
) -> EpisodeSeedSummary:
    """Seed the curated corpus in one transaction. Commits only on complete success."""
    # Validate before anything is opened: a corpus that cannot be trusted must not reach the API.
    corpus, gold = load_corpus_and_gold()

    # The spec's human-verification step, before the API key is even looked for. The seeder enforces
    # this again for any other caller; running it here is what makes the *first* thing an operator
    # hears the blocker that actually applies.
    enforce_review_gate(corpus, allow_unreviewed=allow_unreviewed)
    if allow_unreviewed:
        logger.warning(
            "seeding episodes that no human has reviewed: these are drafts, not verified corpus",
            extra={"unreviewed": len(unreviewed_episodes(corpus)), "episodes": corpus.quotas.total},
        )

    # Verify the durable processing mode before constructing or probing a provider.
    with contextlib.ExitStack() as stack:
        session = SessionLocal()
        stack.callback(session.close)
        try:
            from services.writer_mode import require_legacy_maintenance_mode

            require_legacy_maintenance_mode(session)
            provider = build_provider()
            stack.callback(provider.close)
            _verify_live_snapshot(provider)
            summary = seed_episode_corpus(
                session, corpus, provider, batch_size=batch_size, allow_unreviewed=allow_unreviewed
            )
            _verify_live_snapshot(provider)
            if summary.written == 0 and summary.unchanged == 0:
                # Cannot happen with a validated corpus, and would mean a silent no-op if it did.
                raise RuntimeError("seed processed no episodes; refusing to report success")
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("episode seed failed; transaction rolled back")
            raise

    logger.info(
        "episode seed complete",
        extra={
            "inserted": summary.inserted,
            "updated": summary.updated,
            "unchanged": summary.unchanged,
            "re_embedded": summary.re_embedded,
            "embedding_requests": list(summary.embedding_requests),
            "gold_pairs": len(gold.pairs),
        },
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Seed the database with curated Stage 5 data.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the episode corpus and gold set offline; no database, no API key",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=MAX_EMBEDDING_BATCH_SIZE,
        help=f"texts per embeddings request (max {MAX_EMBEDDING_BATCH_SIZE})",
    )
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help=(
            "seed episodes no human has verified. They are drafts, not verified corpus: for a "
            "development or staging database only"
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.validate_only:
            report = validate_only()
        else:
            # No connectivity probe before this call. Seeding loads the corpus and clears the human
            # review gate first, and the session's own transaction fails just as loudly on an
            # unreachable database at its first read -- before an embedding is ever requested. A
            # `SELECT 1` here bought nothing and cost the gate its place in the order.
            report = seed_episodes(
                batch_size=args.batch_size, allow_unreviewed=args.allow_unreviewed
            ).as_dict()
    except CorpusValidationError as exc:
        # The problems, not a stack trace: this is the message the curator has to act on.
        print(f"corpus validation failed:\n{exc}", file=sys.stderr)
        return 1
    except EpisodeReviewGateError as exc:
        # Not a crash and not a bug: the corpus is still waiting on the human step the spec puts
        # before this one. Same treatment -- the message, not a traceback.
        print(f"episode review gate:\n{exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
