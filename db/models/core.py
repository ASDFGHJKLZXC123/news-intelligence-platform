"""Core pipeline ORM models: sources, articles, embeddings, events, LLM runs, jobs.

These back Stages P3-P9 of the news pipeline. They are the non-model half of the schema;
the crisis-model tables live in `db.models.risk`. Embedding dimension is fixed at
``EMBEDDING_DIM`` so an HNSW index can be built (ADR 0003).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from db.base import Base
from db.models.enums import SourceType

EMBEDDING_DIM = 1536

#: Embedding identity written by the pipeline (ADR 0004). Embedding rows are keyed by
#: ``(article_id, model, model_version)``, so readers must pin the pair they want.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_MODEL_VERSION = "current"

#: Type of a persisted contract score (migration 0014): 0-100 at the contract's two decimal
#: places. ``asdecimal=False`` keeps the mapped attribute a plain ``float``, so the scores
#: stay arithmetic-compatible with the rest of the pipeline and with ``Mapped[float]``.
SCORE_NUMERIC = Numeric(5, 2, asdecimal=False)

# Vocabularies shared with migrations 0013/0014. `*_score` fields are 0-100 -- including
# `similarity_score`, which migration 0014 rescaled off its original 0-1 scale. `confidence`
# and `probability` remain 0-1 (llm-contracts-reconciliation spec).
EPISODE_TYPES = (
    "banking_stress",
    "sovereign_debt",
    "supply_shock",
    "industry_shock",
    "company_distress",
    "geopolitical",
    "pandemic",
    "monetary_regime",
)

EPISODE_OUTCOMES = (
    "contained",
    "systemic_crisis",
    "recession",
    "default",
    "bailout",
    "failure",
    "recovery",
    "regime_change",
)

HORIZONS = ("0_6m", "6_12m", "12_18m", "within_18m")
SCENARIO_NAMES = ("base_case", "upside_case", "downside_case", "tail_risk_case")
RISK_LEVELS = ("low", "medium", "high", "critical")

LLM_RUN_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "validation_failed",
    "cached",
    "skipped",
)
# ADR 0010 lifecycle. `alerts.state` is the single lifecycle column; the API serves it as
# the wire field `status` (api-adapter-contract "Enums").
ALERT_STATES = ("open", "escalated", "downgraded", "resolved", "superseded")
ACTIVE_ALERT_STATES = ("open", "escalated", "downgraded")
REPORT_STATUSES = ("generating", "grounding_check", "published", "failed")
GROUNDING_STATUSES = ("pending", "passed", "failed", "data_quality_note")
REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY = "descriptive_only.v1"
REPORT_CONTENT_POLICY_PREDICTION_BACKED = "prediction_backed.v1"
REPORT_CONTENT_POLICIES = (
    REPORT_CONTENT_POLICY_DESCRIPTIVE_ONLY,
    REPORT_CONTENT_POLICY_PREDICTION_BACKED,
)


def _sql_enum(values: tuple[str, ...]) -> str:
    """Render a vocabulary as a SQL `IN (...)` value list."""
    return ", ".join(f"'{value}'" for value in values)


class GeometryPoint(UserDefinedType):
    """PostGIS WGS84 point type without requiring GeoAlchemy for schema metadata."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "geometry(Point, 4326)"


class Source(Base):
    """A news source (RSS feed for MVP; other channels gated)."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SourceType.RSS.value
    )
    feed_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    homepage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Article(Base):
    """A normalized article. Idempotency key is ``url_hash`` (sha256 of normalized URL)."""

    __tablename__ = "articles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ArticleEmbedding(Base):
    """An embedding vector for an article (fixed dimension for HNSW indexing)."""

    __tablename__ = "article_embeddings"

    article_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False, primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_article_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Event(Base):
    """A cluster of related articles representing one real-world event."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    #: How newsworthy the event is *right now* -- the daily brief ranks Top Events on
    #: `0.6 x hotness_score + 0.4 x max_linked_risk_score` and qualifies nothing below 40
    #: (report-generation spec, "Content selection"). It is not `severity_score`: severity
    #: measures how big the event is (volume and source diversity), hotness measures how fast
    #: and how widely it is being reported and by whom, so it also reads coverage velocity and
    #: source authority. `services.nlp.features.event_hotness_score` is the formula.
    #:
    #: Nullable and never backfilled: hotness is a function of the coverage timing that formed
    #: the cluster, and events clustered before the column existed were never scored on it.
    #: Selection excludes a NULL hotness rather than guessing one (see `services.reports`).
    hotness_score: Mapped[float | None] = mapped_column(SCORE_NUMERIC, nullable=True)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "severity_score IS NULL OR (severity_score >= 0 AND severity_score <= 100)",
            name="ck_events_severity_score",
        ),
        CheckConstraint(
            "hotness_score IS NULL OR (hotness_score >= 0 AND hotness_score <= 100)",
            name="ck_events_hotness_score",
        ),
        # The daily brief's one event query: window on `updated_at` (ADR 0009), then the
        # hotness floor. Leading range column, filter column second.
        Index("ix_events_updated_at_hotness", "updated_at", "hotness_score"),
    )


class EventArticle(Base):
    """Association of an article to an event, with the clustering similarity."""

    __tablename__ = "event_articles"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True
    )
    similarity: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventEmbedding(Base):
    """Embedding vector for event-level semantic search and historical similarity."""

    __tablename__ = "event_embeddings"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_event_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class EventEntity(Base):
    """Canonical entity exposure for an event."""

    __tablename__ = "event_entities"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    entity_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    impact_direction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impact_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "impact_score IS NULL OR (impact_score >= 0 AND impact_score <= 100)",
            name="ck_event_entities_impact_score",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="ck_event_entities_confidence_score",
        ),
        Index("ix_event_entities_entity", "entity_profile_id"),
        Index("ix_event_entities_impact", "impact_direction", "impact_score"),
    )


class EventCompany(Base):
    """Company exposure, impact, and risk explanation for an event."""

    __tablename__ = "event_companies"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    impact_direction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impact_score: Mapped[float | None] = mapped_column(SCORE_NUMERIC, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    exposure_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "impact_score IS NULL OR (impact_score >= 0 AND impact_score <= 100)",
            name="ck_event_companies_impact_score",
        ),
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name="ck_event_companies_risk_score",
        ),
        CheckConstraint(
            "confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)",
            name="ck_event_companies_confidence_score",
        ),
        Index("ix_event_companies_company", "company_id"),
        Index("ix_event_companies_scores", "impact_score", "risk_score"),
    )


class EventIndustry(Base):
    """Industry exposure, impact, and opportunity for an event."""

    __tablename__ = "event_industries"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    industry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    impact_direction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impact_score: Mapped[float | None] = mapped_column(SCORE_NUMERIC, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    opportunity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "impact_score IS NULL OR (impact_score >= 0 AND impact_score <= 100)",
            name="ck_event_industries_impact_score",
        ),
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name="ck_event_industries_risk_score",
        ),
        CheckConstraint(
            "opportunity_score IS NULL OR (opportunity_score >= 0 AND opportunity_score <= 100)",
            name="ck_event_industries_opportunity_score",
        ),
        Index("ix_event_industries_industry", "industry_id"),
        Index("ix_event_industries_scores", "impact_score", "risk_score"),
    )


class EventTimelineItem(Base):
    """Timeline entry for an event detail page."""

    __tablename__ = "event_timeline_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    importance: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_event_timeline_event_timestamp", "event_id", "timestamp"),)


class EventLocation(Base):
    """Geocoded event location for map and country/region queries."""

    __tablename__ = "event_locations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    location_name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    geom: Mapped[Any | None] = mapped_column(GeometryPoint(), nullable=True)
    location_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_event_locations_geom_gist", "geom", postgresql_using="gist"),
        Index("ix_event_locations_country_region", "country_code", "region"),
    )


class GeocodingCache(Base):
    """Cached geocoding result for deterministic address/location resolution."""

    __tablename__ = "geocoding_cache"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_query: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    geom: Mapped[Any | None] = mapped_column(GeometryPoint(), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    admin1: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "normalized_query", name="uq_geocoding_cache_provider_query"),
        Index("ix_geocoding_cache_normalized_query", "normalized_query"),
        Index("ix_geocoding_cache_geom_gist", "geom", postgresql_using="gist"),
    )


class LLMRun(Base):
    """An audit row for one LLM invocation (evidence/audit contract)."""

    __tablename__ = "llm_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    prompt_name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    model_params: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="succeeded")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    output_schema_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    # `output` is the normalized contract payload used downstream; `raw_output` keeps the
    # provider payload exactly as parsed, so a normalized score stays auditable.
    raw_output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    no_finding_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_enum(LLM_RUN_STATUSES)})",
            name="ck_llm_runs_status",
        ),
        CheckConstraint("attempt >= 1", name="ck_llm_runs_attempt_positive"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_llm_runs_input_tokens_non_negative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_llm_runs_output_tokens_non_negative",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_llm_runs_latency_ms_non_negative",
        ),
        CheckConstraint(
            "cost_usd IS NULL OR cost_usd >= 0",
            name="ck_llm_runs_cost_usd_non_negative",
        ),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="ck_llm_runs_temperature_range",
        ),
        Index("ix_llm_runs_trace_id", "trace_id"),
        Index("ix_llm_runs_status_created", "status", "created_at"),
    )


class EvidenceItem(Base):
    """Queryable evidence item linked to raw provider data or normalized records."""

    __tablename__ = "evidence_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    credibility_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    raw_ref: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    evidence_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", name="uq_evidence_items_source"),
        Index("ix_evidence_items_source", "source_type", "source_id"),
        Index("ix_evidence_items_publisher_published", "publisher", "published_at"),
    )


class Claim(Base):
    """Structured claim extracted from evidence or generated by model analysis."""

    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ClaimEvidence(Base):
    """Relationship between a claim and an evidence item."""

    __tablename__ = "claim_evidence"

    claim_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("claims.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("evidence_items.id", ondelete="CASCADE"), primary_key=True
    )
    support_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_claim_evidence_evidence_item", "evidence_item_id"),
        Index("ix_claim_evidence_support_type", "support_type"),
    )


class Job(Base):
    """Durable persistence of the Job Contract (mirrors packages.jobs.contract)."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    job_type: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    related_ids: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    safe_to_rerun: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ProviderRun(Base):
    """Audit row for one external provider ingestion attempt."""

    __tablename__ = "provider_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_key: Mapped[str] = mapped_column(String(192), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="queued", index=True
    )
    parameters: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    stats: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_provider_runs_provider_status", "provider", "status"),)


class SourceCursor(Base):
    """Durable source cursor for resumable provider ingestion."""

    __tablename__ = "source_cursors"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_key: Mapped[str] = mapped_column(String(192), nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    last_provider_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_runs.id", ondelete="SET NULL"), nullable=True
    )
    advanced_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "cursor_key", name="uq_source_cursors_provider_key"),
        Index("ix_source_cursors_provider_key", "provider", "cursor_key"),
    )


class RawIngestionItem(Base):
    """Raw provider payload retained before normalization into domain tables."""

    __tablename__ = "raw_ingestion_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(192), nullable=False, unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_raw_ingestion_items_provider_type", "provider", "item_type"),)


class SourceHealthSnapshot(Base):
    """Point-in-time health metric for an external source or provider."""

    __tablename__ = "source_health_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    checked_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_rate: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    items_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_source_health_provider_checked", "provider", "checked_at"),
        Index("ix_source_health_status_checked", "status", "checked_at"),
    )


class RawDocumentAsset(Base):
    """Pointer to large raw documents stored outside PostgreSQL."""

    __tablename__ = "raw_document_assets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    storage_url: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asset_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "asset_type",
            "external_id",
            "content_hash",
            name="uq_raw_document_assets_provider_type_external_hash",
        ),
        Index("ix_raw_document_assets_provider_type", "provider", "asset_type"),
    )


class MacroSeries(Base):
    """External macroeconomic time-series metadata."""

    __tablename__ = "macro_series"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    series_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    units: Mapped[str | None] = mapped_column(String(128), nullable=True)
    seasonal_adjustment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_metadata: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "series_id", name="uq_macro_series_provider_series_id"),
        Index("ix_macro_series_provider_series_id", "provider", "series_id"),
    )


class MacroObservation(Base):
    """One dated observation for a macroeconomic series."""

    __tablename__ = "macro_observations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    series_uuid: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("macro_series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    realtime_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    realtime_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    observation_metadata: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "series_uuid",
            "observed_on",
            "realtime_start",
            "realtime_end",
            name="uq_macro_observations_series_date_realtime",
        ),
        Index("ix_macro_observations_series_date", "series_uuid", "observed_on"),
    )


class MacroSignalSnapshot(Base):
    """Derived country/date macro signal panel backed by provider observations."""

    __tablename__ = "macro_signal_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("provider_runs.id", ondelete="SET NULL"), nullable=True
    )
    country: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    signals: Mapped[Any] = mapped_column(JSONB, nullable=False)
    observation_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("country", "snapshot_date", name="uq_macro_signal_snapshots_country_date"),
        Index("ix_macro_signal_snapshots_country_date", "country", "snapshot_date"),
    )


class SECCompany(Base):
    """SEC company identity and metadata keyed by CIK."""

    __tablename__ = "sec_companies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cik: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sic: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sic_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fiscal_year_end: Mapped[str | None] = mapped_column(String(8), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    company_metadata: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SECFiling(Base):
    """SEC filing metadata for a company."""

    __tablename__ = "sec_filings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sec_companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    accession_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    form_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    filing_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    report_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    primary_document_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_detail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filing_metadata: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_sec_filings_company_form_date", "company_id", "form_type", "filing_date"),
    )


class SECCompanyFact(Base):
    """Structured XBRL/company-facts values extracted from SEC data."""

    __tablename__ = "sec_company_facts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sec_companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    taxonomy: Mapped[str] = mapped_column(String(64), nullable=False)
    concept: Mapped[str] = mapped_column(String(192), nullable=False)
    unit: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    filed_at: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    accession_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    form_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fiscal_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    frame: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_metadata: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "taxonomy",
            "concept",
            "unit",
            "period_end",
            "accession_number",
            name="uq_sec_company_facts_identity",
        ),
        Index("ix_sec_company_facts_company_concept", "company_id", "taxonomy", "concept"),
    )


class SanctionsList(Base):
    """Provider sanctions list metadata, such as OFAC SDN or consolidated lists."""

    __tablename__ = "sanctions_lists"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    list_code: Mapped[str] = mapped_column(String(64), nullable=False)
    list_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    list_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )

    __table_args__ = (
        UniqueConstraint("provider", "list_code", name="uq_sanctions_lists_provider_code"),
    )


class SanctionsEntity(Base):
    """Normalized sanctions subject retained separately from aliases and identifiers."""

    __tablename__ = "sanctions_entities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sanctions_list_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sanctions_lists.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    list_code: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_uid: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    primary_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    programs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    first_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "list_code",
            "entity_uid",
            name="uq_sanctions_entities_provider_list_uid",
        ),
        Index("ix_sanctions_entities_provider_list", "provider", "list_code"),
        Index("ix_sanctions_entities_normalized_name", "normalized_name"),
    )


class SanctionsAlias(Base):
    """Alias or alternate spelling for a sanctions entity."""

    __tablename__ = "sanctions_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sanctions_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sanctions_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "sanctions_entity_id",
            "normalized_alias",
            "alias_type",
            name="uq_sanctions_aliases_entity_alias_type",
        ),
        Index("ix_sanctions_aliases_normalized_alias", "normalized_alias"),
    )


class SanctionsIdentifier(Base):
    """Document, registration, or other identifier attached to a sanctions entity."""

    __tablename__ = "sanctions_identifiers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sanctions_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sanctions_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identifier_type: Mapped[str] = mapped_column(String(64), nullable=False)
    identifier_value: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "sanctions_entity_id",
            "identifier_type",
            "identifier_value",
            name="uq_sanctions_identifiers_entity_type_value",
        ),
        Index("ix_sanctions_identifiers_type_value", "identifier_type", "identifier_value"),
    )


class SanctionsMatch(Base):
    """Entity-resolution match between an internal target and a sanctions entity."""

    __tablename__ = "sanctions_matches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    sanctions_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sanctions_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_method: Mapped[str] = mapped_column(String(64), nullable=False)
    match_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    matched_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending", index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "target_type",
            "target_id",
            "sanctions_entity_id",
            "match_method",
            name="uq_sanctions_matches_target_entity_method",
        ),
        Index("ix_sanctions_matches_target", "target_type", "target_id"),
    )


class EntityProfile(Base):
    """Canonical entity profile joining filings, sanctions, LEI, and news evidence."""

    __tablename__ = "entity_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    primary_ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    primary_cik: Mapped[str | None] = mapped_column(String(16), nullable=True, unique=True)
    primary_lei: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_entity_profiles_normalized_name", "normalized_name"),)


class EntityIdentifier(Base):
    """Provider-backed identifier for a canonical entity profile."""

    __tablename__ = "entity_identifiers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identifier_type: Mapped[str] = mapped_column(String(64), nullable=False)
    identifier_value: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source_ref: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "entity_profile_id",
            "identifier_type",
            "identifier_value",
            "provider",
            name="uq_entity_identifiers_profile_type_value_provider",
        ),
        Index("ix_entity_identifiers_type_value", "identifier_type", "identifier_value"),
    )


class EntityAlias(Base):
    """Alternate names for a canonical entity profile."""

    __tablename__ = "entity_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, server_default="internal")
    valid_from: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "entity_id",
            "normalized_alias",
            "source",
            name="uq_entity_aliases_entity_normalized_alias_source",
        ),
        # Candidate lookup by surface form is the linker's hot path (ADR 0005/0006).
        Index("ix_entity_aliases_normalized_alias", "normalized_alias"),
    )


class EntityRedirect(Base):
    """Historical aliasing for renamed/merged entities."""

    __tablename__ = "entity_redirects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    old_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    new_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "old_entity_id",
            "new_entity_id",
            "effective_date",
            name="uq_entity_redirects_old_new_effective_date",
        ),
        CheckConstraint(
            "old_entity_id <> new_entity_id", name="ck_entity_redirects_no_self_redirect"
        ),
    )


class EntityRelationship(Base):
    """Parent, subsidiary, ownership, or control relationship between entity profiles."""

    __tablename__ = "entity_relationships"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    child_entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    valid_from: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    relationship_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "parent_entity_id",
            "child_entity_id",
            "relationship_type",
            "provider",
            name="uq_entity_relationships_parent_child_type_provider",
        ),
        Index("ix_entity_relationships_type", "relationship_type"),
    )


class EntityResolutionRun(Base):
    """Audit trail for one deterministic entity-resolution attempt."""

    __tablename__ = "entity_resolution_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_key: Mapped[str] = mapped_column(String(192), nullable=False, unique=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    input_names: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    matched_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_entity_resolution_runs_target", "target_type", "target_id"),)


class Company(Base):
    """Canonical company master row used by frontend/API company surfaces."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entity_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("entity_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_ticker: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("entity_profile_id", name="uq_companies_entity_profile"),
        UniqueConstraint(
            "primary_ticker",
            "exchange",
            name="uq_companies_primary_ticker_exchange",
        ),
        Index("ix_companies_primary_ticker_exchange", "primary_ticker", "exchange"),
        Index("ix_companies_country_industry", "country", "industry"),
        Index("ix_companies_active", "active"),
    )


class CompanyAlias(Base):
    """Alias, former name, or provider-observed spelling for a canonical company."""

    __tablename__ = "company_aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, server_default="internal")
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "normalized_alias",
            "source",
            name="uq_company_aliases_company_alias_source",
        ),
        Index("ix_company_aliases_normalized_alias", "normalized_alias"),
    )


class CompanyIdentifier(Base):
    """Durable identifier attached to a canonical company."""

    __tablename__ = "company_identifiers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identifier_type: Mapped[str] = mapped_column(String(64), nullable=False)
    identifier_value: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, server_default="internal")
    valid_from: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "identifier_type",
            "identifier_value",
            "provider",
            name="uq_company_identifiers_company_type_value_provider",
        ),
        Index("ix_company_identifiers_type_value", "identifier_type", "identifier_value"),
    )


class HistoricalEpisode(Base):
    """Historical episodes with onset embeddings and normalized outcome tags."""

    __tablename__ = "historical_episodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    episode_type: Mapped[str] = mapped_column(String(64), nullable=False)
    onset_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    peak_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    onset_summary: Mapped[str] = mapped_column(Text, nullable=False)
    onset_indicators: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    onset_embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    model: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default="text-embedding-3-small"
    )
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, server_default="current")
    outcome_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcomes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    resolution_mechanism: Mapped[str | None] = mapped_column(Text, nullable=True)
    geography: Mapped[str | None] = mapped_column(Text, nullable=True)
    affected_industries: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    regime_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    parent_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("historical_episodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_counterexample: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    source_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    license_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    review_due_at: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"episode_type IN ({_sql_enum(EPISODE_TYPES)})",
            name="ck_historical_episodes_type",
        ),
        # Array containment, not a subquery: Postgres rejects subqueries in CHECK.
        CheckConstraint(
            f"outcomes IS NULL OR outcomes <@ ARRAY[{_sql_enum(EPISODE_OUTCOMES)}]::text[]",
            name="ck_historical_episodes_outcomes",
        ),
        CheckConstraint("version >= 1", name="ck_historical_episodes_version_positive"),
        CheckConstraint(
            "peak_date IS NULL OR peak_date >= onset_date",
            name="ck_historical_episodes_peak_after_onset",
        ),
        CheckConstraint(
            "end_date IS NULL OR end_date >= onset_date",
            name="ck_historical_episodes_end_after_onset",
        ),
        CheckConstraint(
            "parent_episode_id IS NULL OR parent_episode_id <> id",
            name="ck_historical_episodes_no_self_parent",
        ),
        Index(
            "ix_historical_episodes_onset_embedding_hnsw",
            "onset_embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"onset_embedding": "vector_cosine_ops"},
        ),
        # Hard filters run before vector similarity (episode spec, "Retrieval").
        Index("ix_historical_episodes_episode_type", "episode_type"),
        Index("ix_historical_episodes_onset_date", "onset_date"),
        Index("ix_historical_episodes_regime_tags", "regime_tags", postgresql_using="gin"),
        Index(
            "ix_historical_episodes_affected_industries",
            "affected_industries",
            postgresql_using="gin",
        ),
    )


class HistoricalEpisodeEmbedding(Base):
    """One immutable onset vector for a curated episode revision and model snapshot.

    ``historical_episodes`` keeps its original embedding columns during the expand phase so old
    code and old rows remain valid. New readers use this sidecar instead: its key makes both the
    curated episode revision and the exact model snapshot explicit, so producing a new vector
    never overwrites or relabels an older one.
    """

    __tablename__ = "historical_episode_embeddings"

    historical_episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("historical_episodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False, primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, primary_key=True)
    episode_version: Mapped[int] = mapped_column(Integer, nullable=False, primary_key=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    onset_embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "episode_version >= 1",
            name="ck_historical_episode_embeddings_episode_version_positive",
        ),
        CheckConstraint(
            f"dimension = {EMBEDDING_DIM}",
            name="ck_historical_episode_embeddings_dimension",
        ),
        CheckConstraint(
            "input_contract_version <> ''",
            name="ck_historical_episode_embeddings_input_contract",
        ),
        CheckConstraint(
            "input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_historical_episode_embeddings_input_sha256",
        ),
        CheckConstraint(
            "snapshot_manifest_sha256 IS NULL OR snapshot_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_historical_episode_embeddings_snapshot_manifest_sha256",
        ),
        Index(
            "ix_historical_episode_embeddings_onset_embedding_hnsw",
            "onset_embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"onset_embedding": "vector_cosine_ops"},
        ),
    )


class ForecastScenario(Base):
    """Forecast scenarios generated for a live event."""

    __tablename__ = "forecast_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    llm_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Groups the scenarios emitted by one Forecast Agent run. The MECE rule (probabilities
    #: sum to 1.0 +/- 0.01) is checked by the validator over a set, so the set needs an id.
    scenario_set_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    scenario_name: Mapped[str] = mapped_column(String(32), nullable=False)
    probability: Mapped[float] = mapped_column(Numeric, nullable=False)
    risk_score: Mapped[float] = mapped_column(SCORE_NUMERIC, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    assumptions: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    triggers: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    leading_indicators: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    expected_impact: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    invalidation_signals: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "scenario_set_id",
            "scenario_name",
            name="uq_forecast_scenarios_set_scenario_name",
        ),
        CheckConstraint(
            f"scenario_name IN ({_sql_enum(SCENARIO_NAMES)})",
            name="ck_forecast_scenarios_scenario_name",
        ),
        CheckConstraint(
            "probability >= 0 AND probability <= 1", name="ck_forecast_scenarios_probability"
        ),
        CheckConstraint(
            "risk_score >= 0 AND risk_score <= 100", name="ck_forecast_scenarios_risk_score"
        ),
        CheckConstraint(
            f"severity IN ({_sql_enum(RISK_LEVELS)})",
            name="ck_forecast_scenarios_severity",
        ),
        CheckConstraint(
            f"horizon IN ({_sql_enum(HORIZONS)})",
            name="ck_forecast_scenarios_horizon",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_forecast_scenarios_confidence",
        ),
    )


class EventAnalogy(Base):
    """Historical analogy match for an event."""

    __tablename__ = "event_analogies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    historical_episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("historical_episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    llm_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    similarity_score: Mapped[float] = mapped_column(SCORE_NUMERIC, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    shared_causes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    regime_caveats: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "historical_episode_id",
            name="uq_event_analogies_event_episode",
        ),
        CheckConstraint(
            "similarity_score >= 0 AND similarity_score <= 100",
            name="ck_event_analogies_similarity_score",
        ),
    )


class RiskWarning(Base):
    """Risk warning candidates before persistence into alerts."""

    __tablename__ = "risk_warnings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    llm_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    risk_type: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_score: Mapped[float] = mapped_column(SCORE_NUMERIC, nullable=False)
    probability: Mapped[float] = mapped_column(Numeric, nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    warning_message: Mapped[str] = mapped_column(Text, nullable=False)
    what_could_reduce_risk: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "risk_score >= 0 AND risk_score <= 100", name="ck_risk_warnings_risk_score"
        ),
        CheckConstraint(
            "probability >= 0 AND probability <= 1", name="ck_risk_warnings_probability"
        ),
        CheckConstraint(
            f"horizon IN ({_sql_enum(HORIZONS)})",
            name="ck_risk_warnings_horizon",
        ),
        CheckConstraint(
            f"severity IN ({_sql_enum(RISK_LEVELS)})",
            name="ck_risk_warnings_severity",
        ),
    )


class Security(Base):
    """Tradable security issued by a canonical company."""

    __tablename__ = "securities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    mic: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    asset_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("ticker", "exchange", "mic", name="uq_securities_ticker_exchange_mic"),
        Index("ix_securities_company_active", "company_id", "active"),
        Index("ix_securities_ticker_exchange", "ticker", "exchange"),
    )


class CountryIndicatorSeries(Base):
    """Global country-indicator registry, e.g. World Bank indicator metadata."""

    __tablename__ = "country_indicator_series"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    indicator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "indicator_id",
            name="uq_country_indicator_series_provider_indicator",
        ),
        Index("ix_country_indicator_series_provider_indicator", "provider", "indicator_id"),
    )


class CountryIndicatorObservation(Base):
    """One country/date observation for a country indicator series."""

    __tablename__ = "country_indicator_observations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("country_indicator_series.id", ondelete="CASCADE"), nullable=False
    )
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    country_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_on: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "country_code",
            "observed_on",
            name="uq_country_indicator_observations_series_country_date",
        ),
        Index("ix_country_indicator_observations_series_date", "series_id", "observed_on"),
        Index(
            "ix_country_indicator_observations_country_date",
            "country_code",
            "observed_on",
        ),
    )


class CountryContextSnapshot(Base):
    """Country-level derived context panel used by risk explanation APIs."""

    __tablename__ = "country_context_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    sovereign_context: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    governance_context: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    debt_context: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    trade_context: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    development_context: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    snapshot_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "country_code",
            "snapshot_date",
            name="uq_country_context_snapshots_country_date",
        ),
        Index("ix_country_context_snapshots_country_date", "country_code", "snapshot_date"),
    )


class HumanitarianReport(Base):
    """Curated humanitarian report metadata and retained source payload."""

    __tablename__ = "humanitarian_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    country_codes: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    disaster_types: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    organizations: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    themes: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_humanitarian_reports_provider_id"),
    )


class HumanitarianReportEntity(Base):
    """Named entity extracted from a humanitarian report."""

    __tablename__ = "humanitarian_report_entities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("humanitarian_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "entity_type",
            "normalized_name",
            name="uq_humanitarian_report_entities_report_type_name",
        ),
        Index("ix_humanitarian_report_entities_normalized_name", "normalized_name"),
    )


class GeoIncident(Base):
    """Normalized geospatial incident from disaster or thermal anomaly providers."""

    __tablename__ = "geo_incidents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    magnitude: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    severity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_geo_incidents_provider_id"),
        Index("ix_geo_incidents_country_observed", "country_code", "observed_at"),
        Index("ix_geo_incidents_type_observed", "incident_type", "observed_at"),
    )


class GeoIncidentImpact(Base):
    """Impact estimate associated with a geospatial incident."""

    __tablename__ = "geo_incident_impacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    geo_incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("geo_incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    impact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    affected_assets: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    affected_industries: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    impact_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "geo_incident_id",
            "impact_type",
            name="uq_geo_incident_impacts_incident_type",
        ),
    )


class EnergyMarketSnapshot(Base):
    """Dated energy market context snapshot for a region and commodity."""

    __tablename__ = "energy_market_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    commodity: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    inventory: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    production: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    consumption: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    imports: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    exports: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    snapshot_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "region",
            "commodity",
            "snapshot_date",
            name="uq_energy_market_snapshots_provider_region_commodity_date",
        ),
        Index(
            "ix_energy_market_snapshots_region_commodity_date",
            "region",
            "commodity",
            "snapshot_date",
        ),
    )


class EnergyDisruptionEvent(Base):
    """Energy disruption event retained for market and supply-chain context."""

    __tablename__ = "energy_disruption_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    commodity: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    severity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    raw_document_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_document_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retrieved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_energy_disruption_events_provider_id"),
        Index(
            "ix_energy_disruption_events_region_commodity_started",
            "region",
            "commodity",
            "started_at",
        ),
    )


class RiskScoreObservation(Base):
    """Time-series risk score used by dashboard, risk radar, and trend views."""

    __tablename__ = "risk_score_observations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    risk_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Numeric, nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    as_of: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    driver_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "target_type",
            "target_id",
            "risk_type",
            "as_of",
            "model_version",
            name="uq_risk_score_observations_target_type_risk_as_of_model",
        ),
        Index("ix_risk_score_observations_target_as_of", "target_type", "target_id", "as_of"),
        Index("ix_risk_score_observations_risk_as_of", "risk_type", "as_of"),
    )


class CompanyRiskRollup(Base):
    """Latest or periodic risk rollup for a canonical company."""

    __tablename__ = "company_risk_rollups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    impact_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True, index=True)
    opportunity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    related_event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    top_driver: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("company_id", "as_of", name="uq_company_risk_rollups_company_as_of"),
        Index("ix_company_risk_rollups_company_as_of", "company_id", "as_of"),
    )


class IndustryRiskRollup(Base):
    """Latest or periodic risk rollup for a frontend industry surface."""

    __tablename__ = "industry_risk_rollups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    industry_id: Mapped[str] = mapped_column(String(128), nullable=False)
    as_of: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    impact_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True, index=True)
    opportunity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    news_velocity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    related_event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("industry_id", "as_of", name="uq_industry_risk_rollups_industry_as_of"),
        Index("ix_industry_risk_rollups_industry_as_of", "industry_id", "as_of"),
    )


class DailyIntelligenceSummary(Base):
    """Generated daily summary shown by dashboard and brief/report surfaces."""

    __tablename__ = "daily_intelligence_summaries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    summary_date: Mapped[datetime.date] = mapped_column(
        Date, nullable=False, unique=True, index=True
    )
    overall_risk_level: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_points: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    model_rating_prediction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("crisis_predictions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    generated_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    """Application user profile for persisted workspace behavior."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False, server_default="analyst")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WatchlistItem(Base):
    """User watchlist entry for a company, event, country, industry, or risk type."""

    __tablename__ = "watchlist_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_metadata: Mapped[Any | None] = mapped_column("metadata", JSONB, nullable=True)
    alert_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "item_type",
            "item_id",
            name="uq_watchlist_items_user_type_item",
        ),
        Index("ix_watchlist_items_user_type", "user_id", "item_type"),
    )


class AlertRule(Base):
    """Configurable threshold or rule that can generate persisted alerts."""

    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "rule_type",
            "target_type",
            "target_id",
            name="uq_alert_rules_user_rule_target",
        ),
        Index("ix_alert_rules_user_enabled", "user_id", "enabled"),
        Index("ix_alert_rules_target", "target_type", "target_id"),
    )


class Alert(Base):
    """Persisted alert displayed by the alerts page and notification surfaces."""

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alert_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: The strongest severity this alert has ever held. `severity` tracks the score and decays
    #: with it -- an alert can only resolve at Low -- so a resolved row remembers nothing about
    #: how severe its condition was, and ADR 0010's "a resolved key cannot re-fire for 24h unless
    #: the new severity is higher" would be vacuous: a new alert enters at Medium or above, which
    #: is always higher than the Low a resolution leaves behind. The cooldown compares the new
    #: severity against this peak instead.
    peak_severity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: ADR 0010 lifecycle, and the only status column on this table. The API serves it as
    #: the wire field `status` (api-adapter-contract "Enums").
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="open", index=True
    )
    related_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    related_company_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    related_industry_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: `(risk_type, scope_entity, condition_class)`. At most one *active* alert per key --
    #: enforced by `uq_alerts_active_dedupe_key`; the same condition updates that row.
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    evidence_signal_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid), nullable=True)
    score_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True, server_default="v1"
    )
    #: Structured predicates `(signal_ref, comparator, threshold)` where machine-checkable,
    #: free text otherwise. Evaluated every run to drive downgrades.
    what_could_reduce_risk: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    #: Share of the score contributed by news-velocity terms, 0-1. Rendered as its own badge.
    news_driven: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    #: Hysteresis state. The 2-consecutive-runs velocity rule lives on the row, not in
    #: worker memory; `clear_band_since` drives downgraded -> resolved after 7 days;
    #: `cooldown_until` blocks a resolved key from re-firing for 24h at equal severity.
    velocity_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    clear_band_since: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cooldown_until: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Null-model gate: composite-score alerts stay experimental until they beat
    #: `services/crisis_model/baseline.py` on precision AND lead time.
    experimental: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    #: Notification surface. Resolution emits an explicit all-clear -- de-escalation is
    #: information, not silence -- so it is tracked separately from the first notification.
    notified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    all_clear_notified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)",
            name="ck_alerts_risk_score",
        ),
        CheckConstraint(f"state IN ({_sql_enum(ALERT_STATES)})", name="ck_alerts_state"),
        CheckConstraint(f"severity IN ({_sql_enum(RISK_LEVELS)})", name="ck_alerts_severity"),
        CheckConstraint(
            f"peak_severity IS NULL OR peak_severity IN ({_sql_enum(RISK_LEVELS)})",
            name="ck_alerts_peak_severity",
        ),
        CheckConstraint(
            "news_driven IS NULL OR (news_driven >= 0 AND news_driven <= 1)",
            name="ck_alerts_news_driven",
        ),
        CheckConstraint("velocity_streak >= 0", name="ck_alerts_velocity_streak_non_negative"),
        CheckConstraint(
            "state <> 'resolved' OR resolved_at IS NOT NULL",
            name="ck_alerts_resolved_has_timestamp",
        ),
        CheckConstraint(
            "state <> 'superseded' OR superseded_by IS NOT NULL",
            name="ck_alerts_superseded_has_target",
        ),
        CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> id", name="ck_alerts_no_self_supersede"
        ),
        # Flapping is structurally impossible: one live alert per dedupe key.
        Index(
            "uq_alerts_active_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=text(
                f"dedupe_key IS NOT NULL AND state IN ({_sql_enum(ACTIVE_ALERT_STATES)})"
            ),
        ),
        Index("ix_alerts_user_state_created", "user_id", "state", "created_at"),
        Index("ix_alerts_severity_created", "severity", "created_at"),
        Index("ix_alerts_state_updated", "state", "updated_at"),
    )


class AlertConditionState(Base):
    """Persisted state of an alert condition that has not opened an alert yet (ADR 0010).

    A velocity condition (`z > 2.5`) may not fire until it has held for two consecutive runs, so
    the first qualifying run must be remembered somewhere durable -- "not in worker memory" is
    the whole point of the rule. No `alerts` row can be that home: the three active states are
    exactly the states in which an alert is *live* (the alerts API serves them and the platform
    budget counts them), so a not-yet-fired condition parked in one is a premature alert, while
    the two terminal states are outright lies -- `resolved` claims an all-clear and arms the 24h
    cooldown, `superseded` demands a successor row it does not have.

    This row is the missing home, and only that: it is keyed by the same `dedupe_key`, it is
    invisible to every alerts query, and it is deleted the moment the condition opens an alert --
    from which point `alerts.velocity_streak` owns the streak, exactly as the ADR requires.
    """

    __tablename__ = "alert_condition_states"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: The `(risk_type, scope_entity, condition_class)` key this condition shares with the alert
    #: it will become. Unique: one pre-fire streak per condition, whatever the worker count.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    velocity_streak: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_evaluated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_alert_condition_states_dedupe_key"),
        CheckConstraint(
            "velocity_streak >= 0", name="ck_alert_condition_states_velocity_streak_non_negative"
        ),
    )


class Report(Base):
    """Generated or user-saved report shell."""

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    report_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    brief_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="generating", index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    generated_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("llm_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # NULL means legacy/unclassified and therefore cannot be served while Gate G is closed.
    # New generators stamp one of the versioned policies so output authorization is durable
    # and independent of section-title heuristics.
    content_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_enum(REPORT_STATUSES)})", name="ck_reports_status"),
        CheckConstraint(
            f"content_policy IS NULL OR content_policy IN ({_sql_enum(REPORT_CONTENT_POLICIES)})",
            name="ck_reports_content_policy",
        ),
        CheckConstraint("version >= 1", name="ck_reports_version_positive"),
        # A report is a daily brief (keyed by brief_date) or an event report (keyed by
        # event_id), never both. Neither is allowed: reports predating this split carry
        # no subject key, and ad-hoc types are keyed by neither.
        CheckConstraint(
            "brief_date IS NULL OR event_id IS NULL",
            name="ck_reports_brief_xor_event",
        ),
        # Uniqueness is per (type, subject, version): regeneration adds a version, it never
        # mutates a published report (report-generation spec, "Lifecycle and versioning").
        Index(
            "uq_reports_type_brief_date_version",
            "report_type",
            "brief_date",
            "version",
            unique=True,
            postgresql_where=text("brief_date IS NOT NULL"),
        ),
        Index(
            "uq_reports_type_event_version",
            "report_type",
            "event_id",
            "version",
            unique=True,
            postgresql_where=text("event_id IS NOT NULL"),
        ),
        Index("ix_reports_user_status_created", "user_id", "status", "created_at"),
        Index("ix_reports_report_type_status_created", "report_type", "status", "created_at"),
        Index("ix_reports_brief_date", "brief_date"),
    )


class ReportSection(Base):
    """Ordered body section for a generated report.

    Citations are claim-level, not section-level: the Composer emits claim-tagged blocks
    (`{text, claim_ids[]}`) and `evidence_refs` is the typed list of the claim IDs those
    blocks cite, which resolve through `claim_evidence` to articles (report-generation
    spec, "Citation mechanics"). The grounding gate records its verdict per section.
    """

    __tablename__ = "report_sections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    blocks: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid), nullable=True)
    grounding_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )

    __table_args__ = (
        UniqueConstraint("report_id", "section_order", name="uq_report_sections_report_order"),
        CheckConstraint(
            f"grounding_status IN ({_sql_enum(GROUNDING_STATUSES)})",
            name="ck_report_sections_grounding_status",
        ),
    )


class SavedSearch(Base):
    """Saved search query and filters for a user workspace."""

    __tablename__ = "saved_searches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_saved_searches_user_name"),
        Index("ix_saved_searches_user_created", "user_id", "created_at"),
    )
