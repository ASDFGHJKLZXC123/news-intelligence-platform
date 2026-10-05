"""Seeding the curated corpus: planning, batching, idempotency, and the transaction boundary.

No database and no network. The session is a fake that behaves like the identity map the seeder
actually relies on (get by primary key, add, flush), and the provider is a recording fake at the
canonical dimension, so what is proved here is the *behaviour around* the write: that a fresh
100-row corpus costs two embeddings requests rather than a hundred, that re-running it costs zero,
that a row is re-embedded exactly when its onset moved, that no row is ever stored with a
placeholder vector, and that a parent is always inserted before its children.
"""

from __future__ import annotations

import dataclasses
import socket
import uuid
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from db import base as db_base
from db.models import EMBEDDING_DIM, HistoricalEpisode, HistoricalEpisodeEmbedding
from db.seed import episode_seed, seed
from db.seed.episode_seed import (
    INSERT,
    UNCHANGED,
    UPDATE,
    EpisodeReviewGateError,
    plan_episode_seed,
    seed_episode_corpus,
)
from packages.providers.base import EmbeddingResult
from packages.providers.openai_embeddings import MAX_EMBEDDING_BATCH_SIZE, OpenAIEmbeddingProvider
from services.analogies.corpus import (
    HUMAN_REVIEWED,
    REVIEW_CHECKLIST_KEYS,
    load_corpus,
    unreviewed_episodes,
)
from services.nlp.episodes import (
    CURATED_FIELDS,
    episode_fields_differ,
    episode_onset_is_stale,
)

MODEL = "text-embedding-3-small"
VERSION = "current"


class RecordingProvider:
    """A provider at the canonical dimension that records the size of every request it is sent."""

    dimension = EMBEDDING_DIM

    def __init__(self, model_version: str = VERSION) -> None:
        self.model_name = MODEL
        self.model_version = model_version
        self.calls: list[list[str]] = []

    @property
    def request_sizes(self) -> list[int]:
        return [len(call) for call in self.calls]

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        self.calls.append(list(texts))
        return [
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

    def close(self) -> None:
        pass


class FakeSession:
    """An identity map with the three methods the seeder uses. Records insertion order."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, HistoricalEpisode] = {}
        self.embeddings: dict[tuple[uuid.UUID, str, str, int], HistoricalEpisodeEmbedding] = {}
        self.added: list[Any] = []
        self.flushes = 0

    def get(self, model: Any, primary_key: Any) -> Any:
        if model is HistoricalEpisode:
            return self.rows.get(primary_key)
        if model is HistoricalEpisodeEmbedding:
            return self.embeddings.get(primary_key)
        raise AssertionError(f"unexpected model lookup: {model}")

    def add(self, row: Any) -> None:
        if isinstance(row, HistoricalEpisode):
            self.rows[row.id] = row
        elif isinstance(row, HistoricalEpisodeEmbedding):
            key = (
                row.historical_episode_id,
                row.model,
                row.model_version,
                row.episode_version,
            )
            self.embeddings[key] = row
        else:
            raise AssertionError(f"unexpected row type: {type(row)}")
        self.added.append(row)

    def flush(self) -> None:
        self.flushes += 1


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def _seed(corpus, session: FakeSession | None = None) -> tuple[FakeSession, RecordingProvider, Any]:
    """Seed the committed corpus, which is honestly still a set of unreviewed drafts.

    ``allow_unreviewed=True`` is not a convenience here, it is the truth: every row in
    ``db/seed/episodes`` is ``llm_drafted_pending_human_review``, and the seeder refuses to publish
    those as verified corpus. These tests are about the mechanics *around* the write -- batching,
    idempotency, insertion order -- so they take the draft path explicitly. The gate itself is
    tested separately, below.
    """
    session = session or FakeSession()
    provider = RecordingProvider()
    summary = seed_episode_corpus(session, corpus, provider, allow_unreviewed=True)
    return session, provider, summary


# --- planning ------------------------------------------------------------------------------
def test_a_fresh_database_plans_every_row_as_an_insert_needing_a_vector(corpus):
    plan = plan_episode_seed(FakeSession(), corpus)

    assert len(plan.items) == corpus.quotas.total == 100
    assert all(item.action == INSERT and item.needs_embedding for item in plan.items)
    assert plan.counts() == {"inserted": 100, "updated": 0, "unchanged": 0, "re_embedded": 100}


def test_a_fresh_100_row_corpus_costs_two_requests_of_96_and_4(corpus):
    """ADR 0004 caps a request at 96 texts. One request per episode would be the bug."""
    plan = plan_episode_seed(FakeSession(), corpus)
    assert plan.batch_size == MAX_EMBEDDING_BATCH_SIZE == 96
    assert plan.request_sizes == (96, 4)

    _, provider, summary = _seed(corpus)
    assert provider.request_sizes == [96, 4]
    assert summary.embedding_requests == (96, 4)
    assert summary.re_embedded == 100


def test_planning_embeds_only_onset_text(corpus):
    _, provider, _ = _seed(corpus)

    by_slug = corpus.by_slug
    svb = by_slug["svb-deposit-run-2023"]
    embedded = "\n".join(provider.texts)
    assert svb.onset_summary in embedded
    for episode in corpus.episodes:
        assert episode.outcome_summary not in embedded
        assert episode.resolution_mechanism not in embedded
        assert episode.name not in embedded


# --- writing -------------------------------------------------------------------------------
def test_seeding_writes_every_row_with_a_real_vector(corpus):
    session, _, summary = _seed(corpus)

    assert summary.inserted == 100
    assert len(session.rows) == 100
    assert len(session.embeddings) == 100
    for row in session.rows.values():
        assert row.model == MODEL and row.model_version == VERSION
        assert len(row.onset_embedding) == EMBEDDING_DIM
        # onset_embedding is NOT NULL, and a zero/placeholder vector would still be *retrievable*.
        assert any(row.onset_embedding)
        stored = session.embeddings[(row.id, MODEL, VERSION, row.version)]
        assert len(stored.onset_embedding) == EMBEDDING_DIM
        assert stored.input_sha256 is not None
        assert stored.snapshot_manifest_sha256 is None


def test_rows_are_written_with_their_curated_ids_and_curated_fields(corpus):
    session, _, _ = _seed(corpus)

    for episode in corpus.episodes:
        row = session.rows[episode.episode_id]
        record = episode.to_record()
        assert not episode_fields_differ(row, record)
        assert row.id == episode.episode_id
        assert set(CURATED_FIELDS) <= set(dir(row))


def test_parents_are_inserted_before_their_children(corpus):
    session, _, _ = _seed(corpus)

    parents = [row for row in session.added if isinstance(row, HistoricalEpisode)]
    position = {row.id: index for index, row in enumerate(parents)}
    children = [e for e in corpus.episodes if e.parent_episode_id is not None]
    assert children
    for child in children:
        assert position[child.parent_episode_id] < position[child.episode_id]
        assert session.rows[child.episode_id].parent_episode_id == child.parent_episode_id


def test_reseeding_an_unchanged_corpus_is_a_no_op(corpus):
    """Idempotency: same fixed ids, same onset text, so nothing is written and nothing is embedded."""
    session, _, _ = _seed(corpus)

    provider = RecordingProvider()
    summary = seed_episode_corpus(session, corpus, provider, allow_unreviewed=True)

    assert provider.calls == []
    assert summary.embedding_requests == ()
    assert (summary.inserted, summary.updated, summary.unchanged, summary.re_embedded) == (
        0,
        0,
        100,
        0,
    )
    assert len(session.rows) == 100


def test_only_the_rows_whose_onset_moved_are_re_embedded(corpus):
    session, _, _ = _seed(corpus)

    # One row's curated onset is rewritten and its version bumped; one row's outcome text is
    # corrected, which changes nothing about its vector.
    changed = corpus.by_slug["us-repo-market-stress-2019"]
    outcome_only = corpus.by_slug["black-monday-1987"]
    episodes = []
    for episode in corpus.episodes:
        if episode.slug == changed.slug:
            episode = dataclasses.replace(
                episode,
                onset_summary=episode.onset_summary + " Rates print above the target range.",
                version=episode.version + 1,
            )
        elif episode.slug == outcome_only.slug:
            episode = dataclasses.replace(
                episode, outcome_summary=episode.outcome_summary + " Corrected."
            )
        episodes.append(episode)
    edited = dataclasses.replace(corpus, episodes=tuple(episodes))

    provider = RecordingProvider()
    summary = seed_episode_corpus(session, edited, provider, allow_unreviewed=True)

    assert summary.updated == 2
    assert summary.unchanged == 98
    assert summary.re_embedded == 1
    assert provider.request_sizes == [1]
    assert "Rates print above the target range." in provider.texts[0]

    row = session.rows[changed.episode_id]
    assert row.version == changed.version + 1
    assert (changed.episode_id, MODEL, VERSION, changed.version) in session.embeddings
    assert (changed.episode_id, MODEL, VERSION, changed.version + 1) in session.embeddings
    assert session.rows[outcome_only.episode_id].outcome_summary.endswith("Corrected.")


def test_a_model_space_change_re_embeds_everything(corpus):
    session, _, _ = _seed(corpus)

    # The migration path ADR 0004's stored model/model_version exists for: point the seeder at a
    # new space and every episode is carried across, in the same 96-text chunks.
    provider = RecordingProvider(model_version="v2")
    summary = seed_episode_corpus(session, corpus, provider, allow_unreviewed=True)

    assert summary.re_embedded == 100
    assert summary.unchanged == 100  # the curated fields did not move; only the vector space did
    assert provider.request_sizes == [96, 4]
    # The compatibility columns retain their original vector identity; v2 is appended.
    assert all(row.model_version == VERSION for row in session.rows.values())
    assert all(
        (row.id, MODEL, "v2", row.version) in session.embeddings for row in session.rows.values()
    )
    assert len(session.embeddings) == 200


def test_seeding_without_a_provider_refuses_to_write_a_placeholder_vector(corpus):
    with pytest.raises(ValueError, match="never be a placeholder"):
        seed_episode_corpus(FakeSession(), corpus, provider=None, allow_unreviewed=True)


def test_seeding_without_a_provider_is_allowed_when_nothing_needs_embedding(corpus):
    session, _, _ = _seed(corpus)

    summary = seed_episode_corpus(session, corpus, provider=None, allow_unreviewed=True)

    assert summary.unchanged == 100
    assert summary.embedding_requests == ()


def test_the_planner_agrees_with_the_writer_about_staleness(corpus):
    """The planner batches on `episode_onset_is_stale`; the writer re-checks it row by row."""
    session, _, _ = _seed(corpus)
    episode = corpus.by_slug["svb-deposit-run-2023"]
    row = session.rows[episode.episode_id]

    stored = session.embeddings[(row.id, MODEL, VERSION, row.version)]
    assert not episode_onset_is_stale(
        row,
        episode.to_record(),
        model=MODEL,
        model_version=VERSION,
        embedding=stored,
    )
    moved = dataclasses.replace(episode, version=episode.version + 1)
    assert episode_onset_is_stale(row, moved.to_record(), model=MODEL, model_version=VERSION)
    assert episode_onset_is_stale(row, episode.to_record(), model=MODEL, model_version="v2")


def test_an_onset_edit_without_a_curated_version_bump_is_rejected(corpus):
    session, _, _ = _seed(corpus)
    changed = corpus.by_slug["svb-deposit-run-2023"]
    edited = dataclasses.replace(
        corpus,
        episodes=tuple(
            dataclasses.replace(
                episode,
                onset_summary=episode.onset_summary + " Silent same-version edit.",
            )
            if episode.episode_id == changed.episode_id
            else episode
            for episode in corpus.episodes
        ),
    )

    with pytest.raises(ValueError, match="without an episode version bump"):
        plan_episode_seed(session, edited)


# --- the CLI -------------------------------------------------------------------------------
def _seal_every_exit(monkeypatch) -> list[str]:
    """Wire every way out of this process to explode. Returns the doors that were opened anyway.

    The database (both the engine's ``connect`` and the session factory), the settings lookup that
    holds ``OPENAI_API_KEY``, the provider's constructor, and underneath all of them the socket
    itself. A CLI path that runs to completion with these sealed has *proved* it did its work
    offline, rather than a mock having quietly answered for a real dependency.
    """
    opened: list[str] = []

    def door(name: str):
        def explode(*_args: Any, **_kwargs: Any):
            opened.append(name)
            raise AssertionError(f"{name} must not be reached")

        return explode

    monkeypatch.setattr(Engine, "connect", door("engine.connect"))
    monkeypatch.setattr(db_base, "SessionLocal", door("SessionLocal"))
    monkeypatch.setattr(seed, "SessionLocal", door("SessionLocal"))
    monkeypatch.setattr(seed, "get_settings", door("get_settings"))
    monkeypatch.setattr(seed, "build_provider", door("build_provider"))
    monkeypatch.setattr(seed, "build_embedding_provider", door("build_embedding_provider"))
    monkeypatch.setattr(OpenAIEmbeddingProvider, "__init__", door("OpenAIEmbeddingProvider"))
    monkeypatch.setattr(socket.socket, "connect", door("socket.connect"))
    return opened


def test_validate_only_needs_no_database_and_no_api_key(capsys, monkeypatch):
    opened = _seal_every_exit(monkeypatch)

    assert seed.main(["--validate-only"]) == 0

    assert opened == []
    report = capsys.readouterr().out
    assert '"total": 100' in report
    assert '"counterexamples": 38' in report
    assert '"pairs": 40' in report


def test_seeding_without_an_api_key_fails_with_an_actionable_message(monkeypatch):
    settings = Mock(openai_api_key="")
    monkeypatch.setattr(seed, "get_settings", lambda: settings)

    with pytest.raises(SystemExit, match="OPENAI_API_KEY is not set"):
        seed.build_provider()


def test_the_cli_commits_once_on_success(monkeypatch, corpus):
    session = Mock()
    provider = Mock()
    verifications: list[Any] = []
    monkeypatch.setattr(seed, "SessionLocal", lambda: session)
    monkeypatch.setattr(seed, "build_provider", lambda: provider)
    monkeypatch.setattr(
        seed,
        "_verify_live_snapshot",
        lambda value: verifications.append(value),
    )
    monkeypatch.setattr(
        seed,
        "seed_episode_corpus",
        lambda *_a, **_k: episode_seed.EpisodeSeedSummary(
            inserted=100,
            updated=0,
            unchanged=0,
            re_embedded=100,
            embedding_requests=(96, 4),
            quotas={},
        ),
    )

    summary = seed.seed_episodes(allow_unreviewed=True)

    assert summary.inserted == 100
    assert verifications == [provider, provider]
    session.commit.assert_called_once()
    session.rollback.assert_not_called()
    session.close.assert_called_once()
    provider.close.assert_called_once()


def test_the_cli_rolls_back_and_closes_everything_on_failure(monkeypatch):
    session = Mock()
    provider = Mock()
    monkeypatch.setattr(seed, "SessionLocal", lambda: session)
    monkeypatch.setattr(seed, "build_provider", lambda: provider)
    monkeypatch.setattr(seed, "seed_episode_corpus", Mock(side_effect=RuntimeError("provider 500")))

    with pytest.raises(RuntimeError, match="provider 500"):
        seed.seed_episodes(allow_unreviewed=True)

    session.commit.assert_not_called()
    session.rollback.assert_called_once()
    session.close.assert_called_once()
    provider.close.assert_called_once()


def test_the_cli_refuses_to_report_success_when_no_episode_was_processed(monkeypatch):
    session = Mock()
    monkeypatch.setattr(seed, "SessionLocal", lambda: session)
    monkeypatch.setattr(seed, "build_provider", Mock())
    monkeypatch.setattr(
        seed,
        "seed_episode_corpus",
        lambda *_a, **_k: episode_seed.EpisodeSeedSummary(
            inserted=0,
            updated=0,
            unchanged=0,
            re_embedded=0,
            embedding_requests=(),
            quotas={},
        ),
    )

    with pytest.raises(RuntimeError, match="refusing to report success"):
        seed.seed_episodes(allow_unreviewed=True)

    session.rollback.assert_called_once()


def test_an_action_vocabulary_that_the_summary_reports(corpus):
    assert {INSERT, UPDATE, UNCHANGED} == {"insert", "update", "unchanged"}
    _, _, summary = _seed(corpus)
    assert summary.as_dict()["quotas"]["total"] == 100
    assert summary.as_dict()["embedding_requests"] == [96, 4]


# --- the human-review gate ------------------------------------------------------------------
def _reviewed(episode):
    """The same row, with a real sign-off recorded. Used only to *construct* a reviewed fixture."""
    review = {
        "status": HUMAN_REVIEWED,
        "checklist": dict.fromkeys(REVIEW_CHECKLIST_KEYS, True),
        "human_signoff": {
            "reviewer": "test-reviewer",
            "reviewed_at": "2026-07-14",
            "decision": "approved",
        },
    }
    return dataclasses.replace(episode, review=review)


def test_the_committed_corpus_is_honestly_unreviewed(corpus):
    """The state of the world, asserted rather than assumed.

    Every row is an LLM draft awaiting the human step the spec puts before seeding. This test exists
    so that the day someone genuinely reviews the corpus, it fails and has to be updated -- and so
    that nobody can quietly claim the drafts are verified without it failing first.
    """
    pending = unreviewed_episodes(corpus)

    assert len(pending) == corpus.quotas.total == 100
    assert corpus.quotas.human_reviewed == 0
    assert all(not e.is_human_reviewed for e in corpus.episodes)


def test_the_seeder_refuses_to_publish_unreviewed_drafts_as_verified_corpus(corpus):
    """The spec's workflow is 'LLM drafts -> human verifies -> committed via seed script'.

    Offline validation is not the human. It catches a malformed row, a leaked hindsight phrase, a
    source outside the allowlist -- never a fluent, well-formed, confidently wrong one. So seeding
    refuses by default, and nothing was embedded before it did.
    """
    provider = RecordingProvider()

    with pytest.raises(EpisodeReviewGateError, match="have not been human-reviewed"):
        seed_episode_corpus(FakeSession(), corpus, provider)

    assert provider.calls == []  # refused before a single text reached the embeddings API


def test_the_gate_names_the_flag_and_the_review_block_that_would_open_it(corpus):
    with pytest.raises(EpisodeReviewGateError) as caught:
        seed_episode_corpus(FakeSession(), corpus, RecordingProvider())

    message = str(caught.value)
    assert "100 of 100" in message
    assert "allow_unreviewed=True" in message and "--allow-unreviewed" in message
    assert "human_reviewed" in message


def test_an_explicitly_unreviewed_seed_is_recorded_as_one(corpus):
    """The bypass is allowed, but it can never be mistaken for a verified corpus afterwards."""
    _, _, summary = _seed(corpus)

    assert summary.unreviewed == 100
    assert summary.as_dict()["review_gate"] == "bypassed"
    assert summary.as_dict()["unreviewed"] == 100


def test_a_human_reviewed_corpus_seeds_with_the_gate_enforced(corpus):
    reviewed = dataclasses.replace(corpus, episodes=tuple(_reviewed(e) for e in corpus.episodes))

    summary = seed_episode_corpus(FakeSession(), reviewed, RecordingProvider())

    assert summary.inserted == 100
    assert summary.unreviewed == 0
    assert summary.as_dict()["review_gate"] == "enforced"


def test_the_cli_reports_the_gate_as_an_actionable_message_not_a_traceback(monkeypatch, capsys):
    """The gate is a curation blocker, so it has to fire before anything at all is opened.

    The regression this pins is one of *order*. The CLI used to run a `SELECT 1` connectivity probe
    ahead of the seed, so the operator whose corpus is 100 unreviewed drafts -- which is every
    operator today -- met a SQLAlchemy connection traceback about a database the run was never going
    to reach, instead of the human-review step that was actually stopping them. So every exit is
    sealed here, not mocked: engine, session factory, the settings holding the API key, the
    provider's constructor, and the socket beneath them. `main([])` must still return the message.
    """
    opened = _seal_every_exit(monkeypatch)

    assert seed.main([]) == 1

    # Nothing was touched to find this out: no database, no key, no provider, no network.
    assert opened == []
    stderr = capsys.readouterr().err
    assert "episode review gate" in stderr
    assert "have not been human-reviewed" in stderr
    assert "--allow-unreviewed" in stderr
    assert "Traceback" not in stderr


def test_provider_is_not_opened_when_the_session_cannot_be_opened(monkeypatch):
    """A database refusal must prevent provider construction before the mode fence."""
    provider = Mock()
    factory = Mock(return_value=provider)
    monkeypatch.setattr(seed, "build_provider", factory)
    monkeypatch.setattr(
        seed,
        "SessionLocal",
        Mock(side_effect=OperationalError("SELECT 1", {}, OSError("connection refused"))),
    )
    monkeypatch.setattr(
        seed, "seed_episode_corpus", Mock(side_effect=AssertionError("must not be reached"))
    )

    with pytest.raises(OperationalError):
        seed.seed_episodes(allow_unreviewed=True)

    factory.assert_not_called()
    provider.close.assert_not_called()
