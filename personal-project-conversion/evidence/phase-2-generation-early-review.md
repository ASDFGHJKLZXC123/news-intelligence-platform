# Independent early generation review

Scope: read-only source review plus two database-free probes of production adapter/material/composition/grounding. No database, RSS, grouping, worker, live provider, or paid calls. Recorded from completed tool output without rerunning the old implementation.

Reviewed services/personal/briefs.py before/after SHA256: fac34bdd1a17832bf57cc046ee473d5c31c39e5ec4d918da4004c54c97ac2ab6.
Snapshot implementation SHA256: b2db224bae70054f6e98db719e9ce19a027d0877814e947920a1ae68eb3be491.
Shared composition SHA256: f2d170a5c664ea7b9a96936c6df00539e6d35f4cf0427f425cf482795c90148e.
Grounding SHA256: 736c7bc7e961083bc784681c0affad90e1baa937bf8067f4d1ff42185279e7f3.
Author test changed while reviewing; no author tests were run or claimed.

## Probe 1: failed capture becomes quiet

Constructed a transient PersonalBriefSnapshot with valid canonical hash, schema personal-brief-input.v1, empty candidates/claims, captured start/end, local date2026-09-08, and coverage feeds_attempted2, feeds_succeeded0, feeds_failed2. Called build_personal_brief_inputs(None, snapshot), build_brief_material(prediction_backed_outputs_enabled=False), compose_brief, run_grounding_gate. Orchestrator.run raises AssertionError if called; no calls occurred.

Observed exact result:
```json
{"probe":"failed coverage quiet adapter","quiet":true,"gate":"pass","notes":["Personal Phase 2 does not compute or narrate risk scores.","Historical analogy generation is disabled for the personal Phase 2 workflow.","No prior personal brief is injected into this immutable snapshot.","The configured personal sources produced no matching event candidates."]}
```

The generation terminal mapping then would publish and mark succeeded. The adapter ignored capture coverage and did not establish healthy-zero eligibility.

## Probe 2: all composition providers fail but gate passes

Built one selected event with one citable claim using tests.unit._report_composition_fixtures.single_event_brief(claims=(claim(),)). Built actual material, composed using an orchestrator whose run raises LLMOrchestratorError('offline provider unavailable'), then called actual grounding gate.

Observed exact result:
```json
{"probe":"all composition provider failures","quiet":false,"gate":"pass","generated_sections":0,"degradations":[["composition_failed"],["composition_failed"]]}
```

The shared composer intentionally preserves deterministic data-quality notes; the personal generation terminal mapping incorrectly treated publication eligibility as processing success. These findings were sent to Sol and root. Later independent database reruns are retained separately in /tmp/nip-p2-generation-review-61be.py and /tmp/nip-p2-generation-review-61be.log.
