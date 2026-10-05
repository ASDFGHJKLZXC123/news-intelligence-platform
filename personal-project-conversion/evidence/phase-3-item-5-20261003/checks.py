"""Offline directly related units and final tooling; imports verification environment controls."""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys

from verify import BASE_ENV, OUT, PYTHON, ROOT

UNIT_TARGETS = [
    "tests/unit/test_personal_spending.py",
    "tests/unit/test_personal_settings.py",
    "tests/unit/test_personal_runtime_readiness.py",
    "tests/unit/test_personal_contracts.py",
    "tests/unit/test_personal_claims.py",
    "tests/unit/test_personal_composition_policy.py",
    "tests/unit/test_personal_exports.py",
    "tests/unit/test_http_rss_provider.py",
]


def unit():
    import pytest

    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def blocked(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("offline unit gate cannot open network connections")
        return original(sock, address)

    def blocked_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("offline unit gate cannot open network connections")
        return original_ex(sock, address)

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked_ex
    args = ["-q", "-p", "no:cacheprovider", "--junitxml=" + str(OUT / "unit.xml"), *UNIT_TARGETS]
    try:
        with (
            (OUT / "unit.log").open("w") as log,
            contextlib.redirect_stdout(log),
            contextlib.redirect_stderr(log),
        ):
            code = int(pytest.main(args))
    finally:
        socket.socket.connect = original
        socket.socket.connect_ex = original_ex
    (OUT / "unit-command.json").write_text(
        json.dumps(
            {
                "pytest_args": args,
                "environment": BASE_ENV,
                "network_control": "all Python INET socket connections rejected",
                "exit_code": code,
            },
            indent=2,
        )
        + "\n"
    )
    print((OUT / "unit.log").read_text()[-3000:])
    sys.exit(code)


def tooling():
    records = {}
    commands = {
        "lint": [PYTHON, "-m", "ruff", "check", "--no-cache", "."],
        "format": [
            PYTHON,
            "-m",
            "ruff",
            "format",
            "--check",
            "--no-cache",
            "tests/integration/test_personal_item5_composed.py",
            "tests/integration/test_personal_bounds_upgrade.py",
            str(OUT / "verify.py"),
            str(OUT / "checks.py"),
        ],
        "whitespace": ["git", "diff", "--check"],
        "staged-whitespace": ["git", "diff", "--cached", "--check"],
    }
    for name, args in commands.items():
        result = subprocess.run(args, cwd=ROOT, env=BASE_ENV, text=True, capture_output=True)
        records[name] = {"command": args, "exit_code": result.returncode}
        (OUT / (name + ".log")).write_text(result.stdout + result.stderr)
        print(name, result.returncode, result.stdout.strip())
    (OUT / "tooling.json").write_text(json.dumps(records, indent=2) + "\n")
    assert all(record["exit_code"] == 0 for record in records.values())


if __name__ == "__main__":
    {"unit": unit, "tooling": tooling}[sys.argv[1]]()
