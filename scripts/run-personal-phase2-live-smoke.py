#!/usr/bin/env python3
"""Validate or explicitly launch the bounded Phase 2 live smoke verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.personal.live_smoke import (  # noqa: E402
    LiveSmokeConfig,
    LiveSmokeError,
    LiveSmokeLedger,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a bounded personal Phase 2 live-smoke config. Validation is the default "
            "and performs no RSS or provider requests."
        )
    )
    parser.add_argument("--config", required=True, help="Path to the secret-free JSON config")
    parser.add_argument(
        "--state-dir",
        help="Dedicated state directory; required only for explicit execution",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the production coordinator through its guarded live-smoke seam",
    )
    parser.add_argument(
        "--confirm-verification-id",
        help="Must exactly match the config verification_id for execution",
    )
    return parser


def _execute(config: LiveSmokeConfig, ledger: LiveSmokeLedger) -> MappingResult:
    """Import the worker seam only after all local authorization checks pass."""

    try:
        from workers.personal_tasks import execute_personal_live_smoke
    except (ImportError, AttributeError) as exc:
        raise LiveSmokeError("the personal worker live-smoke seam is not available") from exc
    result = execute_personal_live_smoke(config=config, ledger=ledger)
    if not isinstance(result, dict):
        raise LiveSmokeError("the personal worker returned no structured live-smoke result")
    return result


MappingResult = dict[str, Any]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = LiveSmokeConfig.from_file(args.config)
        if not args.execute:
            print(
                json.dumps(
                    {"mode": "validation_only", "configuration": config.safe_summary()},
                    sort_keys=True,
                )
            )
            return 0
        if not args.state_dir:
            raise LiveSmokeError("--state-dir is required for live execution")
        if not args.confirm_verification_id:
            raise LiveSmokeError("--confirm-verification-id is required for live execution")
        config.require_execution_authorized(args.confirm_verification_id)
        ledger_path = LiveSmokeLedger.canonical_path(args.state_dir, config.verification_id)
        ledger = LiveSmokeLedger.open(ledger_path, config)
        result = _execute(config, ledger)
        print(json.dumps({"mode": "executed", "result": result}, sort_keys=True))
        return 0
    except LiveSmokeError as exc:
        print(f"live smoke blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
