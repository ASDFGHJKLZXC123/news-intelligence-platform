"""The alert dedupe key (ADR 0010).

``dedupe_key = (risk_type, scope_entity, condition_class)``. While an alert with that key is
active the same condition *updates* it rather than creating a new row -- the partial unique
index `uq_alerts_active_dedupe_key` enforces exactly one live alert per key, which is what
makes flapping structurally impossible.

The key is therefore a correctness boundary, so its encoding must be injective. Joining raw
components with a delimiter is not: ``("banking", "us:west", "score")`` and
``("banking", "us", "west:score")`` would collide. Each component is percent-encoded before
joining, so the delimiter cannot occur inside one.

The value is deterministic across processes and releases -- no salted `hash()`, no UUIDs --
because it must match a row written by a different worker on a different day.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

from db.models.enums import RiskType

#: `alerts.dedupe_key` is `String(255)`.
DEDUPE_KEY_MAX_LENGTH = 255

_DELIMITER = ":"
#: Marks a digest-shortened key. Percent-encoding makes this byte impossible inside a
#: component, so a shortened key can never collide with a literal one.
_DIGEST_MARKER = "#"
_DIGEST_BYTES = 16
_PREFIX_BUDGET = DEDUPE_KEY_MAX_LENGTH - len(_DIGEST_MARKER) - (_DIGEST_BYTES * 2)


@dataclass(frozen=True)
class DedupeKey:
    """The `(risk_type, scope_entity, condition_class)` identity of an alert condition."""

    risk_type: RiskType
    scope_entity: str
    condition_class: str

    @property
    def value(self) -> str:
        """The string persisted to `alerts.dedupe_key`; always within 255 characters."""
        encoded = _DELIMITER.join(
            quote(part, safe="")
            for part in (self.risk_type.value, self.scope_entity, self.condition_class)
        )
        if len(encoded) <= DEDUPE_KEY_MAX_LENGTH:
            return encoded
        digest = hashlib.blake2b(encoded.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()
        return f"{encoded[:_PREFIX_BUDGET]}{_DIGEST_MARKER}{digest}"

    def __str__(self) -> str:
        return self.value


def build_dedupe_key(
    risk_type: RiskType | str,
    scope_entity: str,
    condition_class: str,
) -> DedupeKey:
    """Build a validated dedupe key.

    Components are normalised (NFKC, whitespace-stripped, case-folded) so that ``"NVDA "``
    and ``"nvda"`` are the same condition rather than two competing live alerts. Blank
    components are rejected: an empty scope would silently merge unrelated alerts.
    """
    return DedupeKey(
        risk_type=_coerce_risk_type(risk_type),
        scope_entity=_normalise(scope_entity, field="scope_entity"),
        condition_class=_normalise(condition_class, field="condition_class"),
    )


def _coerce_risk_type(value: RiskType | str) -> RiskType:
    if isinstance(value, RiskType):
        return value
    if not isinstance(value, str):
        raise TypeError(f"risk_type must be a RiskType or string, got {type(value).__name__}")
    try:
        return RiskType(value.strip().lower())
    except ValueError as exc:
        supported = ", ".join(sorted(member.value for member in RiskType))
        raise ValueError(f"unknown risk_type {value!r}; supported: {supported}") from exc


def _normalise(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string, got {type(value).__name__}")
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if not text:
        raise ValueError(f"{field} must not be blank")
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise ValueError(f"{field} must not contain control characters: {value!r}")
    return text
