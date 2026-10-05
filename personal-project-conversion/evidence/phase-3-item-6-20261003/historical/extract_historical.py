#!/usr/bin/env python3
"""Reproduce credential-free historical Phase 3 browser artifacts.

Read only the selected browser output text and native screenshot payloads.
Never retain tool-call arguments, whole session records, or launcher logs.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

SOURCE = Path(
    "/Users/f8fq/.codex/sessions/2026/09/20/rollout-2026-09-20T15-41-05-01a0c0fa-efac-7f83-8231-8928b4629fe1.jsonl"
)
EXPECTED_SOURCE_SHA256 = "c83402c379a72d259102652dc65230c7861e10fa1a800f1f05b4a1c98952d025"
OUTPUT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = OUTPUT_DIR.parents[3]
TARGETS = {
    662: (
        "call_r9XRDH8KI3gON0z86dPrz0gR",
        "01-today-before-wording",
        "Initial Real-mode Today; raw readability and coverage",
    ),
    674: (
        "call_gTbQUOo7XwldHuzNCfuuwXk9",
        "02-raw-detail-loading",
        "Clicked first Read retained article; initial loading snapshot",
    ),
    685: (
        "call_swNKsq9PVGZ75vRo4Hpd6tPx",
        "03-raw-detail-loaded",
        "Loaded retained article detail; error-log inspection",
    ),
    718: (
        "call_tBzvJeaKjLJFxCkejusUqylS",
        "04-settings-initial",
        "Loaded settings, collection, backlog, zero spending and reset",
    ),
    780: (
        "call_vqtFrV1kYP7chiZ4MMLcWiYF",
        "05-settings-reload-disabled-feed",
        "Reload after lowering daily limit and deselecting synthetic source",
    ),
    787: (
        "call_pyh2VBHjHPfkXpZMbrLc2sSo",
        "06-settings-reenabled-feed",
        "Re-enabled source; retained backlog becomes eligible",
    ),
    820: (
        "call_49BA6c8BHsKdILmXUE8Af538",
        "07-today-frozen-run",
        "Returned to Today; original frozen run revision retained",
    ),
    1024: (
        "call_iceNuoYFEARf6LrRIq7Uj1zi",
        "08-today-final-wording",
        "Final Grouping is disabled wording, native image and warn/error logs",
    ),
    1053: (
        "call_tyHCgJqws9Ic1cgdZWqD85R1",
        "09-settings-navigation-loading",
        "Final Settings navigation; only loading output, not final loaded verification",
    ),
}
REVIEWED_FILES = [
    "scripts/seed-personal-phase3-browser.py",
    "personal-project-conversion/evidence/phase-3-20260920/browser-seed.json",
    "personal-project-conversion/evidence/phase-3-20260920/independent-intake-review.md",
    "personal-project-conversion/evidence/phase-3-20260920/independent-ledger-review.md",
    "personal-project-conversion/evidence/phase-3-20260920/postgres-ownership.json",
    "personal-project-conversion/evidence/phase-3-20260920/frontend-ownership.json",
    "personal-project-conversion/evidence/phase-3-20260920/spending-implementation.md",
    "personal-project-conversion/evidence/phase-3-items-2-3-20261003/README.md",
    "personal-project-conversion/evidence/phase-3-items-1-4-20261003/README.md",
    "personal-project-conversion/evidence/phase-3-item-5-20261003/README.md",
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(filename: str, data: bytes) -> dict:
    (OUTPUT_DIR / filename).write_bytes(data)
    return {"path": filename, "sha256": digest(data), "bytes": len(data)}


def main() -> None:
    source_bytes = SOURCE.read_bytes()
    assert digest(source_bytes) == EXPECTED_SOURCE_SHA256, "Historical session identity changed"
    rows = source_bytes.decode("utf-8").splitlines()
    observations = []
    for line_number, (call_id, name, action) in TARGETS.items():
        row = json.loads(rows[line_number - 1])
        payload = row["payload"]
        assert row["type"] == "response_item"
        assert payload["type"] == "function_call_output"
        assert payload["call_id"] == call_id
        blocks = payload["output"]
        parts = [
            block["text"]
            for block in blocks
            if block.get("type") == "input_text" and not block["text"].startswith("Wall time:")
        ]
        assert len(parts) == 1
        original_output = parts[0]
        dom_start = original_output.index("- complementary:")
        dom = original_output[dom_start:]
        console = None
        if line_number in {685, 820}:
            assert dom.endswith("[]")
            dom, console = dom[:-2], "[]"
        if line_number == 1024:
            marker = '- generic "Local access"'
            boundary = dom.index(marker) + len(marker)
            console = dom[boundary:]
            assert console.startswith("[")
            dom = dom[:boundary]
        # Native DOM output only; no tool-call arguments or key fill values.
        assert (
            "/placeholder: A key is already set for this tab" in dom
            or "API access key" not in dom
            or "Provider credentials never belong here." in dom
            or line_number in {718, 780}
        )
        assert "Throwaway API key:" not in dom
        assert ".fill(" not in dom
        artifacts = [put(name + ".txt", dom.encode("utf-8"))]
        if console is not None:
            artifacts.append(put(name + "-console.txt", console.encode("utf-8")))
        for block in blocks:
            if block.get("type") == "input_image":
                prefix, encoded = block["image_url"].split(",", 1)
                assert prefix == "data:image/jpeg;base64"
                artifacts.append(put(name + ".jpg", base64.b64decode(encoded, validate=True)))
        observations.append(
            {
                "session_line": line_number,
                "timestamp_utc": row["timestamp"],
                "function_call_id": call_id,
                "action": action,
                "original_output_text_sha256": digest(original_output.encode("utf-8")),
                "artifacts": artifacts,
            }
        )
    inspected_files = []
    for relative in REVIEWED_FILES:
        path = PROJECT_ROOT / relative
        inspected_files.append(
            {"path": relative, "sha256_at_consolidation": digest(path.read_bytes())}
        )
    manifest = {
        "evidence_kind": "historical_actual_chrome_browser_outputs",
        "observed_date": "2026-09-20",
        "consolidated_date": "2026-10-03",
        "observation_timezone": "America/Los_Angeles",
        "source_session": {
            "path": str(SOURCE),
            "sha256": EXPECTED_SOURCE_SHA256,
            "thread_id": "01a0c0fa-efac-7f83-8231-8928b4629fe1",
        },
        "frontend_origin": "http://127.0.0.1:61410",
        "api_origin": "http://127.0.0.1:59751",
        "frontend_url": "http://127.0.0.1:61410/SIGNAL%20-%20Intelligence%20Platform.html?api=http://127.0.0.1:59751",
        "mode": "Real with actual local API/PostgreSQL and labeled synthetic retained RSS inputs",
        "fixture": {
            "source_name": "Synthetic Phase 2 acceptance feed",
            "source_url": "https://offline.personal.test/feed.xml",
            "provider": "FakeRSSProvider",
            "seed_script": "scripts/seed-personal-phase3-browser.py",
            "run_id": "1f658636-2151-45f0-be64-bf579a0703b3",
            "captured": 4,
            "admitted": 2,
            "pending_observed_in_browser": 2,
            "paid_dispatches": 0,
        },
        "source_limitations": [
            "Inspected-file hashes identify retained source at consolidation, not the executable revision pinned when the old browser ran.",
            "No complete historical source hash manifest or retained historical API/database value export accompanies these browser observations.",
            "Old fixture titles describe intended seed roles; publication ordering admitted the Technology reading remains in backlog item. Its actual browser state is admitted raw.",
            "Unknown publication time was observed for a pending item, not an admitted raw article.",
            "The last Settings navigation output is only Reading settings, so it proves no final loaded-state check.",
            "These are historical observations, not fresh current-source browser proof.",
        ],
        "observations": observations,
        "reviewed_files": inspected_files,
        "exclusions": [
            "tool-call arguments",
            "launcher log containing a throwaway access key",
            "whole session records",
            "provider credentials",
        ],
    }
    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "observations": len(observations),
                "native_screenshots": sum(
                    any(a["path"].endswith(".jpg") for a in o["artifacts"]) for o in observations
                ),
                "manifest": str(OUTPUT_DIR / "manifest.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
