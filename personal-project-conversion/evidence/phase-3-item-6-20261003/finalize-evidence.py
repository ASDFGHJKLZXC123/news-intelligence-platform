"""Verify this scoped evidence without reading or mutating unrelated resources."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCOPE = Path(__file__).resolve().parent
ALLOWED = {
    "frontend/SIGNAL - Intelligence Platform.html",
    "frontend/app/page-personal.jsx",
    "frontend/app/personal-foundation.test.js",
    "personal-project-conversion/evidence/phase-3.md",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    baseline = json.loads((SCOPE / "before-source-hashes.json").read_text())
    changed = []
    missing = []
    current = {}
    for name, expected in baseline.items():
        path = ROOT / name
        if not path.is_file():
            missing.append(name)
            continue
        actual = digest(path.read_bytes())
        current[name] = actual
        if actual != expected:
            changed.append({"path": name, "before": expected, "after": actual})
    unexpected = [row for row in changed if row["path"] not in ALLOWED]
    staged = subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    staged_before = (SCOPE / "before-staged-diff.patch").read_bytes()
    whitespace = []
    for args in (["git", "diff", "--check"], ["git", "diff", "--cached", "--check"]):
        proc = subprocess.run(args, cwd=ROOT, capture_output=True)
        whitespace.append(
            {
                "command": args,
                "exit_code": proc.returncode,
                "stdout": proc.stdout.decode(),
                "stderr": proc.stderr.decode(),
            }
        )
    links = []
    broken = []
    for path in (SCOPE / "README.md", ROOT / "personal-project-conversion/evidence/phase-3.md"):
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if target.startswith(("http:", "https:", "#", "codex:")):
                continue
            resolved = path.parent / target.split("#", 1)[0]
            okay = resolved.exists()
            links.append({"from": str(path.relative_to(ROOT)), "target": target, "exists": okay})
            if not okay:
                broken.append(links[-1])
    # The throwaway key stays only in its task-private source file. Never retain it.
    key = (
        Path("/tmp/nip-personal-phase2-offline-p3_item6_20261003_b/browser-key")
        .read_bytes()
        .strip()
    )
    assert key, "Private task key source must exist for exact exclusion verification"
    key_matches = []
    hashes = {}
    for path in sorted(SCOPE.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(SCOPE))
        if relative in {"final-check.json", "evidence-file-hashes.json"}:
            continue
        data = path.read_bytes()
        hashes[relative] = digest(data)
        if key in data:
            key_matches.append(relative)
    capture = json.loads((SCOPE / "browser/capture-index.json").read_text())
    native_hash_failures = []
    for row in capture["captures"]:
        if digest((SCOPE / "browser" / row["dom"]).read_bytes()) != row["dom_sha256"]:
            native_hash_failures.append(row["dom"])
    for row in capture["images"]:
        if digest((SCOPE / "browser" / row["file"]).read_bytes()) != row["sha256"]:
            native_hash_failures.append(row["file"])
    # The original work log is strictly extended; its global matrix stays untouched.
    phase_before = baseline["personal-project-conversion/evidence/phase-3.md"]
    phase = (ROOT / "personal-project-conversion/evidence/phase-3.md").read_text()
    prefix = phase.split("\n## October 3, 2026: scoped remaining-work item 6\n", 1)[0]
    phase_prefix_unchanged = digest(prefix.encode()) == phase_before
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "baseline_file_count": len(baseline),
        "unchanged_file_count": len(baseline) - len(changed) - len(missing),
        "changed_baseline_files": changed,
        "unexpected_changes": unexpected,
        "missing_baseline_files": missing,
        "staged_diff_byte_identical": staged == staged_before,
        "staged_before_sha256": digest(staged_before),
        "staged_after_sha256": digest(staged),
        "phase3_original_prefix_unchanged": phase_prefix_unchanged,
        "whitespace": whitespace,
        "links": links,
        "broken_links": broken,
        "private_key_exclusion": {
            "files_scanned": len(hashes),
            "matching_files": key_matches,
            "scope": "Exact private key byte match; not OCR or all-secret detection",
        },
        "native_capture_hash_failures": native_hash_failures,
        "retained_evidence_file_count": len(hashes),
    }
    okay = (
        not unexpected
        and not missing
        and staged == staged_before
        and phase_prefix_unchanged
        and not broken
        and not key_matches
        and not native_hash_failures
        and all(row["exit_code"] == 0 for row in whitespace)
    )
    result["status"] = "PASS" if okay else "FAIL"
    (SCOPE / "evidence-file-hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
    (SCOPE / "final-check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "status",
                    "baseline_file_count",
                    "unchanged_file_count",
                    "unexpected_changes",
                    "missing_baseline_files",
                    "staged_diff_byte_identical",
                    "phase3_original_prefix_unchanged",
                    "broken_links",
                    "private_key_exclusion",
                    "native_capture_hash_failures",
                ]
            }
        )
    )
    if not okay:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
