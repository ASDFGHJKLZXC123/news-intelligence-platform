"""Literal owned PostgreSQL outage with scripted provider response and retained costs."""
import datetime as dt
import json
from pathlib import Path
import runpy
import subprocess
import time

M=runpy.run_path(str(Path(__file__).with_name('stack.py')));R=M['state']();V=M['V'];OUT=M['OUT'];owner=R['owner']
image=V['resource']()['image'];name='nip-phase4a-outage-'+owner[:12];database='nip_phase4a_outage_'+owner[:12]
args=['docker','run','-d','--pull','never','--name',name,'--label','nip.phase4a.owner='+owner,'--label','nip.phase4a.purpose=literal-postgres-outage','-p','127.0.0.1::5432','--tmpfs','/var/lib/postgresql/data:rw','-e','POSTGRES_USER=news','-e','POSTGRES_PASSWORD=news','-e','POSTGRES_DB='+database,image]
cid=M['command'](args).stdout.strip();row=json.loads(M['command'](['docker','inspect',cid]).stdout)[0];port=int(row['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']);rec={'id':cid,'name':name,'owner':owner,'image':image,'created':row['Created'],'port':port,'mounts':row['Mounts'],'creation_command':args};M['write']('outage-owned-resource.json',rec)

def exact():
 row=json.loads(M['command'](['docker','inspect',cid]).stdout)[0]
 assert row['Id']==cid and row['Image']==image and row['Created']==rec['created'] and row['Config']['Labels']['nip.phase4a.owner']==owner and row['Mounts']==[] and row['HostConfig']['Tmpfs']=={'/var/lib/postgresql/data':'rw'}
 return row

try:
 for attempt in range(60):
  ready=M['command'](['docker','exec',cid,'pg_isready','-U','news','-d',database],check=False)
  if ready.returncode==0:
   time.sleep(.5);break
  time.sleep(.25)
 else:raise RuntimeError('Owned outage PostgreSQL did not start')
 env=M['environment'](R,'manual');env['DATABASE_URL']=f'postgresql+psycopg2://news:news@127.0.0.1:{port}/{database}';env['OUTAGE_CONTAINER_ID']=cid;env['OUTAGE_OWNER']=owner;env['OUTAGE_DOCKER_CREATED']=rec['created'];env['OUTAGE_IMAGE']=image
 upgrade=M['command']([M['PYTHON'],'-m','alembic','upgrade','head'],env=env);(OUT/'outage-upgrade.log').write_text(upgrade.stdout+upgrade.stderr)
 CODE='''
import datetime as dt,json,os,subprocess,time,uuid
from types import SimpleNamespace
import httpx
from sqlalchemy import create_engine,select
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import OperationalError
from db.models import Article,PersonalRun,PersonalWriterMode,Source
from db.models.personal_spending import PersonalPaidRequest
from services.personal.runs import lock_owned_run,PersonalOwnershipLost
from services.personal.spending import PaidRoute,PaidWorkBlocked
from services.personal.paid_runtime import DurablePaidHTTPClient
from tests.integration.test_personal_phase4a_faults import _paid_owner,_reserve,_takeover,_assert_newer_terminal
from tests.unit.test_personal_spending import route_mapping
cid=os.environ['OUTAGE_CONTAINER_ID'];owner=os.environ['OUTAGE_OWNER']
def docker(*args):return subprocess.run(['docker',*args],check=True,capture_output=True,text=True,timeout=15).stdout
row=json.loads(docker('inspect',cid))[0];assert row['Config']['Labels']['nip.phase4a.owner']==owner and row['Created']==os.environ['OUTAGE_DOCKER_CREATED'] and row['Image']==os.environ['OUTAGE_IMAGE'] and row['Mounts']==[]
engine=create_engine(os.environ['DATABASE_URL'],poolclass=NullPool,connect_args={'connect_timeout':1,'options':'-c statement_timeout=1000 -c lock_timeout=1000'})
factory,ledger=_paid_owner(engine)
with factory() as s:
 run=lock_owned_run(s,ledger.run_id,ledger.ownership_token);source=s.scalar(select(Source));article=Article(url_hash='0'*64,title='Synthetic frozen outage input',url='https://fixture.invalid/outage-1',source_id=source.id);s.add(article);s.flush();run.admitted_article_ids=[article.id];run.enrichment_article_ids=[article.id];frozen={'scopes_frozen_at':run.scopes_frozen_at.isoformat(),'admitted':list(map(str,run.admitted_article_ids)),'enrichment':list(map(str,run.enrichment_article_ids))};s.commit()
reserved=_reserve(ledger);sent=[];timings={}
def responding(*args,**kwargs):
 sent.append('scripted physical handoff');docker('pause',cid);assert json.loads(docker('inspect',cid))[0]['State']['Paused']
 return httpx.Response(200,headers={'x-request-id':'synthetic-outage-receipt'},json={'usage':{'prompt_tokens':250,'completion_tokens':250}})
client=DurablePaidHTTPClient(route=PaidRoute.from_mapping(route_mapping(),role='generation'),ledger=ledger,delegate=SimpleNamespace(post=responding))
try:
 began=time.monotonic()
 try:client.post('/v1/chat/completions',json={'model':'synthetic-only','max_tokens':1000})
 except OperationalError:timings['inflight_response_accounting_connection_failure_seconds']=time.monotonic()-began
 else:raise AssertionError('Literal paused database unexpectedly accepted accounting')
 began=time.monotonic();blocked=DurablePaidHTTPClient(route=PaidRoute.from_mapping(route_mapping(),role='generation'),ledger=ledger,delegate=SimpleNamespace(post=lambda *a,**kw:sent.append('forbidden next send')))
 try:blocked.post('/v1/chat/completions',json={'model':'synthetic-only','max_tokens':1000})
 except OperationalError:timings['new_dispatch_connection_failure_seconds']=time.monotonic()-began
 else:raise AssertionError('Database outage did not stop dispatch')
 assert sent==['scripted physical handoff']
finally:
 if json.loads(docker('inspect',cid))[0]['State']['Paused']:docker('unpause',cid)
with factory() as s:
 rows=s.scalars(select(PersonalPaidRequest).where(PersonalPaidRequest.run_id==ledger.run_id)).all();assert len(rows)==2
 inflight=next(x.id for x in rows if x.status=='dispatching');assert s.get(PersonalPaidRequest,reserved).status=='reserved'
 _frozen=s.get(PersonalRun,ledger.run_id);assert frozen=={'scopes_frozen_at':_frozen.scopes_frozen_at.isoformat(),'admitted':list(map(str,_frozen.admitted_article_ids)),'enrichment':list(map(str,_frozen.enrichment_article_ids))}
# Explicit synthetic expiry followed by the production retry/acquire/terminal path.
new_token,generation=_takeover(engine,ledger.run_id)
for operation in ('dispatch','business'):
 try:
  if operation=='dispatch':ledger.dispatch(reserved)
  else:
   with factory() as s:
    lock_owned_run(s,ledger.run_id,ledger.ownership_token);s.get(Article,article.id).title='forbidden stale overwrite';s.commit()
 except (PaidWorkBlocked,PersonalOwnershipLost):pass
 else:raise AssertionError('Reconnect restored a stale token')
ledger.reconcile(inflight,input_tokens=250,output_tokens=250,provider_request_id='synthetic-outage-receipt',evidence={'scripted_provider':True,'actual_database_outage':True})
with factory() as s:
 run=_assert_newer_terminal(s,ledger.run_id);assert s.get(Article,article.id).title=='Synthetic frozen outage input'
 assert frozen=={'scopes_frozen_at':run.scopes_frozen_at.isoformat(),'admitted':list(map(str,run.admitted_article_ids)),'enrichment':list(map(str,run.enrichment_article_ids))}
 request=s.get(PersonalPaidRequest,reserved);assert request.status=='reserved' and request.dispatch_attempt_at is None
 reconciled=s.get(PersonalPaidRequest,inflight);assert reconciled.status=='reconciled' and str(reconciled.actual_usd)=='0.005000000000'
 result={'passed':True,'database_paused_and_unpaused':True,'run_id':str(run.id),'new_attempt':run.attempt,'new_generation':generation,'reservation_id':str(reserved),'inflight_request_id':str(inflight),'frozen_inputs':frozen,'retained_reservation_usd':str(request.reserved_usd),'late_reconciled_actual_usd':str(reconciled.actual_usd),'network_handoffs':sent,'timings':timings,'external_provider_calls':0,'ordinary_paid_activation':False,'ownership_expiry':'explicit test acceleration; production retry/acquire used'}
engine.dispose();print(json.dumps(result,sort_keys=True))
'''
 result=M['command']([M['PYTHON'],'-c',CODE],env=env,check=False,timeout=60);(OUT/'postgres-outage.log').write_text(result.stdout+result.stderr);assert result.returncode==0,result.stderr;M['write']('postgres-outage-result.json',json.loads(result.stdout));print(result.stdout)
finally:
 row=exact()
 if row['State']['Paused']:M['command'](['docker','unpause',cid])
 M['command'](['docker','stop',cid]);exact();M['command'](['docker','rm',cid]);assert M['command'](['docker','ps','-aq','--no-trunc','--filter','id='+cid]).stdout.strip()==''
 M['write']('outage-cleanup.json',{'exact_identity_verified':True,'container_absent':True,'no_volume':True,'resource':rec,'completed_utc':dt.datetime.now(dt.UTC).isoformat()})
