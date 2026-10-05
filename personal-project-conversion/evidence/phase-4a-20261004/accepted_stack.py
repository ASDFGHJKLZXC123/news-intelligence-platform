"""Refresh actual registered-task and managed-child proof after runtime source freeze."""
import json,runpy,time
from pathlib import Path
M=runpy.run_path(str(Path(__file__).with_name('stack.py')));R=M['state']();OUT=M['OUT'];V=M['V'];V['snapshot']('actual-accepted-source-before')
for key in ('celery_accepted','child_accepted','ui_accepted'):M['create_database'](R,key,template='template')
for key in ('manual_accepted','concurrent_accepted'):M['create_database'](R,key);M['py'](R,key,M['SEED'])
worker=[M['PYTHON'],'-m','celery','-A','workers.celery_app.celery_app','worker','--loglevel=INFO','--pool=solo','--queues=pipeline','--hostname=phase4a-accepted-'+R['owner'][:12]+'@localhost','--without-gossip','--without-mingle']
beat=[M['PYTHON'],'-m','celery','-A','workers.celery_app.celery_app','beat','--loglevel=INFO','--schedule',str(OUT/'owned-beat-accepted-epoch-schedule'),'--pidfile',str(OUT/'owned-beat-accepted-epoch.pid')]
M['start'](R,'worker_accepted',worker,M['environment'](R,'celery_accepted',transport='celery'));M['start'](R,'beat_accepted',beat,M['environment'](R,'celery_accepted',transport='celery'))
CODE='''
import datetime as dt,json,time,uuid
from db.base import SessionLocal,engine
from db.models import PersonalRun,PersonalWorkspace
from services.personal.runs import retry_run,record_delivery
from workers.celery_app import celery_app,QUEUE_PIPELINE
from workers.personal_tasks import run_personal_daily_task
rid=uuid.UUID(RUN_ID)
with SessionLocal() as s:
 run=s.get(PersonalRun,rid);w=s.get(PersonalWorkspace,run.workspace_id);d=retry_run(s,w,rid,now=dt.datetime.now(dt.UTC));token=d.run.ownership_token;taskid=str(uuid.uuid4());record_delivery(s,rid,token,taskid);s.commit()
assert celery_app.tasks['workers.personal_tasks.run_personal_daily'] is run_personal_daily_task._get_current_object()
celery_app.send_task('workers.personal_tasks.run_personal_daily',args=[str(rid),str(token)],queue=QUEUE_PIPELINE,task_id=taskid)
for n in range(150):
 with SessionLocal() as s:
  run=s.get(PersonalRun,rid)
  if run.state not in {'queued','running'}:
   result={'task_id':taskid,'run_id':str(rid),'state':run.state,'report_id':str(run.report_id),'registered_task':run_personal_daily_task.name,'delivery_token_rotated':run.ownership_token!=token};print(json.dumps(result));assert run.state=='succeeded' and result['delivery_token_rotated'];break
 time.sleep(.2)
else:raise RuntimeError('Actual accepted Celery baseline timed out')
engine.dispose()
'''
try:
 result=M['command']([M['PYTHON'],'-c','RUN_ID='+repr(R['parity_run_id'])+'\n'+CODE],env=M['environment'](R,'celery_accepted',transport='celery'),check=False);(OUT/'celery-accepted.log').write_text(result.stdout+result.stderr);assert result.returncode==0,result.stderr
finally:M['stop'](R,'beat_accepted');M['stop'](R,'worker_accepted')
M['write']('worker-scheduler-accepted-stopped.json',{'worker':R['processes']['worker_accepted'],'beat':R['processes']['beat_accepted'],'prior_worker':R['processes']['worker'],'prior_beat':R['processes']['beat']})
for key,args in [('child_accepted',['retry',R['parity_run_id']]),('manual_accepted',['start'])]:
 result=M['command']([M['PYTHON'],'-m','services.personal.cli',*args],env=M['environment'](R,key),check=False);(OUT/(key+'-cli.log')).write_text(result.stdout+result.stderr);M['write'](key+'-cli-result.json',{'args':args,'exit_code':result.returncode,'worker_and_scheduler_stopped':True});assert result.returncode==0,result.stderr
R['accepted_api_port']=M['port']();R['accepted_concurrent_api_port']=M['port']();M['save'](R)
M['start'](R,'api_accepted',[M['PYTHON'],'-m','services.personal.app','--port',str(R['accepted_api_port'])],M['environment'](R,'ui_accepted'))
M['start'](R,'api_concurrent_accepted',[M['PYTHON'],'-m','services.personal.app','--port',str(R['accepted_concurrent_api_port'])],M['environment'](R,'concurrent_accepted'))
M['write']('accepted-browser-endpoints.json',{'retry':f"http://127.0.0.1:{R['frontend_port']}/SIGNAL%20-%20Intelligence%20Platform.html?api=http://127.0.0.1:{R['accepted_api_port']}",'concurrent_api':f"http://127.0.0.1:{R['accepted_concurrent_api_port']}"})
print(json.dumps({'retry_api_port':R['accepted_api_port'],'concurrent_api_port':R['accepted_concurrent_api_port'],'both_manual_commands_passed':True,'registered_celery_rotated_token':True}))
