"""Phase 5 calibration helper tests."""

from __future__ import annotations

from services.crisis_model.calibration import (
    IdentityCalibrator,
    PiecewiseIsotonicCalibrator,
    PlattCalibrator,
    calibrate_buckets,
)
from services.crisis_model.types import HorizonBuckets


def test_identity_calibrator_preserves_probability() -> None:
    assert IdentityCalibrator().calibrate(0.27) == 0.27


def test_platt_calibrator_is_deterministic_and_bounded() -> None:
    calibrated = PlattCalibrator(slope=0.8, intercept=0.1).calibrate(0.35)

    assert 0.0 <= calibrated <= 1.0
    assert calibrated == PlattCalibrator(slope=0.8, intercept=0.1).calibrate(0.35)


def test_piecewise_isotonic_calibrator_interpolates() -> None:
    calibrator = PiecewiseIsotonicCalibrator(points=((0.0, 0.0), (0.5, 0.4), (1.0, 0.9)))

    assert calibrator.calibrate(0.25) == 0.2
    assert calibrator.calibrate(0.75) == 0.65


def test_calibrate_buckets_preserves_monotonic_canonical_buckets() -> None:
    buckets = HorizonBuckets(0.10, 0.15, 0.20)
    calibrated = calibrate_buckets(
        buckets,
        calibrator_6m=IdentityCalibrator(),
        calibrator_12m=PiecewiseIsotonicCalibrator(((0.0, 0.0), (1.0, 0.8))),
        calibrator_18m=PiecewiseIsotonicCalibrator(((0.0, 0.0), (1.0, 0.7))),
    )
    payload = calibrated.as_payload()

    assert payload["probability_0_6m"] <= payload["probability_0_6m"] + payload["probability_6_12m"]
    assert payload["probability_within_18m"] >= payload["probability_0_6m"]
