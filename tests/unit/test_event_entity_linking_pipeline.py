"""The ADR 0005 pipeline over one event, and the Celery task that runs it.

The linker is the real one -- real alias lookup, real signal weights, real bands -- so the
scores these tests assert are the scores production computes. Only the two collaborators the
ADR puts outside the deterministic core are faked: the spaCy extractor and the LLM adjudicator.
No model is loaded, no socket is opened, and no API key is needed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from db.models import EntityResolutionRun, EventEntity
from services.entities.adjudication import AdjudicationDecision, MentionAdjudication
from services.entities.event_linking import (
    SKIP_EMPTY,
    SKIP_MISSING,
    SKIP_NON_ENGLISH,
    EventNotFoundError,
    link_event_entities,
)
from services.entities.event_links import ROLE_ASSERTED, ROLE_DENIED, ROLE_SPECULATIVE
from services.entities.news_linking import LinkBand
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import ArticleMentions, ArticleText
from tests.unit.entity_linking_fakes import (
    FakeAdjudicator,
    FakeExtractor,
    FakeSession,
    alias,
    article,
    event,
    event_article,
    mention,
    profile,
    source,
)
from workers import entity_linking_tasks
from workers.celery_app import QUEUE_PIPELINE, celery_app

# The linker's real weights (ADR 0005): ticker .25 + co-mentions .25 + context .20 + url .15
# + location .05 = 0.90, comfortably over the Stage 9 accept threshold of 0.07.
ACCEPT_SCORE = 0.9
# Location cue only = 0.05: the weakest evidence a candidate can carry, and the one thing that
# still lands *below* the recalibrated 0.07 accept threshold and so reaches the adjudicator.
ADJUDICATE_SCORE = 0.05


def _store() -> tuple[FakeSession, Any, Any, Any]:
    """One event, one English article, and an identity store that makes the mentions land."""

    acme = profile(
        "Acme Corp",
        entity_type="company",
        country="US",
        primary_ticker="ACME",
        website="https://acme.example.com",
    )
    feed = source()
    subject = event(country="US")
    item = article(
        feed,
        url="https://acme.example.com/press/q2",
        title="Acme reported revenue",
        body="Acme reported revenue for the period. ACME shares rose. Acme Brands expanded.",
    )
    session = FakeSession(
        acme,
        alias(acme, "Acme Corp", alias_type="legal_name"),
        alias(acme, "Acme Brands", alias_type="brand_product"),
        alias(acme, "Acme Labs", alias_type="colloquial"),
        feed,
        subject,
        item,
        event_article(subject, item),
    )
    return session, subject, item, acme


def _accepting_mentions(key: str, **kwargs: Any) -> tuple[Any, ...]:
    """Three mentions of one company: the co-mentions are what carry it over the threshold."""

    return (
        mention(
            "Acme",
            article_key=key,
            sentence="Acme reported revenue for the period.",
            next_text="ACME shares rose.",
            start_char=0,
            **kwargs,
        ),
        mention(
            "Acme Brands",
            article_key=key,
            sentence="Acme Brands expanded.",
            previous_text="ACME shares rose.",
            start_char=60,
            **kwargs,
        ),
        mention(
            "Acme Labs",
            article_key=key,
            sentence="Acme Labs expanded.",
            previous_text="ACME shares rose.",
            start_char=90,
            **kwargs,
        ),
    )


def _run(session: FakeSession, subject: Any, extractor: FakeExtractor, **kwargs: Any):
    return link_event_entities(session, subject.id, extractor=extractor, **kwargs)


# --- The deterministic path: one batch, accepted links, no LLM --------------------------


def test_an_event_is_extracted_in_exactly_one_batch_call() -> None:
    session, subject, _item, _acme = _store()
    second = article(_source_of(session), title="Acme again")
    session.add(second)
    session.add(event_article(subject, second))
    extractor = FakeExtractor()

    result = _run(session, subject, extractor)

    # The ADR's "CPU-batch in Celery": one nlp.pipe call for the event, never one per article.
    assert extractor.call_count == 1
    assert len(extractor.calls[0]) == 2
    assert result.extraction_calls == 1
    assert result.articles_processed == 2


def test_an_accepted_mention_links_deterministically_and_spends_no_token() -> None:
    session, subject, item, acme = _store()
    extractor = FakeExtractor({str(item.id): _accepting_mentions(str(item.id))})
    adjudicator = FakeAdjudicator()

    result = _run(session, subject, extractor, adjudicator=adjudicator)

    assert adjudicator.call_count == 0
    assert {outcome.band for outcome in result.mentions} == {LinkBand.ACCEPT}
    assert result.accepted_count == 3

    # Three mentions of one company are one link, at the best score any of them earned.
    links = session.all_of(EventEntity)
    assert len(links) == 1
    assert links[0].entity_profile_id == acme.id
    assert float(links[0].confidence_score) == pytest.approx(ACCEPT_SCORE)
    assert links[0].role == ROLE_ASSERTED

    # Every mention is audited, whatever it linked to.
    runs = session.all_of(EntityResolutionRun)
    assert len(runs) == 3
    assert all(run.matched_entity_id == acme.id for run in runs)


def test_a_rerun_relinks_the_same_rows_without_duplicating_or_lowering_them() -> None:
    session, subject, item, _acme = _store()
    extractor = FakeExtractor({str(item.id): _accepting_mentions(str(item.id))})

    first = _run(session, subject, extractor)
    second = _run(session, subject, extractor)

    assert first.as_dict() == second.as_dict()
    assert len(session.all_of(EventEntity)) == 1
    assert len(session.all_of(EntityResolutionRun)) == 3
    assert float(session.all_of(EventEntity)[0].confidence_score) == pytest.approx(ACCEPT_SCORE)


@pytest.mark.parametrize(
    ("status", "role"),
    [
        (AssertionStatus.DENIED, ROLE_DENIED),
        (AssertionStatus.SPECULATIVE, ROLE_SPECULATIVE),
    ],
)
def test_a_denied_or_speculative_mention_is_linked_but_not_risk_eligible(
    status: AssertionStatus, role: str
) -> None:
    session, subject, item, _acme = _store()
    extractor = FakeExtractor(
        {str(item.id): _accepting_mentions(str(item.id), assertion_status=status)}
    )

    result = _run(session, subject, extractor)

    assert result.linked_count == 3
    link = session.all_of(EventEntity)[0]
    assert link.role == role
    # Persisted, auditable, and still not a risk input (ADR 0005).
    from services.entities.event_links import risk_eligible_event_links

    assert risk_eligible_event_links(session, subject.id) == ()


def test_a_mention_that_matches_nothing_links_to_nothing_and_stays_reviewable() -> None:
    session, subject, item, _acme = _store()
    unknown = mention("Unheard Of Ltd", article_key=str(item.id))
    extractor = FakeExtractor({str(item.id): (unknown,)})
    adjudicator = FakeAdjudicator()

    result = _run(session, subject, extractor, adjudicator=adjudicator)

    assert adjudicator.call_count == 0  # NIL never spends a token either
    assert result.mentions[0].band is LinkBand.NIL
    assert result.linked_count == 0
    assert session.all_of(EventEntity) == []
    # The run row is still written, so the surface reaches the weekly review queue.
    assert len(session.all_of(EntityResolutionRun)) == 1


# --- The ambiguous band, and only it, reaches the adjudicator ----------------------------


def _ambiguous_store() -> tuple[FakeSession, Any, Any, Any]:
    """A store whose only evidence is the 0.05 location cue -- below the 0.07 accept threshold.

    No ticker, no context cue word, no known entity type, no co-mention, no matching URL domain:
    the country match is the single weak signal that fires, which is what puts the mention in the
    adjudicate band.
    """
    zeta = profile("Zeta Corp", country="US")
    feed = source()
    subject = event(country="US")
    item = article(
        feed,
        url="https://wire.example.com/markets",
        title="Zeta named in the note",
        body="Zeta was named in the note. The note was circulated widely.",
    )
    session = FakeSession(
        zeta,
        alias(zeta, "Zeta Corp", alias_type="legal_name"),
        feed,
        subject,
        item,
        event_article(subject, item),
    )
    return session, subject, item, zeta


def test_an_ambiguous_mention_is_adjudicated_and_persists_the_deterministic_score() -> None:
    session, subject, item, zeta = _ambiguous_store()
    extractor = FakeExtractor(
        {
            str(item.id): (
                mention(
                    "Zeta",
                    article_key=str(item.id),
                    sentence="Zeta was named in the note.",
                    next_text="The note was circulated widely.",
                ),
            )
        }
    )
    adjudicator = FakeAdjudicator()

    result = _run(session, subject, extractor, adjudicator=adjudicator)

    assert adjudicator.call_count == 1
    outcome = result.mentions[0]
    assert outcome.band is LinkBand.ADJUDICATE
    assert outcome.adjudicated is True
    assert outcome.adjudication_decision is AdjudicationDecision.SELECTED
    assert outcome.confidence_score == pytest.approx(ADJUDICATE_SCORE)

    link = session.all_of(EventEntity)[0]
    assert link.entity_profile_id == zeta.id
    # The selected candidate's own stage-2 score, never a number the model made up.
    assert float(link.confidence_score) == pytest.approx(ADJUDICATE_SCORE)

    # The mention resolved, so its run carries the entity and leaves the unresolved queue.
    run = session.all_of(EntityResolutionRun)[0]
    assert run.matched_entity_id == zeta.id


@pytest.mark.parametrize(
    "decision",
    [AdjudicationDecision.NIL, AdjudicationDecision.FAILED],
)
def test_a_nil_or_failed_adjudication_attaches_nothing(decision: AdjudicationDecision) -> None:
    session, subject, item, _zeta = _ambiguous_store()
    extractor = FakeExtractor(
        {
            str(item.id): (
                mention(
                    "Zeta",
                    article_key=str(item.id),
                    sentence="Zeta was named in the note.",
                    next_text="The note was circulated widely.",
                ),
            )
        }
    )
    adjudicator = FakeAdjudicator(
        lambda _m, result: MentionAdjudication(
            target_id=result.target_id, decision=decision, reason="scripted"
        )
    )

    result = _run(session, subject, extractor, adjudicator=adjudicator)

    assert result.mentions[0].adjudication_decision is decision
    assert result.linked_count == 0
    assert session.all_of(EventEntity) == []
    # Unmatched: it stays in the review queue rather than being reported as linked.
    assert session.all_of(EntityResolutionRun)[0].matched_entity_id is None


def test_without_an_adjudicator_an_ambiguous_mention_simply_does_not_link() -> None:
    session, subject, item, _zeta = _ambiguous_store()
    extractor = FakeExtractor(
        {
            str(item.id): (
                mention(
                    "Zeta",
                    article_key=str(item.id),
                    sentence="Zeta was named in the note.",
                    next_text="The note was circulated widely.",
                ),
            )
        }
    )

    result = _run(session, subject, extractor)

    assert result.mentions[0].band is LinkBand.ADJUDICATE
    assert result.linked_count == 0
    assert session.all_of(EventEntity) == []


def test_an_infrastructure_failure_in_adjudication_propagates() -> None:
    """The event is not half-linked and reported as done: the task's retry gets the whole run."""

    session, subject, item, _zeta = _ambiguous_store()
    extractor = FakeExtractor(
        {
            str(item.id): (
                mention(
                    "Zeta",
                    article_key=str(item.id),
                    sentence="Zeta was named in the note.",
                    next_text="The note was circulated widely.",
                ),
            )
        }
    )

    def explode(_mention: Any, _result: Any) -> MentionAdjudication:
        raise RuntimeError("all providers failed")

    with pytest.raises(RuntimeError, match="all providers failed"):
        _run(session, subject, extractor, adjudicator=FakeAdjudicator(explode))


# --- Article selection: language, emptiness, duplicates, and context ----------------------


def test_a_non_english_article_is_skipped_and_an_unmarked_one_is_not() -> None:
    session, subject, item, _acme = _store()
    feed = _source_of(session)
    french = article(feed, language="fr", title="Acme en France")
    unmarked = article(feed, language=None, title="Acme reported revenue")
    for extra in (french, unmarked):
        session.add(extra)
        session.add(event_article(subject, extra))
    extractor = FakeExtractor()

    result = _run(session, subject, extractor)

    extracted = {text.key for text in extractor.calls[0]}
    assert str(french.id) not in extracted
    assert {str(item.id), str(unmarked.id)} == extracted
    assert result.articles_total == 3
    assert [skip.reason for skip in result.skipped_articles] == [SKIP_NON_ENGLISH]


@pytest.mark.parametrize("language", ["en", "en-US", "EN_gb"])
def test_every_english_tag_variant_is_processed(language: str) -> None:
    session, subject, _item, _acme = _store()
    feed = _source_of(session)
    tagged = article(feed, language=language, title="Acme reported revenue")
    session.add(tagged)
    session.add(event_article(subject, tagged))

    result = _run(session, subject, FakeExtractor())

    assert result.skipped_articles == ()
    assert result.articles_processed == 2


def test_an_empty_article_never_reaches_the_model() -> None:
    session, subject, _item, _acme = _store()
    empty = article(_source_of(session), title="   ", body=None, summary=None)
    session.add(empty)
    session.add(event_article(subject, empty))
    extractor = FakeExtractor()

    result = _run(session, subject, extractor)

    assert {text.key for text in extractor.calls[0]} == {str(_article_of(session, subject).id)}
    assert [skip.reason for skip in result.skipped_articles] == [SKIP_EMPTY]


def test_an_event_with_no_articles_loads_no_model_at_all() -> None:
    session, subject, _item, _acme = _store()
    empty_event = event()
    session.add(empty_event)
    extractor = FakeExtractor()

    result = link_event_entities(session, empty_event.id, extractor=extractor)

    assert extractor.call_count == 0
    assert result.extraction_calls == 0
    assert (result.articles_total, len(result.mentions)) == (0, 0)


def test_an_event_article_pointing_at_a_missing_article_is_skipped_not_crashed() -> None:
    session, subject, _item, _acme = _store()
    session.add(event_article(subject, _phantom()))

    result = _run(session, subject, FakeExtractor())

    assert [skip.reason for skip in result.skipped_articles] == [SKIP_MISSING]


def test_an_article_listed_twice_is_extracted_once() -> None:
    session, subject, item, _acme = _store()
    session.add(event_article(subject, item))  # the same association, again
    extractor = FakeExtractor()

    result = _run(session, subject, extractor)

    assert len(extractor.calls[0]) == 1
    assert result.articles_total == 1


def test_a_missing_event_is_an_error_not_an_empty_result() -> None:
    session, _subject, _item, _acme = _store()

    with pytest.raises(EventNotFoundError):
        link_event_entities(session, uuid.uuid4(), extractor=FakeExtractor())


def test_each_mention_gets_the_other_surfaces_as_co_mentions_and_never_its_own() -> None:
    """The co-mention signal is worth a real 0.25, and a mention never supplies it to itself."""

    session, subject, item, acme = _store()
    mentions = _accepting_mentions(str(item.id))
    extractor = FakeExtractor({str(item.id): mentions})

    result = _run(session, subject, extractor)

    # Every mention accepted, and the only signal that could have done it is the co-mentions
    # of the *other* two surfaces: a mention cannot support itself.
    assert result.accepted_count == 3
    assert float(session.all_of(EntityResolutionRun)[0].confidence_score) == pytest.approx(
        ACCEPT_SCORE
    )

    session_two = FakeSession(*[row for row in session.items if not isinstance(row, EventEntity)])
    lonely = link_event_entities(
        session_two,
        subject.id,
        extractor=FakeExtractor({str(item.id): (mentions[0],)}),
    )

    # Alone, the same mention loses exactly the 0.25 co-mention weight. Under the Stage 9 bands
    # 0.65 still clears accept, so what the co-mention signal moves here is the score, not the
    # outcome -- and the score is the one the audit run records.
    assert lonely.mentions[0].band is LinkBand.ACCEPT
    assert lonely.mentions[0].confidence_score == pytest.approx(ACCEPT_SCORE - 0.25)
    run = next(
        item
        for item in session_two.all_of(EntityResolutionRun)
        if item.target_id.endswith(f"#{mentions[0].start_char}-{mentions[0].end_char}")
    )
    assert float(run.confidence_score) == pytest.approx(ACCEPT_SCORE - 0.25)
    assert run.matched_entity_id == acme.id


def test_the_result_is_json_serializable() -> None:
    session, subject, item, acme = _store()
    extractor = FakeExtractor({str(item.id): _accepting_mentions(str(item.id))})

    payload = _run(session, subject, extractor).as_dict()

    import json

    assert json.loads(json.dumps(payload))["links_persisted"] == 3
    assert payload["event_id"] == str(subject.id)
    assert payload["mentions"][0]["entity_id"] == str(acme.id)
    assert payload["mentions"][0]["band"] == "accept"


# --- The Celery task ----------------------------------------------------------------------


class SpySession(FakeSession):
    def __init__(self, *rows: Any) -> None:
        super().__init__(*rows)
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def task_session(monkeypatch: pytest.MonkeyPatch) -> SpySession:
    session, subject, item, acme = _store()
    spy = SpySession(*session.items)
    spy.event, spy.article, spy.entity = subject, item, acme

    monkeypatch.setattr(entity_linking_tasks, "SessionLocal", lambda: spy)
    monkeypatch.setattr(
        entity_linking_tasks,
        "build_mention_extractor",
        lambda: FakeExtractor({str(item.id): _accepting_mentions(str(item.id))}),
    )
    monkeypatch.setattr(
        entity_linking_tasks, "build_mention_adjudicator", lambda _session: FakeAdjudicator()
    )
    return spy


def test_the_task_is_registered_on_the_pipeline_queue_and_beat_ignores_it() -> None:
    task = celery_app.tasks[entity_linking_tasks.TASK_NAME]

    assert task.queue == QUEUE_PIPELINE
    # ADR 0005 schedules no linking run: an event is linked when it has articles, not on a clock.
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert entity_linking_tasks.TASK_NAME not in scheduled


def test_the_task_links_one_event_and_commits_once(task_session: SpySession) -> None:
    payload = entity_linking_tasks.run_event_entity_linking(str(task_session.event.id))

    assert payload["status"] == "ok"
    assert payload["links_persisted"] == 3
    assert payload["event_id"] == str(task_session.event.id)
    assert (task_session.commits, task_session.rollbacks, task_session.closed) == (1, 0, 1)
    assert task_session.all_of(EventEntity)[0].entity_profile_id == task_session.entity.id


def test_a_failure_rolls_back_closes_and_propagates_for_the_retry(
    task_session: SpySession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage1Task's autoretry is what handles this, so the task must not swallow the failure."""

    def explode(_session: Any) -> Any:
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(entity_linking_tasks, "build_mention_adjudicator", explode)

    with pytest.raises(RuntimeError, match="provider unreachable"):
        entity_linking_tasks.run_event_entity_linking(str(task_session.event.id))

    assert (task_session.commits, task_session.rollbacks, task_session.closed) == (0, 1, 1)


def test_the_task_retries_on_exception_with_backoff() -> None:
    task = celery_app.tasks[entity_linking_tasks.TASK_NAME]

    assert task.autoretry_for == (Exception,)
    assert task.max_retries == 3
    assert task.retry_backoff is True


def test_the_production_extractor_loads_no_model_until_there_is_text_to_extract() -> None:
    """The transformer is a deployment prerequisite, never an import-time or idle-time load.

    spaCy is not installed in this environment, so *any* attempt to load the model raises
    ``MentionExtractionConfigurationError``. That this module imports at all, and that its real
    extractor runs over a batch with nothing to extract, is therefore the assertion: neither the
    import nor the binding nor an empty batch reaches a model.
    """

    extract = entity_linking_tasks.build_mention_extractor()

    assert extract(()) == ()
    assert extract((ArticleText(key="a", text="   "),)) == (
        ArticleMentions(article_key="a", mentions=()),
    )


def _source_of(session: FakeSession) -> Any:
    from db.models import Source

    return session.all_of(Source)[0]


def _article_of(session: FakeSession, subject: Any) -> Any:
    from db.models import Article

    return next(item for item in session.all_of(Article) if item.title.strip())


def _phantom() -> Any:
    from db.models import Article

    return Article(id=uuid.uuid4(), source_id=uuid.uuid4(), url="x", url_hash="y", title="z")
