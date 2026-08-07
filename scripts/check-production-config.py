#!/usr/bin/env python3
"""Run the no-network, secret-safe production configuration preflight."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from packages.config.settings import get_settings  # noqa: E402
from services.operations.production_config import (  # noqa: E402
    PRODUCTION_CONFIG_SCHEMA,
    build_production_config_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = build_production_config_report(get_settings())
    except Exception:  # noqa: BLE001 - never expose configuration values or validation messages
        report = {
            "schema": PRODUCTION_CONFIG_SCHEMA,
            "ready": False,
            "failure": {"reason": "configuration_load_failed"},
        }
    print(
        json.dumps(
            report,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.get("ready") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
