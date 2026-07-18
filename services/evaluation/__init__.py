"""Offline evaluation contracts (Stage 8): versioned gold datasets, pure loaders, validators.

Nothing in this package touches a database, a network, spaCy, or an LLM. A gold dataset is
committed as JSON, loaded and validated deterministically, and converted into the Stage-9
input DTOs on demand. Import the concrete contract from its module, e.g.
``services.evaluation.entity_linking_gold``.

The shared, domain-neutral Stage 9 calibration primitives (deterministic grids, safe binary
metrics, canonical hashing, artifact verification, the status vocabulary) are re-exported here for
convenience; they load no gold set and open no holdout. See
``services.evaluation.calibration`` and ``docs/evaluation/stage9-validation-protocol.md``.

:mod:`services.evaluation.stage9_freeze` is deliberately **not** re-exported: it reads the four
development reports *and* the live production constants, so re-exporting it here would make every
``services.evaluation`` import pull in the whole package plus ``services.entities``. Import it
directly (``from services.evaluation.stage9_freeze import build_frozen_parameters``); the dependency
stays one-way, freeze -> domain evaluators.

:mod:`services.evaluation.entity_linking_final` is **not** re-exported either, and for a stronger
reason: it is the one-time final-holdout command, the only code path allowed to unseal the entity
holdout. Importing ``services.evaluation`` must never pull it in by accident. Import it explicitly.
"""

from services.evaluation.alert_validation import (
    AlertValidationError,
    alert_decision_boundary_grid,
    alert_input_paths,
    build_alert_validation_report,
    write_alert_validation_report,
)
from services.evaluation.analogy_validation import (
    AnalogyValidationError,
    analogy_input_paths,
    build_analogy_validation_report,
    write_analogy_validation_report,
)
from services.evaluation.calibration import (
    ArtifactVerificationError,
    BinaryMetrics,
    CalibrationError,
    CalibrationStatus,
    canonical_json_bytes,
    canonical_json_hash,
    inclusive_decimal_grid,
    sha256_file,
    validate_status,
    verify_artifact_hash,
)
from services.evaluation.clustering_calibration import (
    ClusteringCalibrationError,
    PairObservation,
    ThresholdEvaluation,
    build_clustering_report,
    clustering_grid,
    clustering_not_verifiable_report,
    evaluate_threshold,
    select_precision_knee,
    sweep,
    write_clustering_report,
)
from services.evaluation.entity_linking_calibration import (
    EntityLinkingCalibration,
    EntityLinkingCalibrationError,
    build_entity_linking_report,
    calibrate_entity_linking,
    entity_linking_grid_points,
    entity_linking_policy_grid,
)

__all__ = [
    "AlertValidationError",
    "AnalogyValidationError",
    "ArtifactVerificationError",
    "BinaryMetrics",
    "CalibrationError",
    "CalibrationStatus",
    "ClusteringCalibrationError",
    "EntityLinkingCalibration",
    "EntityLinkingCalibrationError",
    "PairObservation",
    "ThresholdEvaluation",
    "alert_decision_boundary_grid",
    "alert_input_paths",
    "analogy_input_paths",
    "build_alert_validation_report",
    "build_analogy_validation_report",
    "build_clustering_report",
    "build_entity_linking_report",
    "calibrate_entity_linking",
    "canonical_json_bytes",
    "canonical_json_hash",
    "clustering_grid",
    "clustering_not_verifiable_report",
    "entity_linking_grid_points",
    "entity_linking_policy_grid",
    "evaluate_threshold",
    "inclusive_decimal_grid",
    "select_precision_knee",
    "sha256_file",
    "sweep",
    "validate_status",
    "verify_artifact_hash",
    "write_alert_validation_report",
    "write_analogy_validation_report",
    "write_clustering_report",
]
