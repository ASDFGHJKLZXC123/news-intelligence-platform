"""Embedding generation for articles that lack a vector (Stage P4).

Uses an injected ``EmbeddingProvider`` (fake in tests at the canonical dimension; a real
model in production). Persistence is integration-tested.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import EMBEDDING_DIM, Article, ArticleEmbedding
from packages.providers.base import EmbeddingProvider


def embed_unembedded_articles(session: Session, provider: EmbeddingProvider) -> int:
    """Embed every article without an embedding row. Returns the count embedded."""
    stmt = (
        select(Article)
        .outerjoin(ArticleEmbedding, ArticleEmbedding.article_id == Article.id)
        .where(ArticleEmbedding.article_id.is_(None))
        .order_by(Article.fetched_at)
    )
    articles = list(session.scalars(stmt).all())
    if not articles:
        return 0
    texts = [f"{a.title}\n{a.summary or ''}" for a in articles]
    results = provider.embed(texts)
    for article, result in zip(articles, results, strict=True):
        if result.dimension != EMBEDDING_DIM:
            msg = f"embedding dimension {result.dimension} != EMBEDDING_DIM {EMBEDDING_DIM}"
            raise ValueError(msg)
        session.add(
            ArticleEmbedding(
                article_id=article.id,
                model=result.model_name,
                model_version=result.model_version,
                dimension=result.dimension,
                embedding=list(result.vector),
            )
        )
    session.flush()
    return len(articles)
