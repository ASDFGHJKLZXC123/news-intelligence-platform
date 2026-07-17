"""The committed historical alert-episode gold v1 dataset: audits the real data, fully offline.

These tests load the data actually committed under ``evaluation/gold/alert_episodes/v1`` (not a
fixture) and assert the Stage-8 curation contract holds on it: the exact 48/24/12/12 split shape and
the stronger per-window/per-split balance the milestone asked for, strong coverage of risk types,
targets, geographies and horizons, that every onset field is strictly as-of and every citation is an
authoritative https reference dated 2026-07-16, that the manifest is honest about its automated,
unreviewed, verification-pending status, and -- the point of a null-model gold set -- that loading it
never opens the ADR 0010 gate. Nothing here touches the network, a database or an LLM: one test
enforces that by blocking socket connections during a full load and DTO conversion.
"""

from __future__ import annotations

import datetime
import inspect
import json
import re
import socket
from collections import Counter
from urllib.parse import urlparse

import pytest

import services.evaluation.alert_episode_gold as mod
from services.alerts.experimental import REQUIRED_WINDOWS, ExperimentalGate, ScoreBasis
from services.analogies.corpus import find_future_years, find_hindsight_phrases
from services.crisis_model import evaluation as crisis_eval
from services.evaluation.alert_episode_gold import (
    CASES_PER_WINDOW,
    GOLD_ROOT,
    HORIZON_ORDER,
    MANIFEST_FILE,
    OUTCOME_CLASSES,
    SOURCE_KINDS,
    SPLIT_COUNTS,
    SPLIT_FINAL_HOLDOUT,
    SPLIT_ORDER,
    TOTAL_COUNT,
    WINDOW_BOUNDS,
    AlertEpisodeGoldValidationError,
    OutcomeEvaluationInputs,
    WindowLabelInventory,
    load_corpus,
    load_manifest,
    load_split,
    load_tuning_corpus,
    per_split_window_coverage,
)

ACCESS_DATE = datetime.date(2026, 7, 16)

#: The test-maintained allowlist of authoritative source hosts (regulators, central banks,
#: governments, official filings and statistical/IGO bodies). Every committed ref must resolve to
#: one of these; a media, encyclopaedia or blog host would fail the subset assertion below.
AUTHORITATIVE_DOMAINS = frozenset({
    "sec.gov", "federalreserve.gov", "fred.stlouisfed.org", "newyorkfed.org", "fdic.gov",
    "fdicoig.gov", "bis.org", "imf.org", "worldbank.org", "data.worldbank.org",
    "documents.worldbank.org", "oecd.org", "nber.org", "bea.gov", "home.treasury.gov", "cbo.gov",
    "gao.gov", "ec.europa.eu", "ecb.europa.eu", "esma.europa.eu", "bankofengland.co.uk",
    "publications.parliament.uk", "consilium.europa.eu", "bundesbank.de", "bde.es", "bafin.de",
    "finma.ch", "snb.ch", "sedlabanki.is", "cb.is", "who.int", "govinfo.gov",
})

#: Hosts that must never appear: news, wikis and commentary are not primary provenance.
MEDIA_DENYLIST = frozenset({
    "wikipedia.org", "en.wikipedia.org", "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "nytimes.com", "cnbc.com", "theguardian.com", "bbc.co.uk", "bbc.com", "forbes.com",
    "businessinsider.com", "medium.com", "seekingalpha.com", "investopedia.com", "marketwatch.com",
    "cnn.com", "aljazeera.com",
})

#: Institutional hosts that must be exercised at all, so the three windows are substantively sourced.
REQUIRED_SOURCE_HOSTS = frozenset({
    "imf.org", "sec.gov", "federalreserve.gov", "fdic.gov", "nber.org", "fred.stlouisfed.org",
    "bis.org",
})

#: Recognisable, source-datable events each window must actually contain (anchors the curation).
WINDOW_ANCHORS = {
    "2007-2009": {"lehman-brothers-run", "northern-rock-run", "bear-stearns-repo-run",
                  "greece-deficit-revision", "iceland-banking-collapse"},
    "2020": {"us-covid-recession", "wirecard-insolvency", "argentina-2020-default",
             "lebanon-2020-default", "treasury-market-post-backstop-2020"},
    "2023": {"silicon-valley-bank-run", "signature-bank-closure",
             "credit-suisse-confidence-collapse", "first-republic-deposit-flight",
             "silvergate-wind-down"},
}


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


# --- shape, balance and counts -------------------------------------------------------------
def test_committed_corpus_loads_with_exact_counts(corpus):
    assert corpus.quotas.total == TOTAL_COUNT == 48
    assert corpus.includes_holdout is True
    assert corpus.quotas.positives == 24 and corpus.quotas.controls == 24
    assert corpus.quotas.by_split == {"train": 24, "development": 12, "final_holdout": 12}
    for split in SPLIT_ORDER:
        assert len(corpus.cases_for(split)) == SPLIT_COUNTS[split]
    for window in REQUIRED_WINDOWS:
        bucket = corpus.quotas.by_window[window]
        assert bucket == {"total": CASES_PER_WINDOW, "positive": 8, "control": 8}


def test_exact_per_split_per_window_balance(corpus):
    coverage = per_split_window_coverage(corpus.cases)
    expected = {"train": (4, 4), "development": (2, 2), "final_holdout": (2, 2)}
    for split in SPLIT_ORDER:
        for window in REQUIRED_WINDOWS:
            bucket = coverage[(split, window)]
            assert (bucket["positive"], bucket["control"]) == expected[split], (split, window, bucket)


def test_strong_coverage_of_types_targets_geographies_horizons(corpus):
    assert corpus.quotas.risk_types >= 5
    assert corpus.quotas.targets >= 12
    assert corpus.quotas.geographies >= 10
    # Every canonical horizon is exercised meaningfully -- at least six cases each.
    horizon_counts = Counter(c.selected_horizon for c in corpus.cases)
    assert set(horizon_counts) == set(HORIZON_ORDER)
    for horizon in HORIZON_ORDER:
        assert horizon_counts[horizon] >= 6, (horizon, horizon_counts[horizon])
    # The milestone families are all present and defensible.
    assert {"banking", "market_liquidity", "company", "sovereign", "recession"} <= {
        c.risk_type for c in corpus.cases}


def test_at_least_twelve_positives_have_measurable_lead_time(corpus):
    strict = [c for c in corpus.cases
              if c.is_positive and c.outcome.actual_start_date
              and c.outcome.actual_start_date > c.evaluation_as_of]
    assert len(strict) >= 12


def test_each_window_is_substantive(corpus):
    for window in REQUIRED_WINDOWS:
        window_cases = corpus.cases_for_window(window)
        assert len(window_cases) == CASES_PER_WINDOW
        assert len({c.target.id for c in window_cases}) >= 8
        assert len({c.risk_type for c in window_cases}) >= 3
        ids = {c.case_id for c in window_cases}
        assert WINDOW_ANCHORS[window] <= ids, (window, WINDOW_ANCHORS[window] - ids)


# --- horizon bucket / outcome round-trip and the threshold-free DTO ------------------------
def test_every_case_round_trips_bounds_outcome_and_timing(corpus):
    for c in corpus.cases:
        assert len(c.evaluation_horizons) == 1
        bounds = c.selected_bounds
        assert c.evaluation_end == bounds.upper
        assert WINDOW_BOUNDS[c.window][0] <= c.evaluation_as_of <= WINDOW_BOUNDS[c.window][1]
        assert c.label_available_on >= c.evaluation_end
        if c.is_positive:
            start = c.outcome.actual_start_date
            assert start is not None and bounds.contains(start)
            assert c.outcome.classes and (set(c.outcome.classes) - {"contained", "recovery"})
            assert set(c.outcome.classes) <= OUTCOME_CLASSES
            assert c.label_available_on >= start
            if c.outcome.actual_end_date is not None:
                assert c.label_available_on >= c.outcome.actual_end_date
            assert c.control_rationale is None
        else:
            assert c.outcome.occurred is False
            assert c.outcome.actual_start_date is None and c.outcome.actual_end_date is None
            assert c.outcome.classes == ()
            assert c.control_rationale and len(c.control_rationale) >= 40


def test_evaluation_inputs_are_threshold_free_and_match_evaluate_prediction(corpus):
    params = set(inspect.signature(crisis_eval.evaluate_prediction).parameters)
    for c in corpus.cases:
        dto = c.evaluation_inputs()
        assert isinstance(dto, OutcomeEvaluationInputs)
        kwargs = dto.as_kwargs()
        assert set(kwargs) == {"horizon", "evaluation_date", "actual_outcome", "actual_start_date"}
        assert "threshold" not in kwargs
        # Every DTO field is a genuine argument of the Stage-9 scorer; none is a tuning knob.
        assert set(kwargs) <= params
        assert dto.horizon == c.selected_horizon
        assert dto.evaluation_date == c.evaluation_end
        assert dto.actual_outcome is c.outcome.occurred
        assert dto.actual_start_date == c.outcome.actual_start_date
    # Asking for a horizon the case never labelled is refused.
    positive = next(c for c in corpus.cases if c.is_positive)
    other = next(h for h in HORIZON_ORDER if h != positive.selected_horizon)
    with pytest.raises(AlertEpisodeGoldValidationError, match="selected horizon"):
        positive.evaluation_inputs(other)


# --- onset is strictly as-of and never leaks the outcome -----------------------------------
def test_onset_indicators_are_as_of_and_source_resolved(corpus):
    for c in corpus.cases:
        supports = {ref.id: set(ref.supports) for ref in c.source_refs}
        assert len(c.onset.indicators) >= 2
        for indicator in c.onset.indicators:
            assert c.onset.observation_start <= indicator.observed_on <= c.evaluation_as_of
            assert indicator.source_ids
            for sid in indicator.source_ids:
                assert "onset" in supports.get(sid, set()), (c.case_id, sid)
        # Outcome/control provenance resolves to the matching support, never borrowed across it.
        wanted = "outcome" if c.is_positive else "control"
        assert c.outcome.source_ids
        for sid in c.outcome.source_ids:
            assert wanted in supports.get(sid, set()), (c.case_id, sid, wanted)


def test_onset_text_has_no_hindsight_future_or_outcome_leakage(corpus):
    for c in corpus.cases:
        texts = [c.onset.summary] + [ind.description for ind in c.onset.indicators]
        outcome_dates = [d.isoformat() for d in
                         (c.outcome.actual_start_date, c.outcome.actual_end_date) if d]
        titles = [ref.title.lower() for ref in c.source_refs if len(ref.title) >= 12]
        for text in texts:
            lowered = text.lower()
            assert 8 <= len(text) <= mod.MAX_TEXT_CHARS
            assert find_hindsight_phrases(text) == (), (c.case_id, text)
            assert find_future_years(text, c.evaluation_as_of.year) == (), (c.case_id, text)
            for cls in c.outcome.classes:
                assert cls not in lowered and cls.replace("_", " ") not in lowered, (c.case_id, cls)
            for iso in outcome_dates:
                assert iso not in text, (c.case_id, iso)
            for title in titles:
                assert title not in lowered, (c.case_id, title)
        assert len(c.onset.summary) >= mod.MIN_SUMMARY_CHARS


# --- provenance: authoritative, dated, non-placeholder https only --------------------------
def test_every_ref_is_authoritative_https_dated_and_sorted(corpus):
    assert AUTHORITATIVE_DOMAINS.isdisjoint(MEDIA_DENYLIST)
    used_hosts: set[str] = set()
    for c in corpus.cases:
        ids = [ref.id for ref in c.source_refs]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        for ref in c.source_refs:
            host = _host(ref.url)
            assert urlparse(ref.url).scheme == "https", ref.url
            assert host and not any(host == f or host.endswith(f".{f}") for f in mod.FAKE_URL_HOSTS)
            assert ref.accessed == ACCESS_DATE, (c.case_id, ref.accessed)
            assert ref.kind in SOURCE_KINDS
            assert ref.title.strip() and ref.publisher.strip()
            base = host[4:] if host.startswith("www.") else host
            assert base in AUTHORITATIVE_DOMAINS, (c.case_id, base)
            assert base not in MEDIA_DENYLIST
            assert ref.supports and set(ref.supports) <= mod.SUPPORT_KINDS
            used_hosts.add(base)
    # The three windows are substantively sourced, not stubbed against one host.
    assert REQUIRED_SOURCE_HOSTS <= used_hosts
    assert used_hosts <= AUTHORITATIVE_DOMAINS


# --- determinism: sorted, unique, non-leaking across splits --------------------------------
def test_ids_fingerprints_groups_and_targets_do_not_leak(corpus):
    for split in SPLIT_ORDER:
        ids = [c.case_id for c in corpus.cases_for(split)]
        assert ids == sorted(ids)
    assert len({c.case_id for c in corpus.cases}) == TOTAL_COUNT
    assert len({c.fingerprint for c in corpus.cases}) == TOTAL_COUNT
    # Distinct evaluation units: no episode group or (risk_type, target) pair straddles a split.
    group_splits: dict[str, set[str]] = {}
    key_splits: dict[tuple[str, str], set[str]] = {}
    for c in corpus.cases:
        group_splits.setdefault(c.episode_group, set()).add(c.split)
        key_splits.setdefault((c.risk_type, c.target.id), set()).add(c.split)
    assert all(len(v) == 1 for v in group_splits.values())
    assert all(len(v) == 1 for v in key_splits.values())
    assert len({c.target.id for c in corpus.cases}) == TOTAL_COUNT


def test_loading_is_deterministic(corpus):
    again = load_corpus()
    assert [c.case_id for c in again.cases] == [c.case_id for c in corpus.cases]
    assert again.report() == corpus.report()


# --- manifest / provenance honesty ---------------------------------------------------------
_UNSUPPORTED_HUMAN_CLAIMS = (
    r"human[-\s]*authored", r"hand[-\s]*authored", r"human[-\s]*written",
    r"human[-\s]*labell?ed", r"human[-\s]*reviewed",
    r"reviewed by (?:a |the )?(?:human|expert|annotator)s?",
    r"verified by (?:a |the )?(?:human|expert|annotator)s?",
    r"dual[-\s]*reviewed", r"independently verified",
    r"independent (?:historical )?verification (?:is |was |has been )?"
    r"(?:complete|completed|performed|passed|done|finished)",
)

# Language that would falsely claim the null-model gate is open or the backtest has been won.
_UNSUPPORTED_GATE_CLAIMS = (
    r"gate (?:is |has been |was )?(?:open|opened|released|cleared)",
    r"backtest (?:passed|complete|succeeded)",
    r"beats? the baseline", r"ready (?:to|for) (?:ship|release|production)",
    r"released for production", r"passing metrics",
)


def test_manifest_makes_no_unsupported_human_or_gate_claim():
    text = (GOLD_ROOT / MANIFEST_FILE).read_text(encoding="utf-8").lower()
    for pattern in _UNSUPPORTED_HUMAN_CLAIMS + _UNSUPPORTED_GATE_CLAIMS:
        match = re.search(pattern, text)
        assert match is None, f"manifest makes an unsupported claim: {match and match.group(0)!r}"


def test_manifest_states_the_honest_automated_status():
    manifest = load_manifest()
    assert manifest.review["state"] == "automated_contract_validation_only"
    assert manifest.review["human_verified"] is False
    assert manifest.review["independent_historical_verification"] is False
    assert manifest.risk_types == tuple(sorted(manifest.risk_types))
    assert manifest.horizons == HORIZON_ORDER
    blob = json.dumps(manifest.metadata).lower() + json.dumps(manifest.labeling).lower()
    assert "automated" in blob
    assert "no human" in blob
    assert "independent" in blob and "pending" in blob
    assert "paraphrase" in manifest.license["note"].lower()
    assert "automated" in manifest.metadata.get("description", "").lower()


# --- gating, tuning slice, and the label inventory that measures nothing --------------------
def test_holdout_is_gated_and_excluded_from_tuning():
    tuning = load_tuning_corpus()
    assert tuning.includes_holdout is False
    assert len(tuning.cases) == SPLIT_COUNTS["train"] + SPLIT_COUNTS["development"] == 36
    assert not tuning.cases_for(SPLIT_FINAL_HOLDOUT)
    with pytest.raises(AlertEpisodeGoldValidationError, match="gated"):
        load_split(split=SPLIT_FINAL_HOLDOUT)
    opted_in = load_split(split=SPLIT_FINAL_HOLDOUT, allow_holdout=True)
    assert len(opted_in) == SPLIT_COUNTS[SPLIT_FINAL_HOLDOUT] == 12


def test_label_inventory_is_counts_only(corpus):
    inventory = corpus.label_inventory()
    assert [item.window for item in inventory] == list(REQUIRED_WINDOWS)
    for item in inventory:
        assert isinstance(item, WindowLabelInventory)
        assert item.labeled_cases == 16 and item.positives == 8 and item.controls == 8
        # It names what to score, never a measured result.
        assert not hasattr(item, "candidate_precision")
        assert not hasattr(item, "baseline_precision")
        assert not hasattr(item, "lead_time_days")


def test_loading_the_dataset_never_opens_the_gate(corpus):
    # The loader constructs no gate and no evidence, and the composite gate stays fail-closed.
    assert not hasattr(mod, "ExperimentalGate")
    assert not hasattr(mod, "WindowEvidence")
    composite = ExperimentalGate().decide(ScoreBasis.COMPOSITE)
    assert composite.released is False and composite.experimental is True
    assert ExperimentalGate().decide(ScoreBasis.SINGLE_SIGNAL).released is True


# --- the whole audit runs with no network at all -------------------------------------------
# --- provenance is specific: no generic landing pages or dead/whitespace paths ----------
#: URL shapes that are too generic (or dead) to directly support a dated outcome/control claim.
#: The Stage-8-item-5 repair replaced every one of these with a case-specific deep link; these
#: patterns are the guard that keeps them out. A generic company filing list, bare country portal,
#: press-release index, legacy dead path or embedded whitespace all fail here.
GENERIC_URL_PATTERNS = (
    r"/cgi-bin/browse-edgar",                         # EDGAR company filing list, not a document
    r"imf\.org/en/[Cc]ountries/[A-Za-z]{2,3}/?$",     # bare IMF country landing
    r"data\.worldbank\.org/country/",                 # World Bank country portal
    r"worldbank\.org/en/country/[^/]+/?$",            # World Bank country home
    r"/press/press-releases/?$",                       # bare Council/EU press-release index
    r"/press/pr/date/\d{4}/html/index",               # bare ECB annual press index
    r"federalreserve\.gov/newsevents/pressreleases/2008press\.htm",   # dead Fed 2008 index
    r"federalreserve\.gov/regreform/reform-mmlf\.htm",                # dead Fed program page
    r"bafin\.de/EN/PublikationenDaten/publikationendaten_node",       # dead BaFin node
    r"bankofengland\.co\.uk/financial-stability-report/?$",           # bare BoE FSR landing
    r"fdicoig\.gov/reports/?$",                        # bare FDIC OIG reports index
    r"govinfo\.gov/app/details/",                      # GovInfo JS-shell landing, no fetchable document
    r"newyorkfed\.org/markets/treasury-market-functioning",           # dead NY Fed path (soft-404)
    r"\s",                                             # any embedded whitespace
)
_GENERIC_URL_RES = tuple(re.compile(p) for p in GENERIC_URL_PATTERNS)


def _matches_generic(url: str) -> str | None:
    for pat in _GENERIC_URL_RES:
        if pat.search(url):
            return pat.pattern
    return None


def _is_deep_link(url: str) -> bool:
    """A specific document/deep link: a multi-segment path or an explicit query, not a bare landing."""
    parsed = urlparse(url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    return len(segments) >= 2 or bool(parsed.query)


def test_no_committed_source_url_is_generic_or_dead(corpus):
    offenders = [(c.case_id, ref.id, ref.url, _matches_generic(ref.url))
                 for c in corpus.cases for ref in c.source_refs
                 if _matches_generic(ref.url)]
    assert not offenders, offenders


def test_every_outcome_and_control_case_has_a_specific_deep_link(corpus):
    # The claim that decides the label -- a positive's outcome or a control's non-occurrence -- must
    # rest on at least one case-specific deep link, never only a generic portal.
    for c in corpus.cases:
        wanted = "outcome" if c.is_positive else "control"
        deep = [ref for ref in c.source_refs
                if wanted in ref.supports and _is_deep_link(ref.url) and not _matches_generic(ref.url)]
        assert deep, (c.case_id, wanted, [(r.id, r.url) for r in c.source_refs])


# --- exception-iteration-3 repair: the label-deciding refs point at fetchable documents ----
# The prior verification pass found five records whose cited source was a JS-only shell, a soft-404,
# or a document that did not actually support the labeled claim. These offline checks pin each repair
# to the specific authoritative document path so the exact regression cannot silently return. They do
# not assert historical truth -- only that the label-deciding citation is the intended live document.
def _label_urls(case) -> list[str]:
    wanted = "outcome" if case.is_positive else "control"
    return [ref.url for ref in case.source_refs if wanted in ref.supports]


def test_verifier_repaired_records_cite_the_intended_documents(corpus):
    byid = {c.case_id: c for c in corpus.cases}
    # The Financial Crisis Inquiry Report is cited as the direct PDF, never the contentless app shell.
    fcic = "https://www.govinfo.gov/content/pkg/GPO-FCIC/pdf/GPO-FCIC.pdf"
    for cid in ("bear-stearns-repo-run", "aig-collateral-calls"):
        urls = [ref.url for ref in byid[cid].source_refs]
        assert fcic in urls and not any("/app/details/" in u for u in urls), cid
    # Treasury control rests on the Fed's post-period Financial Stability Report, not the dead NY Fed path.
    assert any("financial-stability-report-20201109.pdf" in u
               for u in _label_urls(byid["treasury-market-post-backstop-2020"]))
    # Reserve Primary Fund's break is sourced to the SEC release that names the fund by name.
    assert any("/news/press/2009/2009-104.htm" in u
               for u in _label_urls(byid["reserve-primary-money-fund"]))
    # Boeing's survival cites the actual $25bn offering prospectus and a within-window report.
    assert any("d848621d424b2.htm" in u for u in _label_urls(byid["boeing-avoids-bankruptcy-2020"]))
    # Carvana's control rests on the settled exchange 8-K and a 2024 filing, not the July earnings doc.
    carvana = _label_urls(byid["carvana-avoids-bankruptcy-2023"])
    assert any("cvna-20230830.htm" in u for u in carvana)
    assert any("cvna-20240630.htm" in u for u in carvana)
    assert not any("cvna-20230719.htm" in u for u in carvana)
    # Nigeria's non-default rests on a post-evaluation IMF report (2022), not only the April-2020 RFI.
    assert any("/CR/2022/English/1NGAEA2022001.ashx" in u
               for u in _label_urls(byid["nigeria-no-default-2020"]))


# --- exception-iteration-4 repair: Lehman's fields map to the documents that contain them ---
def test_lehman_repair_pins_onset_and_outcome_to_the_intended_sec_documents(corpus):
    # This is an offline regression guard for the curated mapping, not proof of historical truth.
    case = next(c for c in corpus.cases if c.case_id == "lehman-brothers-run")
    assert case.evaluation_as_of == datetime.date(2008, 9, 10)
    assert case.evaluation_end == datetime.date(2009, 3, 10)
    assert case.label_available_on == datetime.date(2009, 4, 9)
    assert case.onset.observation_start == datetime.date(2008, 9, 10)
    assert case.outcome.actual_start_date == datetime.date(2008, 9, 15)
    assert case.outcome.actual_end_date is None
    assert (case.outcome.actual_start_date - case.evaluation_as_of).days == 5
    assert case.outcome.classes == ("failure",)
    assert case.outcome.source_ids == ("s2",)

    onset_exhibit = (
        "https://www.sec.gov/Archives/edgar/data/806085/000110465908057829/"
        "a08-22764_2ex99d1.htm"
    )
    outcome_8k = (
        "https://www.sec.gov/Archives/edgar/data/806085/000110465908059632/"
        "a08-22764_48k.htm"
    )
    mismatched_8k = (
        "https://www.sec.gov/Archives/edgar/data/806085/000110465908057829/"
        "a08-22764_28k.htm"
    )
    refs = {ref.id: ref for ref in case.source_refs}
    assert set(refs) == {"s1", "s2"}
    assert refs["s1"].url == onset_exhibit
    assert refs["s1"].supports == ("onset",)
    assert refs["s2"].url == outcome_8k
    assert refs["s2"].supports == ("outcome",)
    assert mismatched_8k not in {ref.url for ref in case.source_refs}

    readings = {
        indicator.key: (
            indicator.description,
            indicator.observed_on,
            indicator.value,
            indicator.unit,
            indicator.source_ids,
        )
        for indicator in case.onset.indicators
    }
    assert readings == {
        "preliminary_q3_net_loss": (
            "Estimated fiscal third-quarter net loss announced with preliminary results",
            datetime.date(2008, 9, 10),
            3.9,
            "USD_billion",
            ("s1",),
        ),
        "gross_mark_to_market_adjustments": (
            "Estimated gross negative mark-to-market adjustments on assets for the fiscal third quarter",
            datetime.date(2008, 9, 10),
            7.8,
            "USD_billion",
            ("s1",),
        ),
    }

    onset_text = " ".join(
        [case.onset.summary, *(indicator.description for indicator in case.onset.indicators)]
    ).lower()
    for stale_claim in ("korea development bank", "strategic investor", "45 percent", "45%"):
        assert stale_claim not in onset_text
    assert not any(
        indicator.value == 45 and indicator.unit == "percent"
        for indicator in case.onset.indicators
    )


# --- date-normalization convention (the Stage-8-item-5 FAIL 3 repair) ----------------------
def test_recession_dates_follow_the_month_first_normalization_convention(corpus):
    manifest_text = (GOLD_ROOT / MANIFEST_FILE).read_text(encoding="utf-8").lower()
    assert "representation convention" in manifest_text
    assert "first day of that labeled month" in manifest_text

    byid = {c.case_id: c for c in corpus.cases}
    # NBER dates the US downturn only to a month; the stored start is the first of that month.
    us = byid["us-covid-recession"]
    assert us.outcome.actual_start_date == datetime.date(2020, 2, 1)
    assert us.outcome.actual_start_date.day == 1
    assert us.outcome.actual_end_date is None
    # Italy is likewise dated to its labeled onset month, with the same first-day convention.
    it = byid["italy-covid-recession"]
    assert it.outcome.actual_start_date == datetime.date(2020, 3, 1)
    assert it.outcome.actual_start_date.day == 1
    assert it.outcome.actual_end_date is None
    # No spurious day precision was smuggled back into another recession outcome.
    for c in corpus.cases:
        if c.risk_type == "recession" and c.is_positive:
            assert c.outcome.actual_start_date.day == 1, (c.case_id, c.outcome.actual_start_date)


# --- Treasury control is a post-stabilization market-liquidity bucket (FAIL 4) -------------
def test_treasury_control_is_post_stabilization_not_dash_for_cash(corpus):
    byid = {c.case_id: c for c in corpus.cases}
    assert "treasury-dash-for-cash" not in byid
    t = byid["treasury-market-post-backstop-2020"]
    assert t.split == "train" and t.window == "2020"
    assert t.label == "control" and t.risk_type == "market_liquidity"
    assert not t.outcome.occurred and t.outcome.classes == ()
    # Onset is observed after the Fed's late-March 2020 stabilization action, not during the seizure.
    assert t.evaluation_as_of >= datetime.date(2020, 4, 1)
    assert t.onset.observation_start >= datetime.date(2020, 3, 23)
    # The onset describes residual/alert conditions, not the market having "stopped trading".
    assert "stopped trading" not in t.onset.summary.lower()


# --- the March 2023 contagion family lives entirely in train (FAIL 5) ----------------------
MARCH_2023_FAMILY = frozenset({
    "silicon-valley-bank-run", "signature-bank-closure", "first-republic-deposit-flight",
    "credit-suisse-confidence-collapse", "western-alliance-survives-2023",
    "charles-schwab-survives-2023", "zions-survives-2023", "comerica-survives-2023",
})


def test_march_2023_contagion_family_is_wholly_train(corpus):
    byid = {c.case_id: c for c in corpus.cases}
    for case_id in MARCH_2023_FAMILY:
        assert byid[case_id].split == "train", (case_id, byid[case_id].split)
    # The held-out and development 2023 cases are intentionally different families.
    for split in ("development", "final_holdout"):
        assert MARCH_2023_FAMILY.isdisjoint({c.case_id for c in corpus.cases_for(split)})
    # Silvergate is a distinct crypto-exchange-exposure family and stays held out.
    assert byid["silvergate-wind-down"].split == "final_holdout"


def test_manifest_flags_family_sensitivity_for_shared_macro_shocks():
    text = (GOLD_ROOT / MANIFEST_FILE).read_text(encoding="utf-8").lower()
    assert "family sensitivity" in text


def test_full_load_and_conversion_use_no_network(monkeypatch):
    def _blocked(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("network access attempted during an offline gold load")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    offline = load_corpus()
    assert offline.quotas.total == 48
    for c in offline.cases:
        c.evaluation_inputs()
    assert ExperimentalGate().decide(ScoreBasis.COMPOSITE).released is False
