"""The committed entity-linking gold v1 dataset: audits the real data, fully offline.

These tests load the data actually committed under ``evaluation/gold/entity_linking/v1`` (not a
fixture) and assert the Stage-8 curation contract holds on it: exact split counts, the stronger
case-tag/assertion quotas the milestone asked for, that every record round-trips into the Stage-2
DTOs and every target into ORM rows without a database, that the holdout stays gated, that no
synthetic context claims a real article URL or publisher, and that every citation is an
authoritative https reference. Nothing here touches the network, a database, spaCy, or an LLM --
a guarantee one test enforces by blocking socket connections during a full load.
"""

from __future__ import annotations

import datetime
import json
import re
import socket
from collections import Counter
from urllib.parse import urlparse

import pytest

from db.models.core import EntityAlias, EntityProfile, EntityRelationship
from services.entities.news_linking import (
    COMPANY_MENTION_LABELS,
    LINKABLE_ALIAS_TYPES,
    ArticleLinkingContext,
)
from services.evaluation.entity_linking_gold import (
    FAKE_URL_HOSTS,
    GOLD_ROOT,
    LANGUAGE,
    MANIFEST_FILE,
    REF_KINDS,
    SPLIT_COUNTS,
    SPLIT_FINAL_HOLDOUT,
    SPLIT_ORDER,
    TOTAL_COUNT,
    EntityGoldValidationError,
    document_fingerprint,
    fixture_target_uuid,
    load_corpus,
    load_manifest,
    load_split,
    load_target_catalog,
    load_tuning_corpus,
)
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import EntityLabel, EntityMention

ACCESS_DATE = datetime.date(2026, 7, 16)

# The milestone's stronger minima (the loader enforces only >=1 per tag).
CASE_TAG_MINIMA = {
    "legal_name": 20, "short_name": 20, "former_name": 8, "colloquial": 8, "brand_product": 12,
    "ticker": 15, "transliteration": 5, "acronym": 8, "ambiguous": 20, "subsidiary_parent": 10,
    "geographic_disambiguation": 12, "hard_negative": 30, "nil": 40,
}


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


# --- shape and counts ----------------------------------------------------------------------
def test_committed_corpus_loads_with_exact_counts(corpus):
    assert corpus.quotas.total == TOTAL_COUNT == 200
    assert corpus.includes_holdout is True
    for split in SPLIT_ORDER:
        assert len(corpus.mentions_for(split)) == SPLIT_COUNTS[split]
    assert [len(corpus.mentions_for(s)) for s in SPLIT_ORDER] == [100, 50, 50]
    assert corpus.quotas.links >= 140 and corpus.quotas.nils >= 40
    assert corpus.quotas.links + corpus.quotas.nils == 200


def test_case_tag_quotas_meet_milestone_minima(corpus):
    tags = corpus.quotas.by_case_tag
    for tag, minimum in CASE_TAG_MINIMA.items():
        assert tags.get(tag, 0) >= minimum, (tag, tags.get(tag, 0), minimum)


def test_label_and_language_support(corpus):
    assert corpus.manifest.language == LANGUAGE == "en"
    assert all(m.language == "en" for m in corpus.mentions)
    # Positive links are only the implemented ORG/PRODUCT company labels.
    positive = {m.label for m in corpus.mentions if m.is_link}
    assert positive <= COMPANY_MENTION_LABELS == {EntityLabel.ORG, EntityLabel.PRODUCT}
    assert EntityLabel.ORG in positive and EntityLabel.PRODUCT in positive
    # NIL hard negatives span every extracted label, incl. PERSON/GPE.
    nil_labels = {m.label for m in corpus.mentions if not m.is_link}
    assert {EntityLabel.ORG, EntityLabel.PRODUCT, EntityLabel.PERSON, EntityLabel.GPE} <= nil_labels


def test_assertion_status_minima_per_split(corpus):
    for split in SPLIT_ORDER:
        counts = Counter(m.assertion_status for m in corpus.mentions_for(split))
        assert counts[AssertionStatus.ASSERTED] >= 20, (split, counts)
        assert counts[AssertionStatus.DENIED] >= 12, (split, counts)
        assert counts[AssertionStatus.SPECULATIVE] >= 12, (split, counts)
    # denied/speculative company mentions still link -- identity is independent of assertion.
    non_asserted = [m for m in corpus.mentions
                    if m.assertion_status is not AssertionStatus.ASSERTED and m.is_link]
    assert len(non_asserted) >= 40


# --- catalog: coverage, subsidiaries, sources ----------------------------------------------
def test_catalog_spans_enough_real_targets_and_countries(corpus):
    catalog = corpus.catalog
    assert len(catalog.targets) >= 15
    assert len(catalog.countries) >= 8
    # Every 2-letter country code is well-formed and uppercase.
    assert all(len(c) == 2 and c.isupper() for c in catalog.countries)


def test_subsidiary_cases_expect_the_subsidiary_not_the_parent(corpus):
    by_id = corpus.catalog.by_id
    parents = {t.target_id for t in corpus.catalog.targets if t.parent is not None}
    assert len(parents) >= 2, "need at least two subsidiary/parent pairs"
    # Every parent referenced exists in the catalog.
    for target in corpus.catalog.targets:
        if target.parent is not None:
            assert target.parent.target_id in by_id
    # A subsidiary/parent-tagged link resolves to a target that itself has a parent -- i.e. the
    # brand/subsidiary entity, never the bare parent it belongs to.
    tagged = [m for m in corpus.mentions
              if "subsidiary_parent" in m.case_tags and m.is_link]
    assert len(tagged) >= 10
    for m in tagged:
        assert by_id[m.expected_target_id].parent is not None, m.mention_id


def test_every_expected_target_is_a_catalogued_sourced_entity(corpus):
    by_id = corpus.catalog.by_id
    for m in corpus.mentions:
        if m.is_link:
            assert m.expected_target_id in by_id
            assert by_id[m.expected_target_id].refs, m.expected_target_id
        else:
            assert m.expected_target_id is None
    # Distinct real entities are actually exercised as answers.
    assert len({m.expected_target_id for m in corpus.mentions if m.is_link}) >= 15


def test_authoritative_refs_are_https_dated_and_non_placeholder(corpus):
    for target in corpus.catalog.targets:
        assert target.refs, target.target_id
        for ref in target.refs:
            host = (urlparse(ref.url).netloc or "").lower().removeprefix("www.")
            assert urlparse(ref.url).scheme == "https", ref.url
            assert host and not any(host == f or host.endswith(f".{f}") for f in FAKE_URL_HOSTS), host
            assert ref.accessed == ACCESS_DATE, (target.target_id, ref.accessed)
            assert ref.kind in REF_KINDS
            assert ref.title.strip() and ref.publisher.strip()
    # Every alias uses an ADR alias type; the catalog is sourced by real registries.
    sources = {a.source for t in corpus.catalog.targets for a in t.aliases}
    assert {"sec-edgar", "gleif"} <= sources
    assert all(a.alias_type in LINKABLE_ALIAS_TYPES
               for t in corpus.catalog.targets for a in t.aliases)


# --- ORM + Stage-2 conversion without a database -------------------------------------------
def test_targets_construct_orm_rows_without_a_database(corpus):
    for target in corpus.catalog.targets:
        profile = EntityProfile(**target.profile_row())
        assert profile.id == target.fixture_uuid == fixture_target_uuid(target.target_id)
        for row in target.alias_rows():
            EntityAlias(**row)
        relationship = target.relationship_row()
        if relationship is not None:
            rel = EntityRelationship(**relationship)
            assert rel.child_entity_id == target.fixture_uuid
            assert rel.parent_entity_id == fixture_target_uuid(target.parent.target_id)


def test_every_mention_converts_to_stage2_inputs(corpus):
    catalog = corpus.catalog
    for m in corpus.mentions:
        mention, context = m.to_stage2_inputs(catalog)
        assert isinstance(mention, EntityMention)
        assert isinstance(context, ArticleLinkingContext)
        assert mention.article_key == context.article_key == m.article_key
        assert mention.text == m.mention_text
        # Linked co-mention ids resolve to real fixture UUIDs.
        assert context.linked_entity_ids == tuple(
            fixture_target_uuid(t) for t in m.context.linked_target_ids)


# --- provenance: original synthetic, no borrowed URL/publisher -----------------------------
def test_no_synthetic_context_claims_a_url_or_publisher(corpus):
    for m in corpus.mentions:
        assert m.article_url is None, m.mention_id
        assert m.provenance_kind == "original_synthetic"
    # The manifest is honest about provenance, licence, and the (automated) review status.
    manifest = corpus.manifest
    assert manifest.metadata.get("provenance") == "original_synthetic"
    assert manifest.license.get("id")
    review = manifest.labeling.get("adjudication", "").lower()
    assert "automated" in review and "no human" in review
    assert "dual review" not in " ".join(str(v) for v in manifest.labeling.values()).lower() \
        or "no human dual-review" in review


# Affirmative claims of human involvement the automated curation never earned. Each pattern needs
# the *past-participle/agent* form ("human-reviewed", "reviewed by a human", "dual-reviewed",
# "independent verification complete"), so the truthful negations the manifest does make -- "no
# human dual-review", "no human review", "independent verification is pending" -- never match.
_UNSUPPORTED_HUMAN_CLAIMS = (
    r"human[-\s]*authored",
    r"hand[-\s]*authored",
    r"human[-\s]*written",
    r"human[-\s]*labell?ed",
    r"human[-\s]*reviewed",
    r"reviewed by (?:a |the )?(?:human|expert|annotator)s?",
    r"verified by (?:a |the )?(?:human|expert|annotator)s?",
    r"dual[-\s]*reviewed",
    r"independently verified",
    r"independent verification (?:is |was |has been )?"
    r"(?:complete|completed|performed|passed|done|finished)",
)


def test_manifest_makes_no_unsupported_human_authorship_or_review_claim():
    """The committed manifest text never asserts human authorship/labelling/review/verification it
    did not do, while still stating the true automated-curation, no-human-review, pending-independent
    -verification status. Reads the committed file directly -- fully offline, no loader mutation.
    """
    text = (GOLD_ROOT / MANIFEST_FILE).read_text(encoding="utf-8").lower()
    for pattern in _UNSUPPORTED_HUMAN_CLAIMS:
        match = re.search(pattern, text)
        assert match is None, f"manifest makes an unsupported human claim: {match and match.group(0)!r}"

    # The honest status must still be spelled out, not merely left un-asserted.
    manifest = load_manifest()
    labeling = json.dumps(manifest.labeling).lower()
    assert "automated" in labeling, "adjudication must name the automated curation pass"
    assert "no human" in labeling, "the no-human-review status must be stated as a negation"
    assert "independent verification" in labeling and "pending" in labeling, \
        "pending independent verification must be stated"
    # The description is the field the defect lived in: it must own the automated provenance.
    assert "automated" in manifest.metadata.get("description", "").lower()
    assert manifest.metadata.get("provenance") == "original_synthetic"


# --- isolation, gating, determinism --------------------------------------------------------
def test_holdout_is_gated_and_excluded_from_tuning(corpus):
    tuning = load_tuning_corpus()
    assert tuning.includes_holdout is False
    assert len(tuning.mentions) == SPLIT_COUNTS["train"] + SPLIT_COUNTS["development"] == 150
    assert not tuning.mentions_for(SPLIT_FINAL_HOLDOUT)
    with pytest.raises(EntityGoldValidationError, match="gated"):
        load_split(split=SPLIT_FINAL_HOLDOUT)
    opted_in = load_split(split=SPLIT_FINAL_HOLDOUT, allow_holdout=True)
    assert len(opted_in) == SPLIT_COUNTS[SPLIT_FINAL_HOLDOUT] == 50


def test_no_document_or_leakage_group_crosses_splits(corpus):
    fingerprint_splits: dict[str, set[str]] = {}
    group_splits: dict[str, set[str]] = {}
    for m in corpus.mentions:
        fingerprint_splits.setdefault(m.fingerprint, set()).add(m.split)
        group_splits.setdefault(m.leakage_group, set()).add(m.split)
    assert all(len(splits) == 1 for splits in fingerprint_splits.values())
    assert all(len(splits) == 1 for splits in group_splits.values())
    # Every committed document is unique, and holdout documents are physically distinct.
    assert len(fingerprint_splits) == TOTAL_COUNT
    assert len({document_fingerprint(m.document_text) for m in corpus.mentions}) == TOTAL_COUNT


def test_ids_are_sorted_and_loading_is_deterministic(corpus):
    for split in SPLIT_ORDER:
        ids = [m.mention_id for m in corpus.mentions_for(split)]
        assert ids == sorted(ids)
    assert len({m.mention_id for m in corpus.mentions}) == TOTAL_COUNT
    again = load_corpus()
    assert [m.mention_id for m in again.mentions] == [m.mention_id for m in corpus.mentions]
    assert [m.fingerprint for m in again.mentions] == [m.fingerprint for m in corpus.mentions]
    assert again.report() == corpus.report()


def test_manifest_and_catalog_loaders_agree_with_corpus(corpus):
    manifest = load_manifest()
    assert [name for name, _, _ in manifest.splits] == list(SPLIT_ORDER)
    catalog = load_target_catalog()
    assert [t.target_id for t in catalog.targets] == sorted(t.target_id for t in catalog.targets)
    assert len(catalog.targets) == len(corpus.catalog.targets)


# --- the whole audit runs with no network at all -------------------------------------------
def test_full_load_and_conversion_use_no_network(monkeypatch):
    def _blocked(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network access attempted during an offline gold load")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    offline = load_corpus()
    assert offline.quotas.total == 200
    for m in offline.mentions:
        m.to_stage2_inputs(offline.catalog)
    for t in offline.catalog.targets:
        EntityProfile(**t.profile_row())
