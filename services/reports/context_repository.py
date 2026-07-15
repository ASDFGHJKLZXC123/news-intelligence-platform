"""Bounded, deterministic loads for the brief's composition context.

SQL, and only SQL -- the counterpart of :mod:`services.reports.repository` for the evidence,
historical-parallel and forecast contexts. Every policy decision (what counts as support, which
scenario set is current, how many claims an event may carry) lives in
:mod:`services.reports.context`, where it is tested without a database.

Three properties are enforced here rather than hoped for:

* **Bounded.** Every read has a ``LIMIT``, and the article body is bounded *in the database*:
  the excerpt query selects ``left(body, N + 1)`` and ``length(body)``, so a full article body
  is never transferred, never held in memory, and never available to be leaked into a prompt.
  The one extra character is what makes truncation decidable rather than guessed.
* **Deterministic.** Every read has a total ``ORDER BY``, so a regenerated brief cites the same
  evidence in the same order as the first one.
* **Linked-only.** Evidence reaches a claim along exactly one path --
  ``Event -> EventArticle -> Article -> EvidenceItem -> ClaimEvidence -> Claim`` -- expressed as
  inner joins. A claim that is not on that path cannot be returned by these queries at all,
  which is a stronger guarantee than filtering one out afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Text, cast, func, select
from sqlalchemy.orm import Session, aliased

from db.models.core import (
    Article,
    Claim,
    ClaimEvidence,
    EventAnalogy,
    EventArticle,
    EvidenceItem,
    ForecastScenario,
    HistoricalEpisode,
    Source,
)
from services.reports.context import (
    ARTICLE_EVIDENCE_SOURCE_TYPE,
    MAX_EXCERPT_CHARS,
    SUPPORTIVE_SUPPORT_TYPES,
    AnalogyContext,
    BoundedRead,
    EvidenceRow,
    ForecastScenarioRow,
    HistoricalOnset,
    HistoricalOutcomeContext,
    ParentEpisode,
    build_source_excerpt,
)

#: Bounds. Far above any legitimate day for five events: an event whose articles carry 400
#: supportive claims has an extraction problem, not a reporting one. The pure layer applies the
#: per-event bounds; these are the backstops against a runaway scan.
EVIDENCE_SCAN_LIMIT = 2_000
ANALOGY_SCAN_LIMIT = 200
FORECAST_SCAN_LIMIT = 500


class SQLAlchemyBriefContextRepository:
    """Loads the composition context from the operational schema. Read-only: never writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def evidence_for_events(self, event_ids: Sequence[uuid.UUID]) -> BoundedRead[EvidenceRow]:
        """Supportive, article-linked claims for the selected events.

        The `evidence_items` join carries two predicates and needs both. `source_id` is TEXT and
        `articles.id` is UUID, so the article is matched by casting the id -- the same pattern
        `services.reports.repository` uses for `risk_score_observations.target_id`. But the cast
        join alone is *not* the discriminator: an `rss` (or any other typed) evidence item whose
        `source_id` happened to equal a linked article's UUID would satisfy it, and the brief
        would cite a non-article item as an article. The exact ``source_type = 'article'``
        equality is what excludes that. Both are required -- the cast join ties the item to a
        linked article, the equality confirms the item is that article's news-article evidence.
        """
        if not event_ids:
            return BoundedRead.of((), EVIDENCE_SCAN_LIMIT)

        body_head = func.left(Article.body, MAX_EXCERPT_CHARS + 1)
        summary_head = func.left(Article.summary, MAX_EXCERPT_CHARS + 1)

        stmt = (
            select(
                EventArticle.event_id,
                Claim.id.label("claim_id"),
                Claim.claim_text,
                Claim.claim_type,
                Claim.confidence_score.label("claim_confidence"),
                ClaimEvidence.support_type,
                ClaimEvidence.confidence_score.label("support_confidence"),
                Article.id.label("article_id"),
                Article.title.label("article_title"),
                Article.url,
                Article.published_at,
                func.coalesce(EvidenceItem.publisher, Source.name).label("publisher"),
                Source.authority_score.label("source_credibility"),
                summary_head.label("summary_head"),
                body_head.label("body_head"),
                func.length(Article.summary).label("summary_length"),
                func.length(Article.body).label("body_length"),
            )
            .select_from(EventArticle)
            .join(Article, Article.id == EventArticle.article_id)
            .join(Source, Source.id == Article.source_id)
            .join(
                EvidenceItem,
                (EvidenceItem.source_id == cast(Article.id, Text))
                & (EvidenceItem.source_type == ARTICLE_EVIDENCE_SOURCE_TYPE),
            )
            .join(ClaimEvidence, ClaimEvidence.evidence_item_id == EvidenceItem.id)
            .join(Claim, Claim.id == ClaimEvidence.claim_id)
            .where(
                EventArticle.event_id.in_(event_ids),
                # Supportive only. A `contradicts` row is not a citation, and a brief that
                # printed one as support would be asserting the opposite of its evidence.
                ClaimEvidence.support_type.in_(sorted(SUPPORTIVE_SUPPORT_TYPES)),
            )
            .order_by(
                EventArticle.event_id,
                Claim.confidence_score.desc().nulls_last(),
                Claim.id,
                Article.published_at.desc().nulls_last(),
                Article.id,
                ClaimEvidence.support_type,
            )
            # Over-read by one so the reducer can be told the bound was reached, not left to
            # infer truncation from a row count that equals the bound.
            .limit(EVIDENCE_SCAN_LIMIT + 1)
        )

        fetched = [
            EvidenceRow(
                event_id=row.event_id,
                claim_id=row.claim_id,
                claim_text=row.claim_text,
                claim_type=row.claim_type,
                claim_confidence=_as_float(row.claim_confidence),
                support_type=row.support_type,
                support_confidence=_as_float(row.support_confidence),
                article_id=row.article_id,
                article_title=row.article_title,
                publisher=row.publisher,
                url=row.url,
                published_at=row.published_at,
                source_credibility=_as_float(row.source_credibility),
                excerpt=build_source_excerpt(
                    row.summary_head,
                    row.body_head,
                    summary_length=row.summary_length,
                    body_length=row.body_length,
                ),
            )
            for row in self._session.execute(stmt).all()
        ]
        return BoundedRead.of(fetched, EVIDENCE_SCAN_LIMIT)

    def analogies_for_events(
        self, event_ids: Sequence[uuid.UUID]
    ) -> BoundedRead[AnalogyContext]:
        """Persisted `event_analogies` for the selected events, joined to their episodes.

        Only *persisted* analogies. This does not run retrieval: a brief regenerated next month
        must show the parallels that were drawn on the morning it was for, not the ones today's
        corpus would produce.
        """
        if not event_ids:
            return BoundedRead.of((), ANALOGY_SCAN_LIMIT)

        parent = aliased(HistoricalEpisode)
        stmt = (
            select(EventAnalogy, HistoricalEpisode, parent)
            .join(HistoricalEpisode, HistoricalEpisode.id == EventAnalogy.historical_episode_id)
            .outerjoin(parent, parent.id == HistoricalEpisode.parent_episode_id)
            .where(EventAnalogy.event_id.in_(event_ids))
            .order_by(
                EventAnalogy.event_id,
                EventAnalogy.similarity_score.desc(),
                EventAnalogy.historical_episode_id,
            )
            # Over-read by one so a scan that reached the bound can say so.
            .limit(ANALOGY_SCAN_LIMIT + 1)
        )

        fetched = [
            AnalogyContext(
                event_id=analogy.event_id,
                similarity=float(analogy.similarity_score),
                rationale=analogy.rationale,
                limitations=tuple(analogy.limitations or ()),
                shared_causes=tuple(analogy.shared_causes or ()),
                regime_caveats=tuple(analogy.regime_caveats or ()),
                evidence_refs=analogy.evidence_refs,
                onset=HistoricalOnset(
                    episode_id=episode.id,
                    name=episode.name,
                    episode_type=episode.episode_type,
                    onset_date=episode.onset_date,
                    onset_summary=episode.onset_summary,
                    geography=episode.geography,
                    regime_tags=tuple(episode.regime_tags or ()),
                    is_counterexample=episode.is_counterexample,
                    source_refs=episode.source_refs,
                    parent=(
                        ParentEpisode(
                            episode_id=parent_row.id,
                            name=parent_row.name,
                            onset_summary=parent_row.onset_summary,
                        )
                        if parent_row is not None
                        else None
                    ),
                ),
                outcome=HistoricalOutcomeContext(
                    outcome_summary=episode.outcome_summary,
                    outcomes=tuple(episode.outcomes or ()),
                    resolution_mechanism=episode.resolution_mechanism,
                    peak_date=episode.peak_date,
                    end_date=episode.end_date,
                ),
            )
            for analogy, episode, parent_row in self._session.execute(stmt).all()
        ]
        return BoundedRead.of(fetched, ANALOGY_SCAN_LIMIT)

    def forecasts_for_events(
        self, event_ids: Sequence[uuid.UUID]
    ) -> BoundedRead[ForecastScenarioRow]:
        """Every forecast scenario for the selected events, whole sets at a time.

        Deliberately unfiltered by set: `services.reports.context.select_event_forecasts` picks
        the newest *complete* set, and it can only tell a complete set from a half-written one
        if it is given all of them. A ``WHERE scenario_set_id = (latest)`` here would make that
        judgement in SQL, silently, and would hand the composer a two-scenario set whose
        probabilities sum to 0.6. If the scan bound is ever reached the read is flagged
        truncated, because clipping the tail can sever a set and make it look half-written.
        """
        if not event_ids:
            return BoundedRead.of((), FORECAST_SCAN_LIMIT)

        stmt = (
            select(
                ForecastScenario.event_id,
                ForecastScenario.scenario_set_id,
                ForecastScenario.scenario_name,
                ForecastScenario.probability,
                ForecastScenario.risk_score,
                ForecastScenario.severity,
                ForecastScenario.horizon,
                ForecastScenario.confidence,
                ForecastScenario.evidence_refs,
                ForecastScenario.created_at,
            )
            .where(ForecastScenario.event_id.in_(event_ids))
            .order_by(
                ForecastScenario.event_id,
                ForecastScenario.created_at.desc(),
                ForecastScenario.scenario_set_id,
                ForecastScenario.scenario_name,
            )
            # Over-read by one so a scan that reached the bound can say so.
            .limit(FORECAST_SCAN_LIMIT + 1)
        )

        fetched = [
            ForecastScenarioRow(
                event_id=row.event_id,
                scenario_set_id=row.scenario_set_id,
                scenario_name=row.scenario_name,
                probability=float(row.probability),
                risk_score=float(row.risk_score),
                severity=row.severity,
                horizon=row.horizon,
                confidence=_as_float(row.confidence),
                evidence_refs=row.evidence_refs,
                created_at=row.created_at,
            )
            for row in self._session.execute(stmt).all()
        ]
        return BoundedRead.of(fetched, FORECAST_SCAN_LIMIT)


def _as_float(value: object) -> float | None:
    """NUMERIC reaches Python as `Decimal` on some columns and `float` on others."""
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]
