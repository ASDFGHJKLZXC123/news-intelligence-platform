"""Assemble independently mapped current acceptance and coordinated status drafts."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
REL = str(OUT.relative_to(ROOT))


def load(name):
    return json.loads((OUT / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n")


summaries = [
    "30 observed; exactly 20 admitted, 10 pending and 20 frozen enrichment IDs. Intake and completed enrichment remain distinct in the reader.",
    "Canonical duplicates spend no extra charge; rollback spends none. Two simultaneous HTTP retries yield one new attempt/token/delivery and preserve frozen membership and charges.",
    "A20/B2 with ceiling 4 admits A1, B1, A2, B2; fresh sessions retain the same IDs and ordinals. Older batches and known/unknown publication order are explicit.",
    "Retained candidates survive remote rotation and source disable/re-enable, then admit with original source/content and unknown publication date preserved.",
    "2 MiB response, 501-entry and 2,000-pending boundaries retain accurate statuses/counts and no over-cap insertion or fabricated totals.",
    "Midnight retry preserves logical run limits; new admissions charge their actual server-local day. Old caller dates cannot create a second same-day run or reopen intake.",
    "Disabled, missing, exhausted and failed processing stay distinguishable; available raw articles, Saved and prior Brief remain readable, with zero disabled-path paid probes.",
    "Two concurrent $0.02 requests under $0.03 shared credit permit exactly one dispatch. API-created obligations and the registered worker use the same durable boundary.",
    "Missing completion/timeout retains uncertain obligations; physical retries need distinct credit. Fresh ledger reconstruction and idempotent receipts preserve known and unknown costs.",
    "Reservation handoff revalidates the UTC dispatch month; later completion stays charged there. Local reset and current/all-period disclosures match retained browser/accounting proof.",
    "Populated 0020-to-0022 upgrade retains raw/save/report/run identities; known usage imports once, unknown usage blocks enablement and no unrestricted catch-up starts.",
    "One authored RSS hash drives default/raw/required-failure modes. Default publishes only its persisted eligible snapshot; raw selects zero enrichment; required failure persists failed stage/error and readable content.",
    "100 old raw plus 100 fresh: 100 fresh admissions, 100 old enrichment IDs, fresh capacity-deferred raw records and no second old admission charge. Raw selects zero enrichment.",
    "Retryable/exhausted failed ownership cannot transfer silently; explicit retries preserve scope and the old raw content/failure remains readable.",
]

fresh = ET.parse(OUT / "personal-final.xml")
cases = {}
for case in fresh.findall(".//testcase"):
    node = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
    assert not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")), node
    assert node not in cases
    cases[node] = {"node_id": node, "seconds": float(case.attrib["time"]), "result": "PASS"}
assert len(cases) == 100, len(cases)
assert load("personal-final-result.json") == {
    "exit_code": 0,
    "source_drift": [],
    "protected_unchanged": True,
    "inventory_unchanged": True,
}
source = load("personal-final-source-after.json")
a, b = load("acceptance-map-01-07.json"), load("acceptance-map-08-14.json")
plan = ROOT / "personal-project-conversion/03-bounded-daily-use.md"
requirements = {}
for line in plan.read_text().splitlines():
    if line.startswith("| P3-"):
        cells = [s.strip() for s in line.strip("|").split("|")]
        requirements[cells[0]] = {"input_action": cells[1], "required_result": cells[2]}
assert len(requirements) == 14
second_tests = {t["node_id"]: t for t in b["test_results"]}
rows = []
for i in range(1, 15):
    rid = f"P3-{i:02}"
    if i <= 7:
        mapped = next(r for r in a["rows"] if r["id"] == rid)
        assert mapped["status"] == "PASS", rid
        mapped_tests = {t["nodeid"]: t for t in mapped["tests"]}
        submap = "acceptance-map-01-07.json"
    else:
        mapped = next(r for r in b["acceptance"] if r["id"] == rid)
        assert mapped["status"] in ("PASS", "covered_synthetic"), (rid, mapped["status"])
        node_ids = {n for c in mapped["claims"] for n in c["test_node_ids"]}
        mapped_tests = {n: second_tests[n] for n in sorted(node_ids)}
        submap = "acceptance-map-08-14.json"
    proof = []
    for node, details in mapped_tests.items():
        assert node in cases, (rid, node)
        file = node.split("::")[0]
        assert digest(ROOT / file) == source["hashes"][file]
        proof.append(
            {
                **cases[node],
                "junit": REL + "/personal-final.xml",
                "log": REL + "/personal-final.log",
                "test_source_sha256": source["hashes"][file],
                "mapped_assertions": details,
            }
        )
    rows.append(
        {
            "id": rid,
            "status": "PASS",
            **requirements[rid],
            "actual": summaries[i - 1],
            "current_proof": proof,
            "claim_map": REL + "/" + submap,
            "detailed_mapping": mapped,
            "remaining_gap": None,
        }
    )
for i, name in (
    (2, "test_two_concurrent_http_retries_rotate_one_attempt_and_preserve_frozen_admissions"),
    (6, "test_http_old_caller_dates_cannot_backdate_run_or_reopen_same_day_admission"),
):
    assert any(name in p["node_id"] for p in rows[i - 1]["current_proof"])
assert sum("test_same_authored_rss_fixture" in p["node_id"] for p in rows[11]["current_proof"]) == 3
save(
    "acceptance-matrix.json",
    {
        "schema": "phase3-current-acceptance-v1",
        "user_date": "2026-10-03",
        "timezone": "America/Los_Angeles",
        "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "status": "Complete: local/synthetic engineering acceptance",
        "totals": {"pass": 14, "pending": 0, "fail": 0},
        "rows": rows,
        "source_manifest": REL + "/personal-final-source-after.json",
        "fresh_gate": {"tests": 100, "junit": REL + "/personal-final.xml", "includes_new_cases": 5},
        "totals_policy": "100 includes the 95 earlier personal cases plus five new cases; repeated and earlier gate totals overlap and are never added.",
        "qualification": "Production HTTP/registered task/coordinator/ledger components with scripted broker/provider transport. No real paid activation, service/process restart, personal usefulness or clean release claimed.",
        "history": REL + "/historical-phase-3-record.txt",
    },
)

intro = """# Phase 3 acceptance and verification

Status: **Complete — local/synthetic engineering acceptance, October 3, 2026 (America/Los_Angeles).** All fourteen requirements below have explicit current evidence and independent review. Phases 1–2 and CORE-01 remain complete. Ordinary paid runtime remains off; saved preferences are unapplied and no recurring spending allowance has been authorized. Phase 4A, Phase 4B and Phase 5 remain not started.

This is the authoritative current Phase 3 status. The original pending matrix and earlier scoped logs are retained as dated history below and in the byte-identical archive. They no longer describe current acceptance.

## Current acceptance

"""
table = "| Acceptance | Status | Actual result | Exact proof |\n| --- | --- | --- | --- |\n"
for i, row in enumerate(rows):
    sub = "acceptance-map-01-07.md" if i < 7 else "acceptance-map-08-14.md"
    anchor = row["id"].lower()
    table += f"| {row['id']} | PASS | {row['actual']} | [Mapped assertions](phase-3-closeout-20261003/{sub}#{anchor}); [current per-case proof](phase-3-closeout-20261003/acceptance-matrix.json) |\n"
body = """
Acceptance totals: **14 PASS, 0 pending, 0 open recorded failures.** Original failures remain retained; the real failure-stage persistence defect is fixed and verified, and the publication oracle now checks the app's actual published report/snapshot rather than an invented status key.

## Verification and limits

The closeout refreshed **100 personal PostgreSQL integration cases: 95 existing and five new cases**, all passing with no skips/errors/failures. The five new cases close concurrent-retry, caller-old-date and matched-fixture default/raw/failure gaps. Independent execution reran those same five cases. Their original first gate (two failures/six passes), preceding five-case gate and final runs are retained separately. Repeated checks do not create extra distinct coverage.

The earlier item-5 **238 integration / 175 related unit** and item-6 **50 frontend** results are inspected retained evidence, not freshly rerun here. The one-line coordinator correction supersedes the earlier coordinator hash; every mapped personal integration case was freshly rerun against the corrected source. Item-6 frontend source and native Chrome proof still match their retained verification. Historical September results remain historical. [Commands, JUnit, source identities, native provenance, corrections and cleanup](phase-3-closeout-20261003/README.md).

The production HTTP handlers, registered task, coordinator, PostgreSQL invariants and guarded serializers/adapters execute with scripted broker delivery, feed/provider transport and clocks. Ledger/session reconstruction proves durable recovery; an actual broker/worker service restart, operating-system crash, real billed month transition and personal usefulness are not inferred. Native Chrome proof loaded existing CDN prerequisites; no completely isolated browser-network claim is made. This shared dirty checkout is not a clean tracked release.

[Independent review](phase-3-closeout-20261003/independent-review.md), [owned resource cleanup](phase-3-closeout-20261003/resource-cleanup.json) and [final preservation/status checks](phase-3-closeout-20261003/final-check.json) retain the disposition. Current cleanup removes only the fresh exact-owned tmpfs PostgreSQL container, restores its database/role inventory and preserves all twenty preexisting container records. Earlier broad comparison failures and their separate adjudications remain visible; the historical stack missing its original ownership manifest remains preserved.

## Status maintenance and next dependency

The phase plan, conversion index and root pointers show this phase-level status and link here. Any later behavior change must identify affected P3 criteria and update their evidence/status in the same change. Keep prior results dated; mark invalidated or missing proof pending with its exact gap. Suite totals alone cannot promote acceptance.

The next building-plan milestone is **Phase 4A**, replacing always-running Celery worker/scheduler transport with a supervised on-demand child while retaining Redis initially. Its concurrency, ownership, deadlines and recovery evidence belongs to Phase 4; Phase 4B and Phase 5 follow separately. No Phase 4/5 implementation or live activation occurred in this closeout.

## Historical status and scoped logs

The following original record is preserved unchanged. Its pending totals and assignment-specific open-work statements describe their recorded time and are superseded by the current acceptance above. [Byte-identical archive and provenance](phase-3-closeout-20261003/historical-record-provenance.json).

<details>
<summary>Original Phase 3 status and dated scoped records</summary>

"""
archive = (OUT / "historical-phase-3-record.txt").read_text()
phase3 = intro + table + body + archive + "\n</details>\n"
(OUT / "acceptance-matrix.md").write_text(
    intro
    + table.replace("phase-3-closeout-20261003/", "")
    + "\nSee [the current acceptance JSON](acceptance-matrix.json) for every exact expected/actual claim, JUnit node and test-source hash.\n"
)

documents = {"personal-project-conversion/evidence/phase-3.md": phase3}
replacements = {
    "personal-project-conversion/03-bounded-daily-use.md": [
        (
            "**Status: implementation in progress.** Phases 1 and 2 are complete. The [Phase 3 implementation record](evidence/phase-3.md) distinguishes delivered changes from pending acceptance.",
            "**Status: complete for local/synthetic engineering acceptance.** Phases 1 and 2 are complete. The [authoritative Phase 3 acceptance record](evidence/phase-3.md) maps all fourteen passed criteria, current proof and retained historical status. Ordinary paid activation remains off; Phase 4/5 remain not started.",
        )
    ],
    "personal-project-conversion/README.md": [
        (
            "**Phases 1 and 2 are complete; Phase 3 is in progress; Phases 4–5 have not started.**",
            "**Phases 1–3 are complete; Phase 3 has local/synthetic engineering acceptance with ordinary paid activation off. Phases 4–5 have not started.**",
        ),
        (
            "| Phase 3 | Written | In progress | [Settings, retained intake, raw reading, durable ledger and ordinary guarded worker implemented; scoped items 1–4 verified; real paid activation remains off and global acceptance is pending](evidence/phase-3.md). |",
            "| Phase 3 | Written | Complete | [All P3-01–P3-14 accepted with mapped local/synthetic proof and independent review; ordinary paid activation remains off](evidence/phase-3.md). |",
        ),
        (
            "[Phase 3](evidence/phase-3.md) is in progress; Phases 4–5 remain not started.",
            "[Phase 3](evidence/phase-3.md) is complete for local/synthetic engineering acceptance; Phases 4–5 remain not started.",
        ),
        (
            "Update this index only when the phase evidence supports the change.",
            "Update this index only when the phase evidence supports the change. Phase 3's authoritative acceptance table is `evidence/phase-3.md`; behavior changes must update affected criteria and evidence in the same change, retaining prior proof as dated history.",
        ),
    ],
    "README.md": [
        (
            "remains inactive; Phase 3 is in progress and Phases 4–5 have not started. See the\n[Phase 2 record]",
            "remains inactive; Phase 3 is complete for local/synthetic engineering acceptance and Phases 4–5\nhave not started. See the [current Phase 3 acceptance record](personal-project-conversion/evidence/phase-3.md),\n[Phase 2 record]",
        )
    ],
    "PERSONAL_PROJECT_CONVERSION_PLAN.md": [
        (
            "Phase 3 is in progress; see its [implementation and acceptance record]",
            "Phase 3 is **complete for local/synthetic engineering acceptance**, with all fourteen criteria passed and ordinary paid activation still off; see its [authoritative acceptance record]",
        )
    ],
}
for relative, changes in replacements.items():
    text = (ROOT / relative).read_text()
    for before, after in changes:
        assert text.count(before) == 1, (relative, before)
        text = text.replace(before, after)
    documents[relative] = text
save(
    "proposed-status-documents.json",
    {
        "documents": documents,
        "apply_policy": "One coordinated application after current proof and independent rerun; original historical bytes retained.",
    },
)
if "--apply" in sys.argv:
    independent = ET.parse(OUT / "independent-closeout-proof.xml")
    assert len(independent.findall(".//testcase")) == 5
    assert not any(independent.findall(f".//{tag}") for tag in ("failure", "error", "skipped"))
    assert load("independent-closeout-proof-result.json")["exit_code"] == 0
    assert load("resource-cleanup.json")["preexisting_containers_unchanged"]
    baseline = load("before-file-hashes.json")
    for relative in documents:
        assert digest(ROOT / relative) == baseline[relative], relative
    for relative, text in documents.items():
        (ROOT / relative).write_text(text)
    save(
        "status-update.json",
        {
            "updated_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            "documents": {p: digest(ROOT / p) for p in documents},
            "historical_record_bytes_embedded_unchanged": archive in phase3,
        },
    )
print(
    json.dumps(
        {
            "acceptance_rows": len(rows),
            "current_integration_passes": len(cases),
            "status_documents": len(documents),
            "applied": "--apply" in sys.argv,
        }
    )
)
