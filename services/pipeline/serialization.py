"""Fail-closed deserialization for durable pipeline results.

The database JSON is untrusted input at this boundary: a partial write, an old schema, or a
manual edit must never be treated as a succeeded idempotency record.  Reconstruction therefore
uses the public frozen contracts and verifies the deterministic identity, exact stage graph,
terminal-state derivation, and Gate G exclusion set.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from services.pipeline.contracts import (
    ExcludedGateGStage,
    PipelineRunResult,
    PipelineStage,
    PipelineState,
    StageExecution,
    StageItemFailure,
    StageResult,
    StageStatus,
    daily_pipeline_identity,
)


class PipelineResultDeserializationError(ValueError):
    """Persisted pipeline JSON is incomplete, malformed, or internally inconsistent."""


def _mapping(value: object, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise PipelineResultDeserializationError(f"{path} must be an object with string keys")
    return value


def _sequence(value: object, *, path: str) -> Sequence[Any]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise PipelineResultDeserializationError(f"{path} must be an array")
    return value


def _string(value: object, *, path: str) -> str:
    if not isinstance(value, str):
        raise PipelineResultDeserializationError(f"{path} must be a string")
    return value


def _integer(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PipelineResultDeserializationError(f"{path} must be an integer")
    return value


def _boolean(value: object, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise PipelineResultDeserializationError(f"{path} must be a boolean")
    return value


def _optional_string(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path=path)


def _optional_nonnegative_number(value: object, *, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PipelineResultDeserializationError(f"{path} must be a nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise PipelineResultDeserializationError(f"{path} must be a finite nonnegative number")
    return number


def _enum(enum_type: type[Any], value: object, *, path: str) -> Any:
    raw = _string(value, path=path)
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise PipelineResultDeserializationError(
            f"{path} contains unsupported value {raw!r}"
        ) from exc


def _failure(value: object, *, path: str) -> StageItemFailure:
    item = _mapping(value, path=path)
    try:
        return StageItemFailure(
            item_key=_string(item.get("item_key"), path=f"{path}.item_key"),
            code=_string(item.get("code"), path=f"{path}.code"),
            message=_string(item.get("message"), path=f"{path}.message"),
            retryable=_boolean(item.get("retryable"), path=f"{path}.retryable"),
            error_type=_optional_string(item.get("error_type"), path=f"{path}.error_type"),
        )
    except (TypeError, ValueError) as exc:
        raise PipelineResultDeserializationError(f"{path} is invalid: {exc}") from exc


def _stage(value: object, *, path: str) -> StageExecution:
    item = _mapping(value, path=path)
    failures = tuple(
        _failure(failure, path=f"{path}.failures[{index}]")
        for index, failure in enumerate(_sequence(item.get("failures"), path=f"{path}.failures"))
    )
    output = _mapping(item.get("output"), path=f"{path}.output")
    dependencies = tuple(
        _enum(PipelineStage, dependency, path=f"{path}.required_dependencies[]")
        for dependency in _sequence(
            item.get("required_dependencies"),
            path=f"{path}.required_dependencies",
        )
    )
    blockers = tuple(
        _enum(PipelineStage, blocker, path=f"{path}.blocked_by[]")
        for blocker in _sequence(item.get("blocked_by"), path=f"{path}.blocked_by")
    )
    try:
        result = StageResult(
            status=_enum(StageStatus, item.get("status"), path=f"{path}.status"),
            attempted_count=_integer(
                item.get("attempted_count"),
                path=f"{path}.attempted_count",
            ),
            succeeded_count=_integer(
                item.get("succeeded_count"),
                path=f"{path}.succeeded_count",
            ),
            failures=failures,
            output=output,
        )
        if _integer(item.get("failed_count"), path=f"{path}.failed_count") != len(failures):
            raise PipelineResultDeserializationError(f"{path}.failed_count disagrees with failures")
        return StageExecution(
            stage=_enum(PipelineStage, item.get("stage"), path=f"{path}.stage"),
            required_dependencies=dependencies,
            result=result,
            blocked_by=blockers,
            duration_ms=_optional_nonnegative_number(
                item.get("duration_ms"), path=f"{path}.duration_ms"
            ),
        )
    except PipelineResultDeserializationError:
        raise
    except (TypeError, ValueError) as exc:
        raise PipelineResultDeserializationError(f"{path} is invalid: {exc}") from exc


def pipeline_run_result_from_dict(value: object) -> PipelineRunResult:
    """Reconstruct and fully validate a stored :class:`PipelineRunResult`."""

    payload = _mapping(value, path="result")
    identity_payload = _mapping(payload.get("identity"), path="result.identity")
    process_date = _string(
        identity_payload.get("process_date"),
        path="result.identity.process_date",
    )
    try:
        identity = daily_pipeline_identity(process_date)
    except (TypeError, ValueError) as exc:
        raise PipelineResultDeserializationError(
            f"result.identity.process_date is invalid: {exc}"
        ) from exc
    if _string(
        identity_payload.get("process_key"),
        path="result.identity.process_key",
    ) != identity.process_key or _string(
        identity_payload.get("process_id"),
        path="result.identity.process_id",
    ) != str(identity.process_id):
        raise PipelineResultDeserializationError("result.identity key/id do not match process_date")

    stages = tuple(
        _stage(stage, path=f"result.stages[{index}]")
        for index, stage in enumerate(_sequence(payload.get("stages"), path="result.stages"))
    )
    exclusions = tuple(
        _enum(
            ExcludedGateGStage,
            stage,
            path="result.excluded_gate_g_stages[]",
        )
        for stage in _sequence(
            payload.get("excluded_gate_g_stages"),
            path="result.excluded_gate_g_stages",
        )
    )
    try:
        return PipelineRunResult(
            identity=identity,
            state=_enum(PipelineState, payload.get("state"), path="result.state"),
            stages=stages,
            excluded_gate_g_stages=exclusions,
            idempotent_skip=_boolean(
                payload.get("idempotent_skip"),
                path="result.idempotent_skip",
            ),
        )
    except PipelineResultDeserializationError:
        raise
    except (TypeError, ValueError) as exc:
        raise PipelineResultDeserializationError(f"result is invalid: {exc}") from exc
