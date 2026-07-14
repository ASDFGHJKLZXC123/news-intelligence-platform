"""Embedding persistence services and Celery lifecycle tasks (ADR 0004).

No database and no network: the session is a mock/fake and the provider is a recording fake at
the canonical dimension. What is proved here is the contract around the vector -- which model
space a call pins, what is refused before a row is built, when an episode is re-embedded, and
that the worker commits exactly once on success and rolls back on failure.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql

from db.models import (
    EMBEDDING_DIM,
    Article,
    ArticleEmbedding,
    Event,
    EventEmbedding,
    HistoricalEpisode,
)
from packages.providers.base import EmbeddingResult
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE
from services.nlp.embeddings import (
    embed_unembedded_articles,
    embed_unembedded_events,
)
from services.nlp.episodes import (
    EpisodeEmbedding,
    EpisodeRecord,
    embed_episode_onset,
    refresh_episode_embeddings,
    upsert_historical_episode,
)
from workers import embedding_tasks
from workers.celery_app import QUEUE_PIPELINE, Stage1Task, celery_app

MODEL = "text-embedding-3-small"
VERSION = "current"
ONSET_DATE = datetime.date(2023, 3, 8)


class RecordingProvider:
    """A provider at the canonical dimension that records every batch it is handed."""

    def __init__(
        self,
        *,
        model_name: str = MODEL,
        model_version: str = VERSION,
        dimension: int = EMBEDDING_DIM,
        drop_results: int = 0,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version
        self.dimension = dimension
        self.calls: list[list[str]] = []
        self._drop_results = drop_results

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        self.calls.append(list(texts))
        results = [
            EmbeddingResult(
                vector=(0.25,) * self.dimension,
                provider_name="fake",
                model_name=self.model_name,
                model_version=self.model_version,
                dimension=self.dimension,
                model_run_id="run",
            )
            for _ in texts
        ]
        return results[: len(results) - self._drop_results]


def _articles(count: int) -> list[Article]:
    return [
        Article(id=uuid.uuid4(), title=f"Title {index}", summary="Lede.", body="Body.")
        for index in range(count)
    ]


def _events(count: int) -> list[Event]:
    return [
        Event(id=uuid.uuid4(), title=f"Event {index}", summary="Event summary.")
        for index in range(count)
    ]


def _session(rows: list[Any]) -> Mock:
    session = Mock()
    session.scalars.return_value.all.return_value = rows
    return session


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _episode(**overrides: Any) -> HistoricalEpisode:
    episode = HistoricalEpisode(
        id=uuid.uuid4(),
        name="2023 regional banking stress",
        episode_type="banking_stress",
        onset_date=ONSET_DATE,
        onset_summary="Concentrated uninsured deposits face rapid withdrawals.",
        onset_indicators={"uninsured_deposit_pct": 94},
        onset_embedding=[0.1] * EMBEDDING_DIM,
        model=MODEL,
        model_version=VERSION,
        version=1,
    )
    for name, value in overrides.items():
        setattr(episode, name, value)
    return episode


def _record(**overrides: Any) -> EpisodeRecord:
    fields: dict[str, Any] = {
        "name": "2023 regional banking stress",
        "episode_type": "banking_stress",
        "onset_date": ONSET_DATE,
        "onset_summary": "Concentrated uninsured deposits face rapid withdrawals.",
        "onset_indicators": {"uninsured_deposit_pct": 94},
        "version": 1,
    }
    fields.update(overrides)
    return EpisodeRecord(**fields)


# --- articles -------------------------------------------------------------------------
def test_articles_are_embedded_from_the_canonical_title_lede_body_text() -> None:
    article = Article(id=uuid.uuid4(), title="Bank halts", summary="Lede.", body="Body text.")
    provider = RecordingProvider()
    session = _session([article])

    assert embed_unembedded_articles(session, provider) == 1

    assert provider.texts == ["Bank halts\n\nLede.\n\nBody text."]
    embedding = session.add.call_args.args[0]
    assert isinstance(embedding, ArticleEmbedding)
    assert (embedding.model, embedding.model_version) == (MODEL, VERSION)
    assert embedding.dimension == EMBEDDING_DIM
    assert len(embedding.embedding) == EMBEDDING_DIM


def test_article_batches_are_capped_at_96_per_request() -> None:
    provider = RecordingProvider()
    session = _session(_articles(150))

    assert embed_unembedded_articles(session, provider) == 150

    assert [len(call) for call in provider.calls] == [96, 54]
    assert max(len(call) for call in provider.calls) <= MAX_EMBEDDING_BATCH_SIZE
    assert session.add.call_count == 150


def test_only_vectors_missing_from_the_selected_model_space_are_generated() -> None:
    session = _session([])

    assert embed_unembedded_articles(session, RecordingProvider()) == 0

    sql = _sql(session.scalars.call_args.args[0])
    assert f"article_embeddings.model = '{MODEL}'" in sql
    assert f"article_embeddings.model_version = '{VERSION}'" in sql
    assert "article_embeddings.article_id IS NULL" in sql


def test_a_result_from_another_model_space_is_refused_before_any_row_is_built() -> None:
    provider = RecordingProvider(model_name="text-embedding-3-large")
    session = _session(_articles(1))

    with pytest.raises(ValueError, match="configured identity"):
        embed_unembedded_articles(session, provider, embedding_model=MODEL)

    session.add.assert_not_called()


def test_a_wrong_dimension_result_is_refused_before_any_row_is_built() -> None:
    provider = RecordingProvider(dimension=8)
    session = _session(_articles(1))

    with pytest.raises(ValueError, match="EMBEDDING_DIM"):
        embed_unembedded_articles(session, provider)

    session.add.assert_not_called()


def test_a_short_provider_response_is_never_zipped_against_the_articles() -> None:
    provider = RecordingProvider(drop_results=1)
    session = _session(_articles(3))

    with pytest.raises(ValueError, match="2 vectors for 3 texts"):
        embed_unembedded_articles(session, provider)

    session.add.assert_not_called()


# --- events ---------------------------------------------------------------------------
def test_events_are_embedded_from_the_canonical_title_and_summary() -> None:
    provider = RecordingProvider()
    session = _session(_events(1))

    assert embed_unembedded_events(session, provider) == 1

    assert provider.texts == ["Event 0\n\nEvent summary."]
    embedding = session.add.call_args.args[0]
    assert isinstance(embedding, EventEmbedding)
    assert (embedding.model, embedding.model_version) == (MODEL, VERSION)
    assert len(embedding.embedding) == EMBEDDING_DIM


def test_event_embeddings_pin_one_model_space_and_batch_at_96() -> None:
    provider = RecordingProvider()
    session = _session(_events(100))

    assert embed_unembedded_events(session, provider) == 100

    assert [len(call) for call in provider.calls] == [96, 4]
    sql = _sql(session.scalars.call_args.args[0])
    assert f"event_embeddings.model = '{MODEL}'" in sql
    assert f"event_embeddings.model_version = '{VERSION}'" in sql


# --- historical episodes --------------------------------------------------------------
def test_embed_episode_onset_returns_a_validated_vector_for_insertion() -> None:
    provider = RecordingProvider()

    embedding = embed_episode_onset(provider, "Onset.", {"a": 1})

    assert isinstance(embedding, EpisodeEmbedding)
    assert len(embedding.vector) == EMBEDDING_DIM
    assert (embedding.model, embedding.model_version) == (MODEL, VERSION)
    assert provider.texts == ['Onset.\n\n{"a":1}']


def test_a_new_episode_is_embedded_before_insertion_never_with_a_placeholder() -> None:
    provider = RecordingProvider()
    session = Mock()
    session.scalars.return_value.first.return_value = None

    episode = upsert_historical_episode(session, _record(), provider=provider)

    session.add.assert_called_once()
    assert len(episode.onset_embedding) == EMBEDDING_DIM
    assert set(episode.onset_embedding) != {0.0}  # a zero vector would match anything
    assert (episode.model, episode.model_version) == (MODEL, VERSION)
    assert len(provider.calls) == 1


def test_a_new_episode_without_a_provider_or_vector_is_refused() -> None:
    session = Mock()
    session.scalars.return_value.first.return_value = None

    with pytest.raises(ValueError, match="NOT NULL"):
        upsert_historical_episode(session, _record(), embedding_model=MODEL)

    session.add.assert_not_called()


def test_a_curated_record_can_supply_its_own_vector() -> None:
    session = Mock()
    session.scalars.return_value.first.return_value = None
    supplied = EpisodeEmbedding(vector=(0.5,) * EMBEDDING_DIM, model=MODEL, model_version=VERSION)

    episode = upsert_historical_episode(session, _record(), embedding=supplied)

    assert episode.onset_embedding == [0.5] * EMBEDDING_DIM


@pytest.mark.parametrize(
    "supplied",
    [
        EpisodeEmbedding(vector=(0.5,) * EMBEDDING_DIM, model="other", model_version=VERSION),
        EpisodeEmbedding(vector=(0.5,) * 8, model=MODEL, model_version=VERSION),
    ],
)
def test_a_supplied_vector_is_validated_against_the_configured_space(
    supplied: EpisodeEmbedding,
) -> None:
    session = Mock()
    session.scalars.return_value.first.return_value = None

    with pytest.raises(ValueError):
        upsert_historical_episode(session, _record(), embedding=supplied, embedding_model=MODEL)

    session.add.assert_not_called()


def test_an_unchanged_episode_is_not_re_embedded() -> None:
    existing = _episode()
    provider = RecordingProvider()
    session = Mock()
    session.scalars.return_value.first.return_value = existing

    episode = upsert_historical_episode(session, _record(), provider=provider)

    assert provider.calls == []  # the stored vector still describes this onset
    assert episode.onset_embedding == [0.1] * EMBEDDING_DIM
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"onset_summary": "A different onset, rewritten by the curator."},
        {"onset_indicators": {"uninsured_deposit_pct": 55}},
        {"version": 2},
    ],
)
def test_a_changed_onset_or_version_re_embeds(change: dict[str, Any]) -> None:
    existing = _episode()
    provider = RecordingProvider()
    session = Mock()
    session.scalars.return_value.first.return_value = existing

    episode = upsert_historical_episode(session, _record(**change), provider=provider)

    assert len(provider.calls) == 1
    assert episode.onset_embedding == [0.25] * EMBEDDING_DIM  # the freshly embedded vector


def test_an_episode_stored_in_another_model_space_is_re_embedded() -> None:
    existing = _episode(model="text-embedding-3-large")
    provider = RecordingProvider()
    session = Mock()
    session.scalars.return_value.first.return_value = existing

    episode = upsert_historical_episode(session, _record(), provider=provider)

    assert len(provider.calls) == 1
    assert (episode.model, episode.model_version) == (MODEL, VERSION)


def test_no_outcome_text_ever_reaches_the_embedded_episode_text() -> None:
    """The look-ahead-bias rule: retrieval must never see hindsight."""
    provider = RecordingProvider()
    session = Mock()
    session.scalars.return_value.first.return_value = None
    record = _record(
        name="Silicon Valley Bank failure",  # the name is hindsight too
        outcome_summary="The bank failed and was placed into receivership.",
        outcomes=["failure", "bailout"],
        resolution_mechanism="FDIC systemic risk exception",
    )

    episode = upsert_historical_episode(session, record, provider=provider)

    embedded = provider.texts[0]
    for hindsight in ("failed", "receivership", "failure", "bailout", "FDIC", "Silicon Valley"):
        assert hindsight not in embedded
    assert embedded.startswith("Concentrated uninsured deposits face rapid withdrawals.")
    # The outcomes are still persisted on the row -- they are joined in after matching.
    assert episode.outcomes == ["failure", "bailout"]
    assert episode.outcome_summary == record.outcome_summary


def test_refresh_re_embeds_only_episodes_outside_the_configured_space() -> None:
    stale = _episode(model="text-embedding-3-large")
    provider = RecordingProvider()
    session = _session([stale])

    assert refresh_episode_embeddings(session, provider) == 1

    assert stale.onset_embedding == [0.25] * EMBEDDING_DIM
    assert (stale.model, stale.model_version) == (MODEL, VERSION)
    assert provider.texts == [
        'Concentrated uninsured deposits face rapid withdrawals.\n\n{"uninsured_deposit_pct":94}'
    ]
    sql = _sql(session.scalars.call_args.args[0])
    assert f"historical_episodes.model != '{MODEL}'" in sql
    assert f"historical_episodes.model_version != '{VERSION}'" in sql


def test_refresh_is_a_noop_when_every_episode_is_current() -> None:
    provider = RecordingProvider()

    assert refresh_episode_embeddings(_session([]), provider) == 0
    assert provider.calls == []


# --- Celery lifecycle -----------------------------------------------------------------
class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class ClosableProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: Any, work_name: str, work: Any) -> tuple[FakeSession, ClosableProvider]:
    session = FakeSession()
    provider = ClosableProvider()
    monkeypatch.setattr(embedding_tasks, "SessionLocal", lambda: session)
    monkeypatch.setattr(embedding_tasks, "build_provider", lambda: provider)
    monkeypatch.setattr(embedding_tasks, work_name, work)
    return session, provider


TASKS = (
    (embedding_tasks.TASK_ARTICLE_EMBEDDING, "embed_unembedded_articles", "articles_embedded"),
    (embedding_tasks.TASK_EVENT_EMBEDDING, "embed_unembedded_events", "events_embedded"),
    (
        embedding_tasks.TASK_EPISODE_EMBEDDING_REFRESH,
        "refresh_episode_embeddings",
        "episodes_embedded",
    ),
)


def test_embedding_tasks_are_registered_on_the_pipeline_queue_with_retry_defaults() -> None:
    for task_name, _work, _key in TASKS:
        task = celery_app.tasks[task_name]
        assert isinstance(task, Stage1Task)
        assert task.queue == QUEUE_PIPELINE
        assert task.retry_backoff is True


def test_embedding_tasks_are_not_beat_scheduled() -> None:
    # Neither ADR 0004 nor the episode spec schedules an embedding run.
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    for task_name, _work, _key in TASKS:
        assert task_name not in scheduled


@pytest.mark.parametrize(("task_name", "work_name", "count_key"), TASKS)
def test_a_successful_run_commits_once_and_closes_everything(
    monkeypatch: Any, task_name: str, work_name: str, count_key: str
) -> None:
    session, provider = _install(monkeypatch, work_name, lambda *_args, **_kwargs: 7)
    task = celery_app.tasks[task_name]

    result = task.run()

    assert result["status"] == "ok"
    assert result[count_key] == 7
    assert (session.commits, session.rollbacks) == (1, 0)
    assert session.closed is True
    assert provider.closed is True


@pytest.mark.parametrize(("task_name", "work_name", "count_key"), TASKS)
def test_a_failed_run_rolls_back_commits_nothing_and_reraises(
    monkeypatch: Any, task_name: str, work_name: str, count_key: str
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("provider is down")

    session, provider = _install(monkeypatch, work_name, boom)
    task = celery_app.tasks[task_name]

    with pytest.raises(RuntimeError, match="provider is down"):
        task.run()

    assert (session.commits, session.rollbacks) == (0, 1)
    assert session.closed is True
    assert provider.closed is True


@pytest.mark.parametrize(("task_name", "work_name", "count_key"), TASKS)
def test_tasks_default_to_the_96_text_batch_cap(
    monkeypatch: Any, task_name: str, work_name: str, count_key: str
) -> None:
    seen: dict[str, Any] = {}

    def record(_session: Any, _provider: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    _install(monkeypatch, work_name, record)

    celery_app.tasks[task_name].run()

    assert seen["batch_size"] == MAX_EMBEDDING_BATCH_SIZE == 96
