"""The day-over-day consistency post-check (report-generation spec).

The spec's one rule for continuity: **any reversal must be acknowledged explicitly**. A reversal
is a risk whose *level* changed between yesterday's cutoff and today's -- the deterministic
"What Changed Since Yesterday" section already emits one ``REVERSAL -- ...`` line per reversal,
with signed before/after values and no invented cause. This post-check verifies that every
reversal present in the section material actually survived into the final brief's text; an absent
or unacknowledged reversal fails the grounding gate.

It invents nothing. It does not author a cause for a reversal (the spec forbids an *unacknowledged*
reversal, not an *unexplained* one), and it does not decide which reversals matter -- it checks
that the deterministic acknowledgement lines that already comply are present.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.reports.contracts import RiskKey
from services.reports.material import ChangeRow


def reversal_marker(key: RiskKey) -> str:
    """The exact prefix the What Changed render emits for a reversal of ``key``.

    Kept identical to :func:`services.reports.composition._render_what_changed` so the post-check
    matches what the composer stage actually wrote, not an approximation of it.
    """

    return f"REVERSAL -- {key.target_type}:{key.target_id}:{key.risk_type}"


@dataclass(frozen=True)
class ConsistencyFinding:
    """One reversal the final brief did not acknowledge. Blocks the gate."""

    key: RiskKey
    previous_level: str
    current_level: str
    detail: str


def brief_reversals(change_rows: Sequence[ChangeRow]) -> tuple[ChangeRow, ...]:
    """The reversals among a What Changed section's change rows -- the ones that must be acknowledged."""

    return tuple(row for row in change_rows if row.is_reversal)


def check_reversal_acknowledgement(
    reversals: Sequence[ChangeRow], *, final_brief_text: str
) -> tuple[ConsistencyFinding, ...]:
    """Fail for every reversal whose acknowledgement line is absent from the final brief text.

    The deterministic What Changed section supplies the acknowledgement; this confirms it reached
    the published text rather than being dropped by a later transformation. It does not require a
    cause -- only that the reversal is stated.
    """

    findings: list[ConsistencyFinding] = []
    for row in reversals:
        if reversal_marker(row.key) not in final_brief_text:
            findings.append(
                ConsistencyFinding(
                    key=row.key,
                    previous_level=row.previous_level,
                    current_level=row.current_level,
                    detail=(
                        f"risk {row.key.target_type}:{row.key.target_id}:{row.key.risk_type} "
                        f"reversed {row.previous_level} -> {row.current_level} but the final "
                        "brief does not acknowledge it."
                    ),
                )
            )
    return tuple(findings)


__all__ = [
    "ConsistencyFinding",
    "brief_reversals",
    "check_reversal_acknowledgement",
    "reversal_marker",
]
