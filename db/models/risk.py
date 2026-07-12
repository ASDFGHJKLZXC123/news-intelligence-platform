"""Canonical crisis-model ORM tables (single source of truth: canonical-schema.md).

These four tables are the contract between the pipeline (Tracks P/D) and the standalone
crisis model (Track M). ``event_risk_features`` is the output contract of Stages P4-P5.
``crisis_predictions`` and ``crisis_prediction_evaluations`` are written by Track M only
and are NOT wired into the pipeline/API/frontend until the Integration Gate.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class CountryDailyRiskSignal(Base):
    """Daily macro/market/news signal panel per country (Track D + P4-P5 aggregates)."""

    __tablename__ = "country_daily_risk_signals"

    country: Mapped[str] = mapped_column(Text, primary_key=True)
    date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)

    # Macro / credit (Track D1)
    credit_to_gdp_gap: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    debt_service_ratio: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    house_price_gap: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    current_account_balance_gdp: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fiscal_deficit_gdp: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fx_reserve_change_3m: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    # Market / financial stress (Track D2)
    financial_stress_index: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    credit_spread_zscore: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    bank_equity_drawdown_30d: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    volatility_zscore: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    liquidity_stress_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    # News / events (Stages P4-P5 aggregates)
    policy_uncertainty_news_zscore: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    banking_stress_news_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    deposit_outflow_news_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    civil_unrest_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    war_escalation_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    sanctions_risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    natural_disaster_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    supply_chain_disruption_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    news_velocity_zscore: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source_diversity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    official_confirmation_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventRiskFeature(Base):
    """Structured per-event risk features. OUTPUT CONTRACT of Stages P4-P5."""

    __tablename__ = "event_risk_features"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_subtype: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mechanism: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    novelty_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    velocity_zscore: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source_diversity_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    source_authority_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    official_confirmation: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rumor_risk_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    market_relevance_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    macro_relevance_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    affected_industries: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    affected_companies: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    observed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_article_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid), nullable=True)
    confidence_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CrisisPrediction(Base):
    """A probabilistic crisis forecast. Written by Track M only (held behind the gate)."""

    __tablename__ = "crisis_predictions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    risk_type: Mapped[str] = mapped_column(String(48), nullable=False)
    as_of_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)

    probability_0_6m: Mapped[float] = mapped_column(Numeric, nullable=False)
    probability_6_12m: Mapped[float] = mapped_column(Numeric, nullable=False)
    probability_12_18m: Mapped[float] = mapped_column(Numeric, nullable=False)
    probability_within_18m: Mapped[float] = mapped_column(Numeric, nullable=False)

    risk_score: Mapped[float] = mapped_column(Numeric, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Numeric, nullable=False)

    model_versions: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    top_drivers: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    historical_analogies: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    what_could_escalate: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    what_could_reduce_risk: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CrisisPredictionEvaluation(Base):
    """Post-horizon scoring of a prediction. Written by Track M only."""

    __tablename__ = "crisis_prediction_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    prediction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("crisis_predictions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evaluation_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    actual_outcome: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    actual_start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    brier_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    log_loss: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    false_positive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    false_negative: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
