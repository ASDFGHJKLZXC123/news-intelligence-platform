"""The historical alert-episode gold v1 contract: pure, offline schema, loaders and validators.

ADR 0010 gates every composite alert behind a null-model backtest over the windows
**2007-2009, 2020 and 2023** (:data:`services.alerts.experimental.REQUIRED_WINDOWS`): a composite
alert stays ``experimental`` until it beats ``services.crisis_model.baseline`` on precision *and*
lead time in every one of them. That backtest needs a labelled episode set. This module is the
*contract* for that set -- nothing more. It never runs the backtest, never measures precision or
lead time, never constructs a :class:`~services.alerts.experimental.WindowEvidence`, and never
releases the gate. It loads committed JSON, rejects everything it can before a backtest ever starts,
and hands Stage 9 the outcome-side truth it needs to score a model against.

The dataset is four physical files under ``evaluation/gold/alert_episodes/v1/``: ``manifest.json``
plus one file per split (``train.json`` 24, ``development.json`` 12, ``final_holdout.json`` 12,
total 48). Every window carries exactly 16 cases, and every split carries every window with at least
one positive and one control in it. The prose is original paraphrase; nothing here reproduces a
copyrighted article, and no source *title* is ever surfaced as a model input.

Three rules a human eye slides over are enforced mechanically, because getting them wrong makes a
backtest lie about itself:

* **The holdout is gated.** :func:`load_tuning_corpus` never returns ``final_holdout`` and
  :func:`load_split` refuses it without an explicit opt-in -- tuning a threshold on data you then
  report a held-out number against is the one failure a gold set exists to prevent.
* **Onset and outcome are separate, and onset is strictly as-of.** An onset field may not mention a
  hindsight phrase, a year after ``evaluation_as_of``, the realised outcome's dates or classes, or a
  source title. The outcome is what the future revealed; it is never an input.
* **Labels are not available to the inputs.** ``label_available_on`` sits on or after
  ``evaluation_end``, which sits after ``evaluation_as_of`` -- the label could not have been known
  when the model would have had to predict.
* **One case, one horizon.** A case selects exactly one canonical horizon and is judged only inside
  that horizon's calendar-month bucket (see :func:`horizon_bounds`): ``evaluation_end`` equals the
  bucket's upper bound and a positive's ``actual_start_date`` must fall inside it, with the boundary
  inclusivity fixed so a date lands in exactly one contiguous bucket. Re-using one ``occurred`` label
  across several horizons would call an event that started in ``0_6m`` a positive in ``6_12m`` too,
  so the schema forbids a multi-horizon case outright.

The review state is recorded honestly, not asserted: the committed manifest declares
``automated_contract_validation_only`` and that no human or independent historical verification has
happened yet, and the loader refuses a manifest that claims otherwise.
"""

from __future__ import annotations

import calendar
import datetime
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from db.models.core import EPISODE_OUTCOMES
from db.models.enums import Horizon, RiskType
from services.alerts.experimental import REQUIRED_WINDOWS
from services.analogies.corpus import find_future_years, find_hindsight_phrases
from services.provider_data.common import normalize_name, stable_hash

# --- identifiers (exact v1) ----------------------------------------------------------------
DATASET_ID = "historical_alert_episodes"
SCHEMA_VERSION = "v1"
MANIFEST_FILE = "manifest.json"

SPLIT_TRAIN = "train"
SPLIT_DEVELOPMENT = "development"
SPLIT_FINAL_HOLDOUT = "final_holdout"
#: Physical, deterministic order: what tuning may see comes first, the holdout last.
SPLIT_ORDER = (SPLIT_TRAIN, SPLIT_DEVELOPMENT, SPLIT_FINAL_HOLDOUT)
SPLIT_FILES = {
    SPLIT_TRAIN: "train.json",
    SPLIT_DEVELOPMENT: "development.json",
    SPLIT_FINAL_HOLDOUT: "final_holdout.json",
}
SPLIT_COUNTS = {SPLIT_TRAIN: 24, SPLIT_DEVELOPMENT: 12, SPLIT_FINAL_HOLDOUT: 12}
TOTAL_COUNT = 48
#: The splits a default/tuning consumer may hold; the holdout is deliberately absent.
TUNING_SPLITS = (SPLIT_TRAIN, SPLIT_DEVELOPMENT)

GOLD_ROOT = Path(__file__).resolve().parents[2] / "evaluation" / "gold" / "alert_episodes" / "v1"

# --- windows (ADR 0010, exact bounds) ------------------------------------------------------
#: The backtest windows, with the exact date bounds a case's ``evaluation_as_of`` must fall inside.
WINDOW_BOUNDS: dict[str, tuple[datetime.date, datetime.date]] = {
    "2007-2009": (datetime.date(2007, 1, 1), datetime.date(2009, 12, 31)),
    "2020": (datetime.date(2020, 1, 1), datetime.date(2020, 12, 31)),
    "2023": (datetime.date(2023, 1, 1), datetime.date(2023, 12, 31)),
}
#: 48 cases over 3 windows: each window carries exactly this many.
CASES_PER_WINDOW = TOTAL_COUNT // len(REQUIRED_WINDOWS)

# --- controlled vocabularies ---------------------------------------------------------------
LABEL_POSITIVE = "positive"
LABEL_CONTROL = "control"
LABELS = frozenset({LABEL_POSITIVE, LABEL_CONTROL})

RISK_TYPES = frozenset(risk_type.value for risk_type in RiskType)
#: Canonical horizon order (``db.models.enums.Horizon``): a case selects exactly one of these.
HORIZON_ORDER = tuple(horizon.value for horizon in Horizon)
HORIZON_SET = frozenset(HORIZON_ORDER)
#: Calendar months from ``evaluation_as_of`` to each bucket's *upper* bound. The upper bound is
#: inclusive and is exactly the ``evaluation_end`` a case selecting that horizon must declare.
HORIZON_END_MONTHS = {"0_6m": 6, "6_12m": 12, "12_18m": 18, "within_18m": 18}
#: Calendar months from ``evaluation_as_of`` to each bucket's *lower* bound. ``0_6m`` and
#: ``within_18m`` open at ``as_of`` itself; the tail buckets open at the previous boundary.
HORIZON_START_MONTHS = {"0_6m": 0, "6_12m": 6, "12_18m": 12, "within_18m": 0}
#: Whether each bucket includes its lower bound. The tail buckets (``6_12m``, ``12_18m``) are
#: half-open on the low side so a single onset date lands in exactly one contiguous bucket; every
#: upper bound is closed.
HORIZON_LOWER_INCLUSIVE = {"0_6m": True, "6_12m": False, "12_18m": False, "within_18m": True}

TARGET_TYPES = frozenset({"country", "company", "market", "region", "global"})
#: Region codes that stand in for a genuinely multi-country target, beside ISO-3166 alpha-2.
NAMED_GEOGRAPHIES = frozenset({"GLOBAL", "EU", "EA", "ASIA", "LATAM", "MENA", "AFRICA"})

#: What a case's outcome may be classified as -- the curated episode outcome vocabulary, reused so
#: the two never drift. A positive must carry at least one adverse class; the benign two describe a
#: non-event and can never, on their own, make a case positive.
OUTCOME_CLASSES = frozenset(EPISODE_OUTCOMES)
BENIGN_OUTCOME_CLASSES = frozenset({"contained", "recovery"})
ADVERSE_OUTCOME_CLASSES = OUTCOME_CLASSES - BENIGN_OUTCOME_CLASSES

SOURCE_KINDS = frozenset({"primary", "institutional", "statistical"})
#: What a source may attest to. Every onset/outcome/control ref must resolve to a source declaring
#: the matching support, so provenance cannot be borrowed across the onset/outcome boundary.
SUPPORT_KINDS = frozenset({"onset", "outcome", "control"})

PROVENANCE_KIND = "original_paraphrase"
REVIEW_STATE = "automated_contract_validation_only"

# --- coverage quotas (the contract, not an aspiration) -------------------------------------
MIN_POSITIVES_TOTAL = 24
MIN_CONTROLS_TOTAL = 12
MIN_POSITIVES_PER_WINDOW = 6
MIN_CONTROLS_PER_WINDOW = 4
MIN_RISK_TYPES = 3
MIN_TARGETS = 6
MIN_GEOGRAPHIES = 6
MIN_INDICATORS = 2

# --- text bounds ---------------------------------------------------------------------------
MIN_SUMMARY_CHARS = 40
MAX_TEXT_CHARS = 1500
MAX_QUOTE_CHARS = 200
FAKE_URL_HOSTS = frozenset(
    {"example.com", "example.org", "example.net", "example.edu", "test", "localhost", "invalid",
     "fake.com", "placeholder.com"}
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")
_GEO_ISO = re.compile(r"^[A-Z]{2}$")
_QUOTE_RUN = re.compile(r'"([^"]*)"')


# --- error ---------------------------------------------------------------------------------
class AlertEpisodeGoldValidationError(ValueError):
    """The committed gold set (or a candidate) is not usable. Carries every problem, not the first.

    Raised before any backtest: a bad gold set must never gate a live alert. Each problem names the
    file and case it came from, so a curator can find it without a debugger.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        listed = "\n  - ".join(self.problems)
        super().__init__(f"{len(self.problems)} alert-episode gold problem(s):\n  - {listed}")


# --- helpers -------------------------------------------------------------------------------
def onset_fingerprint(summary: str) -> str:
    """A stable fingerprint of a *normalized* onset summary, for duplicate detection."""
    return stable_hash(normalize_name(summary))


def _add_months(day: datetime.date, months: int) -> datetime.date:
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True, slots=True)
class HorizonBounds:
    """The calendar-month bucket a single horizon judges, with explicit boundary inclusivity.

    The upper bound is always inclusive; ``lower_inclusive`` distinguishes the closed head buckets
    (``0_6m``, ``within_18m``) from the half-open tail buckets (``6_12m``, ``12_18m``), so two
    contiguous buckets never both claim the same boundary date.
    """

    horizon: str
    lower: datetime.date
    lower_inclusive: bool
    upper: datetime.date

    def contains(self, day: datetime.date) -> bool:
        """Whether ``day`` falls in the bucket, honouring the exact lower/upper inclusivity."""
        after_lower = self.lower <= day if self.lower_inclusive else self.lower < day
        return after_lower and day <= self.upper

    @property
    def interval(self) -> str:
        """Human-readable interval such as ``(2007-02-15, 2007-08-15]`` -- for error messages."""
        left = "[" if self.lower_inclusive else "("
        return f"{left}{self.lower}, {self.upper}]"


def horizon_bounds(as_of: datetime.date, horizon: str) -> HorizonBounds:
    """The deterministic bucket a horizon judges, relative to ``as_of`` (calendar-month arithmetic).

    Boundary inclusivity is fixed so a single date lands in exactly one contiguous bucket:

    * ``0_6m``       -> ``[as_of, as_of+6m]``      lower inclusive, upper inclusive
    * ``6_12m``      -> ``(as_of+6m, as_of+12m]``  lower exclusive, upper inclusive
    * ``12_18m``     -> ``(as_of+12m, as_of+18m]`` lower exclusive, upper inclusive
    * ``within_18m`` -> ``[as_of, as_of+18m]``     lower inclusive, upper inclusive

    Raises :class:`AlertEpisodeGoldValidationError` for an unknown horizon.
    """
    if horizon not in HORIZON_END_MONTHS:
        raise AlertEpisodeGoldValidationError(
            [f"unknown horizon {horizon!r}; expected one of {sorted(HORIZON_END_MONTHS)}"]
        )
    return HorizonBounds(
        horizon=horizon,
        lower=_add_months(as_of, HORIZON_START_MONTHS[horizon]),
        lower_inclusive=HORIZON_LOWER_INCLUSIVE[horizon],
        upper=_add_months(as_of, HORIZON_END_MONTHS[horizon]),
    )


def _keys(raw: Any, where: str, required: set[str], optional: set[str], problems: list[str]) -> bool:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: expected an object, got {type(raw).__name__}")
        return False
    present = set(raw)
    unknown = present - required - optional
    missing = required - present
    if unknown:
        problems.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    if missing:
        problems.append(f"{where}: missing field(s) {', '.join(sorted(missing))}")
    return not missing


def _str(raw: Mapping[str, Any], key: str, where: str, problems: list[str], *, min_len: int = 1) -> str | None:
    value = raw.get(key)
    if not isinstance(value, str) or len(value.strip()) < min_len:
        problems.append(f"{where}.{key}: must be a non-empty string of at least {min_len} char(s)")
        return None
    return value


def _date(value: Any, where: str, problems: list[str]) -> datetime.date | None:
    if not isinstance(value, str):
        problems.append(f"{where}: expected an ISO date string, got {type(value).__name__}")
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        problems.append(f"{where}: {value!r} is not an ISO date")
        return None


def _bool(value: Any, where: str, problems: list[str]) -> bool | None:
    if not isinstance(value, bool):
        problems.append(f"{where}: must be a boolean")
        return None
    return value


def _str_list(
    value: Any, where: str, problems: list[str], vocabulary: frozenset[str] | None = None, *, minimum: int = 0
) -> tuple[str, ...]:
    """A sorted, duplicate-free list of strings (optionally from a vocabulary)."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{where}: must be a list of strings")
        return ()
    if len(value) < minimum:
        problems.append(f"{where}: needs at least {minimum} value(s)")
    if len(set(value)) != len(value):
        problems.append(f"{where}: contains duplicate value(s)")
    if list(value) != sorted(value):
        problems.append(f"{where}: must be in sorted order")
    if vocabulary is not None:
        unknown = [item for item in value if item not in vocabulary]
        if unknown:
            problems.append(f"{where}: unknown value(s) {', '.join(sorted(set(unknown)))}")
    return tuple(value)


def _text(value: Any, where: str, problems: list[str], *, min_len: int) -> str | None:
    """Substantive, concise, original prose: bounded length and no long quoted run."""
    if not isinstance(value, str) or len(value.strip()) < min_len:
        problems.append(f"{where}: must be substantive prose of at least {min_len} char(s)")
        return None
    if len(value) > MAX_TEXT_CHARS:
        problems.append(f"{where}: {len(value)} chars exceeds the {MAX_TEXT_CHARS} bound")
    for quote in _QUOTE_RUN.findall(value):
        if len(quote) > MAX_QUOTE_CHARS:
            problems.append(f"{where}: quoted run of {len(quote)} chars exceeds {MAX_QUOTE_CHARS}")
            break
    return value


def _https(url: Any, where: str, problems: list[str]) -> str | None:
    if not isinstance(url, str):
        problems.append(f"{where}.url: must be a string")
        return None
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if parsed.scheme != "https":
        problems.append(f"{where}.url: must be https, got {url!r}")
    elif not host:
        problems.append(f"{where}.url: has no host, got {url!r}")
    elif any(host == fake or host.endswith(f".{fake}") for fake in FAKE_URL_HOSTS):
        problems.append(f"{where}.url: {host!r} is a placeholder/fake host")
    return url


def _read_json(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{path.name}: unreadable ({exc})")
        return None


# --- dataclasses ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OutcomeEvaluationInputs:
    """The outcome-side truth Stage 9 feeds ``crisis_model.evaluation.evaluate_prediction``.

    It carries the horizon, the date the outcome can be judged, whether it occurred, and when it
    started -- and *deliberately no threshold*. The threshold is the model's decision boundary; a
    gold set that supplied one would be tuning the very thing the backtest exists to measure. The
    horizon here is always the case's single selected horizon; it is never re-used for a horizon the
    case never labelled, so the ``occurred`` flag can never mislabel a neighbouring bucket.
    """

    horizon: str
    evaluation_date: datetime.date
    actual_outcome: bool
    actual_start_date: datetime.date | None

    def as_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``evaluate_prediction`` -- no ``threshold``, no ``prediction``."""
        return {
            "horizon": self.horizon,
            "evaluation_date": self.evaluation_date,
            "actual_outcome": self.actual_outcome,
            "actual_start_date": self.actual_start_date,
        }


@dataclass(frozen=True, slots=True)
class WindowLabelInventory:
    """What a window offers a backtest: labelled cases and their positive/control split.

    Counts only. It names nothing a backtest measures -- no precision, no lead time -- so Stage 9
    can see what there is to score without this module pretending to have scored it.
    """

    window: str
    labeled_cases: int
    positives: int
    controls: int

    def as_dict(self) -> dict[str, int]:
        return {
            "labeled_cases": self.labeled_cases,
            "positives": self.positives,
            "controls": self.controls,
        }


@dataclass(frozen=True, slots=True)
class AlertTarget:
    """What a case is about: a typed, geolocated object the alert would concern."""

    type: str
    id: str
    name: str
    geography: str


@dataclass(frozen=True, slots=True)
class AlertOnsetIndicator:
    """One signal observable at onset: a typed, dated, sourced reading -- never a result."""

    key: str
    description: str
    observed_on: datetime.date
    value: float | int | str | bool | None
    unit: str | None
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertOnset:
    """What a live observer could see as of ``evaluation_as_of``. Strictly separate from outcome."""

    observation_start: datetime.date
    summary: str
    indicators: tuple[AlertOnsetIndicator, ...]


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    """What the future revealed. Never an input; the label is a function of ``occurred``."""

    occurred: bool
    actual_start_date: datetime.date | None
    actual_end_date: datetime.date | None
    summary: str
    classes: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertSourceRef:
    """A citation that makes a case auditable, and what it is allowed to attest to."""

    id: str
    title: str
    publisher: str
    url: str
    accessed: datetime.date
    kind: str
    supports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertProvenance:
    """How the case was made and how far it has been reviewed. Recorded, never inflated."""

    kind: str
    created_on: datetime.date
    review_status: str


@dataclass(frozen=True, slots=True)
class AlertEpisodeCase:
    """One validated labelled alert episode, with onset strictly separated from outcome.

    A case selects exactly one canonical horizon: ``evaluation_horizons`` is a one-item list kept for
    schema shape, and the case is judged only inside that horizon's bucket (:attr:`selected_bounds`).
    """

    case_id: str
    split: str
    window: str
    episode_group: str
    label: str
    risk_type: str
    target: AlertTarget
    evaluation_as_of: datetime.date
    #: The one canonical horizon this case labels, as a one-item list (see :attr:`selected_horizon`).
    evaluation_horizons: tuple[str, ...]
    evaluation_end: datetime.date
    label_available_on: datetime.date
    onset: AlertOnset
    outcome: AlertOutcome
    control_rationale: str | None
    source_refs: tuple[AlertSourceRef, ...]
    provenance: AlertProvenance

    @property
    def is_positive(self) -> bool:
        return self.label == LABEL_POSITIVE

    @property
    def fingerprint(self) -> str:
        return onset_fingerprint(self.onset.summary)

    @property
    def selected_horizon(self) -> str:
        """The single canonical horizon this case labels -- the only bucket it may be judged in."""
        return self.evaluation_horizons[0]

    @property
    def selected_bounds(self) -> HorizonBounds:
        """The calendar-month bucket the selected horizon judges, off ``evaluation_as_of``."""
        return horizon_bounds(self.evaluation_as_of, self.selected_horizon)

    def evaluation_inputs(self, horizon: str | None = None) -> OutcomeEvaluationInputs:
        """The threshold-free outcome truth for ``evaluate_prediction`` at this case's horizon.

        Pure: it reads this case's own outcome, judged at ``evaluation_end`` (the selected bucket's
        upper bound), and sets no threshold. ``horizon`` defaults to :attr:`selected_horizon`; passing
        any other horizon is refused, because this case's ``occurred`` labels only its own bucket -- a
        positive that started inside ``0_6m`` says nothing about whether ``6_12m`` stayed clear.
        """
        target = self.selected_horizon if horizon is None else horizon
        if target != self.selected_horizon:
            raise AlertEpisodeGoldValidationError(
                [
                    f"{self.case_id}: horizon {target!r} is not this case's selected horizon "
                    f"{self.selected_horizon!r}"
                ]
            )
        return OutcomeEvaluationInputs(
            horizon=target,
            evaluation_date=self.evaluation_end,
            actual_outcome=self.outcome.occurred,
            actual_start_date=self.outcome.actual_start_date,
        )


@dataclass(frozen=True, slots=True)
class AlertEpisodeGoldManifest:
    """The dataset descriptor: identifiers, windows, split map, vocabularies, and honest review."""

    dataset_id: str
    schema_version: str
    windows: tuple[tuple[str, datetime.date, datetime.date], ...]
    splits: tuple[tuple[str, str, int], ...]
    total_count: int
    risk_types: tuple[str, ...]
    horizons: tuple[str, ...]
    metadata: Mapping[str, Any]
    labeling: Mapping[str, Any]
    source_policy: str
    license: Mapping[str, Any]
    review: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AlertEpisodeGoldQuotas:
    """The audit the contract asks for. Reported on every full load, JSON-serializable."""

    total: int
    positives: int
    controls: int
    risk_types: int
    targets: int
    geographies: int
    horizons: tuple[str, ...]
    by_window: dict[str, dict[str, int]]
    by_split: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "positives": self.positives,
            "controls": self.controls,
            "risk_types": self.risk_types,
            "targets": self.targets,
            "geographies": self.geographies,
            "horizons": list(self.horizons),
            "by_window": {w: dict(sorted(v.items())) for w, v in sorted(self.by_window.items())},
            "by_split": dict(sorted(self.by_split.items())),
        }


@dataclass(frozen=True, slots=True)
class AlertEpisodeGoldCorpus:
    """A validated dataset (or a validated tuning slice), in deterministic order."""

    manifest: AlertEpisodeGoldManifest
    cases: tuple[AlertEpisodeCase, ...]
    quotas: AlertEpisodeGoldQuotas
    includes_holdout: bool

    def cases_for(self, split: str) -> tuple[AlertEpisodeCase, ...]:
        return tuple(case for case in self.cases if case.split == split)

    def cases_for_window(self, window: str) -> tuple[AlertEpisodeCase, ...]:
        return tuple(case for case in self.cases if case.window == window)

    def label_inventory(self) -> tuple[WindowLabelInventory, ...]:
        """Per-window labelled/positive/control counts, in ADR window order. Counts, not evidence."""
        inventory: list[WindowLabelInventory] = []
        for window in REQUIRED_WINDOWS:
            window_cases = self.cases_for_window(window)
            positives = sum(1 for case in window_cases if case.is_positive)
            inventory.append(
                WindowLabelInventory(
                    window=window,
                    labeled_cases=len(window_cases),
                    positives=positives,
                    controls=len(window_cases) - positives,
                )
            )
        return tuple(inventory)

    def report(self) -> dict[str, Any]:
        return {
            "dataset_id": self.manifest.dataset_id,
            "schema_version": self.manifest.schema_version,
            "includes_holdout": self.includes_holdout,
            "quotas": self.quotas.as_dict(),
            "label_inventory": [item.as_dict() | {"window": item.window} for item in self.label_inventory()],
        }


# --- manifest ------------------------------------------------------------------------------
_MANIFEST_KEYS = {
    "dataset_id", "schema_version", "windows", "splits", "total_count", "risk_types", "horizons",
    "metadata", "labeling", "source_policy", "license", "review",
}
_METADATA_KEYS = {"created_on", "description", "provenance"}
_LABELING_KEYS = {
    "positive_rule", "control_rule", "label_timing", "onset_outcome_separation", "controls",
    "lead_time",
}
_LICENSE_KEYS = {"id", "note"}
_REVIEW_KEYS = {"state", "human_verified", "independent_historical_verification"}


def load_manifest(root: Path = GOLD_ROOT) -> AlertEpisodeGoldManifest:
    """Load and validate ``manifest.json``: exact identifiers, windows, split map, honest review."""
    problems: list[str] = []
    raw = _read_json(root / MANIFEST_FILE, problems)
    if raw is None or not _keys(raw, MANIFEST_FILE, _MANIFEST_KEYS, set(), problems):
        raise AlertEpisodeGoldValidationError(problems or [f"{MANIFEST_FILE}: unreadable"])

    if raw.get("dataset_id") != DATASET_ID:
        problems.append(f"{MANIFEST_FILE}: dataset_id must be {DATASET_ID!r}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{MANIFEST_FILE}: schema_version must be {SCHEMA_VERSION!r}")

    windows = _manifest_windows(raw.get("windows"), problems)
    splits = _manifest_splits(raw.get("splits"), problems)
    if raw.get("total_count") != TOTAL_COUNT:
        problems.append(f"{MANIFEST_FILE}: total_count must be {TOTAL_COUNT}")
    if splits and sum(count for _, _, count in splits) != TOTAL_COUNT:
        problems.append(f"{MANIFEST_FILE}: split counts must sum to {TOTAL_COUNT}")

    risk_types = _str_list(raw.get("risk_types"), f"{MANIFEST_FILE}.risk_types", problems, RISK_TYPES, minimum=1)
    horizons = raw.get("horizons")
    if horizons != list(HORIZON_ORDER):
        problems.append(f"{MANIFEST_FILE}.horizons: must be exactly {list(HORIZON_ORDER)} in canonical order")

    _manifest_metadata(raw.get("metadata"), problems)
    if not _keys(raw.get("labeling"), f"{MANIFEST_FILE}.labeling", _LABELING_KEYS, set(), problems):
        pass
    elif not all(isinstance(raw["labeling"].get(k), str) and raw["labeling"][k].strip() for k in _LABELING_KEYS):
        problems.append(f"{MANIFEST_FILE}.labeling: every rule must be a non-empty string")
    if not isinstance(raw.get("source_policy"), str) or not raw["source_policy"].strip():
        problems.append(f"{MANIFEST_FILE}.source_policy: must be a non-empty string")
    _manifest_license(raw.get("license"), problems)
    review = _manifest_review(raw.get("review"), problems)

    if problems:
        raise AlertEpisodeGoldValidationError(problems)
    return AlertEpisodeGoldManifest(
        dataset_id=DATASET_ID,
        schema_version=SCHEMA_VERSION,
        windows=windows,
        splits=splits,
        total_count=TOTAL_COUNT,
        risk_types=risk_types,
        horizons=tuple(horizons),
        metadata=dict(raw["metadata"]),
        labeling=dict(raw["labeling"]),
        source_policy=raw["source_policy"],
        license=dict(raw["license"]),
        review=review,
    )


def _manifest_windows(raw: Any, problems: list[str]) -> tuple[tuple[str, datetime.date, datetime.date], ...]:
    if not isinstance(raw, list):
        problems.append(f"{MANIFEST_FILE}.windows: must be a list")
        return ()
    names = [item.get("name") if isinstance(item, Mapping) else None for item in raw]
    if names != list(REQUIRED_WINDOWS):
        problems.append(f"{MANIFEST_FILE}.windows: must be exactly {list(REQUIRED_WINDOWS)} in order")
    parsed: list[tuple[str, datetime.date, datetime.date]] = []
    for item in raw:
        where = f"{MANIFEST_FILE}.windows"
        if not _keys(item, where, {"name", "start", "end"}, set(), problems):
            continue
        name = item["name"]
        bounds = WINDOW_BOUNDS.get(name)
        start = _date(item.get("start"), f"{where}[{name}].start", problems)
        end = _date(item.get("end"), f"{where}[{name}].end", problems)
        if bounds is None:
            problems.append(f"{where}[{name}]: unknown window")
        elif (start, end) != bounds:
            problems.append(f"{where}[{name}]: bounds must be {bounds[0]}..{bounds[1]}")
        if bounds is not None and start is not None and end is not None:
            parsed.append((name, bounds[0], bounds[1]))
    return tuple(parsed)


def _manifest_splits(raw: Any, problems: list[str]) -> tuple[tuple[str, str, int], ...]:
    if not isinstance(raw, list):
        problems.append(f"{MANIFEST_FILE}.splits: must be a list")
        return ()
    names = [item.get("name") if isinstance(item, Mapping) else None for item in raw]
    if names != list(SPLIT_ORDER):
        problems.append(f"{MANIFEST_FILE}.splits: must be exactly {list(SPLIT_ORDER)} in order")
    parsed: list[tuple[str, str, int]] = []
    for item in raw:
        where = f"{MANIFEST_FILE}.splits"
        if not _keys(item, where, {"name", "file", "count"}, set(), problems):
            continue
        name = item["name"]
        if item.get("file") != SPLIT_FILES.get(name):
            problems.append(f"{where}[{name}]: file must be {SPLIT_FILES.get(name)!r}")
        if item.get("count") != SPLIT_COUNTS.get(name):
            problems.append(f"{where}[{name}]: count must be {SPLIT_COUNTS.get(name)}")
        parsed.append((name, SPLIT_FILES.get(name, ""), SPLIT_COUNTS.get(name, 0)))
    return tuple(parsed)


def _manifest_metadata(raw: Any, problems: list[str]) -> None:
    if not _keys(raw, f"{MANIFEST_FILE}.metadata", _METADATA_KEYS, set(), problems):
        return
    _date(raw.get("created_on"), f"{MANIFEST_FILE}.metadata.created_on", problems)
    for key in ("description", "provenance"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            problems.append(f"{MANIFEST_FILE}.metadata.{key}: must be a non-empty string")


def _manifest_license(raw: Any, problems: list[str]) -> None:
    if not _keys(raw, f"{MANIFEST_FILE}.license", _LICENSE_KEYS, set(), problems):
        return
    if not isinstance(raw.get("id"), str) or not raw["id"].strip():
        problems.append(f"{MANIFEST_FILE}.license.id: must be a non-empty string")
    note = raw.get("note")
    if not isinstance(note, str) or "paraphrase" not in note.lower():
        problems.append(f"{MANIFEST_FILE}.license.note: must state the prose is original paraphrase")


def _manifest_review(raw: Any, problems: list[str]) -> dict[str, Any]:
    if not _keys(raw, f"{MANIFEST_FILE}.review", _REVIEW_KEYS, set(), problems):
        return {}
    if raw.get("state") != REVIEW_STATE:
        problems.append(f"{MANIFEST_FILE}.review.state: must be {REVIEW_STATE!r}")
    for key in ("human_verified", "independent_historical_verification"):
        value = raw.get(key)
        if value is not False:
            # An honest manifest cannot claim a verification that has not happened.
            problems.append(f"{MANIFEST_FILE}.review.{key}: must be false (no such review has happened yet)")
    return dict(raw)


# --- cases ---------------------------------------------------------------------------------
_CASE_REQUIRED = {
    "case_id", "split", "window", "episode_group", "label", "risk_type", "target",
    "evaluation_as_of", "evaluation_horizons", "evaluation_end", "label_available_on",
    "onset", "outcome", "control_rationale", "source_refs", "provenance",
}
_TARGET_KEYS = {"type", "id", "name", "geography"}
_ONSET_KEYS = {"observation_start", "summary", "indicators"}
_INDICATOR_REQUIRED = {"key", "description", "observed_on", "source_ids"}
_INDICATOR_OPTIONAL = {"value", "unit"}
_OUTCOME_KEYS = {"occurred", "actual_start_date", "actual_end_date", "summary", "classes", "source_ids"}
_REF_KEYS = {"id", "title", "publisher", "url", "accessed", "kind", "supports"}
_PROVENANCE_KEYS = {"kind", "created_on", "review_status"}


def load_split(root: Path = GOLD_ROOT, split: str = SPLIT_TRAIN, *, allow_holdout: bool = False) -> tuple[AlertEpisodeCase, ...]:
    """Load and validate one named split. The holdout refuses to load without ``allow_holdout``."""
    if split not in SPLIT_ORDER:
        raise AlertEpisodeGoldValidationError([f"unknown split {split!r}; expected one of {list(SPLIT_ORDER)}"])
    if split == SPLIT_FINAL_HOLDOUT and not allow_holdout:
        raise AlertEpisodeGoldValidationError(
            [f"{split} is gated: pass allow_holdout=True to load the held-out evaluation split"]
        )
    problems: list[str] = []
    cases = _load_split_cases(root, split, problems)
    if problems:
        raise AlertEpisodeGoldValidationError(problems)
    return cases


def _load_split_cases(root: Path, split: str, problems: list[str]) -> tuple[AlertEpisodeCase, ...]:
    file = SPLIT_FILES[split]
    raw = _read_json(root / file, problems)
    if raw is None or not _keys(raw, file, {"schema_version", "split", "cases"}, set(), problems):
        return ()
    if raw.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{file}: schema_version must be {SCHEMA_VERSION!r}")
    if raw.get("split") != split:
        problems.append(f"{file}: split must be {split!r}")
    entries = raw.get("cases")
    if not isinstance(entries, list):
        problems.append(f"{file}.cases: must be a list")
        return ()
    cases: list[AlertEpisodeCase] = []
    for item in entries:
        case = _check_case(item, split, file, problems)
        if case is not None:
            cases.append(case)
    order = [case.case_id for case in cases]
    if order != sorted(order):
        problems.append(f"{file}.cases: must be sorted by case_id")
    return tuple(cases)


def _check_case(raw: Any, split: str, file: str, problems: list[str]) -> AlertEpisodeCase | None:
    case_id = raw.get("case_id") if isinstance(raw, Mapping) else None
    where = f"{file}:{case_id or '<no id>'}"
    if not _keys(raw, where, _CASE_REQUIRED, set(), problems):
        return None
    if not isinstance(case_id, str) or not _ID_PATTERN.match(case_id):
        problems.append(f"{where}: case_id must match {_ID_PATTERN.pattern}")
        return None
    if raw.get("split") != split:
        problems.append(f"{where}: split must be {split!r}")

    window = raw.get("window")
    bounds = WINDOW_BOUNDS.get(window) if isinstance(window, str) else None
    if bounds is None:
        problems.append(f"{where}.window: must be one of {list(REQUIRED_WINDOWS)}")
    episode_group = _str(raw, "episode_group", where, problems)
    label = raw.get("label")
    if label not in LABELS:
        problems.append(f"{where}.label: must be one of {sorted(LABELS)}")
    if raw.get("risk_type") not in RISK_TYPES:
        problems.append(f"{where}.risk_type: {raw.get('risk_type')!r} is not a canonical RiskType")

    target = _check_target(raw.get("target"), where, problems)
    as_of = _date(raw.get("evaluation_as_of"), f"{where}.evaluation_as_of", problems)
    end = _date(raw.get("evaluation_end"), f"{where}.evaluation_end", problems)
    available = _date(raw.get("label_available_on"), f"{where}.label_available_on", problems)
    horizons = _check_horizons(raw.get("evaluation_horizons"), where, problems)
    selected = horizons[0] if len(horizons) == 1 and horizons[0] in HORIZON_SET else None
    _check_timing(as_of, end, available, window, bounds, selected, where, problems)

    refs, support_map = _check_source_refs(raw.get("source_refs"), where, problems)
    outcome = _check_outcome(raw.get("outcome"), label, as_of, selected, available, support_map, where, problems)
    onset = _check_onset(raw.get("onset"), as_of, outcome, refs, support_map, where, problems)
    rationale = _check_control_rationale(raw.get("control_rationale"), label, where, problems)
    provenance = _check_provenance(raw.get("provenance"), where, problems)

    if None in (episode_group, target, as_of, end, available, outcome, onset, provenance) or bounds is None or label not in LABELS:
        return None
    return AlertEpisodeCase(
        case_id=case_id,
        split=split,
        window=window,  # type: ignore[arg-type]
        episode_group=episode_group,  # type: ignore[arg-type]
        label=label,  # type: ignore[arg-type]
        risk_type=str(raw.get("risk_type")),
        target=target,  # type: ignore[arg-type]
        evaluation_as_of=as_of,
        evaluation_horizons=horizons,
        evaluation_end=end,
        label_available_on=available,
        onset=onset,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        control_rationale=rationale,
        source_refs=refs,
        provenance=provenance,  # type: ignore[arg-type]
    )


def _check_target(raw: Any, where: str, problems: list[str]) -> AlertTarget | None:
    if not _keys(raw, f"{where}.target", _TARGET_KEYS, set(), problems):
        return None
    type_ = raw.get("type")
    if type_ not in TARGET_TYPES:
        problems.append(f"{where}.target.type: must be one of {sorted(TARGET_TYPES)}")
        type_ = None
    target_id = raw.get("id")
    if not isinstance(target_id, str) or not _ID_PATTERN.match(target_id):
        problems.append(f"{where}.target.id: must match {_ID_PATTERN.pattern}")
        target_id = None
    name = _str(raw, "name", f"{where}.target", problems)
    geography = raw.get("geography")
    if not isinstance(geography, str) or not (_GEO_ISO.match(geography) or geography in NAMED_GEOGRAPHIES):
        problems.append(f"{where}.target.geography: must be an ISO alpha-2 code or one of {sorted(NAMED_GEOGRAPHIES)}")
        geography = None
    if None in (type_, target_id, name, geography):
        return None
    return AlertTarget(type=str(type_), id=str(target_id), name=str(name), geography=str(geography))


def _check_horizons(raw: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    """The one canonical horizon a case selects: a one-item list drawn from the horizon vocabulary.

    A case labels a single horizon's bucket, so zero or more-than-one horizons are both rejected --
    a multi-horizon list would let one ``occurred`` flag stand in for several distinct buckets.
    """
    field = f"{where}.evaluation_horizons"
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        problems.append(f"{field}: must be a list of strings")
        return ()
    if len(raw) != 1:
        problems.append(
            f"{field}: must select exactly one canonical evaluation horizon, got {len(raw)} "
            f"(a case is judged only inside its own horizon's bucket, never re-used across horizons)"
        )
    unknown = [item for item in raw if item not in HORIZON_SET]
    if unknown:
        problems.append(f"{field}: unknown value(s) {', '.join(sorted(set(unknown)))}")
    return tuple(raw)


def _check_timing(
    as_of: datetime.date | None,
    end: datetime.date | None,
    available: datetime.date | None,
    window: Any,
    bounds: tuple[datetime.date, datetime.date] | None,
    selected: str | None,
    where: str,
    problems: list[str],
) -> None:
    """As-of in window, and ``evaluation_end`` *equal to* the selected bucket's upper bound.

    Requiring equality (not merely sufficiency) makes label availability and control coverage
    deterministic: every case ends on exactly the last day its one horizon can still judge.
    """
    if as_of is not None and bounds is not None and not bounds[0] <= as_of <= bounds[1]:
        problems.append(f"{where}: evaluation_as_of {as_of} is outside window {window} ({bounds[0]}..{bounds[1]})")
    if as_of is not None and end is not None:
        if end <= as_of:
            problems.append(f"{where}: evaluation_end {end} must be after evaluation_as_of {as_of}")
        elif selected is not None:
            upper = horizon_bounds(as_of, selected).upper
            if end != upper:
                problems.append(
                    f"{where}: evaluation_end {end} must equal the selected horizon {selected!r} "
                    f"bucket upper bound {upper} (as_of + {HORIZON_END_MONTHS[selected]} calendar months)"
                )
    if end is not None and available is not None and available < end:
        problems.append(f"{where}: label_available_on {available} precedes evaluation_end {end} (labels leak to inputs)")


def _check_source_refs(raw: Any, where: str, problems: list[str]) -> tuple[tuple[AlertSourceRef, ...], dict[str, frozenset[str]]]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where}.source_refs: must be a non-empty list")
        return (), {}
    refs: list[AlertSourceRef] = []
    support_map: dict[str, frozenset[str]] = {}
    ids: list[str] = []
    for index, item in enumerate(raw):
        spot = f"{where}.source_refs[{index}]"
        if not _keys(item, spot, _REF_KEYS, set(), problems):
            continue
        ref_id = _str(item, "id", spot, problems)
        title = _str(item, "title", spot, problems)
        publisher = _str(item, "publisher", spot, problems)
        _https(item.get("url"), spot, problems)
        accessed = _date(item.get("accessed"), f"{spot}.accessed", problems)
        if item.get("kind") not in SOURCE_KINDS:
            problems.append(f"{spot}.kind: must be one of {sorted(SOURCE_KINDS)}")
        supports = _str_list(item.get("supports"), f"{spot}.supports", problems, SUPPORT_KINDS, minimum=1)
        if None not in (ref_id, title, publisher, accessed) and ref_id is not None:
            ids.append(ref_id)
            support_map[ref_id] = frozenset(supports)
            refs.append(AlertSourceRef(ref_id, title, publisher, str(item["url"]), accessed, str(item["kind"]), supports))  # type: ignore[arg-type]
    if ids != sorted(ids):
        problems.append(f"{where}.source_refs: must be sorted by id")
    if len(set(ids)) != len(ids):
        problems.append(f"{where}.source_refs: duplicate source id")
    return tuple(refs), support_map


def _resolve_support(source_ids: tuple[str, ...], required: str, support_map: dict[str, frozenset[str]], where: str, problems: list[str]) -> None:
    for source_id in source_ids:
        if source_id not in support_map:
            problems.append(f"{where}: source_id {source_id!r} does not resolve to a source ref")
        elif required not in support_map[source_id]:
            problems.append(f"{where}: source_id {source_id!r} does not declare {required!r} support")


def _check_outcome(
    raw: Any,
    label: Any,
    as_of: datetime.date | None,
    selected: str | None,
    available: datetime.date | None,
    support_map: dict[str, frozenset[str]],
    where: str,
    problems: list[str],
) -> AlertOutcome | None:
    if not _keys(raw, f"{where}.outcome", _OUTCOME_KEYS, set(), problems):
        return None
    occurred = _bool(raw.get("occurred"), f"{where}.outcome.occurred", problems)
    start = _date(raw.get("actual_start_date"), f"{where}.outcome.actual_start_date", problems) if raw.get("actual_start_date") is not None else None
    stop = _date(raw.get("actual_end_date"), f"{where}.outcome.actual_end_date", problems) if raw.get("actual_end_date") is not None else None
    summary = _text(raw.get("summary"), f"{where}.outcome.summary", problems, min_len=MIN_SUMMARY_CHARS)
    outcome_source_ids = _str_list(raw.get("source_ids"), f"{where}.outcome.source_ids", problems, minimum=1)
    is_positive = label == LABEL_POSITIVE

    if occurred is not None and is_positive != occurred:
        problems.append(f"{where}: label is {label!r} but outcome.occurred is {occurred} (label must be positive iff occurred)")

    if is_positive:
        classes = _str_list(raw.get("classes"), f"{where}.outcome.classes", problems, OUTCOME_CLASSES, minimum=1)
        if classes and not (ADVERSE_OUTCOME_CLASSES & set(classes)):
            problems.append(f"{where}.outcome.classes: a positive needs at least one adverse class")
        if raw.get("actual_start_date") is None:
            problems.append(f"{where}.outcome.actual_start_date: a positive requires an actual start date")
        elif start is not None and as_of is not None and selected is not None:
            bucket = horizon_bounds(as_of, selected)
            if not bucket.contains(start):
                problems.append(
                    f"{where}.outcome.actual_start_date {start}: must fall within the selected "
                    f"horizon {selected!r} bucket {bucket.interval}"
                )
        _resolve_support(outcome_source_ids, "outcome", support_map, f"{where}.outcome", problems)
    else:
        # A control asserts no target outcome *inside its selected bucket* through evaluation_end
        # (the bucket's upper bound); it makes no claim about events outside that bucket.
        classes = _str_list(raw.get("classes"), f"{where}.outcome.classes", problems, OUTCOME_CLASSES)
        if classes:
            problems.append(f"{where}.outcome.classes: a control must have no outcome classes")
        if raw.get("actual_start_date") is not None or raw.get("actual_end_date") is not None:
            problems.append(f"{where}.outcome: a control must have null actual_start_date and actual_end_date")
        _resolve_support(outcome_source_ids, "control", support_map, f"{where}.outcome", problems)

    if start is not None and stop is not None and stop < start:
        problems.append(f"{where}.outcome: actual_end_date {stop} precedes actual_start_date {start}")
    if available is not None:
        for name, value in (("actual_start_date", start), ("actual_end_date", stop)):
            if value is not None and available < value:
                problems.append(f"{where}: label_available_on {available} precedes outcome.{name} {value}")

    if occurred is None or summary is None:
        return None
    return AlertOutcome(
        occurred=occurred,
        actual_start_date=start,
        actual_end_date=stop,
        summary=summary,
        classes=classes,
        source_ids=outcome_source_ids,
    )


def _check_onset(
    raw: Any,
    as_of: datetime.date | None,
    outcome: AlertOutcome | None,
    refs: tuple[AlertSourceRef, ...],
    support_map: dict[str, frozenset[str]],
    where: str,
    problems: list[str],
) -> AlertOnset | None:
    if not _keys(raw, f"{where}.onset", _ONSET_KEYS, set(), problems):
        return None
    start = _date(raw.get("observation_start"), f"{where}.onset.observation_start", problems)
    summary = _text(raw.get("summary"), f"{where}.onset.summary", problems, min_len=MIN_SUMMARY_CHARS)
    if start is not None and as_of is not None and start > as_of:
        problems.append(f"{where}.onset.observation_start {start}: must be on or before evaluation_as_of {as_of}")

    outcome_dates = tuple(d for d in ((outcome.actual_start_date, outcome.actual_end_date) if outcome else ()) if d is not None)
    outcome_classes = outcome.classes if outcome else ()
    source_titles = tuple(ref.title for ref in refs)
    source_ids = frozenset(support_map)
    if summary is not None and as_of is not None:
        _check_as_of(summary, as_of, outcome_dates, outcome_classes, source_titles, f"{where}.onset.summary", problems)

    indicators = _check_indicators(raw.get("indicators"), as_of, start, outcome_dates, outcome_classes, source_titles, source_ids, support_map, where, problems)
    if start is None or summary is None:
        return None
    return AlertOnset(observation_start=start, summary=summary, indicators=indicators)


def _check_indicators(
    raw: Any,
    as_of: datetime.date | None,
    onset_start: datetime.date | None,
    outcome_dates: tuple[datetime.date, ...],
    outcome_classes: tuple[str, ...],
    source_titles: tuple[str, ...],
    source_ids: frozenset[str],
    support_map: dict[str, frozenset[str]],
    where: str,
    problems: list[str],
) -> tuple[AlertOnsetIndicator, ...]:
    if not isinstance(raw, list) or len(raw) < MIN_INDICATORS:
        problems.append(f"{where}.onset.indicators: must be a list of at least {MIN_INDICATORS} signals")
        return ()
    indicators: list[AlertOnsetIndicator] = []
    keys: set[str] = set()
    for index, item in enumerate(raw):
        spot = f"{where}.onset.indicators[{index}]"
        if not _keys(item, spot, _INDICATOR_REQUIRED, _INDICATOR_OPTIONAL, problems):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not _SNAKE.match(key):
            problems.append(f"{spot}.key: must be snake_case")
        else:
            if key in keys:
                problems.append(f"{spot}.key: duplicate indicator key {key!r}")
            keys.add(key)
            # A key is a machine handle, not a smuggling channel: it may not carry a source ref id or
            # an outcome class (hyphens and underscores are treated alike, so neither form slips past).
            norm_key = key.replace("-", "_")
            embedded = [tok for tok in (*source_ids, *outcome_classes) if tok.replace("-", "_") in norm_key]
            if embedded:
                problems.append(f"{spot}.key: must not embed an outcome/source id ({', '.join(sorted(embedded))})")
        description = _text(item.get("description"), f"{spot}.description", problems, min_len=8)
        observed = _date(item.get("observed_on"), f"{spot}.observed_on", problems)
        if observed is not None and as_of is not None and observed > as_of:
            problems.append(f"{spot}.observed_on {observed}: must be on or before evaluation_as_of {as_of}")
        if observed is not None and onset_start is not None and observed < onset_start:
            problems.append(f"{spot}.observed_on {observed}: precedes onset observation_start {onset_start}")
        value = item.get("value")
        if value is not None and not isinstance(value, bool | int | float | str):
            problems.append(f"{spot}.value: must be a scalar (number, string or boolean) or absent")
        unit = item.get("unit")
        if unit is not None and (not isinstance(unit, str) or not unit.strip()):
            problems.append(f"{spot}.unit: must be a non-empty string or absent")
        support_ids = _str_list(item.get("source_ids"), f"{spot}.source_ids", problems, minimum=1)
        _resolve_support(support_ids, "onset", support_map, spot, problems)
        if description is not None and as_of is not None:
            _check_as_of(description, as_of, outcome_dates, outcome_classes, source_titles, f"{spot}.description", problems)
        if isinstance(key, str) and observed is not None:
            indicators.append(
                AlertOnsetIndicator(
                    key=key,
                    description=item.get("description", ""),
                    observed_on=observed,
                    value=value if isinstance(value, bool | int | float | str) else None,
                    unit=unit if isinstance(unit, str) else None,
                    source_ids=support_ids,
                )
            )
    return tuple(indicators)


def _check_as_of(
    text: str,
    as_of: datetime.date,
    outcome_dates: tuple[datetime.date, ...],
    outcome_classes: tuple[str, ...],
    source_titles: tuple[str, ...],
    where: str,
    problems: list[str],
) -> None:
    """An onset field must be writable by a live observer: no hindsight, no outcome, no source text."""
    lowered = text.lower()
    for phrase in find_hindsight_phrases(text):
        problems.append(f"{where}: hindsight phrase {phrase!r} in onset text")
    for year in find_future_years(text, as_of.year):
        problems.append(f"{where}: mentions {year}, later than the evaluation_as_of year {as_of.year}")
    for outcome_date in outcome_dates:
        if outcome_date.isoformat() in text:
            problems.append(f"{where}: leaks the outcome date {outcome_date}")
    for cls in outcome_classes:
        if cls in lowered or cls.replace("_", " ") in lowered:
            problems.append(f"{where}: mentions the outcome class {cls!r}, which the onset cannot know")
    for title in source_titles:
        if len(title) >= 12 and title.lower() in lowered:
            problems.append(f"{where}: repeats a source title -- no source title is a model input")


def _check_control_rationale(raw: Any, label: Any, where: str, problems: list[str]) -> str | None:
    if label == LABEL_CONTROL:
        return _text(raw, f"{where}.control_rationale", problems, min_len=MIN_SUMMARY_CHARS)
    if raw is not None:
        problems.append(f"{where}.control_rationale: must be null for a positive case")
    return None


def _check_provenance(raw: Any, where: str, problems: list[str]) -> AlertProvenance | None:
    if not _keys(raw, f"{where}.provenance", _PROVENANCE_KEYS, set(), problems):
        return None
    if raw.get("kind") != PROVENANCE_KIND:
        problems.append(f"{where}.provenance.kind: must be {PROVENANCE_KIND!r}")
    if raw.get("review_status") != REVIEW_STATE:
        problems.append(f"{where}.provenance.review_status: must be {REVIEW_STATE!r} (no human-review claim)")
    created_on = _date(raw.get("created_on"), f"{where}.provenance.created_on", problems)
    if created_on is None:
        return None
    return AlertProvenance(kind=PROVENANCE_KIND, created_on=created_on, review_status=REVIEW_STATE)


# --- corpus assembly -----------------------------------------------------------------------
def load_corpus(root: Path = GOLD_ROOT) -> AlertEpisodeGoldCorpus:
    """Load and audit the whole 48-case dataset, holdout included -- the integrity audit."""
    return _assemble(root, SPLIT_ORDER, includes_holdout=True, enforce_quotas=True)


def load_tuning_corpus(root: Path = GOLD_ROOT) -> AlertEpisodeGoldCorpus:
    """Load train + development for tuning. The final holdout is excluded by construction."""
    return _assemble(root, TUNING_SPLITS, includes_holdout=False, enforce_quotas=False)


def _assemble(root: Path, splits: Sequence[str], *, includes_holdout: bool, enforce_quotas: bool) -> AlertEpisodeGoldCorpus:
    problems: list[str] = []
    manifest = load_manifest(root)

    cases: list[AlertEpisodeCase] = []
    for split in splits:
        split_cases = _load_split_cases(root, split, problems)
        if len(split_cases) != SPLIT_COUNTS[split]:
            problems.append(f"{SPLIT_FILES[split]}: holds {len(split_cases)} cases, expected {SPLIT_COUNTS[split]}")
        cases.extend(split_cases)

    for case in cases:
        if case.risk_type not in manifest.risk_types:
            problems.append(f"{case.case_id}: risk_type {case.risk_type!r} is not declared in the manifest")
        for horizon in case.evaluation_horizons:
            if horizon not in manifest.horizons:
                problems.append(f"{case.case_id}: horizon {horizon!r} is not declared in the manifest")

    _check_uniqueness(cases, problems)
    _check_no_leakage(cases, problems)
    if includes_holdout and len(cases) != TOTAL_COUNT:
        problems.append(f"corpus holds {len(cases)} cases; expected {TOTAL_COUNT}")

    quotas = _quotas(cases)
    if enforce_quotas:
        _check_quotas(quotas, problems)
        _check_split_window_coverage(cases, problems)
    if problems:
        raise AlertEpisodeGoldValidationError(problems)
    return AlertEpisodeGoldCorpus(manifest=manifest, cases=tuple(cases), quotas=quotas, includes_holdout=includes_holdout)


def _check_uniqueness(cases: Sequence[AlertEpisodeCase], problems: list[str]) -> None:
    duplicates = sorted({cid for cid, count in Counter(case.case_id for case in cases).items() if count > 1})
    if duplicates:
        problems.append(f"corpus: duplicate case_id(s) {', '.join(duplicates)}")
    seen: dict[str, str] = {}
    for case in cases:
        fingerprint = case.fingerprint
        if fingerprint in seen:
            problems.append(f"corpus: cases {seen[fingerprint]!r} and {case.case_id!r} share a normalized onset-summary fingerprint")
        else:
            seen[fingerprint] = case.case_id


def _check_no_leakage(cases: Sequence[AlertEpisodeCase], problems: list[str]) -> None:
    groups: dict[str, set[str]] = {}
    for case in cases:
        groups.setdefault(case.episode_group, set()).add(case.split)
    for group, splits in sorted(groups.items()):
        if len(splits) > 1:
            problems.append(f"corpus: episode_group {group!r} spans splits {sorted(splits)}")

    by_key: dict[tuple[str, str], list[AlertEpisodeCase]] = {}
    for case in cases:
        by_key.setdefault((case.risk_type, case.target.id), []).append(case)
    for (risk_type, target_id), group in sorted(by_key.items()):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                if left.split == right.split:
                    continue
                if left.evaluation_as_of <= right.evaluation_end and right.evaluation_as_of <= left.evaluation_end:
                    problems.append(
                        f"corpus: cases {left.case_id!r} and {right.case_id!r} overlap on "
                        f"(risk_type={risk_type}, target={target_id}) across splits {sorted({left.split, right.split})}"
                    )


def _quotas(cases: Sequence[AlertEpisodeCase]) -> AlertEpisodeGoldQuotas:
    by_window: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = by_window.setdefault(case.window, {"total": 0, "positive": 0, "control": 0})
        bucket["total"] += 1
        bucket["positive" if case.is_positive else "control"] += 1
    positives = sum(1 for case in cases if case.is_positive)
    return AlertEpisodeGoldQuotas(
        total=len(cases),
        positives=positives,
        controls=len(cases) - positives,
        risk_types=len({case.risk_type for case in cases}),
        targets=len({case.target.id for case in cases}),
        geographies=len({case.target.geography for case in cases}),
        horizons=tuple(h for h in HORIZON_ORDER if any(h in case.evaluation_horizons for case in cases)),
        by_window=by_window,
        by_split=dict(Counter(case.split for case in cases)),
    )


def _check_quotas(quotas: AlertEpisodeGoldQuotas, problems: list[str]) -> None:
    if quotas.positives < MIN_POSITIVES_TOTAL:
        problems.append(f"corpus holds {quotas.positives} positives; at least {MIN_POSITIVES_TOTAL} required")
    if quotas.controls < MIN_CONTROLS_TOTAL:
        problems.append(f"corpus holds {quotas.controls} controls; at least {MIN_CONTROLS_TOTAL} required")
    if quotas.risk_types < MIN_RISK_TYPES:
        problems.append(f"corpus spans {quotas.risk_types} risk types; at least {MIN_RISK_TYPES} required")
    if quotas.targets < MIN_TARGETS:
        problems.append(f"corpus spans {quotas.targets} targets; at least {MIN_TARGETS} required")
    if quotas.geographies < MIN_GEOGRAPHIES:
        problems.append(f"corpus spans {quotas.geographies} geographies; at least {MIN_GEOGRAPHIES} required")
    if set(quotas.horizons) != HORIZON_SET:
        missing = sorted(HORIZON_SET - set(quotas.horizons))
        problems.append(f"corpus never exercises horizon(s) {', '.join(missing)}")

    for window in REQUIRED_WINDOWS:
        bucket = quotas.by_window.get(window, {"total": 0, "positive": 0, "control": 0})
        if bucket["total"] != CASES_PER_WINDOW:
            problems.append(f"window {window!r} holds {bucket['total']} cases; expected exactly {CASES_PER_WINDOW}")
        if bucket["positive"] < MIN_POSITIVES_PER_WINDOW:
            problems.append(f"window {window!r} holds {bucket['positive']} positives; at least {MIN_POSITIVES_PER_WINDOW} required")
        if bucket["control"] < MIN_CONTROLS_PER_WINDOW:
            problems.append(f"window {window!r} holds {bucket['control']} controls; at least {MIN_CONTROLS_PER_WINDOW} required")

    for split in SPLIT_ORDER:
        if quotas.by_split.get(split, 0) != SPLIT_COUNTS[split]:
            problems.append(f"split {split!r} holds {quotas.by_split.get(split, 0)} cases; expected {SPLIT_COUNTS[split]}")


def per_split_window_coverage(cases: Sequence[AlertEpisodeCase]) -> dict[tuple[str, str], dict[str, int]]:
    """Positive/control counts per (split, window). Every split must cover every window with both."""
    coverage: dict[tuple[str, str], dict[str, int]] = {}
    for case in cases:
        bucket = coverage.setdefault((case.split, case.window), {"positive": 0, "control": 0})
        bucket["positive" if case.is_positive else "control"] += 1
    return coverage


def _check_split_window_coverage(cases: Sequence[AlertEpisodeCase], problems: list[str]) -> None:
    coverage = per_split_window_coverage(cases)
    for split in SPLIT_ORDER:
        for window in REQUIRED_WINDOWS:
            bucket = coverage.get((split, window), {"positive": 0, "control": 0})
            if bucket["positive"] < 1 or bucket["control"] < 1:
                problems.append(
                    f"split {split!r} window {window!r} needs at least one positive and one control "
                    f"(has {bucket['positive']} positive, {bucket['control']} control)"
                )


__all__ = [
    "CASES_PER_WINDOW",
    "DATASET_ID",
    "GOLD_ROOT",
    "HORIZON_ORDER",
    "LABELS",
    "LABEL_CONTROL",
    "LABEL_POSITIVE",
    "MANIFEST_FILE",
    "OUTCOME_CLASSES",
    "REVIEW_STATE",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "SPLIT_COUNTS",
    "SPLIT_FILES",
    "SPLIT_FINAL_HOLDOUT",
    "SPLIT_ORDER",
    "SUPPORT_KINDS",
    "TARGET_TYPES",
    "TOTAL_COUNT",
    "TUNING_SPLITS",
    "WINDOW_BOUNDS",
    "AlertEpisodeCase",
    "AlertEpisodeGoldCorpus",
    "AlertEpisodeGoldManifest",
    "AlertEpisodeGoldQuotas",
    "AlertEpisodeGoldValidationError",
    "AlertOnset",
    "AlertOnsetIndicator",
    "AlertOutcome",
    "AlertProvenance",
    "AlertSourceRef",
    "AlertTarget",
    "HorizonBounds",
    "OutcomeEvaluationInputs",
    "WindowLabelInventory",
    "horizon_bounds",
    "load_corpus",
    "load_manifest",
    "load_split",
    "load_tuning_corpus",
    "onset_fingerprint",
    "per_split_window_coverage",
]
