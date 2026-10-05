#!/usr/bin/env python3
"""Read-only final absence/preservation adjudication; no resource mutation."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess

OUT=Path(__file__).resolve().parent
ORIGINAL=json.loads((OUT/'historical-cleanup-result.json').read_text())
CID='1e7c72d3ff3bd27f581ea5682e5b8a51206ddf98118be06cea933fb8bb2b55b6'
VOLUME='6694b72dd93cde45316410bd92ad1a80c28e2745b960ade62ae38a17489b377e'
COMMANDS=[]
def run(argv):
 r=subprocess.run(argv,text=True,capture_output=True,timeout=30)
 COMMANDS.append({'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'argv':argv,'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
 return r

def projection(c):
 return {'id':c['Id'],'name':c['Name'],'created':c['Created'],'image':c['Image'],'labels':c['Config'].get('Labels') or {},'mounts':c['Mounts'],'ports':c['NetworkSettings']['Ports'],'port_bindings':c['HostConfig']['PortBindings'],'state':{k:c['State'].get(k) for k in ['Running','Status','StartedAt','FinishedAt','ExitCode','Pid']}}

def mount_identity(value):
 return sorted(value,key=lambda x:json.dumps(x,sort_keys=True))

ids=run(['docker','ps','-aq','--no-trunc']).stdout.split()
raw=run(['docker','inspect',*ids])
assert raw.returncode==0
current={v['Id']:projection(v) for v in json.loads(raw.stdout)}
COMMANDS[-1]['stdout']='[secret-safe projection in final current resource records; Docker environment omitted]'
comparison={}
for id,before in ORIGINAL['containers_before'].items():
 if id==CID: continue
 assert id in current,('unexpected missing unrelated container',id)
 after=current[id]
 fields=['id','name','created','image','labels','port_bindings']
 matches={k:before[k]==after[k] for k in fields}
 matches['mount_object_identities']=mount_identity(before['mounts'])==mount_identity(after['mounts'])
 comparison[id]={'matches':matches,'raw_mount_order_changed':before['mounts']!=after['mounts'],'state_changes':{k:{'before':before[k],'after':after[k]} for k in ['ports','state'] if before[k]!=after[k]}}
 assert all(matches.values()),(id,matches)
assert CID not in current
absence={}
for identity in [CID,'nip-phase3-tests-02ea38048d']:
 r=run(['docker','inspect',identity,'--format','{{.Id}}'])
 absence[identity]=r.returncode!=0 and 'no such object' in r.stderr.lower()
 assert absence[identity]
r=run(['docker','volume','inspect',VOLUME])
absence[VOLUME]=r.returncode!=0 and 'no such volume' in r.stderr.lower()
assert absence[VOLUME]
r=run(['ps','-p','47982','-o','lstart=,command='])
absence['frontend_pid_47982']=r.returncode!=0 and not r.stdout.strip()
assert absence['frontend_pid_47982']
r=run(['lsof','-nP','-iTCP:61410','-F','pcnT'])
absence['frontend_listener_61410']=r.returncode==1 and not r.stdout.strip()
assert absence['frontend_listener_61410']
assert len(ORIGINAL['actions'])==5 and all(a.get('absence_verified',a.get('process_absence_verified')) for a in ORIGINAL['actions'])
for name,backup in ORIGINAL['database_backups'].items():
 data=Path(backup['path']).read_bytes()
 assert hashlib.sha256(data).hexdigest()==backup['sha256'] and len(data)==backup['bytes']
result={'captured_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'owned_cleanup_complete':True,'owned_actions':ORIGINAL['actions'],'fresh_absence':absence,
 'retained_database_backups':ORIGINAL['database_backups'],'original_failed_result_preserved':'historical-cleanup-result.json',
 'original_broad_raw_mount_comparison_failed':True,'raw_mount_order_only_identities':[id for id,c in comparison.items() if c['raw_mount_order_changed']],
 'unrelated_identity_semantic_comparison':comparison,'unrelated_state_changes':{id:c['state_changes'] for id,c in comparison.items() if c['state_changes']},
 'current_containers':current,'container_ids_added_during_cleanup':sorted(set(current)-set(ORIGINAL['containers_before'])),
 'adjudication_resource_mutations':[],
 'preserved_missing_manifest_browser_stack':True,'new_item6_harness_left_untouched':True,
 'reason':'Owned absence proofs completed. Original broad comparison compared mount arrays positionally; two immutable bind objects on unrelated Fencepost container were reversed. Exact mount object identities match; original failed record remains retained. No unrelated restoration or cleanup occurred.'}
(OUT/'historical-cleanup-adjudication.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
(OUT/'historical-cleanup-adjudication-commands.json').write_text(json.dumps(COMMANDS,indent=2,sort_keys=True)+'\n')
print(json.dumps({'owned_cleanup_complete':True,'fresh_absence':absence,'unrelated_identity_comparisons':len(comparison),'raw_mount_order_changed':result['raw_mount_order_only_identities'],'unrelated_state_changes':result['unrelated_state_changes']}))
