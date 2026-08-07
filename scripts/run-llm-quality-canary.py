#!/usr/bin/env python3
"""Plan or run the bounded Gemini/DeepSeek production-workload quality canary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from packages.config.settings import get_settings  # noqa: E402
from services.evaluation.llm_quality_canary import (  # noqa: E402
    ACTIVE_ROUTE_MAX_CALLS,
    ACTIVE_ROUTE_MAX_COST_USD,
    ACTIVE_ROUTE_REPORT_SCHEMA,
    DEFAULT_CASES_PATH,
    DEFAULT_MAX_CALLS,
    DEFAULT_MAX_COST_USD,
    REPORT_SCHEMA,
    LLMQualityCanaryError,
    build_active_route_canary_plan,
    build_canary_plan,
    run_active_route_canary,
    run_quality_canary,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the configured Gemini and DeepSeek routes on four synthetic production "
            "workloads. Without --live this prints a no-network execution plan."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="authorize the bounded paid provider calls",
    )
    parser.add_argument(
        "--active-route",
        action="store_true",
        help="run the fixed Gemini-T1/OpenAI-GPT-4.1-T2 active-route profile",
    )
    parser.add_argument(
        "--provider",
        action="append",
        choices=("gemini", "deepseek"),
        help="limit the matrix to one provider (repeatable)",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="limit the matrix to one case_id (repeatable)",
    )
    parser.add_argument(
        "--cases-file",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="synthetic fixture file",
    )
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--pretty", action="store_true")
    return parser


def _print_json(value: object, *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    active_route = bool(args.active_route)
    max_calls = (
        (ACTIVE_ROUTE_MAX_CALLS if active_route else DEFAULT_MAX_CALLS)
        if args.max_calls is None
        else args.max_calls
    )
    max_cost_usd = (
        (ACTIVE_ROUTE_MAX_COST_USD if active_route else DEFAULT_MAX_COST_USD)
        if args.max_cost_usd is None
        else args.max_cost_usd
    )
    try:
        if active_route and (args.provider or args.case_ids):
            raise LLMQualityCanaryError(
                "the fixed active-route profile does not accept provider or case filters"
            )
        settings = get_settings()
        if active_route and args.live:
            report = run_active_route_canary(
                settings,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
            )
        elif active_route:
            report = build_active_route_canary_plan(
                settings,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
            )
        elif args.live:
            report = run_quality_canary(
                settings,
                provider_names=args.provider,
                case_ids=args.case_ids,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
            )
        else:
            report = build_canary_plan(
                settings,
                provider_names=args.provider,
                case_ids=args.case_ids,
                max_calls=max_calls,
                max_cost_usd=max_cost_usd,
                cases_path=args.cases_file,
            )
    except LLMQualityCanaryError as exc:
        # Exception messages may contain configuration detail.  The CLI emits only a stable
        # refusal class; callers can adjust documented flags without risking a secret leak.
        _print_json(
            {
                "schema": ACTIVE_ROUTE_REPORT_SCHEMA if active_route else REPORT_SCHEMA,
                "mode": (
                    "active_route_live"
                    if active_route and args.live
                    else "active_route_plan"
                    if active_route
                    else "live"
                    if args.live
                    else "plan"
                ),
                "decision": "hold",
                "failure": {
                    "type": type(exc).__name__,
                    "reason": "preflight_or_configuration_refusal",
                },
            },
            pretty=args.pretty,
        )
        return 2

    _print_json(report, pretty=args.pretty)
    if not args.live:
        return 0
    if active_route:
        return 0 if report.get("decision") == "pass" else 1
    return 0 if report.get("decision") == "advance_to_shadow" else 1


if __name__ == "__main__":
    raise SystemExit(main())
