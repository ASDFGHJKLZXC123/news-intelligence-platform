"""Two actual simultaneous HTTP starts; one actual child and durable logical run."""
import json,runpy,threading,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
M=runpy.run_path(str(Path(__file__).with_name('stack.py')));R=M['state']();url='http://127.0.0.1:'+str(R['accepted_concurrent_api_port']);barrier=threading.Barrier(2)
def start(n):
 barrier.wait(timeout=10);response=httpx.post(url+'/api/v1/personal/runs',timeout=15);return {'status':response.status_code,'body':response.json(),'request_id':response.headers.get('x-request-id')}
with ThreadPoolExecutor(max_workers=2) as executor:responses=list(executor.map(start,range(2)))
M['write']('concurrent-api-accepted-responses.json',responses);assert all(x['status']==202 for x in responses),responses
assert all(x['body']['run']['runtime']['transport']=='subprocess' for x in responses),responses
ids={x['body']['run']['id'] for x in responses};assert len(ids)==1;rid=ids.pop()
for n in range(150):
 result=httpx.get(url+'/api/v1/personal/runs/'+rid,timeout=5).json()['run']
 if result['state'] not in {'queued','running'}:break
 time.sleep(.1)
assert result['state']=='succeeded' and result['attempt']==1
CODE='''
import json
from sqlalchemy import func,select
from db.base import SessionLocal,engine
from db.models import PersonalRun,PersonalWriterMode,Article,Event
from db.models.personal_spending import PersonalPaidRequest
with SessionLocal() as s:
 control=s.get(PersonalWriterMode,True)
 proof={'runs':s.scalar(select(func.count()).select_from(PersonalRun)),'child_history':control.child_history,'articles':s.scalar(select(func.count()).select_from(Article)),'events':s.scalar(select(func.count()).select_from(Event)),'paid_requests':s.scalar(select(func.count()).select_from(PersonalPaidRequest))}
 print(json.dumps(proof,sort_keys=True,default=str))
engine.dispose()
'''
proof=json.loads(M['py'](R,'concurrent_accepted',CODE).stdout);assert proof['runs']==1 and len(proof['child_history'])==1 and proof['articles']==1 and proof['events']==1 and proof['paid_requests']==0
M['write']('concurrent-api-accepted-result.json',{'responses':responses,'terminal':result,'database':proof,'one_actual_child':True,'worker_scheduler_stopped':True,'external_provider_calls':0});print('Two simultaneous HTTP starts completed one logical attempt and one actual child')
