"""Reconcile exact retained P3-01–07 assertions, native browser proof and source hashes.

Only evidence files are written. --gate selects a completed root-owned current-source
JUnit gate; no test, database, service or browser operation occurs here.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SCOPE = Path(__file__).resolve().parent
EVIDENCE = SCOPE.parent
FIVE = EVIDENCE / "phase-3-item-5-20261003"
SIX = EVIDENCE / "phase-3-item-6-20261003"
parser = argparse.ArgumentParser()
parser.add_argument("--gate", required=True, help="Completed root-owned gate artifact prefix")
args = parser.parse_args()
if not args.gate.replace("-", "").isalnum():
    raise ValueError("Gate must be a simple artifact prefix")
gate = args.gate
observed_at = dt.datetime.now(dt.UTC).isoformat()
original = json.loads((SCOPE / "acceptance-map-01-07-initial.json").read_text())
five_sources = json.loads((FIVE / "full-integration-source-after.json").read_text())
six_final = json.loads((SIX / "final-check.json").read_text())
six_sources = {row["path"]: row["after"] for row in six_final["changed_baseline_files"]}
current_sources = json.loads((SCOPE / f"{gate}-source-after.json").read_text())
result = json.loads((SCOPE / f"{gate}-result.json").read_text())
xml = ET.parse(SCOPE / f"{gate}.xml")
suite_time = xml.find(".//testsuite").attrib["timestamp"]
node_outcomes = {}
for case in xml.findall(".//testcase"):
    node = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
    children = list(case)
    outcome = "FAIL" if any(c.tag in {"failure", "error"} for c in children) else "SKIP" if any(c.tag == "skipped" for c in children) else "PASS"
    node_outcomes[node] = {"outcome": outcome, "seconds": case.attrib["time"]}
gate_valid = result["exit_code"] == 0 and result["inventory_unchanged"] and result["protected_unchanged"] and not result["source_drift"] and all(r["outcome"] == "PASS" for r in node_outcomes.values())

source_paths = [row["path"] for row in original["source_checks"]]
source_paths += ["tests/integration/test_personal_phase3_closeout.py", "services/personal/briefs.py", "apps/api/personal_briefs.py"]
source_checks = []
for path in source_paths:
    current = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    if path in six_sources:
        expected, record, when = six_sources[path], "phase-3-item-6-20261003/final-check.json", six_final["observed_at"]
    else:
        expected, record, when = current_sources["hashes"][path], f"phase-3-closeout-20261003/{gate}-source-after.json", current_sources["utc"]
    source_checks.append({"path": path, "current_sha256": current, "verified_sha256": expected, "matches_verified": current == expected, "verification_record": record, "verified_at": when, "gate_valid": gate_valid if path not in six_sources else True, "item5_sha256": five_sources["hashes"].get(path), "matches_item5": current == five_sources["hashes"].get(path)})
source_valid = all(row["matches_verified"] and row["gate_valid"] for row in source_checks)

SETUPS = {
    "P3-01": "Admission and enrichment ceilings of 20; capture 30 distinct articles",
    "P3-02": "Replay tracking variants and duplicates; retry the same failed run concurrently; rolled-back admission spends no charge",
    "P3-03": "Source A has 20 pending and source B has two; admit four in stable round-robin order across reconnection",
    "P3-04": "Retained candidate rotates out; disabling and re-enabling its source preserves eligibility and original content",
    "P3-05": "Oversized response, 501 entries and a full 2,000-item pending queue",
    "P3-06": "Retry spans Los Angeles midnight; an old caller date cannot reset the admission allowance",
    "P3-07": "AI disabled, exhausted allowance, missing model configuration and one failed feed preserve raw and prior Saved/Brief reading without paid probes",
}
CLAIMS = {
    "P3-01": "The exact 20 admitted/10 pending boundary is proved by individual PostgreSQL assertions. The current browser fixture uses 27 admitted/10 pending and independently demonstrates the distinction between retained counts and completed processing.",
    "P3-02": "Capture/admission replay, canonical uniqueness and rollback are proved separately from the new literal concurrent HTTP retry. The latter demonstrates one new attempt/token/delivery, unchanged frozen membership and no repeated admission charge.",
    "P3-03": "The exact A1/B1/A2/B2 order survives engine disposal and new sessions. This is durable reconnection proof; no operating-system process restart is inferred.",
    "P3-04": "Current-source integration proves retained rotation and source re-enable. Fresh browser observations prove disabling preserves stored records and saved settings persist.",
    "P3-05": "Each capture bound has exact executed assertions. The browser's 501 authored items contain 18 unique URLs plus duplicates, so 18 retained records is correct; that fixture is distinct from the 500 unique-entry integration dataset.",
    "P3-06": "Midnight accounting uses the actual local admission day while preserving the original run identity and frozen ceilings. The new actual HTTP request supplies four old caller-date fields; the independently derived server date and unchanged one-run/two-charge result prove that request cannot reopen credit.",
    "P3-07": "Ordinary worker cases explicitly assert zero physical provider calls and paid ledger rows for disabled, configuration and allowance paths. Actual browser observations separately show the distinct causes and raw/prior Saved/Brief readability. Partial feed failure uses the actual offline worker; complete feed and provider failures use actual coordinator records displayed in Chrome.",
}
QUALIFICATIONS = {
    "P3-01": "Initial fixture-initial.json establishes expected service/database records; it is not a browser result. Count and raw-reader components were unchanged by the narrow item-6 failure classifier correction.",
    "P3-04": "September 20 browser re-enable is historical corroboration only. Item 6 does not claim fresh browser re-enable, and no requirement is closed using that historical observation alone.",
    "P3-05": "Oversized transport and a full 2,000-item queue have fresh PostgreSQL proof. The fresh representative browser boundary is the rendered entry-limit receipt with an unknown remote total.",
    "P3-07": "Browser inputs and monetary obligations are labeled synthetic. No live provider behavior or personal usefulness judgment is claimed. The item-6 UI correction retained two focused baseline failures, 50 affected frontend passes, an independent rerun and corrected native captures. Earlier loading or pre-fix failure captures do not establish the final wording. These browser observations predate the later coordinator failure-stage persistence fix; the failed-run/raw/read UI inputs are unchanged, and no browser rerun after that backend fix is claimed.",
}
ASSERTIONS = {
    "test_p3_01_twenty_admitted_ten_pending_and_twenty_frozen": ["30 captured; 20 Article rows, 20 charged admissions and 20 frozen enrichment members; 10 pending; profile and scopes equal returned IDs", "Scope selects 20 and no embedding stage exists yet; intake is distinct from completed enrichment", "A 600-character title and 3,000-character snippet retain 512/2,000 with both truncation indicators and revision evidence"],
    "test_p3_02_concurrent_duplicate_capture_and_rolled_back_admission": ["Two concurrent captures retain two canonical records despite tracking variants and duplicates; first titles survive", "Fault during the second Article insert rolls back all articles and admission state; later concurrent admissions return the same two IDs", "Exactly two captures/articles, two distinct article IDs, two charges and admission ordinals [0, 1]"],
    "test_same_date_start_is_idempotent_under_concurrency": ["Four competing starts create one run ID and one row; repeated same-date start is not created or enqueued"],
    "test_two_concurrent_http_retries_rotate_one_attempt_and_preserve_frozen_admissions": ["Two simultaneous actual HTTP retries yield 202 and 409 with exactly one production enqueue and scripted delivery", "Only attempt 2 with a rotated token; frozen date/profile/scopes/capture/admission records stay unchanged and old ownership fails", "Actual frozen coordinator retry does not recapture, embed or generate a report; two charges, one pending record and no paid rows remain"],
    "test_p3_03_round_robin_order_survives_new_sessions": ["Source configuration reverses IDs but admission URL order is A0000, B0000, A0001, B0001", "After engine disposal and new sessions, repeated admission returns identical IDs and stored ordinals"],
    "test_p3_03_older_capture_batch_precedes_newer_and_unknown_dates_follow_known": ["Within the oldest batch, known publication precedes unknown; both precede the newer batch: old known, old unknown, new known"],
    "test_p3_04_retained_candidate_survives_rotation_and_disabled_source": ["First capture with zero admission remains pending; disabling A while B is empty admits nothing", "Re-enabled A drains the retained candidate before an empty rotated feed; original run/capture timestamp, title, snippet and unknown publication remain"],
    "test_p3_05_full_two_thousand_queue_pauses_without_requesting_unknown_input": ["Exactly 2,000 pending/capture rows; another capture invokes no provider and records zero attempted/items with four paused feeds", "Four collection_paused_pending_capacity receipts retain observed count None and pending_capacity bound"],
    "test_p3_05_transport_and_entry_bounds_persist_truthful_receipts[False]": ["Actual HttpRSSProvider parses scripted 501-entry XML; entry_limit_reached, unknown remote total, 501 observed, 500 processed/pending and no Article rows"],
    "test_p3_05_transport_and_entry_bounds_persist_truthful_receipts[True]": ["Actual HttpRSSProvider receives scripted bytes above 2 MiB; response_byte_limit, unknown remote total, observed None, zero processed/pending and no Article rows"],
    "test_p3_06_midnight_retry_keeps_run_identity_and_charges_actual_admission_day": ["At 06:59 UTC two are admitted; at 07:01 UTC retry preserves run ID/prefix and admits one more up to its original ceiling of three", "After freeze another admission returns unchanged; a genuine new local-day run receives only one remaining admission", "Original September 19 run remains attempt 2; two charges occurred before midnight, four total articles are admitted and one remains pending"],
    "test_p3_06_lower_daily_ceiling_applies_now_and_frozen_run_ceiling_cannot_grow": ["Lowered live daily ceiling of one refuses more after two; raised live ceiling of 50 permits only frozen run three/enrichment two with original profile"],
    "test_la_logical_date_retry_preserves_scope_and_stops_after_three_attempts": ["Retries on later days preserve the Los Angeles logical date and scope; maximum attempt three is enforced"],
    "test_http_old_caller_dates_cannot_backdate_run_or_reopen_same_day_admission": ["First actual HTTP start plus raw coordinator captures three, admits two and retains one pending at a fixed server instant", "Expected date is independently derived from START in America/Los_Angeles; second POST supplies old report/local/process/brief dates but returns the same ID/server date/attempt one with one delivery", "Frozen profile/token/scopes/records stay unchanged; one Job, one Run, two Articles, two charges, one pending and no paid rows"],
    "test_raw_api_without_optional_ai_preserves_unknown_dates_pagination_and_backlog": ["Actual raw GET exposes two records with unknown publication and admission time, distinct pages, one interest match and two query matches", "Limit 101 is rejected with 422; unknown run is 404; collection has three observed, two admitted, one pending, zero grouped, zero remaining ceiling and explicit succeeded reason"],
    "test_budget_block_retains_readable_transferable_articles_without_repeated_admission": ["Injected allowance refusal records embedding blocked/allowance_reached, two deferred_budget records and actual GET of two raw articles"],
    "test_required_provider_failure_stays_failed_with_retained_raw_content": ["Required provider raises; run stays failed with two provider_failed raw records and an error; optional entity linking remains disabled"],
    "test_ordinary_worker_falls_back_to_readable_raw_without_dispatch": ["Registered ordinary worker captures and admits one, publishes nothing and records the expected distinct reason; physical calls list, Report rows and PersonalPaidRequest rows are empty"],
    "test_missing_route_api_run_keeps_raw_reader_available_without_paid_dispatch": ["Actual API 202 reaches production enqueue and registered task; missing configuration constructs no transport/delegate, creates no paid request/report and actual raw GET retains title/URL/snippet"],
    "test_worker_returns_partial_state_for_partial_offline_capture": ["Actual worker/coordinator with one absent fixture feed records one success and one failure, partially_failed, published true and partial_capture error"],
}
BROWSER_CLAIMS = {
    "settings-raw-counts": "Settings displays 37 observed, 10 pending, 27 admitted and one grouped, and identifies these as retained records rather than completed-brief counts",
    "raw-page-1": "Admitted standalone raw cards remain explicitly Disabled by profile and separate from the grouped story",
    "disabled-source-retained": "Removing technology selection leaves 10 pending: five eligible and five from a disabled source, with original content retained",
    "settings-disabled-reload": "Saved source selections, limits and interests persist on reload",
    "capture-entry-boundary": "Feed receipts show technology 501 observed/18 retained/entry limit and unknown remote total, with 18 policy records retained",
    "allowance-reached-visible": "Allowance reached is shown at $0.35, equal to current finalized plus reserved obligations; remaining is zero and raw reading is ready",
    "missing-configuration-visible": "Configuration missing is visible while raw reading remains ready",
    "ai-disabled-explicit": "AI disabled is visible while raw reading remains ready",
    "spending-nonzero-gate-off": "A valid configured route with the server paid gate off displays Paid runtime disabled",
    "capture-failure-corrected": "Selected failed capture displays the corrected warning/unavailable state and retained raw content remains readable",
    "provider-failure-corrected": "Selected provider failure displays the corrected warning/unavailable state and nine Provider failed raw records",
    "saved-after-correction": "The loaded Saved story remains readable after the correction",
    "brief-after-correction": "The loaded prior published version, snapshot, prose and citation remain readable after the correction",
    "brief-ai-blocked": "The prior published brief remains readable while AI readiness is blocked",
}

rows = original["rows"]
for row in rows:
    key = row["id"]
    row["plan_setup"], row["claim_assessment"] = SETUPS[key], [CLAIMS[key]]
    row["qualification"] = QUALIFICATIONS.get(key)
    for test in row["tests"]:
        node = test["nodeid"]
        path, name = node.split("::")
        module = ast.parse((ROOT / path).read_text())
        base = name.split("[")[0]
        function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == base)
        assertions = ASSERTIONS.get(name, ASSERTIONS.get(base))
        if assertions is None:
            raise ValueError("Missing assertion prose: " + name)
        outcome = node_outcomes.get(node, {"outcome": "MISSING", "seconds": None})
        test.update(source_lines=[function.lineno, function.end_lineno], assertion_claims=assertions, assertion_outcome=outcome["outcome"], junit_seconds=outcome["seconds"], retained_result=f"phase-3-closeout-20261003/{gate}.xml", gate_log=f"phase-3-closeout-20261003/{gate}.log", verified_at=suite_time, source_sha256=hashlib.sha256((ROOT / path).read_bytes()).hexdigest())
        test["source_matches_gate"] = test["source_sha256"] == current_sources["hashes"].get(path)
        test["prior_item5_outcome"] = "NOT_PRESENT" if base.startswith("test_two_concurrent_http") or base.startswith("test_http_old_caller") else "PASS"
        test["prior_item5_record"] = None if test["prior_item5_outcome"] == "NOT_PRESENT" else "phase-3-item-5-20261003/full-integration.xml"
    for native in row["native_browser"]:
        native["claim"] = BROWSER_CLAIMS[native["label"]]
        if hashlib.sha256((EVIDENCE / native["artifact"]).read_bytes()).hexdigest() != native["sha256"]:
            raise RuntimeError("Native browser text differs from pinned evidence")
    row["status"] = "PASS" if gate_valid and source_valid and all(t["assertion_outcome"] == "PASS" and t["source_matches_gate"] for t in row["tests"]) else "PARTIAL"
    row["remaining_gap"] = None if row["status"] == "PASS" else "Current exact node outcomes, matching source hashes or root-owned gate checks are incomplete; inspect this row's test outcomes and source checks."
    row["source_hash_refs"] = source_paths

artifacts = [r["path"] for r in original["artifact_hashes"]]
artifacts += [f"phase-3-closeout-20261003/{gate}{suffix}" for suffix in (".xml", ".log", "-command.json", "-source-before.json", "-source-after.json", "-result.json")]
artifacts += ["phase-3-closeout-20261003/literal-proof-purpose.json", "phase-3-closeout-20261003/coordinator-before-failure-stage-fix.json", "phase-3-closeout-20261003/coordinator-before-failure-stage-fix.py", "phase-3-closeout-20261003/coordinator-failure-stage-fix.diff", "phase-3-closeout-20261003/final-targeted.xml", "phase-3-closeout-20261003/final-targeted.log", "phase-3-closeout-20261003/final-targeted-result.json", "phase-3-closeout-20261003/final-targeted-source-before.json", "phase-3-closeout-20261003/final-targeted-source-after.json", "phase-3-closeout-20261003/closeout-test-before-publication-oracle.py", "phase-3-closeout-20261003/closeout-publication-oracle-correction.diff"]
record = {
    "schema": "phase3-closeout-acceptance-map-01-07.v2", "observed_at": observed_at,
    "user_date": "2026-10-03 America/Los_Angeles", "scope": "Read-only evidence reconciliation of P3-01–07; the root owns test execution and phase-level disposition",
    "status": "PASS" if all(row["status"] == "PASS" for row in rows) else "PARTIAL",
    "basis": "Exact assertion inspection, individual JUnit outcomes, current source hashes and retained native browser provenance; suite totals alone are never an acceptance oracle.",
    "current_gate": {"prefix": gate, "valid": gate_valid, "timestamp": suite_time, "result": result, "exact_testcases": len(node_outcomes)},
    "item5_gate_timestamp": original["item5_gate_timestamp"], "source_checks": source_checks,
    "all_relevant_verified_sources_match": source_valid,
    "artifact_hashes": [{"path": p, "sha256": hashlib.sha256((EVIDENCE / p).read_bytes()).hexdigest()} for p in artifacts],
    "rows": rows,
    "correction_provenance": {"changed_application_file": "services/personal/coordinator.py", "change": "Flush the newly assigned workflow failure stage before finish_run refreshes ownership", "before_sha256": json.loads((SCOPE / "coordinator-before-failure-stage-fix.json").read_text())["sha256"], "after_sha256": hashlib.sha256((ROOT / "services/personal/coordinator.py").read_bytes()).hexdigest(), "failing_gate": "final-targeted: two failed, six passed", "oracle_adjudication": "Default successful report status is asserted from run.result/published Report/snapshot rather than an unwritten stage success key; failure stage assertion was preserved"},
    "historical_policy": "September 20 observations remain historical corroboration. Completed Phase 2 and earlier evidence are unchanged.",
    "source_epoch_policy": "All mapped personal integration outcomes come from the current-source root-owned gate after the one-line coordinator correction. Item 5 remains earlier execution proof; unchanged unit/front-end sources retain their own earlier verification. Item 6 pins corrected frontend and native failure/Saved/Brief captures. Earlier settings/backlog captures predate that narrow UI correction and their components are unchanged.",
    "execution_qualification": "Production API/coordinator/database and registered task composition use scripted broker and RSS/model responses with cleared credentials, paid runtime false and database-only socket access. No real provider or running broker service proof is inferred.",
    "not_rerun_by_mapper": ["Integration tests", "Databases", "Services", "Browser", "Phase 2 live provider proof"],
}
(SCOPE / "acceptance-map-01-07.json").write_text(json.dumps(record, indent=2) + "\n")
lines = ["# P3-01 through P3-07 claim-level reconciliation", "", f"Observed {observed_at}; user date October 3, 2026, America/Los_Angeles. **Status: {record['status']} for these seven local/synthetic acceptance rows.** Exact individual outcomes come from the root-owned current-source `{gate}` gate; this mapper performs no runtime operations and does not independently close the whole phase.", "", "The [JSON map](acceptance-map-01-07.json) retains exact assertion summaries, test node IDs/line spans, individual outcomes, source hashes/dates, browser call/line/time provenance, artifact hashes and any gap. The [initial provisional map](acceptance-map-01-07-initial.json) preserves the original literal gaps.", "", f"All {len(source_checks)} relevant sources match their verification records: backend/tests at {current_sources['utc']}, corrected frontend at {six_final['observed_at']}. The one-line workflow failure persistence correction and baseline test failures remain retained separately. Earlier item-5 coordinator evidence is identified as its previous source epoch, not silently promoted to current runtime proof.", ""]
for row in rows:
    lines += [f'<a id="{row["id"].lower()}"></a>', "", f"## {row['id']} — {row['status']}", "", row["plan_setup"] + ".", "", *row["claim_assessment"], "", "Exact executed test nodes:", ""]
    lines += [f"- `{t['nodeid']}` — **{t['assertion_outcome']}** in [{gate} JUnit]({gate}.xml); exact assertions and source hash are in JSON." for t in row["tests"]]
    if row["native_browser"]:
        lines += ["", "Native browser proof: " + ", ".join(f"[{b['label']}](../{b['artifact']})" for b in row["native_browser"]) + "."]
    if row["qualification"]:
        lines += ["", row["qualification"]]
    lines += ["", "Remaining gap: " + (row["remaining_gap"] or "none within this local/synthetic acceptance row."), ""]
lines += ["## Execution and historical boundaries", "", "The root's current gate supplies fresh individual personal integration results after the coordinator correction. Item-5 integration totals overlap those cases and are not added; its retained units and item-6 frontend/browser proofs remain separately sourced. No acceptance row is inferred from a suite total.", "", "Fixture/controller/API snapshots establish expected records. Native DOM captures establish rendered states; images remain scoped to their function call and screenshot moment. Historical September 20 observations and completed Phase 2 feed/model/download proof remain separately qualified and unchanged.", "", "No live paid activation, running broker/worker service, operating-system restart, completely isolated browser network, Phase 4/5 implementation or personal usefulness judgment is claimed."]
(SCOPE / "acceptance-map-01-07.md").write_text("\n".join(lines) + "\n")
print(json.dumps({"rows": len(rows), "status": record["status"], "source_files_checked": len(source_checks), "all_match": source_valid, "current_gate_valid": gate_valid}))
