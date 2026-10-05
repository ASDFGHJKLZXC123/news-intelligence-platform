"""Exact-owned populated logical restore; synthetic records and no external providers."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import runpy
import subprocess

M=runpy.run_path(str(Path(__file__).with_name('stack.py')));R=M['state']();V=M['V'];PG=V['resource']();OUT=M['OUT']
assert R['databases']['backup_source']['created_by_owner']==R['owner']
SEED='''
import datetime as dt,json
from sqlalchemy import select
from db.base import SessionLocal,engine
from db.models import PersonalRun,PersonalProfileRevision,PersonalWorkspace,PersonalWriterMode,Source
from db.models.personal_spending import PersonalPaidRequest
from services.personal.settings import update_settings,PersonalSettingsUpdate
from services.personal.runs import create_daily_run,acquire_run,finish_run
from services.personal.spending import SpendingLedger,PaidRoute
from tests.unit.test_personal_spending import model_route,route_mapping
now=dt.datetime.now(dt.UTC)+dt.timedelta(days=1)
with SessionLocal() as s:
 w=s.scalar(select(PersonalWorkspace));source=s.scalar(select(Source));control=s.get(PersonalWriterMode,True)
 if control.active_run_id:
  r=s.get(PersonalRun,control.active_run_id);now=r.started_at
 else:
  update_settings(s,w,PersonalSettingsUpdate(execution_profile='assisted',selected_source_ids=[source.id],ai_enabled=True,monthly_allowance_usd='0.03',model_route=model_route()))
  d=create_daily_run(s,w,now=now);r=acquire_run(s,d.run.id,d.run.ownership_token,now=now);r.scopes_frozen_at=now
 rid,token,wid=r.id,r.ownership_token,w.id;s.commit()
ledger=SpendingLedger(session_factory=SessionLocal,workspace_id=wid,run_id=rid,ownership_token=token,clock=lambda:now)
route=PaidRoute.from_mapping(route_mapping(),role='generation')
with SessionLocal() as s:
 known=s.scalar(select(PersonalPaidRequest.id).where(PersonalPaidRequest.run_id==rid))
if known is None:
 known=ledger.reserve(route,input_token_bound=1000,output_token_bound=1000);ledger.dispatch(known)
ledger.reconcile(known,input_tokens=250,output_tokens=250,provider_request_id='synthetic-backup-known',evidence={'synthetic':True,'no_external_call':True})
uncertain=ledger.reserve(route,input_token_bound=1000,output_token_bound=1000);ledger.dispatch(uncertain);ledger.mark_uncertain(uncertain,'synthetic_backup_no_external_call')
with SessionLocal() as s:
 finish_run(s,rid,token,state='failed',error={'code':'synthetic_backup_history','retryable':True},now=now);s.commit()
print(json.dumps({'run_id':str(rid),'known_request_id':str(known),'uncertain_request_id':str(uncertain),'external_calls':0,'ordinary_paid_activation':False}))
engine.dispose()
'''
result=M['py'](R,'backup_source',SEED);M['write']('backup-seed.json',json.loads(result.stdout));(OUT/'backup-seed.log').write_text(result.stdout+result.stderr)
SNAPSHOT='''
import json
from sqlalchemy import text
from db.base import engine
with engine.connect() as c:
 tables=c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars().all()
 values={name:c.execute(text('SELECT row_to_json(x) FROM "'+name+'" x ORDER BY row_to_json(x)::text')).scalars().all() for name in tables}
 print(json.dumps(values,sort_keys=True,default=str))
engine.dispose()
'''
before=json.loads(M['py'](R,'backup_source',SNAPSHOT).stdout);M['write']('backup-before.json',before)
source=R['databases']['backup_source']['name'];dump_args=['docker','exec',PG['id'],'pg_dump','-U','news','-d',source,'--format=custom','--no-owner','--no-acl'];V['resource']()
dump=subprocess.run(dump_args,capture_output=True,check=True);path=OUT/'populated-personal.backup';assert not path.exists();path.write_bytes(dump.stdout)
# Create a new exact-owned empty target. Record its OID before restore, without schema migration.
import psycopg2
name='nip_phase4a_restored_'+R['owner'][:12]
conn=psycopg2.connect(host='127.0.0.1',port=PG['port'],user='news',password='news',dbname='postgres');conn.autocommit=True
with conn.cursor() as c:
 c.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,));assert c.fetchone() is None
 c.execute('CREATE DATABASE "'+name+'"');c.execute('SELECT oid FROM pg_database WHERE datname=%s',(name,));oid=c.fetchone()[0]
conn.close();R['databases']['restored']={'name':name,'oid':oid,'created_by_owner':R['owner']};M['save'](R)
restore_args=['docker','exec','-i',PG['id'],'pg_restore','-U','news','--dbname',name,'--exit-on-error','--no-owner','--no-acl'];V['resource']();restored=subprocess.run(restore_args,input=dump.stdout,capture_output=True,check=True);(OUT/'populated-restore.log').write_bytes(restored.stdout+restored.stderr)
after=json.loads(M['py'](R,'restored',SNAPSHOT).stdout);M['write']('backup-restored.json',after);assert before==after
mode=M['command']([M['PYTHON'],'-m','services.personal.cli','mode','legacy'],env=M['environment'](R,'restored'));(OUT/'restored-idle-legacy-switch.log').write_text(mode.stdout+mode.stderr)
post=json.loads(M['py'](R,'restored',SNAPSHOT).stdout);M['write']('backup-after-mode-switch.json',post)
assert before.keys()==post.keys();assert all(before[k]==post[k] for k in before if k!='personal_writer_mode')
assert post['personal_writer_mode'][0]['mode']=='legacy';assert post['personal_writer_mode'][0]['generation']>before['personal_writer_mode'][0]['generation']
READ='''
import json
from fastapi.testclient import TestClient
from apps.api.main import app
from db.base import engine
with TestClient(app) as c:
 result={path:c.get(path).status_code for path in ('/api/v1/personal/saved','/api/v1/personal/articles','/api/v1/personal/briefs','/api/v1/personal/spending')}
 assert all(value==200 for value in result.values()),result
 print(json.dumps(result))
engine.dispose()
'''
reads=json.loads(M['py'](R,'restored',READ).stdout);M['write']('restore-read-status.json',reads)
M['write']('backup-restore-result.json',{'dump_command':dump_args,'restore_command':restore_args,'backup_sha256':hashlib.sha256(dump.stdout).hexdigest(),'backup_bytes':len(dump.stdout),'exact_all_public_rows_equal':True,'row_counts':{k:len(v) for k,v in before.items()},'schema_revision':after['alembic_version'],'business_rows_equal_after_idle_legacy_switch':True,'all_tables_retained':True,'reads':reads,'source':R['databases']['backup_source'],'target':R['databases']['restored'],'ordinary_paid_activation':False,'external_provider_calls':0})
print('Populated restore and idle legacy rollback retained every public-table row and all business identities')
