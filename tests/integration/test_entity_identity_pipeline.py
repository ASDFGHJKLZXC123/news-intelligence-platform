"""ADR 0006 identity pipeline against a real PostgreSQL, on a throwaway database.

This is the one test in the suite that writes entity rows, so it must never touch the
developer's database: it provisions its own, asserts that it did, and drops it afterwards.
The default database is opened only to issue ``CREATE DATABASE``/``DROP DATABASE``, which
does not read or modify anything inside it.

What is exercised end to end, through the real Celery task bodies and real constraints:
SEC seed (precedence 1) -> GLEIF fill and Level-2 parent edge (2) -> Wikidata enrichment,
relations, and the redirect API (3) -- plus persistence, idempotency, uniqueness, and the
precedence rule that a lower source never overwrites a higher one's field.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.base import Base
from db.models import (
    EntityAlias,
    EntityIdentifier,
    EntityProfile,
    EntityRedirect,
    EntityRelationship,
    Job,
    PersonalWriterMode,
    ProviderRun,
    RawIngestionItem,
)
from packages.config.settings import get_settings
from packages.providers.base import (
    LEIRecord,
    LEIRelationship,
    SECCompanyTicker,
    WikidataAlias,
    WikidataEntity,
    WikidataItemRef,
    WikidataQidMatch,
)
from packages.providers.fakes import (
    FakeEntityIdentityProvider,
    FakeSECCompanyTickerProvider,
    FakeWikidataProvider,
)
from services.provider_data import resolve_entity_redirect, upsert_entity_redirect
from workers import provider_data_tasks

pytestmark = pytest.mark.integration

# Every throwaway database this test creates carries this prefix, so a leaked one is obvious.
DISPOSABLE_DB_PREFIX = "nip_identity_it_"

APPLE_CIK = "0000320193"
ALPHABET_CIK = "0001652044"
APPLE_LEI = "HWUPKR0MPOU8FGXBT394"
PARENT_LEI = "213800D1EI4B9WTWWD28"

# The tables the identity pipeline writes plus the two durable writer-fence dependencies every
# legacy task reads before it constructs a provider. The set is closed under its foreign keys.
IDENTITY_MODELS = (
    Job,
    PersonalWriterMode,
    ProviderRun,
    RawIngestionItem,
    EntityProfile,
    EntityIdentifier,
    EntityAlias,
    EntityRelationship,
    EntityRedirect,
)


@pytest.fixture
def disposable_db(require_postgres: None):
    """Create a throwaway database on the configured server, and drop it afterwards."""

    configured = make_url(get_settings().database_url)
    name = f"{DISPOSABLE_DB_PREFIX}{uuid.uuid4().hex[:12]}"

    # The whole point of this fixture: never point the identity pipeline at the developer's
    # database. If these ever coincide, fail rather than write.
    assert name != configured.database
    assert name.startswith(DISPOSABLE_DB_PREFIX)

    # CREATE/DROP DATABASE cannot run inside a transaction, hence AUTOCOMMIT. The maintenance
    # database is only a connection target here; nothing in it is read or written.
    admin = create_engine(
        configured.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))

        engine = create_engine(configured.set(database=name), future=True)
        try:
            # Built from the same ORM metadata the migrations create their tables from.
            Base.metadata.create_all(engine, tables=[m.__table__ for m in IDENTITY_MODELS])
            with Session(engine) as session:
                session.add(PersonalWriterMode(singleton=True, mode="legacy"))
                session.commit()
            yield sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
                {"n": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def identity_tasks(disposable_db, monkeypatch):
    """Point the real task bodies at the throwaway database, with fakes for every provider."""

    monkeypatch.setattr(provider_data_tasks, "SessionLocal", disposable_db)
    for name, provider in (
        ("_sec_identity_provider_binding", _sec_provider()),
        ("_gleif_provider_binding", _gleif_provider()),
        ("_wikidata_provider_binding", _wikidata_provider()),
    ):
        monkeypatch.setattr(
            provider_data_tasks,
            name,
            lambda provider=provider: provider_data_tasks.ProviderBinding(
                provider=provider, mode="test_fake_ingestion", network_called=False
            ),
        )
    return disposable_db


def _sec_provider() -> FakeSECCompanyTickerProvider:
    """The real company_tickers.json shape, including a company with two share classes.

    Alphabet's GOOGL and GOOG are two rows carrying one CIK and one legal name, so the seed
    reaches the same profile and the same normalized alias twice in a single run. That is the
    case a unique constraint rejects unless each upsert can see what the run already added.
    """
    return FakeSECCompanyTickerProvider(
        tickers=[
            SECCompanyTicker(cik=APPLE_CIK, ticker="AAPL", title="Apple Inc."),
            SECCompanyTicker(cik=ALPHABET_CIK, ticker="GOOGL", title="Alphabet Inc."),
            SECCompanyTicker(cik=ALPHABET_CIK, ticker="GOOG", title="Alphabet Inc."),
        ]
    )


def _gleif_provider() -> FakeEntityIdentityProvider:
    """Apple resolves from the SEC seed; its parent is reachable only through Level 2."""
    return FakeEntityIdentityProvider(
        records=[
            LEIRecord(
                lei=APPLE_LEI,
                legal_name="Apple Inc.",
                entity_status="ACTIVE",
                registration_status="ISSUED",
                country_code="US",
                jurisdiction="US-CA",
            ),
            LEIRecord(
                lei=PARENT_LEI,
                legal_name="Example Holdings Inc.",
                entity_status="ACTIVE",
                registration_status="ISSUED",
                country_code="US",
            ),
        ],
        relationships=[
            LEIRelationship(
                relationship_id="rel-apple-parent",
                lei=APPLE_LEI,
                related_lei=PARENT_LEI,
                relationship_type="DIRECT_PARENT",
                status="ACTIVE",
                start_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
            )
        ],
    )


def _wikidata_provider() -> FakeWikidataProvider:
    """Wikidata knows Apple by its CIK, and offers a name it must not be allowed to win with."""
    return FakeWikidataProvider(
        entities=[
            WikidataEntity(
                qid="Q312",
                label="Apple Computer",  # stale: SEC owns the canonical name
                aliases=(
                    WikidataAlias(value="Apple", alias_type="colloquial"),
                    WikidataAlias(
                        value="Apple Computer, Inc.",
                        alias_type="former_name",
                        valid_from=datetime.date(1977, 1, 3),
                        valid_to=datetime.date(2007, 1, 9),
                    ),
                ),
                tickers=("AAPL",),
                ciks=(APPLE_CIK,),
                subsidiaries=(WikidataItemRef(qid="Q95", label="Alphabet"),),
            ),
            WikidataEntity(qid="Q95", label="Alphabet Inc.", ciks=(ALPHABET_CIK,)),
        ],
        qid_matches=[
            WikidataQidMatch(qid="Q312", identifier_type="cik", identifier_value=APPLE_CIK),
            WikidataQidMatch(qid="Q95", identifier_type="cik", identifier_value=ALPHABET_CIK),
        ],
    )


def _run_pipeline(week: str, month: str) -> tuple[dict, dict, dict]:
    """The three scheduled refreshes, in ADR precedence order, each independently callable."""
    sec = provider_data_tasks.run_sec_identity_refresh(period=week)
    gleif = provider_data_tasks.run_entity_identity_ingestion(
        # The curated watchlist is the only way a parent the SEC seed never lists can enter
        # the store: an unresolved parent LEI is skipped, never minted as a placeholder.
        curated_watchlist=["Example Holdings Inc."],
        period=month,
    )
    wikidata = provider_data_tasks.run_wikidata_identity_ingestion(period=month)
    return sec, gleif, wikidata


def test_the_disposable_database_is_not_the_developer_database(disposable_db) -> None:
    with disposable_db() as session:
        name = session.execute(text("SELECT current_database()")).scalar_one()

    assert name.startswith(DISPOSABLE_DB_PREFIX)
    assert name != make_url(get_settings().database_url).database


def test_sec_seeds_gleif_fills_and_wikidata_enriches_without_breaking_precedence(
    identity_tasks,
) -> None:
    sec, gleif, wikidata = _run_pipeline("2026-W28", "2026-07")

    assert (sec["state"], gleif["state"], wikidata["state"]) == ("succeeded",) * 3

    with identity_tasks() as session:
        apple = session.execute(
            select(EntityProfile).where(EntityProfile.primary_cik == APPLE_CIK)
        ).scalar_one()

        # SEC (1) owns the name, CIK, and ticker; GLEIF (2) filled the LEI and country it
        # left empty; Wikidata (3) overwrote nothing, though it offered a stale name.
        assert apple.canonical_name == "Apple Inc."
        assert apple.primary_ticker == "AAPL"
        assert apple.primary_lei == APPLE_LEI
        assert apple.country == "US"
        sources = apple.profile_metadata["identity_sources"]
        assert sources["canonical_name"] == "sec-edgar"
        assert sources["primary_cik"] == "sec-edgar"
        assert sources["primary_lei"] == "gleif"
        assert "wikidata" not in sources.values()

        # Aliases merge across all three sources rather than conflicting (ADR 0006).
        aliases = (
            session.execute(select(EntityAlias).where(EntityAlias.entity_id == apple.id))
            .scalars()
            .all()
        )
        assert {a.source for a in aliases} == {"sec-edgar", "gleif", "wikidata"}
        former = next(a for a in aliases if a.alias_type == "former_name")
        assert (former.valid_from, former.valid_to) == (
            datetime.date(1977, 1, 3),
            datetime.date(2007, 1, 9),
        )

        # Identifiers carry their own provenance, one row per (entity, type, value, provider).
        identifiers = (
            session.execute(
                select(EntityIdentifier).where(EntityIdentifier.entity_profile_id == apple.id)
            )
            .scalars()
            .all()
        )
        assert {i.provider for i in identifiers} == {"sec-edgar", "gleif", "wikidata"}
        assert any(
            i.identifier_type == "wikidata_qid" and i.identifier_value == "Q312"
            for i in identifiers
        )

        # The watchlisted parent is a real entity the SEC seed never listed (ADR 0006's
        # reason for the watchlist), and GLEIF Level 2 is what attaches it to Apple.
        holdings = session.execute(
            select(EntityProfile).where(EntityProfile.primary_lei == PARENT_LEI)
        ).scalar_one()

        # Both sources' parent claims coexist: GLEIF Level 2 made Holdings the parent of
        # Apple, and Wikidata's P355 made Alphabet Apple's child. Distinct provider edges.
        edges = session.execute(select(EntityRelationship)).scalars().all()
        assert {e.provider for e in edges} == {"gleif", "wikidata"}
        assert all(e.relationship_type == "parent_of" for e in edges)
        gleif_edge = next(e for e in edges if e.provider == "gleif")
        assert (gleif_edge.parent_entity_id, gleif_edge.child_entity_id) == (holdings.id, apple.id)
        assert gleif_edge.valid_from == datetime.date(2020, 1, 1)

        # One ProviderRun per task, each owning the raw items its service retained: the
        # services never open a nested run of their own.
        runs = session.execute(select(ProviderRun)).scalars().all()
        assert {(r.provider, r.status) for r in runs} == {
            ("sec-edgar", "succeeded"),
            ("gleif", "succeeded"),
            ("wikidata", "succeeded"),
        }
        raw = session.execute(select(RawIngestionItem)).scalars().all()
        assert raw and {item.provider_run_id for item in raw} <= {r.id for r in runs}


def test_rerunning_every_refresh_is_idempotent(identity_tasks) -> None:
    _run_pipeline("2026-W28", "2026-07")

    def snapshot() -> dict[str, int]:
        with identity_tasks() as session:
            return {
                model.__name__: session.execute(select(model)).scalars().all().__len__()
                for model in (EntityProfile, EntityAlias, EntityIdentifier, EntityRelationship)
            }

    before = snapshot()
    # A later period re-reads the same upstream data: new runs, no new entity rows.
    _run_pipeline("2026-W32", "2026-08")

    assert snapshot() == before
    with identity_tasks() as session:
        assert len(session.execute(select(ProviderRun)).scalars().all()) == 6


def test_a_repeat_inside_one_period_reuses_the_same_provider_run(identity_tasks) -> None:
    first = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")
    repeat = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    assert first["run_id"] == repeat["run_id"]
    assert repeat["idempotent"] is True
    with identity_tasks() as session:
        assert len(session.execute(select(ProviderRun)).scalars().all()) == 1


def test_real_uniqueness_constraints_reject_duplicate_identity_rows(identity_tasks) -> None:
    _run_pipeline("2026-W28", "2026-07")

    with identity_tasks() as session:
        apple = session.execute(
            select(EntityProfile).where(EntityProfile.primary_cik == APPLE_CIK)
        ).scalar_one()
        existing = (
            session.execute(select(EntityAlias).where(EntityAlias.entity_id == apple.id))
            .scalars()
            .first()
        )

        # uq_entity_aliases_entity_normalized_alias_source: one row per entity+key+source.
        session.add(
            EntityAlias(
                id=uuid.uuid4(),
                entity_id=apple.id,
                alias=existing.alias,
                normalized_alias=existing.normalized_alias,
                alias_type=existing.alias_type,
                source=existing.source,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    # A second profile must not be able to claim a CIK that already belongs to Apple.
    with identity_tasks() as session:
        session.add(
            EntityProfile(
                id=uuid.uuid4(),
                canonical_name="Impostor Inc.",
                normalized_name="impostor inc.",
                entity_type="company",
                primary_cik=APPLE_CIK,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_a_rename_is_a_redirect_row_that_resolution_follows(identity_tasks) -> None:
    """ADR 0006: renames and mergers are data operations, not code changes."""
    _run_pipeline("2026-W28", "2026-07")

    with identity_tasks() as session:
        apple = session.execute(
            select(EntityProfile).where(EntityProfile.primary_cik == APPLE_CIK)
        ).scalar_one()
        alphabet = session.execute(
            select(EntityProfile).where(EntityProfile.primary_cik == ALPHABET_CIK)
        ).scalar_one()

        _redirect, created = upsert_entity_redirect(
            session,
            old_entity_id=apple.id,
            new_entity_id=alphabet.id,
            effective_date="2026-07-01",
            reason="test merger",
        )
        session.commit()
        assert created is True

        # Re-recording the same redirect is idempotent, and resolution follows the hop.
        _again, created_again = upsert_entity_redirect(
            session,
            old_entity_id=apple.id,
            new_entity_id=alphabet.id,
            effective_date="2026-07-01",
        )
        session.commit()
        assert created_again is False
        assert resolve_entity_redirect(session, apple.id) == alphabet.id
        # An entity nothing redirects away from resolves to itself.
        assert resolve_entity_redirect(session, alphabet.id) == alphabet.id
        # Before the effective date the old entity still stands on its own.
        assert (
            resolve_entity_redirect(session, apple.id, as_of=datetime.date(2026, 6, 30)) == apple.id
        )


def test_a_provider_failure_persists_a_failed_run_and_leaves_the_store_untouched(
    identity_tasks, monkeypatch
) -> None:
    class Broken:
        def fetch_company_tickers(self):  # noqa: ANN202
            raise RuntimeError("SEC 503")

    monkeypatch.setattr(
        provider_data_tasks,
        "_sec_identity_provider_binding",
        lambda: provider_data_tasks.ProviderBinding(
            provider=Broken(), mode="test_fake_ingestion", network_called=False
        ),
    )

    with pytest.raises(RuntimeError, match="SEC 503"):
        provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    with identity_tasks() as session:
        run = session.execute(select(ProviderRun)).scalars().one()
        # The failure is durable, and the aborted work was rolled back rather than committed.
        assert run.status == "failed"
        assert run.error["type"] == "RuntimeError"
        assert session.execute(select(EntityProfile)).scalars().all() == []


def test_two_share_classes_of_one_company_stay_one_entity(identity_tasks) -> None:
    """The SEC seed reaches one CIK twice (GOOGL and GOOG). It must not create two Alphabets,
    nor two copies of the alias they share -- which a real unique constraint would reject."""
    result = provider_data_tasks.run_sec_identity_refresh(period="2026-W28")

    # Three seed rows, two companies: the second share class is not a second entity.
    assert result["fetched"] == 3
    assert result["inserted"] == 2

    with identity_tasks() as session:
        alphabet = session.execute(
            select(EntityProfile).where(EntityProfile.primary_cik == ALPHABET_CIK)
        ).scalar_one()
        aliases = (
            session.execute(select(EntityAlias).where(EntityAlias.entity_id == alphabet.id))
            .scalars()
            .all()
        )

        # One legal-name alias (both rows normalize to "alphabet"), one per ticker.
        assert sorted(a.alias for a in aliases) == ["Alphabet Inc.", "GOOG", "GOOGL"]
        # Seed order decides the primary ticker; the second class never displaces it.
        assert alphabet.primary_ticker == "GOOGL"
        tickers = (
            session.execute(
                select(EntityIdentifier).where(
                    EntityIdentifier.entity_profile_id == alphabet.id,
                    EntityIdentifier.identifier_type == "ticker",
                )
            )
            .scalars()
            .all()
        )
        assert sorted(t.identifier_value for t in tickers) == ["GOOG", "GOOGL"]
