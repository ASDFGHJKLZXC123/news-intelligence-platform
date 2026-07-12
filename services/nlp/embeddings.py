"""Embedding generation for articles that lack a vector (Stage P4).

Uses an injected ``EmbeddingProvider`` (fake in tests at the canonical dimension; a real
model in production). Persistence is integration-tested.
"""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from db.models import EMBEDDING_DIM, Article, ArticleEmbedding
from packages.providers.base import EmbeddingProvider


def embed_unembedded_articles(
    session: Session,
    provider: EmbeddingProvider,
    *,
    embedding_model: str | None = None,
    embedding_model_version: str | None = None,
) -> int:
    """Embed articles missing the provider's configured vector space.

    Embeddings are keyed by ``(article_id, model, model_version)``, so an article may
    carry one vector per model. Provider identity is used unless an explicit identity
    is supplied. Returned metadata must agree with that identity before it is written.
    """
    model = provider.model_name if embedding_model is None else embedding_model
    model_version = (
        provider.model_version if embedding_model_version is None else embedding_model_version
    )
    stmt = select(Article).outerjoin(
        ArticleEmbedding,
        and_(
            ArticleEmbedding.article_id == Article.id,
            ArticleEmbedding.model == model,
            ArticleEmbedding.model_version == model_version,
        ),
    )
    stmt = stmt.where(ArticleEmbedding.article_id.is_(None)).order_by(Article.fetched_at)
    articles = list(session.scalars(stmt).all())
    if not articles:
        return 0
    texts = [f"{a.title}\n{a.summary or ''}" for a in articles]
    results = provider.embed(texts)
    for article, result in zip(articles, results, strict=True):
        if (result.model_name, result.model_version) != (model, model_version):
            msg = (
                "embedding result identity "
                f"{result.model_name}@{result.model_version} != configured identity "
                f"{model}@{model_version}"
            )
            raise ValueError(msg)
        if result.dimension != EMBEDDING_DIM:
            msg = f"embedding dimension {result.dimension} != EMBEDDING_DIM {EMBEDDING_DIM}"
            raise ValueError(msg)
        session.add(
            ArticleEmbedding(
                article_id=article.id,
                model=model,
                model_version=model_version,
                dimension=result.dimension,
                embedding=list(result.vector),
            )
        )
    session.flush()
    return len(articles)
