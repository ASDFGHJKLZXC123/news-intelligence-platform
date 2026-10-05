#!/usr/bin/env python3
"""Read-only resource capture; writes only this item-6 evidence directory."""
import datetime
import hashlib
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[4]
OUT = pathlib.Path(__file__).resolve().parent
HIST = ROOT / 'personal-project-conversion/evidence/phase-3-20260920'
PG = '1e7c72d3ff3bd27f581ea5682e5b8a51206ddf98118be06cea933fb8bb2b55b6'
BROWSER_PG = 'bc28beea6a80bc44be166ee99d283060bff58425119088a6407bc5f03e738ea5'
BROWSER_REDIS = 'd578e2a90083b795178154ccb9ad21954010235b2fec0ce539a9ecc1b6702b73'
DBS = {'nip_phase3_full_a5ca8c211b71': 296986, 'nip_phase3_final_444731f8f123': 649104}
COMMANDS = []

def run(argv):
    start = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = subprocess.run(argv, text=True, capture_output=True, timeout=30, check=False)
    COMMANDS.append({'started_at_utc': start, 'argv': argv, 'exit_code': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
    return result

def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')

ids = run(['docker', 'ps', '-aq', '--no-trunc']).stdout.split()
raw = json.loads(run(['docker', 'inspect', *ids]).stdout)
containers = []
for c in raw:
    containers.append({'id': c['Id'], 'name': c['Name'], 'created': c['Created'],
                       'image': c['Image'], 'labels': c['Config'].get('Labels') or {},
                       'mounts': c['Mounts'], 'ports': c['NetworkSettings']['Ports'],
                       'state': {k:c['State'].get(k) for k in
                                 ['Running','Status','StartedAt','FinishedAt','ExitCode','Pid']}})
# Omit all raw docker inspect output, especially container environment variables.
COMMANDS[-1]['stdout'] = '[projected secret-safe container identity fields in containers-current.json]'
save('containers-current.json', containers)

manifest_paths = ['postgres-ownership.json', 'full-suite-database.json',
                  'final-suite-database.json', 'frontend-ownership.json']
manifest_records = {}
for name in manifest_paths:
    path = HIST / name
    record = json.loads(path.read_text())
    record.pop('database_url', None)
    manifest_records[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                              'safe_record': record}
item23 = ROOT / 'personal-project-conversion/evidence/phase-3-items-2-3-20261003/final-integration-resources.json'
r23 = json.loads(item23.read_text())
manifest_records['item23-final-resources'] = {'path': str(item23),
    'sha256': hashlib.sha256(item23.read_bytes()).hexdigest(),
    'container_id': r23['container_id'], 'container_label': r23['container_label'],
    'before': r23['before'], 'after': r23['after'], 'inventory_unchanged': r23['inventory_unchanged']}
save('historical-safe-manifests.json', manifest_records)

rollout = pathlib.Path('/Users/f8fq/.codex/sessions/2026/09/20/rollout-2026-09-20T15-41-05-01a0c0fa-efac-7f83-8231-8928b4629fe1.jsonl')
creation = []
for number,line in enumerate(rollout.read_text().splitlines(),1):
    d = json.loads(line)
    item = d.get('payload',{}).get('item',{})
    cmd = '\n'.join(item.get('command',[]))
    if number not in {246,866,1062}: continue
    assert ('CREATE DATABASE' in cmd) or ('nip.phase3.owned' in cmd and 'docker' in cmd)
    safe_cmd = cmd.replace('news:news@', '[synthetic-role]:[redacted]@').replace('POSTGRES_PASSWORD=news', 'POSTGRES_PASSWORD=[redacted]')
    creation.append({'rollout_line': number, 'item_id': item.get('id'),
                     'started_at_ms': d['payload'].get('started_at_ms'),
                     'completed_at_ms': d['payload'].get('completed_at_ms'),
                     'original_command_sha256': hashlib.sha256(cmd.encode()).hexdigest(),
                     'redacted_command': safe_cmd, 'exit_code': item.get('exit_code'),
                     'stdout': item.get('stdout',''), 'status': item.get('status')})
save('historical-create-provenance.json', {'session_path': str(rollout),
    'session_sha256': hashlib.sha256(rollout.read_bytes()).hexdigest(),
    'session_id':'01a0c0fa-efac-7f83-8231-8928b4629fe1', 'records': creation,
    'oid_boundary': 'Original CREATE records did not record OIDs; October 3 item-2/3 before/after inventories pin the exact name/OID pairs freshly matched here.'})

queries = {
    'inventory': "SELECT oid,datname,pg_get_userbyid(datdba) owner FROM pg_database ORDER BY datname",
    'clients': "SELECT datname,pid,usename,application_name,client_addr,backend_start,state FROM pg_stat_activity WHERE backend_type='client backend' ORDER BY datname,pid",
    'head': "SELECT current_database(),version_num FROM alembic_version",
    'counts': "SELECT 'personal_workspaces' table_name,count(*) rows FROM personal_workspaces UNION ALL SELECT 'personal_runs',count(*) FROM personal_runs UNION ALL SELECT 'personal_captures',count(*) FROM personal_captures UNION ALL SELECT 'personal_paid_requests',count(*) FROM personal_paid_requests UNION ALL SELECT 'personal_legacy_usage',count(*) FROM personal_legacy_usage UNION ALL SELECT 'reports',count(*) FROM reports UNION ALL SELECT 'articles',count(*) FROM articles UNION ALL SELECT 'sources',count(*) FROM sources",
    'fixture_sources': 'SELECT id,name,feed_url,active FROM sources ORDER BY id',
    'fixture_articles': 'SELECT id,source_id,title,url,published_at,fetched_at FROM articles ORDER BY id',
}
database_records = {}
for cid in [PG, BROWSER_PG]:
    record = {}
    for kind in ['inventory','clients']:
        record[kind] = run(['docker','exec',cid,'psql','-U','news','-d','postgres','-X','-A','-F','|','-c',queries[kind]]).stdout
    database_records[cid] = record
for name,oid in DBS.items():
    record = {'expected_oid':oid}
    for kind in ['head','counts','fixture_sources','fixture_articles']:
        record[kind] = run(['docker','exec',PG,'psql','-U','news','-d',name,'-X','-A','-F','|','-c',queries[kind]]).stdout
    database_records[name] = record
save('database-current.json', database_records)

wanted = [36237,36245,47982]
process = run(['ps','-p',','.join(map(str,wanted)),'-o','pid=,ppid=,lstart=,command='])
save('processes-current.json', {'historical_manifest_processes': process.stdout,
    'listener_clients': run(['lsof','-nP','-iTCP:58553','-iTCP:61410','-iTCP:59751','-F','pcnT']).stdout,
    'owned_browser_process_sockets': run(['lsof','-nP','-a','-p',','.join(map(str,wanted)),'-iTCP','-F','pcnT']).stdout,
    'missing_browser_manifest': str(pathlib.Path('/private/tmp/nip-personal-phase2-offline-p3_browser_20260920_a/manifest.json')),
    'browser_manifest_exists': pathlib.Path('/private/tmp/nip-personal-phase2-offline-p3_browser_20260920_a/manifest.json').exists()})

volumes = ['6694b72dd93cde45316410bd92ad1a80c28e2745b960ade62ae38a17489b377e',
           '8f7e3e99d7af3dad37bf64935f151b60b8ea71bff9aa984362d50e33d203c08b',
           '61991b35cbf67c77fa7fd990704376da228adaa4eda039e1722cdc6351db8dcf']
v = json.loads(run(['docker','volume','inspect',*volumes]).stdout)
for x in v:
    x['container_references'] = run(['docker','ps','-a','--no-trunc','--filter','volume='+x['Name'],'--format','{{.ID}} {{.Names}}']).stdout.splitlines()
save('volumes-current.json', v)
absence = {}
for cid in ['33a5ddf17f2b38d328e9634f9071ff7a7f8d0922f83d5afc1cee7c217979c424',
            'fad29e3f021a120416b5ff692009c2e1d23d47cbb7ba19ce9c727a866b4ec8ab']:
    result = run(['docker','inspect',cid,'--format','{{.Id}}'])
    absence[cid] = {'absent': result.returncode != 0 and 'No such object' in result.stderr,
                    'exit_code':result.returncode,'stderr':result.stderr}
save('prior-owned-absence.json', absence)
save('commands.json', COMMANDS)
save('audit-summary.json', {'captured_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'resource_mutations':[], 'evidence_only_writes':True,
    'new_item6_harness_excluded_from_cleanup':'p3_item6_20261003_b',
    'dedicated_container_identity_match':next(c for c in containers if c['id']==PG)['created']==manifest_records['postgres-ownership.json']['safe_record']['created'],
    'database_oids_match_item23':all({'name':name,'oid':oid} in r23['after'] for name,oid in DBS.items()),
    'historical_browser_disposition':'Preserve: original private ownership manifest missing; no cleanup inferred from names.',
    'unrelated_infrastructure_disposition':'Preserve all, including stopped restart-test containers; historical item-5 broad comparison remains failed.',
    'shared_development_postgres':'Preserve exact ID 07ba195cbb13986dda64ac6300377ca5c512c5df6113c2eb230c1b95594f944a and named volume news-intelligence-platform_pgdata; no database connection made to it.'})
print(json.dumps({'evidence_dir':str(OUT),'commands':len(COMMANDS),'resource_mutations':0}))
