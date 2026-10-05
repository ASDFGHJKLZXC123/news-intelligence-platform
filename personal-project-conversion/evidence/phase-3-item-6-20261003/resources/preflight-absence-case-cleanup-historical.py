#!/usr/bin/env python3
"""Identity-scoped cleanup of explicitly authorized completed Phase 3 resources."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

OUT = Path(__file__).resolve().parent
CID = '1e7c72d3ff3bd27f581ea5682e5b8a51206ddf98118be06cea933fb8bb2b55b6'
NAME = '/nip-phase3-tests-02ea38048d'
IMAGE = 'sha256:f87fefc064506355f316088583365f286bde08bf8dd175ec08bcb3a6b1823c1c'
CREATED = '2026-09-20T22:50:40.739020472Z'
VOLUME = '6694b72dd93cde45316410bd92ad1a80c28e2745b960ade62ae38a17489b377e'
DBS = {'nip_phase3_full_a5ca8c211b71':296986,'nip_phase3_final_444731f8f123':649104}
BASE_DBS = [{'oid':5,'datname':'postgres','owner':'news'}, {'oid':4,'datname':'template0','owner':'news'}, {'oid':1,'datname':'template1','owner':'news'}]
EVENTS = []
SUMMARY = {'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'authorization':'Human item-6 assignment and root explicit exact-resource cleanup coordination.',
           'new_item6_harness_excluded':'p3_item6_20261003_b', 'database_backups':{}, 'actions':[],
           'cleanup_complete':False,'preserved_browser_stack_missing_manifest':True}

def save():
    (OUT/'historical-cleanup-commands.json').write_text(json.dumps(EVENTS,indent=2,sort_keys=True)+'\n')
    (OUT/'historical-cleanup-result.json').write_text(json.dumps(SUMMARY,indent=2,sort_keys=True)+'\n')

def run(argv, check=True, binary=False):
    started=datetime.datetime.now(datetime.timezone.utc).isoformat()
    r=subprocess.run(argv,capture_output=True,text=not binary,timeout=45,check=False)
    event={'started_at_utc':started,'argv':argv,'exit_code':r.returncode}
    if binary:
        event.update({'stdout_bytes':len(r.stdout),'stdout_sha256':hashlib.sha256(r.stdout).hexdigest(), 'stderr':r.stderr.decode(errors='replace')})
    else:
        event.update({'stdout':r.stdout,'stderr':r.stderr})
    if argv[:2]==['docker','inspect'] and r.returncode==0 and '--format' not in argv:
        event['stdout']='[secret-safe identity projection retained separately; Docker environment omitted]'
    EVENTS.append(event)
    save()
    if check and r.returncode: raise RuntimeError('Command failed: '+str(argv)+' exit '+str(r.returncode))
    return r

def projection(c):
    return {'id':c['Id'],'name':c['Name'],'created':c['Created'],'image':c['Image'],
            'labels':c['Config'].get('Labels') or {},'mounts':c['Mounts'],
            'ports':c['NetworkSettings']['Ports'],'port_bindings':c['HostConfig']['PortBindings'],
            'state':{k:c['State'].get(k) for k in ['Running','Status','StartedAt','FinishedAt','ExitCode','Pid']}}

def all_containers():
    ids=run(['docker','ps','-aq','--no-trunc']).stdout.split()
    values=json.loads(run(['docker','inspect',*ids]).stdout)
    return {c['Id']:projection(c) for c in values}

def check_container(running=True):
    c=json.loads(run(['docker','inspect',CID]).stdout)[0]
    p=projection(c)
    assert p['id']==CID and p['name']==NAME and p['created']==CREATED and p['image']==IMAGE,p
    assert p['labels'].get('nip.phase3.owned')=='nip-phase3-tests-02ea38048d',p
    assert p['mounts']==BEFORE[CID]['mounts'] and len(p['mounts'])==1,p
    assert p['mounts'][0]['Name']==VOLUME and p['mounts'][0]['Destination']=='/var/lib/postgresql/data',p
    assert p['port_bindings']==BEFORE[CID]['port_bindings'],p
    assert p['state']['Running']==running,p
    if running: assert p['ports']=={'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'58553'}]},p
    return p

def sql(database,query):
    return run(['docker','exec',CID,'psql','-U','news','-d',database,'-X','-A','-t','-v','ON_ERROR_STOP=1','-c',query]).stdout.strip()

def inventory():
    return json.loads(sql('postgres',"SELECT COALESCE(json_agg(d ORDER BY datname),'[]'::json) FROM (SELECT oid,datname,pg_get_userbyid(datdba) owner FROM pg_database) d"))

def check_dbs(remaining):
    current=inventory()
    for record in current:
        record['oid']=int(record['oid'])
    expected=BASE_DBS+[{'oid':DBS[n],'datname':n,'owner':'news'} for n in remaining]
    assert sorted(current,key=lambda x:x['datname'])==sorted(expected,key=lambda x:x['datname']),current
    clients=json.loads(sql('postgres',"SELECT COALESCE(json_agg(d),'[]'::json) FROM (SELECT datname,pid,usename,application_name,client_addr,state FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()) d"))
    assert clients==[],clients
    return current

def check_volume(expected_ref):
    v=json.loads(run(['docker','volume','inspect',VOLUME]).stdout)[0]
    assert v['Name']==VOLUME and v['CreatedAt']=='2026-09-20T22:50:40Z',v
    assert v['Driver']=='local' and v['Labels']=={'com.docker.volume.anonymous':''},v
    refs=run(['docker','ps','-a','--no-trunc','--filter','volume='+VOLUME,'--format','{{.ID}}']).stdout.split()
    assert refs==expected_ref,refs
    return {'identity':v,'references':refs}

BEFORE=all_containers()
SUMMARY['containers_before']=BEFORE
save()
try:
    SUMMARY['container_before']=check_container()
    SUMMARY['volume_before']=check_volume([CID])
    remaining=list(DBS)
    SUMMARY['databases_before']=check_dbs(remaining)
    # No personal/ledger rows may be discarded; preserve complete synthetic schema/data dumps.
    for database in remaining:
        assert sql(database,"SELECT version_num FROM alembic_version")=='0022_personal_spending'
        for table in ['personal_workspaces','personal_runs','personal_captures','personal_paid_requests','personal_legacy_usage','reports']:
            assert sql(database,'SELECT count(*) FROM '+table)=='0',(database,table)
        result=run(['docker','exec',CID,'pg_dump','-U','news','--no-owner','--no-acl',database],binary=True)
        assert len(result.stdout)>10000 and b'PostgreSQL database dump complete' in result.stdout
        archive=OUT/(database+'.sql')
        archive.write_bytes(result.stdout)
        archive.chmod(0o600)
        SUMMARY['database_backups'][database]={'path':str(archive),'bytes':len(result.stdout),'sha256':hashlib.sha256(result.stdout).hexdigest(),'synthetic_disposable_only':True}
        save()
    for database in list(remaining):
        check_container()
        check_dbs(remaining)
        # Non-force DROP; no pg_terminate_backend, FORCE, cascade, or broad selection.
        sql('postgres','DROP DATABASE "'+database+'"')
        remaining.remove(database)
        current=check_dbs(remaining)
        assert not any(d['datname']==database or d['oid']==DBS[database] for d in current)
        SUMMARY['actions'].append({'action':'drop_database','name':database,'oid':DBS[database],'absence_verified':True})
        save()
    SUMMARY['databases_after']=check_dbs([])
    SUMMARY['container_identity_before_stop']=check_container()
    run(['docker','stop','--time','10',CID])
    SUMMARY['container_identity_before_remove']=check_container(running=False)
    run(['docker','rm',CID])
    absent=run(['docker','inspect',CID,'--format','{{.Id}}'],check=False)
    assert absent.returncode!=0 and 'No such object' in absent.stderr
    same_name=run(['docker','inspect',NAME.lstrip('/'),'--format','{{.Id}}'],check=False)
    assert same_name.returncode!=0 and 'No such object' in same_name.stderr
    SUMMARY['actions'].append({'action':'remove_container','id':CID,'absence_verified':True})
    save()
    SUMMARY['volume_identity_before_remove']=check_volume([])
    run(['docker','volume','rm',VOLUME])
    absent=run(['docker','volume','inspect',VOLUME],check=False)
    assert absent.returncode!=0 and 'no such volume' in absent.stderr.lower()
    SUMMARY['actions'].append({'action':'remove_volume','name':VOLUME,'absence_verified':True})
    save()
    # Separately manifested old frontend, with trusted root no-current-browser-use observation.
    started=run(['ps','-p','47982','-o','lstart=']).stdout.strip()
    command=run(['ps','-p','47982','-o','command=']).stdout.strip()
    assert started=='Sun Sep 20 16:06:03 2026' and '-m http.server 61410 --bind 127.0.0.1 --directory frontend' in command,(started,command)
    client=run(['lsof','-nP','-iTCP:61410','-F','pcnT'],check=False)
    assert client.returncode==0 and 'TST=ESTABLISHED' not in client.stdout,client.stdout
    SUMMARY['frontend_before']={'pid':47982,'started':started,'command':command,'sockets':client.stdout,'root_browser_observation':'Enabled-tab inventory showed no 61410 tab; locked DevTools inventory unavailable, original chat inactive; no ongoing task uses this origin.'}
    # Recheck immediately before TERM.
    assert run(['ps','-p','47982','-o','lstart=']).stdout.strip()==started
    assert run(['ps','-p','47982','-o','command=']).stdout.strip()==command
    os.kill(47982,signal.SIGTERM)
    EVENTS.append({'started_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'action':'SIGTERM','pid':47982,'identity_rechecked':True})
    save()
    for _ in range(20):
        r=run(['ps','-p','47982','-o','lstart=,command='],check=False)
        if r.returncode!=0 and not r.stdout.strip(): break
        time.sleep(0.25)
    else: raise RuntimeError('Old frontend remained present after TERM; no forced signal sent.')
    listener=run(['lsof','-nP','-iTCP:61410','-F','pcnT'],check=False)
    assert listener.returncode==1 and not listener.stdout.strip(),listener.stdout
    SUMMARY['actions'].append({'action':'stop_frontend','pid':47982,'signal':'TERM','process_absence_verified':True,'listener_absence_verified':True})
    AFTER=all_containers()
    SUMMARY['containers_after']=AFTER
    other_ids=set(BEFORE)-{CID}
    SUMMARY['preexisting_identity_comparison']={}
    for id in sorted(other_ids):
        assert id in AFTER,('Unrelated container disappeared',id)
        before,after=BEFORE[id],AFTER[id]
        identity_fields=['id','name','created','image','labels','mounts','port_bindings']
        matches={field:before[field]==after[field] for field in identity_fields}
        state_changes={field:{'before':before[field],'after':after[field]} for field in ['ports','state'] if before[field]!=after[field]}
        SUMMARY['preexisting_identity_comparison'][id]={'identity_matches':matches,'state_changes':state_changes}
        assert all(matches.values()),(id,matches)
    assert CID not in AFTER
    SUMMARY['remaining_container_ids_added_during_cleanup']=sorted(set(AFTER)-set(BEFORE))
    SUMMARY['cleanup_complete']=True
    SUMMARY['completed_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    save()
except Exception as error:
    SUMMARY['error']=str(error)
    SUMMARY['completed_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat()
    save()
    raise
print(json.dumps({'cleanup_complete':SUMMARY['cleanup_complete'],'actions':SUMMARY['actions'],'unrelated_containers_compared':len(SUMMARY['preexisting_identity_comparison']),'unrelated_state_changes':{k:v['state_changes'] for k,v in SUMMARY['preexisting_identity_comparison'].items() if v['state_changes']}}))
