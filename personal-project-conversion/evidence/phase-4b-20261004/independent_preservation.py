"""Read-only baseline check; write only this phase's final preservation evidence."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASELINE = OUT / "baseline.json"


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git(*args: str) -> str:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", *args], cwd=ROOT, env=env, check=True, text=True, capture_output=True
    )
    return result.stdout


def main() -> None:
    baseline = json.loads(BASELINE.read_text())
    saved = baseline["files"]
    historical = {
        name: sha
        for name, sha in saved.items()
        if name.startswith("personal-project-conversion/evidence/")
        and not name.startswith("personal-project-conversion/evidence/phase-4b")
    }
    selected = [".env", "personal-project-conversion/SELECTED_PREFERENCES.md"]
    expected = {name: saved[name] for name in selected} | historical
    observed = {name: digest(ROOT / name) for name in expected}
    mismatches = [name for name in expected if expected[name] != observed[name]]
    index_path = Path(git("rev-parse", "--git-path", "index").strip())
    if not index_path.is_absolute():
        index_path = ROOT / index_path
    index_sha = digest(index_path)
    staged_sha = hashlib.sha256(git("diff", "--cached", "--binary").encode()).hexdigest()
    head = git("rev-parse", "HEAD").strip()
    branch = git("branch", "--show-current").strip()
    control_checks = {
        "git_index_unchanged": index_sha == baseline["index_sha256"],
        "staged_diff_unchanged": staged_sha == baseline["staged_diff_sha256"],
        "head_unchanged": head == baseline["head"],
        "branch_unchanged": branch == baseline["branch"],
    }
    baseline_changes = [name for name, sha in saved.items() if digest(ROOT / name) != sha]
    accepted = OUT / "actual-final-formatted-source-before.json"
    current_implementation = [
        "services/personal/local_runtime.py", "services/personal/paid_runtime.py",
        "services/personal/runner.py", "services/llm/cache.py", "services/llm/limiter.py",
        "apps/api/main.py", "apps/api/deps.py", "apps/api/health.py",
        "apps/api/middleware.py", "apps/api/personal_frontend.py",
        "frontend/app/api-adapter.js", "frontend/app/shell.jsx",
    ]
    accepted_hashes = json.loads(accepted.read_text())["hashes"]
    implementation_drift = [
        name for name in current_implementation if digest(ROOT / name) != accepted_hashes[name]
    ]
    result = {
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
        "kind": "independent final read-only protected baseline comparison",
        "baseline_sha256": digest(BASELINE),
        "baseline_timestamp_utc": baseline["timestamp_utc"],
        "baseline_file_count": len(saved),
        "protected_file_count": len(expected),
        "historical_evidence_file_count": len(historical),
        "protected_mismatches": mismatches,
        "all_historical_evidence_unchanged": all(name not in mismatches for name in historical),
        "env_unchanged": ".env" not in mismatches,
        "selected_preferences_unchanged": selected[1] not in mismatches,
        "control_checks": control_checks,
        "current_git_index_sha256": index_sha,
        "current_staged_diff_sha256": staged_sha,
        "current_head": head,
        "current_branch": branch,
        "baseline_files_changed": baseline_changes,
        "current_implementation_manifest": accepted.name,
        "current_implementation_paths_checked": current_implementation,
        "current_implementation_drift": implementation_drift,
        "closure_artifact_sha256": {
            name: digest(OUT / name)
            for name in ["stack-cleanup.json", "resource-cleanup.json", "independent-review.md"]
        },
        "status_document_sha256": {
            name: digest(ROOT / name)
            for name in [
                "README.md", "PERSONAL_PROJECT_CONVERSION_PLAN.md",
                "personal-project-conversion/README.md",
                "personal-project-conversion/04-local-runtime.md",
                "personal-project-conversion/evidence/phase-4b.md",
            ]
        },
    }
    result["passed"] = not mismatches and all(control_checks.values()) and not implementation_drift
    (OUT / "preservation-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps({
        "passed": result["passed"], "historical_evidence_file_count": len(historical),
        "protected_mismatches": mismatches, "control_checks": control_checks,
        "current_implementation_drift": implementation_drift,
    }, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
