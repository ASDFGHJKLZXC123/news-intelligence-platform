#!/usr/bin/env python3
"""Plan or run the bounded Gemini/DeepSeek synthetic shadow evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from packages.config.settings import get_settings  # noqa: E402
from services.evaluation.llm_shadow_evaluation import (  # noqa: E402
    DEFAULT_CASES_PATH,
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_COST_USD,
    DIAGNOSTIC_MAX_CALLS,
    DIAGNOSTIC_MAX_COST_USD,
    DIAGNOSTIC_REPORT_SCHEMA,
    REPORT_SCHEMA,
    build_shadow_diagnostic_plan,
    build_shadow_plan,
    run_shadow_diagnostic,
    run_shadow_evaluation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the configured Gemini and DeepSeek routes on 125 synthetic production-"
            "workload cases. Without --live this prints a no-network execution plan."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="authorize bounded paid provider calls",
    )
    parser.add_argument(
        "--diagnostic-only",
        action="store_true",
        help=(
            "use the fixed nine-pair failure-triage profile; this profile is never "
            "promotion-eligible"
        ),
    )
    parser.add_argument(
        "--cases-file",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="synthetic shadow family fixture",
    )
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--quiet-progress",
        action="store_true",
        help="suppress aggregate progress counters on stderr",
    )
    return parser


def _print_json(value: object, *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _progress(value: Mapping[str, Any]) -> None:
    print(
        "shadow-progress "
        f"{int(value['completed_pairs'])}/{int(value['expected_pairs'])} "
        f"provider={value['provider']} workload={value['workload']} "
        f"status={value['status']} calls={int(value['network_calls'])} "
        f"cost_upper_usd={float(value['accounted_cost_upper_bound_usd']):.6f}",
        file=sys.stderr,
        flush=True,
    )


def _diagnostic_progress(value: Mapping[str, Any]) -> None:
    print(
        "shadow-diagnostic-progress "
        f"{int(value['completed_pairs'])}/{int(value['expected_pairs'])} "
        f"calls={int(value['network_calls'])} "
        f"cost_upper_usd={float(value['accounted_cost_upper_bound_usd']):.6f}",
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    diagnostic = bool(args.diagnostic_only)
    max_calls = (
        (DIAGNOSTIC_MAX_CALLS if diagnostic else DEFAULT_MAX_CALLS)
        if args.max_calls is None
        else args.max_calls
    )
    max_cost_usd = (
        (DIAGNOSTIC_MAX_COST_USD if diagnostic else DEFAULT_MAX_COST_USD)
        if args.max_cost_usd is None
        else args.max_cost_usd
    )
    try:
        settings = get_settings()
        if diagnostic and args.live:
            report = run_shadow_diagnostic(
                settings,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
                progress=None if args.quiet_progress else _diagnostic_progress,
            )
        elif diagnostic:
            report = build_shadow_diagnostic_plan(
                settings,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
            )
        elif args.live:
            report = run_shadow_evaluation(
                settings,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
                progress=None if args.quiet_progress else _progress,
            )
        else:
            report = build_shadow_plan(
                settings,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
            )
    except Exception:  # noqa: BLE001 - stdout must never receive configuration/error details
        _print_json(
            {
                "schema": DIAGNOSTIC_REPORT_SCHEMA if diagnostic else REPORT_SCHEMA,
                "mode": (
                    "diagnostic_live"
                    if diagnostic and args.live
                    else "diagnostic_plan"
                    if diagnostic
                    else "live"
                    if args.live
                    else "plan"
                ),
                "decision": "hold",
                "failure": {
                    "reason": "preflight_or_configuration_refusal",
                },
            },
            pretty=args.pretty,
        )
        return 2

    _print_json(report, pretty=args.pretty)
    if not args.live:
        return 0
    if diagnostic:
        return (
            0
            if report.get("execution_status") == "complete"
            and report.get("complete_profile") is True
            and int(report.get("cleanup_failure_count", 0)) == 0
            else 1
        )
    return 0 if report.get("decision") == "advance_to_limited_rollout" else 1


if __name__ == "__main__":
    raise SystemExit(main())
