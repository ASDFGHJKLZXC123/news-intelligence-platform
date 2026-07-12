"""core schema: pipeline + canonical crisis-model tables

Creates the Stage 2 tables (sources, articles, article_embeddings, events,
event_articles, llm_runs, jobs) plus the four canonical crisis-model tables
(country_daily_risk_signals, event_risk_features, crisis_predictions,
crisis_prediction_evaluations). Built from the ORM metadata so the migration cannot
drift from the models; frozen to ``STAGE2_TABLES`` so later stages add their own
migrations. The pgvector extension is created by migration 0001.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

from db.models import STAGE2_TABLES

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = [model.__table__ for model in STAGE2_TABLES]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
