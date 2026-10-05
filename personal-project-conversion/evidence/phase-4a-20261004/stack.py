"""Exact-owned synthetic Phase 4A stack. No shared resource or paid provider is a target."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import runpy
import signal
import socket
import subprocess
import sys
import time
import uuid

V = runpy.run_path(str(Path(__file__).with_name('verify.py')))
ROOT, OUT, PYTHON = V['ROOT'], V['OUT'], V['PYTHON']
STATE = OUT/'stack-resource.json'


def command(args, *, env=None, check=True, timeout=60):
    r = subprocess.run(args, cwd=ROOT, env=env or os.environ, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode: raise RuntimeError(f'{args[:4]}: {r.stderr[-2500:]}')
    return r


def write(name, value):
    (OUT/name).write_text(json.dumps(value, indent=2, sort_keys=True, default=str)+'\n')


def state():
    r=json.loads(STATE.read_text()); assert r['owner']==V['resource']()['owner']; return r


def save(r): write(STATE.name, r)


def port():
    with socket.socket() as s: s.bind(('127.0.0.1',0)); return s.getsockname()[1]


def environment(r, db, *, transport='subprocess'):
    e=dict(V['BASE_ENV']); pg=V['resource']()
    e.update(DATABASE_URL=pg['url'].rsplit('/',1)[0]+'/'+r['databases'][db]['name'], REDIS_URL=f"redis://127.0.0.1:{r['redis']['port']}/0", CELERY_BROKER_URL=f"redis://127.0.0.1:{r['redis']['port']}/1", CELERY_RESULT_BACKEND=f"redis://127.0.0.1:{r['redis']['port']}/2", PERSONAL_PROCESSING_TRANSPORT=transport, PERSONAL_PROCESSING_MODE='personal', PERSONAL_OFFLINE_FIXTURE_PATH=str(ROOT/'personal-project-conversion/fixtures/phase-2-offline-workflow.json'), CORS_ALLOW_ORIGINS=f"http://127.0.0.1:{r['frontend_port']}", PYTHONPATH=str(ROOT), STACK_OWNER=r['owner'])
    return e


def py(r, db, code, *, transport='subprocess'):
    return command([PYTHON,'-c',code],env=environment(r,db,transport=transport))


def create_database(r, key, template=None):
    import psycopg2
    pg=V['resource'](); name='nip_phase4a_'+key+'_'+r['owner'][:12]
    conn=psycopg2.connect(host='127.0.0.1',port=pg['port'],user='news',password='news',dbname='postgres');conn.autocommit=True
    try:
        with conn.cursor() as c:
            c.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,)); assert c.fetchone() is None
            q='CREATE DATABASE "'+name+'"'+(' TEMPLATE "'+r['databases'][template]['name']+'"' if template else '')
            c.execute(q);c.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,)); oid=c.fetchone()[0]
            r['databases'][key]={'name':name,'oid':oid,'created_by_owner':r['owner']};save(r)
    finally: conn.close()
    if template is None:
        x=command([PYTHON,'-m','alembic','upgrade','head'],env=environment(r,key)); (OUT/(key+'-upgrade.log')).write_text(x.stdout+x.stderr)


SEED='''
from sqlalchemy.orm import Session
from db.base import engine
from db.models import Source
from services.personal.workspace import ensure_workspace,configure_profile
from services.personal.offline_fixture import offline_fixture_route
from services.personal.processing import switch_processing_mode
with Session(engine) as s:
 source=Source(name='Synthetic Phase 4A owned feed',feed_url='https://offline.personal.test/feed.xml');s.add(source);s.flush()
 workspace,_=ensure_workspace(s)
 configure_profile(s,workspace,selected_source_ids=[source.id],execution_profile='assisted',settings={'model_route':offline_fixture_route('phase-2-offline-workflow-v1'),'authorized_spend_usd':0})
 switch_processing_mode(s,'personal');s.commit()
engine.dispose()
'''


def start(r, kind, args, env):
    assert kind not in r['processes']
    with (OUT/(kind+'.log')).open('w') as log:
        proc=subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True,shell=False)
    started=command(['ps','-p',str(proc.pid),'-o','lstart=']).stdout.strip()
    cmdline=command(['ps','-ww','-p',str(proc.pid),'-o','command=']).stdout.strip()
    r['processes'][kind]={'pid':proc.pid,'created':started,'args':args,'cmdline':cmdline,'pgid':os.getpgid(proc.pid),'owner':r['owner'],'started_utc':dt.datetime.now(dt.UTC).isoformat()};save(r)


def stop(r, kind):
    rec=r['processes'][kind]
    if rec.get('stopped_utc'): return
    started=command(['ps','-p',str(rec['pid']),'-o','lstart='],check=False).stdout.strip()
    current=command(['ps','-ww','-p',str(rec['pid']),'-o','command='],check=False).stdout.strip()
    if not started:
        rec['already_absent']=True
    else:
        assert started==rec['created'] and current==rec['cmdline'] and os.getpgid(rec['pid'])==rec['pgid']==rec['pid']
        os.killpg(rec['pid'],signal.SIGTERM)
        for attempt in range(350):
            result=command(['ps','-p',str(rec['pid']),'-o','stat='],check=False)
            if not result.stdout.strip() or 'Z' in result.stdout: break
            time.sleep(.1)
        else:
            assert command(['ps','-p',str(rec['pid']),'-o','lstart=']).stdout.strip()==rec['created']
            assert command(['ps','-ww','-p',str(rec['pid']),'-o','command=']).stdout.strip()==rec['cmdline']
            os.killpg(rec['pid'],signal.SIGKILL)
    rec['stopped_utc']=dt.datetime.now(dt.UTC).isoformat();save(r)


def init():
    assert not STATE.exists(); pg=V['resource'](); owner=pg['owner']; image='sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf'
    r={'owner':owner,'databases':{},'processes':{},'api_port':port(),'frontend_port':port()}
    cid=command(['docker','run','-d','--pull','never','--name','nip-phase4a-redis-'+owner[:12],'--label','nip.phase4a.owner='+owner,'-p','127.0.0.1::6379','--tmpfs','/data:rw',image,'redis-server','--save','','--appendonly','no']).stdout.strip()
    row=json.loads(command(['docker','inspect',cid]).stdout)[0]
    r['redis']={'id':cid,'image':row['Image'],'name':row['Name'],'created':row['Created'],'port':int(row['NetworkSettings']['Ports']['6379/tcp'][0]['HostPort']),'mounts':row['Mounts']};save(r)
    create_database(r,'template');py(r,'template',SEED)
    # Prepare one failed attempt with frozen article/event/evidence/snapshot inputs.
    code='''
import datetime as dt,json
from db.base import SessionLocal,engine
from services.personal.workspace import get_workspace
from services.personal.runs import create_daily_run
from services.personal.coordinator import run_personal_daily,PersonalCoordinatorError
from services.personal.offline_fixture import load_offline_personal_fixture
from packages.config.settings import get_settings
with SessionLocal() as s:
 w,_=get_workspace(s); d=create_daily_run(s,w,now=dt.datetime.now(dt.UTC));rid,token=d.run.id,d.run.ownership_token;s.commit()
f=load_offline_personal_fixture(get_settings().personal_offline_fixture_path)
def fail(*args): raise RuntimeError('controlled parity setup failure after input snapshot freeze')
try:run_personal_daily(rid,token,session_factory=SessionLocal,rss_provider_factory=f.rss_provider,embedding_provider=f.embedding_provider(),orchestrator_factory=fail,now=dt.datetime.now(dt.UTC))
except PersonalCoordinatorError:pass
from db.models import PersonalRun
with SessionLocal() as s:
 run=s.get(PersonalRun,rid);assert run.state in {'failed','partially_failed'} and run.snapshot_id;print(json.dumps({'run_id':str(rid),'snapshot_id':str(run.snapshot_id),'state':run.state}))
engine.dispose()
'''
    result=py(r,'template',code);(OUT/'parity-template.log').write_text(result.stdout+result.stderr);r['parity_run_id']=json.loads(result.stdout.strip().splitlines()[-1])['run_id'];save(r)
    create_database(r,'celery',template='template');create_database(r,'child',template='template')
    create_database(r,'ui');py(r,'ui',SEED);create_database(r,'manual');py(r,'manual',SEED)
    worker=[PYTHON,'-m','celery','-A','workers.celery_app.celery_app','worker','--loglevel=INFO','--pool=solo','--queues=pipeline','--hostname=phase4a-'+owner[:12]+'@localhost','--without-gossip','--without-mingle']
    start(r,'worker',worker,environment(r,'celery',transport='celery'))
    beat=[PYTHON,'-m','celery','-A','workers.celery_app.celery_app','beat','--loglevel=INFO','--schedule',str(OUT/'owned-beat-schedule'),'--pidfile',str(OUT/'owned-beat.pid')]
    start(r,'beat',beat,environment(r,'celery',transport='celery'))
    print(json.dumps({'api_port':r['api_port'],'frontend_port':r['frontend_port'],'redis_port':r['redis']['port'],'parity_run_id':r['parity_run_id']}))


def task_baseline():
    r=state()
    code='''
import datetime as dt,json,time,uuid
from db.base import SessionLocal
from db.models import PersonalRun,PersonalWorkspace
from services.personal.runs import retry_run,record_delivery
from workers.celery_app import celery_app,QUEUE_PIPELINE
from workers.personal_tasks import run_personal_daily_task
rid=uuid.UUID(%s)
with SessionLocal() as s:
 run=s.get(PersonalRun,rid);w=s.get(PersonalWorkspace,run.workspace_id);d=retry_run(s,w,rid,now=dt.datetime.now(dt.UTC));token=d.run.ownership_token;taskid=str(uuid.uuid4());record_delivery(s,rid,token,taskid);s.commit()
assert celery_app.tasks['workers.personal_tasks.run_personal_daily'] is run_personal_daily_task._get_current_object()
result=celery_app.send_task('workers.personal_tasks.run_personal_daily',args=[str(rid),str(token)],queue=QUEUE_PIPELINE,task_id=taskid)
for n in range(150):
 with SessionLocal() as s:
  run=s.get(PersonalRun,rid)
  if run.state not in {'queued','running'}: print(json.dumps({'task_id':taskid,'run_id':str(rid),'state':run.state,'report_id':str(run.report_id),'registered_task':run_personal_daily_task.name}));assert run.state=='succeeded';break
 time.sleep(.2)
else:raise RuntimeError('registered Celery baseline timed out')
''' % repr(r['parity_run_id'])
    result=py(r,'celery2',code,transport='celery');(OUT/'celery-baseline.log').write_text(result.stdout+result.stderr)
    stop(r,'beat');stop(r,'worker');write('worker-scheduler-stopped.json',{'worker':r['processes']['worker'],'beat':r['processes']['beat'],'utc':dt.datetime.now(dt.UTC).isoformat()})
    print(result.stdout)


def start_ui():
    r=state(); assert r['processes']['worker'].get('stopped_utc') and r['processes']['beat'].get('stopped_utc')
    start(r,'api',[PYTHON,'-m','services.personal.app','--port',str(r['api_port'])],environment(r,'ui'))
    start(r,'frontend',[PYTHON,'-m','http.server',str(r['frontend_port']),'--bind','127.0.0.1','--directory',str(ROOT/'frontend')],environment(r,'ui'))
    print(f"http://127.0.0.1:{r['frontend_port']}/SIGNAL%20-%20Intelligence%20Platform.html?api=http://127.0.0.1:{r['api_port']}")


def manual():
    r=state(); assert r['processes']['worker'].get('stopped_utc') and r['processes']['beat'].get('stopped_utc')
    for db,args in [('child',['retry',r['parity_run_id']]),('manual',['start'])]:
        result=command([PYTHON,'-m','services.personal.cli',*args],env=environment(r,db),check=False,timeout=60)
        (OUT/(db+'-cli.log')).write_text(result.stdout+result.stderr);write(db+'-cli-result.json',{'command':[PYTHON,'-m','services.personal.cli',*args],'exit_code':result.returncode,'dataset':'synthetic','ordinary_paid_activation':False})
        assert result.returncode==0,result.stdout+result.stderr
    print('Both actual manual commands completed')


def cleanup():
    r=state()
    for kind in reversed(tuple(r['processes'])):
        if kind in r['processes']:stop(r,kind)
    import psycopg2
    pg=V['resource']();conn=psycopg2.connect(host='127.0.0.1',port=pg['port'],user='news',password='news',dbname='postgres');conn.autocommit=True
    try:
        with conn.cursor() as c:
            for rec in r['databases'].values():
                c.execute('SELECT oid FROM pg_database WHERE datname=%s',(rec['name'],));assert c.fetchone()==(rec['oid'],)
                c.execute('SELECT pid FROM pg_stat_activity WHERE datname=%s',(rec['name'],));assert c.fetchall()==[]
                c.execute('DROP DATABASE "'+rec['name']+'"');c.execute('SELECT oid FROM pg_database WHERE datname=%s',(rec['name'],));assert c.fetchone() is None;rec['absent_after_drop']=True
    finally:conn.close()
    rec=r['redis'];row=json.loads(command(['docker','inspect',rec['id']]).stdout)[0]
    assert row['Id']==rec['id'] and row['Image']==rec['image'] and row['Created']==rec['created'] and row['Config']['Labels']['nip.phase4a.owner']==r['owner'] and row['Mounts']==rec['mounts']==[]
    command(['docker','stop',rec['id']]);command(['docker','rm',rec['id']]);rec['removed']=True;save(r)
    write('stack-cleanup.json',{'databases':r['databases'],'processes':r['processes'],'redis':rec,'completed_utc':dt.datetime.now(dt.UTC).isoformat()})
    print('Exact-owned stack stopped and removed')


if __name__=='__main__':
    {'init':init,'baseline':task_baseline,'ui':start_ui,'manual':manual,'cleanup':cleanup}[sys.argv[1]]()
