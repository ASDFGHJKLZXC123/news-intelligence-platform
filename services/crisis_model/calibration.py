"""Deterministic probability calibration helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from services.crisis_model.types import HorizonBuckets


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class IdentityCalibrator:
    """No-op calibrator for untrained MVP paths."""

    def calibrate(self, probability: float) -> float:
        return _clamp_probability(probability)


@dataclass(frozen=True)
class PlattCalibrator:
    """Simple parameterized Platt-style sigmoid calibrator."""

    slope: float = 1.0
    intercept: float = 0.0

    def calibrate(self, probability: float) -> float:
        p = max(1e-6, min(1.0 - 1e-6, probability))
        logit = math.log(p / (1.0 - p))
        return _clamp_probability(1.0 / (1.0 + math.exp(-(self.intercept + self.slope * logit))))


@dataclass(frozen=True)
class PiecewiseIsotonicCalibrator:
    """Monotonic piecewise-linear calibrator over supplied probability points."""

    points: tuple[tuple[float, float], ...]

    def calibrate(self, probability: float) -> float:
        if not self.points:
            return _clamp_probability(probability)
        points = tuple(sorted((float(x), float(y)) for x, y in self.points))
        p = _clamp_probability(probability)
        if p <= points[0][0]:
            return _clamp_probability(points[0][1])
        for index in range(1, len(points)):
            x0, y0 = points[index - 1]
            x1, y1 = points[index]
            if p <= x1:
                if x1 == x0:
                    return _clamp_probability(y1)
                ratio = (p - x0) / (x1 - x0)
                return _clamp_probability(y0 + ratio * (y1 - y0))
        return _clamp_probability(points[-1][1])


def calibrate_buckets(
    buckets: HorizonBuckets,
    *,
    calibrator_6m: object | None = None,
    calibrator_12m: object | None = None,
    calibrator_18m: object | None = None,
) -> HorizonBuckets:
    """Calibrate cumulative horizons and convert back to canonical buckets."""
    c6 = (calibrator_6m or IdentityCalibrator()).calibrate(buckets.probability_0_6m)
    c12 = (calibrator_12m or IdentityCalibrator()).calibrate(buckets.cumulative_12m)
    c18 = (calibrator_18m or IdentityCalibrator()).calibrate(buckets.probability_within_18m)
    c12 = max(c6, c12)
    c18 = max(c12, c18)
    return HorizonBuckets(
        round(c6, 6),
        round(c12 - c6, 6),
        round(c18 - c12, 6),
    )
