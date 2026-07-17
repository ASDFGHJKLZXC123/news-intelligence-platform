"""The entity-linking gold v1 contract: schema, loaders, validators (no database, no network).

The committed gold data does not exist yet, so these tests build a *valid* 200-mention dataset in
``tmp_path`` -- catalog plus three split files that meet every quota -- and assert the loaders accept
it, then feed the validator deliberately broken copies and assert it refuses each one. A validator
that only ever sees valid input is not evidence of anything.
"""

from __future__ import annotations

import copy
import datetime
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from db.models.core import EntityAlias, EntityProfile, EntityRelationship
from services.evaluation.entity_linking_gold import (
    CASE_TAGS,
    SPLIT_COUNTS,
    SPLIT_FILES,
    SPLIT_ORDER,
    TARGET_UUID_NAMESPACE,
    TOTAL_COUNT,
    EntityGoldValidationError,
    fixture_target_uuid,
    load_corpus,
    load_manifest,
    load_split,
    load_target_catalog,
    load_tuning_corpus,
)
from services.nlp.assertions import AssertionStatus
from services.nlp.mentions import EntityLabel
from services.provider_data.common import normalize_alias, normalize_name

COUNTRIES = ["US", "GB", "DE", "FR", "JP", "CA", "NL", "SE", "CH", "IE", "AU", "KR"]
KINDS = [
    ("link", "ORG", "target-02"),
    ("link", "PRODUCT", "target-00"),
    ("nil", "ORG", None),
    ("nil", "PRODUCT", None),
    ("nil", "PERSON", None),
    ("nil", "GPE", None),
]
Mutation = Callable[[dict[str, Any]], None]


def _canonical(index: int) -> str:
    return f"Meridian {index:02d} Corporation"


def _aliases(index: int) -> list[dict[str, Any]]:
    canonical, short = _canonical(index), f"Meridian {index:02d}"
    items = [
        {"value": canonical, "normalized": normalize_alias(canonical), "alias_type": "legal_name", "source": "sec-edgar"},
        {"value": short, "normalized": normalize_alias(short), "alias_type": "short_name", "source": "wikidata"},
    ]
    return sorted(items, key=lambda a: (a["alias_type"], a["normalized"], a["source"]))


def _target(index: int) -> dict[str, Any]:
    target_id = f"target-{index:02d}"
    canonical = _canonical(index)
    target: dict[str, Any] = {
        "target_id": target_id,
        "fixture_uuid": str(fixture_target_uuid(target_id)),
        "canonical_name": canonical,
        "normalized_name": normalize_name(canonical),
        "entity_type": "product" if index == 0 else "public_company",
        "country": COUNTRIES[index],
        "aliases": _aliases(index),
        "authoritative_refs": [
            {
                "id": "ref-01",
                "title": "Registration statement",
                "publisher": "US Securities and Exchange Commission",
                "url": "https://www.sec.gov/cgi-bin/browse-edgar",
                "accessed": "2023-01-05",
                "kind": "regulatory_filing",
            }
        ],
    }
    if index == 1:
        target["parent"] = {"target_id": "target-00", "relationship_type": "subsidiary"}
    if index == 2:
        target["ticker"] = "MER"
        target["cik"] = "0000012345"
        target["lei"] = "ABCDEFGHIJ1234567890"
        target["website"] = "https://www.meridian-corp.news"
    return target


def _split_of(index: int) -> str:
    return SPLIT_ORDER[0] if index < 100 else SPLIT_ORDER[1] if index < 150 else SPLIT_ORDER[2]


def _mention(index: int) -> dict[str, Any]:
    expected_label, label, target_id = KINDS[index % len(KINDS)]
    surface = _canonical(int(target_id.split("-")[1])) if target_id else f"Northgate {label.title()} {index:03d}"
    document = f"{surface} appeared in market dispatch number {index:03d}."
    published = (datetime.date(2022, 1, 1) + datetime.timedelta(days=index)).isoformat()
    return {
        "mention_id": f"m-{index:03d}",
        "split": _split_of(index),
        "language": "en",
        "article_key": f"article-{index:03d}",
        "document_text": document,
        "sentence": {"index": 0, "text": document, "start": 0, "end": len(document), "previous_text": None, "next_text": None},
        "mention_text": surface,
        "start": 0,
        "end": len(surface),
        "label": label,
        "assertion_status": ["asserted", "denied", "speculative"][index % 3],
        "published_on": published,
        "article_url": None if index % 5 == 0 else f"https://gazette-press.news/story/{index:03d}",
        "context": {
            "source_category": "business" if index % 2 else None,
            "industry_terms": ["software"],
            "location_terms": ["us"],
            "co_mention_surfaces": [],
            "linked_target_ids": ["target-03"] if index == 0 else [],
        },
        "expected_target_id": target_id,
        "expected_label": expected_label,
        "case_tags": [CASE_TAGS[index % len(CASE_TAGS)]],
        "leakage_group": f"grp-{index:03d}",
        "note": "Synthetic gold mention.",
        "provenance": {"kind": "original_synthetic", "created_on": "2024-02-01"},
    }


def _dataset() -> dict[str, Any]:
    manifest = {
        "dataset_id": "entity_linking_gold",
        "schema_version": "v1",
        "language": "en",
        "target_catalog": "targets.json",
        "splits": [{"name": name, "file": SPLIT_FILES[name], "count": SPLIT_COUNTS[name]} for name in SPLIT_ORDER],
        "total_count": TOTAL_COUNT,
        "metadata": {"title": "Entity Linking Gold v1", "description": "Synthetic labelled mentions.", "created_on": "2024-02-01"},
        "labeling": {"guidelines": "ADR 0005 stage 2 labelling guide.", "labelers": ["curator-a", "curator-b"], "adjudication": "dual review with tie-break"},
        "license": {"id": "CC-BY-4.0", "note": "Original synthetic prose."},
    }
    catalog = {"schema_version": "v1", "targets": [_target(i) for i in range(12)]}
    mentions = [_mention(i) for i in range(TOTAL_COUNT)]
    splits = {name: {"schema_version": "v1", "split": name, "mentions": [m for m in mentions if m["split"] == name]} for name in SPLIT_ORDER}
    return {"manifest": manifest, "catalog": catalog, "splits": splits}


def _materialize(tmp_path: Path, mutate: Mutation | None = None) -> Path:
    data = copy.deepcopy(_dataset())
    if mutate is not None:
        mutate(data)
    (tmp_path / "manifest.json").write_text(json.dumps(data["manifest"]), encoding="utf-8")
    (tmp_path / "targets.json").write_text(json.dumps(data["catalog"]), encoding="utf-8")
    for name, payload in data["splits"].items():
        (tmp_path / SPLIT_FILES[name]).write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def _first_link(data: dict[str, Any]) -> dict[str, Any]:
    return data["splits"]["train"]["mentions"][0]


def _first_nil(data: dict[str, Any]) -> dict[str, Any]:
    return next(m for m in data["splits"]["train"]["mentions"] if m["expected_label"] == "nil")


def _copy_document(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Give ``dst`` ``src``'s exact document (and consistent span), so only its fingerprint clashes."""
    for key in ("document_text", "sentence", "mention_text", "start", "end"):
        dst[key] = copy.deepcopy(src[key])


def _problems(tmp_path: Path, mutate: Mutation) -> tuple[str, ...]:
    with pytest.raises(EntityGoldValidationError) as excinfo:
        load_corpus(_materialize(tmp_path, mutate))
    return excinfo.value.problems


# --- the valid dataset loads ---------------------------------------------------------------
def test_valid_corpus_loads_and_reports(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    assert corpus.quotas.total == TOTAL_COUNT == 200
    assert corpus.includes_holdout is True
    assert corpus.quotas.links and corpus.quotas.nils
    assert set(corpus.quotas.by_assertion) == {s.value for s in AssertionStatus}
    assert set(corpus.quotas.by_case_tag) == set(CASE_TAGS)
    assert corpus.quotas.targets >= 12 and corpus.quotas.countries >= 8
    assert corpus.report()["dataset_id"] == "entity_linking_gold"


def test_manifest_and_catalog_load(tmp_path):
    root = _materialize(tmp_path)
    manifest = load_manifest(root)
    assert [name for name, _, _ in manifest.splits] == list(SPLIT_ORDER)
    catalog = load_target_catalog(root)
    assert len(catalog.targets) == 12
    assert [t.target_id for t in catalog.targets] == sorted(t.target_id for t in catalog.targets)


def test_tuning_corpus_excludes_holdout(tmp_path):
    corpus = load_tuning_corpus(_materialize(tmp_path))
    assert corpus.includes_holdout is False
    assert len(corpus.mentions) == SPLIT_COUNTS["train"] + SPLIT_COUNTS["development"] == 150
    assert not corpus.mentions_for("final_holdout")


def test_final_holdout_is_gated(tmp_path):
    root = _materialize(tmp_path)
    with pytest.raises(EntityGoldValidationError, match="gated"):
        load_split(root, "final_holdout")
    opted_in = load_split(root, "final_holdout", allow_holdout=True)
    assert len(opted_in) == SPLIT_COUNTS["final_holdout"] == 50


def test_fixture_uuid_is_deterministic_and_orm_compatible(tmp_path):
    catalog = load_target_catalog(_materialize(tmp_path))
    target = catalog.by_id["target-02"]
    assert target.fixture_uuid == uuid.uuid5(TARGET_UUID_NAMESPACE, "target-02")
    row = target.profile_row()
    profile = EntityProfile(**row)
    assert profile.id == target.fixture_uuid
    aliases = [EntityAlias(**a) for a in target.alias_rows()]
    assert {a.normalized_alias for a in aliases} == {al.normalized for al in target.aliases}
    child = catalog.by_id["target-01"]
    relationship = EntityRelationship(**child.relationship_row())
    assert relationship.parent_entity_id == fixture_target_uuid("target-00")
    assert relationship.child_entity_id == child.fixture_uuid


def test_conversion_to_stage2_inputs_is_deterministic(tmp_path):
    corpus = load_corpus(_materialize(tmp_path))
    catalog = corpus.catalog
    mention = next(m for m in corpus.mentions if m.mention_id == "m-000")
    first = mention.to_stage2_inputs(catalog)
    second = mention.to_stage2_inputs(catalog)
    assert first == second
    entity_mention, context = first
    assert entity_mention.text == mention.mention_text
    assert entity_mention.label is EntityLabel.ORG
    assert entity_mention.sentence.text == mention.sentence.text
    assert entity_mention.sentence.start_char == mention.sentence.start
    assert context.article_key == mention.article_key
    assert context.linked_entity_ids == (fixture_target_uuid("target-03"),)


# --- the validator refuses broken data -----------------------------------------------------
def _set_manifest(data, key, value):
    data["manifest"][key] = value


def _mutations() -> list[tuple[str, Mutation, str]]:
    return [
        ("unknown target field", lambda d: d["catalog"]["targets"][0].__setitem__("bogus", 1), "unknown field"),
        ("missing mention field", lambda d: _first_link(d).pop("note"), "missing field"),
        ("non-integer offset", lambda d: _first_link(d).__setitem__("start", "x"), "start/end must be integers"),
        ("bad manifest split count", lambda d: d["manifest"]["splits"][0].__setitem__("count", 99), "count must be"),
        ("dropped mention", lambda d: d["splits"]["train"]["mentions"].pop(), "expected"),
        ("unsorted catalog", lambda d: d["catalog"]["targets"].reverse(), "sorted by target_id"),
        ("unsorted mentions", lambda d: d["splits"]["train"]["mentions"].reverse(), "sorted by mention_id"),
        ("wrong fixture uuid", lambda d: d["catalog"]["targets"][0].__setitem__("fixture_uuid", str(uuid.uuid4())), "fixture_uuid must be"),
        ("bad normalized name", lambda d: d["catalog"]["targets"][0].__setitem__("normalized_name", "WRONG"), "normalized_name must equal"),
        ("bad alias normalization", lambda d: d["catalog"]["targets"][0]["aliases"][0].__setitem__("normalized", "wrong"), "normalize_alias"),
        ("unknown alias source", lambda d: d["catalog"]["targets"][0]["aliases"][0].__setitem__("source", "myspace"), "unknown source"),
        ("inverted alias dates", lambda d: d["catalog"]["targets"][0]["aliases"][0].update({"valid_from": "2020-01-01", "valid_to": "2010-01-01"}), "follows valid_to"),
        ("self parent", lambda d: d["catalog"]["targets"][0].__setitem__("parent", {"target_id": "target-00", "relationship_type": "subsidiary"}), "its own parent"),
        ("parent cycle", lambda d: d["catalog"]["targets"][0].__setitem__("parent", {"target_id": "target-01", "relationship_type": "subsidiary"}), "cycle"),
        ("missing parent target", lambda d: d["catalog"]["targets"][1]["parent"].__setitem__("target_id", "target-99"), "not in the catalog"),
        ("cross-split leakage group", lambda d: d["splits"]["development"]["mentions"][0].__setitem__("leakage_group", "grp-000"), "spans splits"),
        ("cross-split identical document", lambda d: _copy_document(d["splits"]["development"]["mentions"][0], d["splits"]["train"]["mentions"][0]), "identical document"),
        ("duplicate mention id", lambda d: d["splits"]["development"]["mentions"][0].__setitem__("mention_id", "m-000"), "duplicate mention_id"),
        ("link without target", lambda d: _first_link(d).__setitem__("expected_target_id", "target-99"), "expected_target_id in the catalog"),
        ("linked ref missing target", lambda d: _first_link(d)["context"].__setitem__("linked_target_ids", ["target-99"]), "not in the catalog"),
        ("nil with a target", lambda d: _first_nil(d).__setitem__("expected_target_id", "target-02"), "must have a null"),
        ("positive labelled person", lambda d: _first_link(d).__setitem__("label", "PERSON"), "ORG or PRODUCT"),
        ("mention offset mismatch", lambda d: _first_link(d).__setitem__("end", 3), "is not the mention text"),
        ("sentence text mismatch", lambda d: _first_link(d)["sentence"].__setitem__("text", "WRONG"), "does not match document"),
        ("unsupported language", lambda d: _first_link(d).__setitem__("language", "fr"), "language must be"),
        ("invalid label enum", lambda d: _first_nil(d).__setitem__("label", "COMPANY"), "not a valid EntityLabel"),
        ("invalid expected label", lambda d: _first_link(d).__setitem__("expected_label", "maybe"), "expected_label"),
        ("bad provenance kind", lambda d: _first_link(d)["provenance"].__setitem__("kind", "scraped"), "original_synthetic"),
        ("fake article url", lambda d: _first_link(d).__setitem__("article_url", "https://example.com/x"), "placeholder/fake host"),
        ("over-long quote", lambda d: _first_link(d).__setitem__("document_text", _first_link(d)["mention_text"] + ' said "' + "a" * 300 + '".'), "quoted run"),
        ("duplicate case tags", lambda d: _first_link(d).__setitem__("case_tags", ["ticker", "ticker"]), "duplicate"),
        ("duplicate industry terms", lambda d: _first_link(d)["context"].__setitem__("industry_terms", ["x", "x"]), "duplicate"),
        ("wrong manifest language", lambda d: _set_manifest(d, "language", "fr"), "language must be"),
    ]


_MUTATIONS = _mutations()


@pytest.mark.parametrize("name,mutate,expected", _MUTATIONS, ids=[m[0] for m in _MUTATIONS])
def test_validator_rejects(tmp_path, name, mutate, expected):
    problems = _problems(tmp_path, mutate)
    assert any(expected in problem for problem in problems), (name, problems)
