"""Read-only baseline audit; writes only its verification record."""
from pathlib import Path
import hashlib,json,runpy,subprocess,datetime as dt
OUT=Path(__file__).resolve().parent; ROOT=OUT.parents[2]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
base=json.loads((OUT/'baseline-files.json').read_text());meta=json.loads((OUT/'baseline.json').read_text());initial=json.loads((OUT/'source-before.json').read_text());executed=json.loads((OUT/'phase4a-accepted-source-before.json').read_text())
allowed=set('''README.md
PERSONAL_PROJECT_CONVERSION_PLAN.md
personal-project-conversion/README.md
personal-project-conversion/03-bounded-daily-use.md
personal-project-conversion/04-local-runtime.md
personal-project-conversion/evidence/phase-3.md
apps/api/deps.py
apps/api/intelligence.py
apps/api/main.py
apps/api/personal.py
apps/api/pipeline.py
db/models/__init__.py
db/models/personal.py
db/seed/episode_seed.py
db/seed/seed.py
frontend/app/page-personal.jsx
packages/config/settings.py
scripts/test-postgres-restore.sh
services/ingestion/http_provider.py
services/llm/orchestrator.py
services/personal/__init__.py
services/personal/briefs.py
services/personal/coordinator.py
services/personal/paid_runtime.py
services/personal/runs.py
services/personal/spending.py
services/personal/workspace.py
services/writer_mode.py
tests/integration/test_personal_bounds_upgrade.py
tests/integration/test_personal_brief_api.py
tests/integration/test_personal_brief_generation.py
tests/integration/test_personal_coordinator.py
tests/integration/test_personal_daily_bounds.py
tests/integration/test_personal_item5_composed.py
tests/integration/test_personal_paid_worker.py
tests/integration/test_personal_phase2.py
tests/integration/test_personal_phase3_closeout.py
tests/integration/test_personal_reading_bounds.py
tests/integration/test_personal_run_claim_snapshot.py
tests/integration/test_personal_settings.py
tests/integration/test_personal_spending.py
tests/integration/test_personal_spending_metadata.py
tests/integration/test_personal_worker.py
tests/integration/test_personal_writer_mode.py
tests/unit/test_episode_seed.py
workers/personal_tasks.py'''.splitlines())
changed=sorted(k for k,v in base.items() if (ROOT/k).is_file() and sha(ROOT/k)!=v);missing=sorted(k for k in base if not (ROOT/k).is_file());unexpected=sorted(set(changed)-allowed)
assert not missing and not unexpected,(missing,unexpected)
protected={k:sha(ROOT/k)==v for k,v in initial['protected_hashes'].items()}
assert all(protected.values())
assert sha(ROOT/'.git/index')==meta['index_sha256']
assert git('rev-parse','HEAD').decode().strip()==meta['head'] and git('branch','--show-current').decode().strip()==meta['branch']
assert git('diff','--cached','--binary')==(OUT/'baseline-staged.diff').read_bytes()
prior='personal-project-conversion/evidence/phase-3.md';old=(OUT/'phase-3-accepted-before-phase4a.md').read_bytes();assert hashlib.sha256(old).hexdigest()==base[prior]
assert old in (ROOT/prior).read_bytes()
historical={k:sha(ROOT/k)==v for k,v in base.items() if k.startswith('personal-project-conversion/evidence/') and k!=prior};assert all(historical.values())
changes_after_execution=sorted(k for k,v in executed['hashes'].items() if not (ROOT/k).is_file() or sha(ROOT/k)!=v)
assert set(changes_after_execution)<=set(['README.md','PERSONAL_PROJECT_CONVERSION_PLAN.md','personal-project-conversion/README.md','personal-project-conversion/03-bounded-daily-use.md','personal-project-conversion/04-local-runtime.md']),changes_after_execution
frontend=json.loads((OUT/'frontend-final-source-before.json').read_text())
frontend_changed=[k for k,v in frontend['hashes'].items() if k.startswith('frontend/') and (not (ROOT/k).is_file() or sha(ROOT/k)!=v)];assert not frontend_changed
resources=json.loads((OUT/'resource-cleanup.json').read_text());stack=json.loads((OUT/'stack-cleanup.json').read_text());assert resources['container_absent'] and resources['database_role_inventory_restored'] and resources['preexisting_containers_unchanged'];assert all(d['absent_after_drop'] for d in stack['databases'].values()) and all(p['stopped_utc'] for p in stack['processes'].values()) and stack['redis']['removed']
value={'utc':dt.datetime.now(dt.UTC).isoformat(),'head':meta['head'],'branch':meta['branch'],'baseline_file_count':len(base),'authorized_changed_baseline_files':changed,'missing_baseline_files':missing,'unexpected_changes':unexpected,'protected_hashes_equal':protected,'index_byte_identical':True,'staged_diff_byte_identical':True,'phase3_prior_record_byte_identical_archive':True,'phase3_prior_body_retained':True,'other_historical_evidence_files_unchanged':len(historical),'implementation_and_executed_test_hashes_unchanged':True,'only_changes_after_accepted_execution':changes_after_execution,'frontend_executed_inputs_unchanged':True,'all_verification_resources_cleaned':True,'preexisting_container_count_preserved':resources['preexisting_count'],'verified_file_hashes':{k:sha(ROOT/k) for k in changed},'remaining_phase_scope':'4B and 5 unstarted; paid disabled; preferences unapplied'}
(OUT/'preservation-final.json').write_text(json.dumps(value,indent=2,sort_keys=True)+'\n');print(json.dumps({k:v for k,v in value.items() if k not in ('verified_file_hashes','authorized_changed_baseline_files')},indent=2))
