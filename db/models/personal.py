"""Durable records for the single-owner personal news desk.

These tables are additive.  They point at the existing users, jobs, articles, events and
reports without changing the meaning or ownership of any legacy row.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

PERSONAL_REPORT_TYPE = "personal_daily_brief"
PERSONAL_JOB_TYPE = "personal_daily"
PERSONAL_TIMEZONE = "America/Los_Angeles"
PERSONAL_EXECUTION_PROFILES = ("assisted", "raw")
PERSONAL_RUN_STATES = ("queued", "running", "succeeded", "partially_failed", "failed")


class PersonalWorkspace(Base):
    __tablename__ = "personal_workspaces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    singleton: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, unique=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    timezone: Mapped[str] = mapped_column(String(128), nullable=False, default=PERSONAL_TIMEZONE)
    active_profile_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "personal_profile_revisions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_personal_workspaces_active_profile_revision",
        ),
        nullable=True,
    )
    setup_provenance: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("singleton", name="ck_personal_workspaces_singleton"),)


class PersonalProfileRevision(Base):
    __tablename__ = "personal_profile_revisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_source_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    include_phrases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    exclude_phrases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    execution_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="assisted")
    settings: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    schema_revision: Mapped[str] = mapped_column(
        String(64), nullable=False, default="personal-profile.v1"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "revision", name="uq_personal_profiles_revision"),
        CheckConstraint("revision >= 1", name="ck_personal_profiles_revision_positive"),
        CheckConstraint(
            "execution_profile IN ('assisted', 'raw')",
            name="ck_personal_profiles_execution_profile",
        ),
        CheckConstraint(
            "cardinality(include_phrases) <= 20 AND cardinality(exclude_phrases) <= 20",
            name="ck_personal_profiles_phrase_count",
        ),
    )


class PersonalRun(Base):
    __tablename__ = "personal_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    local_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("personal_profile_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="personal")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    ownership_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivery_transport: Mapped[str] = mapped_column(
        String(32), nullable=False, default="celery", server_default="celery"
    )
    fencing_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    queued_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    graceful_deadline_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hard_deadline_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capture_started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    capture_ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    admitted_article_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    enrichment_article_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    event_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False, default=list)
    scopes_frozen_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "personal_brief_snapshots.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_personal_runs_snapshot_id",
        ),
        nullable=True,
    )
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="SET NULL"), nullable=True
    )
    coverage: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    stage_results: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    terminal_manifest: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "local_date", name="uq_personal_runs_workspace_date"),
        CheckConstraint(
            "state IN ('queued', 'running', 'succeeded', 'partially_failed', 'failed')",
            name="ck_personal_runs_state",
        ),
        CheckConstraint("attempt >= 1 AND max_attempts = 3", name="ck_personal_runs_attempts"),
        CheckConstraint("execution_mode = 'personal'", name="ck_personal_runs_execution_mode"),
        Index("ix_personal_runs_workspace_date", "workspace_id", "local_date"),
    )


class PersonalCapture(Base):
    __tablename__ = "personal_captures"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rss_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    captured_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("articles.id", ondelete="SET NULL"), nullable=True
    )
    admitted_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="SET NULL"), nullable=True
    )
    admitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    receipt: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    original_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    admission_charged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    processing_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="RESTRICT"), nullable=True
    )
    enrichment_state: Mapped[str] = mapped_column(
        String(64), nullable=False, default="pending_admission", server_default="pending_admission"
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "source_id", "url_hash", name="uq_personal_capture_run_source_url"
        ),
        UniqueConstraint("workspace_id", "url_hash", name="uq_personal_capture_workspace_url"),
        Index("ix_personal_captures_pending", "workspace_id", "admitted_at", "captured_at"),
        UniqueConstraint("workspace_id", "article_id", name="uq_personal_capture_article"),
    )


class PersonalFeedReceipt(Base):
    """A durable bounded request receipt, including zero-item and failed captures."""

    __tablename__ = "personal_feed_receipts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)


class PersonalEnrichmentTransfer(Base):
    """Explicit transfer of a terminal run's intentionally deferred raw article."""

    __tablename__ = "personal_enrichment_transfers"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False
    )
    from_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="RESTRICT"), nullable=False
    )
    to_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    transferred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("to_run_id", "article_id", name="uq_personal_transfer_run_article"),
        CheckConstraint("from_run_id <> to_run_id", name="ck_personal_transfer_distinct_runs"),
    )


class PersonalRunEventObservation(Base):
    __tablename__ = "personal_run_event_observations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    qualifying_article_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    source_inputs: Mapped[Any] = mapped_column(JSONB, nullable=False)
    ranking_inputs: Mapped[Any] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "event_id", "revision", name="uq_personal_observation_revision"),
        CheckConstraint("revision >= 1", name="ck_personal_observation_revision_positive"),
    )


class PersonalBriefSnapshot(Base):
    __tablename__ = "personal_brief_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    profile_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_profile_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_event_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    selected_event_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, default=list
    )
    input_payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_route: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    input_contract: Mapped[str] = mapped_column(
        String(64), nullable=False, default="personal-brief-input.v1"
    )
    prepared_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PersonalReportLink(Base):
    __tablename__ = "personal_report_links"

    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_workspaces.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_brief_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    brief_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "brief_date",
            "version",
            name="uq_personal_reports_workspace_date_version",
        ),
        CheckConstraint("version >= 1", name="ck_personal_report_version_positive"),
        Index("ix_personal_reports_workspace_date", "workspace_id", "brief_date", "version"),
    )


class PersonalArticleRevision(Base):
    """Bounded retained source revision used by claim preparation and snapshots."""

    __tablename__ = "personal_article_revisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retained_title: Mapped[str] = mapped_column(Text, nullable=False)
    retained_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    retained_url: Mapped[str] = mapped_column(Text, nullable=False)
    retained_publisher: Mapped[str | None] = mapped_column(Text, nullable=True)
    retained_published_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provenance: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "article_id", name="uq_personal_article_revision_run_article"),
    )


class PersonalClaimPreparation(Base):
    __tablename__ = "personal_claim_preparations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_runs.id", ondelete="CASCADE"), nullable=False
    )
    article_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("personal_article_revisions.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("articles.id", ondelete="RESTRICT"), nullable=False
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("claims.id", ondelete="SET NULL"), nullable=True
    )
    evidence_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("evidence_items.id", ondelete="SET NULL"), nullable=True
    )
    normalized_claim_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_field: Mapped[str] = mapped_column(String(16), nullable=False)
    span_start: Mapped[int] = mapped_column(Integer, nullable=False)
    span_end: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation: Mapped[Any] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "article_revision_id",
            "normalized_claim_key",
            name="uq_personal_claim_preparation_identity",
        ),
        CheckConstraint(
            "span_start >= 0 AND span_end >= span_start", name="ck_personal_claim_span"
        ),
        CheckConstraint(
            "status IN ('supported', 'abstained', 'failed')",
            name="ck_personal_claim_preparation_status",
        ),
        CheckConstraint(
            "source_field IN ('title', 'summary')", name="ck_personal_claim_source_field"
        ),
        Index("ix_personal_claim_preparations_run_status", "run_id", "status"),
    )


# Compatibility import: the existing table is expanded, never replaced.
from db.models.processing import PersonalWriterMode as PersonalWriterMode  # noqa: E402
