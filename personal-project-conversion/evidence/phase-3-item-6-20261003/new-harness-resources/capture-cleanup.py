#!/usr/bin/env python3
"""Capture owned synthetic harness; --execute runs only explicitly released cleanup."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[3]
PRIVATE=Path('/private/tmp/nip-personal-phase2-offline-p3_item6_20261003_b')
MANIFEST=PRIVATE/'manifest.json'
ORIGINAL=json.loads(MANIFEST.read_text())
FRONT=json.loads((OUT.parent/'frontend-process.json').read_text())
PG='2f1b11050a2254fdf18b20e9a3c280180119a64a34bfd56e19aea9b8b1431771'
REDIS='f64fd6cd14383a285f6c7c4a3279feba404a25a0351edb65719ec4df884a8825'
DATABASE='nip_personal_phase2_offline_p3_item6_20261003_b'
OWNED=[PG,REDIS]
VOL={PG:'892de755d83c104d051f96ec7cf43651e73752d14df08c1d4d08ef0596998cef',REDIS:'e06def07668b8eca28d07e9ec356d1f694ce427aba8ea8823833945b887f6965'}
EXPECTED={PG:{'name':'/nip-personal-phase2-offline-pg-p3_item6_20261003_b','created':'2026-10-04T00:55:50.499785966Z','image':'sha256:f87fefc064506355f316088583365f286bde08bf8dd175ec08bcb3a6b1823c1c','port':'56414','protocol':'5432/tcp'},REDIS:{'name':'/nip-personal-phase2-offline-redis-p3_item6_20261003_b','created':'2026-10-04T00:55:59.179249053Z','image':'sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf','port':'56427','protocol':'6379/tcp'}}
VOL_CREATED={PG:'2026-10-04T00:55:50Z',REDIS:'2026-10-04T00:55:59Z'}
PRIVATE_API_ENV=subprocess.check_output(['ps','eww','-p',str(ORIGINAL['processes']['api']['pid']),'-o','command='],text=True)
PRIVATE_API_MATCH=re.search(r'(?:^|\s)API_KEY=([^\s]*)',PRIVATE_API_ENV)
assert PRIVATE_API_MATCH is not None and PRIVATE_API_MATCH.group(1)
SECRETS=[PRIVATE_API_MATCH.group(1),ORIGINAL['owner_token'],ORIGINAL['database']['url']]
PRIVATE_API_ENV=None
PRIVATE_API_MATCH=None
COMMANDS=[]
RESULT={'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'run_id':'p3_item6_20261003_b','authorization':'Human item-6 assignment; root explicitly released fresh harness after completed browser work and closing owned Chrome tab 952904347.','actions':[],'cleanup_complete':False}

def scrub(text):
 for value in SECRETS:
  if value: text=text.replace(value,'[PRIVATE-HARNESS-VALUE-REDACTED]')
 return text

def save():
 (OUT/'commands.json').write_text(json.dumps(COMMANDS,indent=2,sort_keys=True)+'\n')
 (OUT/'result.json').write_text(json.dumps(RESULT,indent=2,sort_keys=True)+'\n')

def run(argv,check=True,private_stdout=False,env=None,binary=False):
 r=subprocess.run(argv,text=not binary,capture_output=True,timeout=60,check=False,env=env)
 event={'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'argv':argv,'exit_code':r.returncode}
 if binary:
  event.update({'stdout_bytes':len(r.stdout),'stdout_sha256':hashlib.sha256(r.stdout).hexdigest(),'stderr':scrub(r.stderr.decode(errors='replace'))})
 else:
  event.update({'stdout':'[private/raw environment output omitted]' if private_stdout else scrub(r.stdout),'stderr':scrub(r.stderr)})
 COMMANDS.append(event);save()
 if check and r.returncode: raise RuntimeError('Failed command '+str(argv)+' exit '+str(r.returncode))
 return r

def projection(c):
 return {'id':c['Id'],'name':c['Name'],'created':c['Created'],'image':c['Image'],'labels':c['Config'].get('Labels') or {},'mounts':c['Mounts'],'ports':c['NetworkSettings']['Ports'],'port_bindings':c['HostConfig']['PortBindings'],'state':{k:c['State'].get(k) for k in ['Running','Status','StartedAt','FinishedAt','ExitCode','Pid']}}

def all_containers():
 ids=run(['docker','ps','-aq','--no-trunc']).stdout.split()
 raw=run(['docker','inspect',*ids],private_stdout=True)
 return {c['Id']:projection(c) for c in json.loads(raw.stdout)}

def match_container(cid):
 c=projection(json.loads(run(['docker','inspect',cid],private_stdout=True).stdout)[0])
 e=EXPECTED[cid]
 assert c['id']==cid and all(c[k]==e[k] for k in ['name','created','image']),c
 assert c['labels']==BEFORE[cid]['labels'] and c['mounts']==BEFORE[cid]['mounts'] and len(c['mounts'])==1,c
 assert c['mounts'][0]['Name']==VOL[cid],c
 assert c['port_bindings']==BEFORE[cid]['port_bindings'],c
 assert c['state']['Running'] and c['ports']=={e['protocol']:[{'HostIp':'127.0.0.1','HostPort':e['port']}]},c
 return c

def volume(cid,refs):
 v=json.loads(run(['docker','volume','inspect',VOL[cid]]).stdout)[0]
 assert v['Name']==VOL[cid] and v['CreatedAt']==VOL_CREATED[cid] and v['Driver']=='local' and v['Labels']=={'com.docker.volume.anonymous':''},v
 current=run(['docker','ps','-a','--no-trunc','--filter','volume='+VOL[cid],'--format','{{.ID}}']).stdout.split()
 assert current==refs,current
 return {'identity':v,'references':current}

def sql(db,query,private=False):
 return run(['docker','exec',PG,'psql','-U','news','-d',db,'-X','-A','-t','-v','ON_ERROR_STOP=1','-c',query],private_stdout=private).stdout.strip()

def check_database():
 inv=json.loads(sql('postgres',"SELECT json_agg(d ORDER BY datname) FROM (SELECT oid,datname,pg_get_userbyid(datdba) owner FROM pg_database) d"))
 for r in inv:r['oid']=int(r['oid'])
 expected=[{'oid':16384,'datname':DATABASE,'owner':'news'},{'oid':5,'datname':'postgres','owner':'news'},{'oid':4,'datname':'template0','owner':'news'},{'oid':1,'datname':'template1','owner':'news'}]
 assert sorted(inv,key=lambda x:x['datname'])==sorted(expected,key=lambda x:x['datname']),inv
 marker=sql(DATABASE,'SELECT owner_token FROM personal_offline_harness_ownership',private=True)
 assert marker==ORIGINAL['owner_token']
 return {'inventory':inv,'marker_sha256':hashlib.sha256(marker.encode()).hexdigest(),'marker_matches_private_manifest':True,'head':sql(DATABASE,'SELECT version_num FROM alembic_version')}

def process(pid,started,marker):
 actual_start=run(['ps','-p',str(pid),'-o','lstart=']).stdout.strip()
 cmd=run(['ps','-p',str(pid),'-o','command=']).stdout.strip()
 assert ' '.join(actual_start.split())==' '.join(started.split()) and marker in cmd,(pid,actual_start,cmd)
 return {'pid':pid,'started':actual_start,'command':cmd,'marker':marker,'identity_matches':True}

def logs(stage):
 for kind in ['api','worker','frontend']:
  source=PRIVATE/(kind+'.log')
  raw=source.read_text(errors='replace')
  clean=scrub(raw)
  clean=re.sub(r'(?i)(authorization:\s*)([^\s]+)',r'\1[REDACTED]',clean)
  path=OUT/(stage+'-'+kind+'.log')
  path.write_text(clean)
  RESULT.setdefault('logs',{})[stage+'-'+kind]={'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'redacted_path':str(path),'redacted_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'private_values_removed':raw!=scrub(raw)}
 save()

BEFORE=all_containers()
RESULT['containers_before']=BEFORE
try:
 assert ORIGINAL['run_id']=='p3_item6_20261003_b' and ORIGINAL['dataset']=='synthetic'
 assert ORIGINAL['postgres']['container_id']==PG and ORIGINAL['redis']['container_id']==REDIS
 assert ORIGINAL['database_name']==DATABASE and ORIGINAL['database']['owned'] and ORIGINAL['postgres']['owned'] and ORIGINAL['redis']['owned']
 safe={k:v for k,v in ORIGINAL.items() if k not in ['api_key','owner_token','database']}
 safe['owner_token_sha256']=hashlib.sha256(ORIGINAL['owner_token'].encode()).hexdigest()
 safe['database']={'owned':True,'name':DATABASE,'url':'[REDACTED]','oid':16384,'owner':'news'}
 safe['private_manifest_sha256']=hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
 (OUT/'manifest-safe.json').write_text(json.dumps(safe,indent=2,sort_keys=True)+'\n')
 RESULT['owned_containers']={cid:match_container(cid) for cid in OWNED}
 RESULT['owned_volumes_before']={cid:volume(cid,[cid]) for cid in OWNED}
 RESULT['database_before']=check_database()
 assert RESULT['database_before']['head']=='0022_personal_spending'
 RESULT['processes_before']={}
 RESULT['runtime_environment_projection']={}
 for kind,r in ORIGINAL['processes'].items():
  RESULT['processes_before'][kind]=process(r['pid'],r['started'],r['command_marker'])
  env=run(['ps','eww','-p',str(r['pid']),'-o','command='],private_stdout=True).stdout
  projected={}
  for key in ['APP_ENV','PERSONAL_PAID_RUNTIME_ENABLED','OPENAI_API_KEY','GEMINI_API_KEY','ANTHROPIC_API_KEY','DEEPSEEK_API_KEY']:
   m=re.search(r'(?:^|\s)'+key+r'=([^\s]*)',env)
   if key.endswith('API_KEY'):
    projected[key]={'present':m is not None,'blank':m is not None and m.group(1)==''}
    assert projected[key]['blank'],key
   else:
    projected[key]=None if m is None else m.group(1)
  assert projected['APP_ENV']=='test' and projected['PERSONAL_PAID_RUNTIME_ENABLED']=='false',projected
  RESULT['runtime_environment_projection'][kind]=projected
 RESULT['processes_before']['frontend']=process(FRONT['pid'],FRONT['started'],FRONT['marker'])
 RESULT['listeners_before']=run(['lsof','-nP','-iTCP:56430','-iTCP:60316','-iTCP:56414','-iTCP:56427','-F','pcnT']).stdout
 logs('before')
 dump=run(['docker','exec',PG,'pg_dump','-U','news','--no-owner','--no-acl',DATABASE],binary=True)
 assert len(dump.stdout)>10000 and b'PostgreSQL database dump complete' in dump.stdout
 raw=dump.stdout.decode()
 archive=OUT/(DATABASE+'-secret-safe.sql')
 archive.write_text(scrub(raw));archive.chmod(0o600)
 RESULT['database_archive']={'path':str(archive),'source_dump_sha256':hashlib.sha256(dump.stdout).hexdigest(),'source_bytes':len(dump.stdout),'retained_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'retained_bytes':len(archive.read_bytes()),'redaction':'Private manifest access key, owner marker token and database URL are replaced wherever present; complete schema and other synthetic data retained.','private_values_absent':all(secret not in archive.read_text() for secret in SECRETS)}
 assert RESULT['database_archive']['private_values_absent']
 save()
 parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
 if not args.execute:
  RESULT['prepared_only']=True;save();print(json.dumps({'prepared_only':True,'resource_mutations':0}));raise SystemExit(0)
 # Immediate exact guards after archive, immediately before existing stop procedure.
 for cid in OWNED:match_container(cid)
 RESULT['database_immediately_before_stop']=check_database()
 for kind,r in ORIGINAL['processes'].items():process(r['pid'],r['started'],r['command_marker'])
 env=dict(os.environ)
 env.update({'APP_ENV':'test','PERSONAL_PAID_RUNTIME_ENABLED':'false','PYTHONDONTWRITEBYTECODE':'1'})
 for key in ['ANTHROPIC_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY','DEEPSEEK_API_KEY','GOOGLE_API_KEY','FRED_API_KEY','EIA_API_KEY','NASA_FIRMS_MAP_KEY']:env[key]=''
 stop=run([str(ROOT/'scripts/stop-personal-phase2-offline.sh'),str(PRIVATE)],env=env)
 after_manifest=json.loads(MANIFEST.read_text())
 assert after_manifest['database']['owned'] is False
 RESULT['stop_script']={'exit_code':stop.returncode,'database_owned_false_after_success':True,'database_after_boundary':'Existing helper verifies private marker then succeeds at identity-scoped DROP before removing owned containers; container absence subsequently confirms dataset disposal. No live post-DROP OID query was possible after the helper removed its server.'}
 for cid in OWNED:
  for ident in [cid,EXPECTED[cid]['name'].lstrip('/')]:
   r=run(['docker','inspect',ident,'--format','{{.Id}}'],check=False)
   assert r.returncode!=0 and 'no such object' in r.stderr.lower(),r.stderr
  RESULT['actions'].append({'action':'stop-script-container-cleanup','id':cid,'id_and_name_absent':True})
 for kind,r in ORIGINAL['processes'].items():
  current=run(['ps','-p',str(r['pid']),'-o','lstart=,command='],check=False)
  assert current.returncode!=0 and not current.stdout.strip(),(kind,current.stdout)
  RESULT['actions'].append({'action':'stop-script-process-cleanup','kind':kind,'pid':r['pid'],'absent':True})
 # Separately owned frontend only; TERM, no forced signal.
 current=process(FRONT['pid'],FRONT['started'],FRONT['marker'])
 os.kill(FRONT['pid'],signal.SIGTERM)
 COMMANDS.append({'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'action':'SIGTERM','pid':FRONT['pid'],'exact_identity_rechecked':True});save()
 for _ in range(20):
  r=run(['ps','-p',str(FRONT['pid']),'-o','lstart=,command='],check=False)
  if r.returncode!=0 and not r.stdout.strip():break
  time.sleep(0.25)
 else:raise RuntimeError('Frontend remained after TERM; no force action performed.')
 RESULT['actions'].append({'action':'stop-owned-frontend','pid':FRONT['pid'],'absent':True,'signal':'TERM'})
 for cid in OWNED:
  RESULT.setdefault('volumes_immediately_before_remove',{})[cid]=volume(cid,[])
  run(['docker','volume','rm',VOL[cid]])
  r=run(['docker','volume','inspect',VOL[cid]],check=False)
  assert r.returncode!=0 and 'no such volume' in r.stderr.lower(),r.stderr
  RESULT['actions'].append({'action':'remove-exact-anonymous-volume','name':VOL[cid],'absent':True})
 logs('after')
 for port in ['56430','60316','56414','56427']:
  r=run(['lsof','-nP','-iTCP:'+port,'-F','pcnT'],check=False)
  assert r.returncode==1 and not r.stdout.strip(),(port,r.stdout)
  RESULT.setdefault('listeners_absent_after',{})[port]=True
 AFTER=all_containers();RESULT['containers_after']=AFTER
 comparisons={}
 for id,before in BEFORE.items():
  if id in OWNED:continue
  assert id in AFTER,('Unexpected unrelated disappearance',id)
  after=AFTER[id]
  matches={k:before[k]==after[k] for k in ['id','name','created','image','labels','port_bindings']}
  matches['mount_objects']=sorted(before['mounts'],key=lambda x:json.dumps(x,sort_keys=True))==sorted(after['mounts'],key=lambda x:json.dumps(x,sort_keys=True))
  comparisons[id]={'identity_matches':matches,'raw_mount_order_changed':before['mounts']!=after['mounts'],'state_changes':{k:{'before':before[k],'after':after[k]} for k in ['ports','state'] if before[k]!=after[k]}}
  assert all(matches.values()),(id,matches)
 RESULT['other_container_comparison']=comparisons
 RESULT['added_container_ids']=sorted(set(AFTER)-set(BEFORE))
 RESULT['cleanup_complete']=True
 RESULT['completed_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
 print(json.dumps({'cleanup_complete':True,'actions':RESULT['actions'],'other_containers_compared':len(comparisons),'other_state_changes':{k:v['state_changes'] for k,v in comparisons.items() if v['state_changes']},'archive_retained':RESULT['database_archive']['path']}))
except Exception as error:
 RESULT['error']=scrub(repr(error));RESULT['completed_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();save();raise
