"""Verify acceptance provenance, coordinated status, preservation and owned cleanup."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
}


def load(name):
    return json.loads((OUT / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def junit(name):
    tree = ET.parse(OUT / name)
    cases = {}
    for case in tree.findall(".//testcase"):
        node = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
        assert not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")), node
        cases[node] = case.attrib
    return cases


matrix = load("acceptance-matrix.json")
assert matrix["totals"] == {"pass": 14, "pending": 0, "fail": 0}
assert [r["id"] for r in matrix["rows"]] == [f"P3-{i:02}" for i in range(1, 15)]
current, independent = junit("personal-final.xml"), junit("independent-closeout-proof.xml")
assert len(current) == 100 and len(independent) == 5
assert set(independent) <= set(current)
source = load("personal-final-source-after.json")
for row in matrix["rows"]:
    assert row["status"] == "PASS" and row["remaining_gap"] is None
    assert row["current_proof"]
    for proof in row["current_proof"]:
        assert proof["node_id"] in current and proof["result"] == "PASS"
        relative = proof["node_id"].split("::")[0]
        assert digest(ROOT / relative) == proof["test_source_sha256"] == source["hashes"][relative]
for prefix in ("personal-final", "independent-closeout-proof"):
    result = load(prefix + "-result.json")
    assert result["exit_code"] == 0 and result["source_drift"] == []
    assert result["protected_unchanged"] and result["inventory_unchanged"]
    resources = load(prefix + "-resources.json")
    assert resources["inventory_unchanged"] and resources["all_owned_databases_removed"]
    assert all(
        d["created_by_this_run"] and d["identity_verified_before_drop"] and d["absent_after_drop"]
        for d in resources["databases"]
    )
initial_source = load("source-before.json")
for relative, sha in initial_source["protected_hashes"].items():
    assert digest(ROOT / relative) == sha, relative
assert (
    subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    == (OUT / "before-staged-diff.patch").read_bytes()
)

baseline = load("before-file-hashes.json")
allowed_docs = set(load("scope.json")["allowed_status_documents"])
allowed_changes = allowed_docs | {"services/personal/coordinator.py"}
changed, missing = [], []
for relative, sha in baseline.items():
    path = ROOT / relative
    if not path.is_file():
        missing.append(relative)
    elif digest(path) != sha:
        changed.append(relative)
assert not missing and set(changed) == allowed_changes, (missing, changed)
added = []
for path in ROOT.rglob("*"):
    if not path.is_file() or EXCLUDED & set(path.relative_to(ROOT).parts):
        continue
    if path.is_relative_to(OUT):
        continue
    relative = str(path.relative_to(ROOT))
    if relative not in baseline:
        added.append(relative)
assert added == ["tests/integration/test_personal_phase3_closeout.py"], added
for relative, sha in source["hashes"].items():
    if relative in allowed_docs:
        continue
    assert digest(ROOT / relative) == sha, ("post-gate source drift", relative)
archive = OUT / "historical-phase-3-record.txt"
assert digest(archive) == baseline["personal-project-conversion/evidence/phase-3.md"]
active_phase = ROOT / "personal-project-conversion/evidence/phase-3.md"
assert archive.read_bytes() in active_phase.read_bytes()
current_phase = active_phase.read_text().split("<details>")[0]
assert "14 PASS, 0 pending, 0 open recorded failures" in current_phase
assert "implementation in progress" not in current_phase
for relative in allowed_docs:
    text = (ROOT / relative).read_text()
    if relative == "personal-project-conversion/evidence/phase-3.md":
        text = current_phase
    assert "Phase 3 is in progress" not in text and "Status: implementation in progress" not in text
    assert "local/synthetic engineering acceptance" in text
    assert "not started" in text
plan = (ROOT / "personal-project-conversion/03-bounded-daily-use.md").read_text()
for row in matrix["rows"]:
    expected = f"| {row['id']} | {row['input_action']} | {row['required_result']} |"
    assert expected in plan, row["id"]
cleanup = load("resource-cleanup.json")
assert (
    cleanup["container_absent"]
    and cleanup["no_owned_volume"]
    and cleanup["database_role_inventory_restored"]
)
assert cleanup["preexisting_count"] == 20 and cleanup["preexisting_containers_unchanged"]
owned = load("owned-resource.json")
absence = subprocess.run(["docker", "inspect", owned["id"]], cwd=ROOT, capture_output=True)
assert absence.returncode != 0 and b"no such object" in absence.stderr.lower()
assert json.loads(absence.stdout) == [] and owned["id"].encode() in absence.stderr


def anchors(text):
    result = set(re.findall(r'<a\s+id="([^"]+)"', text))
    for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.M):
        heading = re.sub(r"<[^>]*>|[`*_]", "", heading).lower()
        heading = "".join(
            c
            for c in heading
            if c.isalnum() or c in " -_" or unicodedata.category(c).startswith("M")
        )
        result.add(heading.replace(" ", "-"))
    return result


final_output = OUT / "final-check.json"
if not final_output.exists():
    final_output.write_text(
        json.dumps(
            {
                "status": "Verification pending",
                "purpose": "Provisional self-output before exact final link pass; only successful completion promotes PASS.",
            },
            indent=2,
        )
        + "\n"
    )

bad_links = []
checked = 0
markdown = [ROOT / p for p in sorted(allowed_docs)] + sorted(OUT.glob("*.md"))
for path in markdown:
    text = path.read_text()
    for raw in re.findall(r"(?<!!)\[[^\]]*\]\(([^\n)]+)\)", text):
        raw = raw.strip().strip("<>")
        if re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I):
            continue
        destination, _, anchor = raw.partition("#")
        target = path.parent / unquote(destination) if destination else path
        checked += 1
        if not target.is_file():
            bad_links.append(
                {"from": str(path.relative_to(ROOT)), "target": raw, "error": "missing file"}
            )
        elif (
            anchor and target.suffix == ".md" and unquote(anchor) not in anchors(target.read_text())
        ):
            bad_links.append(
                {"from": str(path.relative_to(ROOT)), "target": raw, "error": "missing anchor"}
            )
assert not bad_links, bad_links
for args in (["git", "diff", "--check"], ["git", "diff", "--cached", "--check"]):
    subprocess.run(args, cwd=ROOT, capture_output=True, check=True)
review = load("independent-final-check.json")
assert review["status"] == "PASS"
value = {
    "status": "PASS",
    "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
    "user_date": "2026-10-03",
    "acceptance": matrix["totals"],
    "current_personal_integration": 100,
    "independent_repeated_new_cases": 5,
    "original_failed_gate_retained": "final-targeted.xml",
    "baseline_files": len(baseline),
    "changed_existing_files": sorted(changed),
    "added_source_files": added,
    "earlier_evidence_unchanged": True,
    "operational_preferences_env_index_staged_diff_unchanged": True,
    "historical_record_byte_identical": True,
    "all_fresh_gate_source_hashes_match_except_authorized_status_updates": True,
    "local_links_and_anchors_checked": checked,
    "broken_links": bad_links,
    "owned_container_absence_rechecked": True,
    "preexisting_containers_preserved": 20,
    "proof_sha256": {
        name: digest(OUT / name)
        for name in (
            "acceptance-matrix.json",
            "personal-final.xml",
            "personal-final-result.json",
            "personal-final-source-after.json",
            "independent-closeout-proof.xml",
            "independent-closeout-proof-result.json",
            "resource-cleanup.json",
            "independent-final-check.json",
            "historical-phase-3-record.txt",
        )
    },
}
(OUT / "final-check.json").write_text(json.dumps(value, indent=2) + "\n")
print(
    json.dumps(
        {
            "status": value["status"],
            "acceptance": value["acceptance"],
            "baseline_files": len(baseline),
            "local_links_checked": checked,
            "changed_files": len(changed),
            "added_source_files": len(added),
        }
    )
)
