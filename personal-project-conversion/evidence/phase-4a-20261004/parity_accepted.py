"""Compare actual registered Celery and supervised child runs over cloned frozen inputs."""
import hashlib,json,runpy,sys
from pathlib import Path
M=runpy.run_path(str(Path(__file__).with_name('stack.py')));R=M['state']()
CODE=r'''import json,uuid
from sqlalchemy import select
from db.base import SessionLocal,engine
from db.models import PersonalRun,PersonalBriefSnapshot,Report,ReportSection,EventArticle,Article,Event
from db.models.personal_spending import PersonalPaidRequest
rid=uuid.UUID(RUN_ID)
with SessionLocal() as s:
 run=s.get(PersonalRun,rid);snapshot=s.get(PersonalBriefSnapshot,run.snapshot_id);report=s.get(Report,run.report_id)
 result={"run_id":str(run.id),"state":run.state,"attempt":run.attempt,"article_ids":sorted(map(str,run.admitted_article_ids)),"enrichment_ids":sorted(map(str,run.enrichment_article_ids)),"event_ids":sorted(map(str,run.event_ids)),"snapshot_id":str(snapshot.id),"snapshot_hash":snapshot.input_hash,"snapshot_payload":snapshot.input_payload,"snapshot_route":snapshot.model_route,"snapshot_selected_events":sorted(map(str,snapshot.selected_event_ids)),"article_event_associations":sorted([str(row.event_id),str(row.article_id)] for row in s.scalars(select(EventArticle).where(EventArticle.event_id.in_(run.event_ids)))),"articles":[{"id":str(a.id),"title":a.title,"url":a.url,"summary":a.summary,"source_id":str(a.source_id)} for a in s.scalars(select(Article).where(Article.id.in_(run.admitted_article_ids)).order_by(Article.id))],"events":[{"id":str(e.id),"title":e.title,"summary":e.summary} for e in s.scalars(select(Event).where(Event.id.in_(run.event_ids)).order_by(Event.id))],"report":{"title":report.title,"status":report.status,"version":report.version,"content_policy":report.content_policy},"sections":[{"order":x.section_order,"title":x.title,"body":x.body,"blocks":x.blocks,"evidence_refs":list(map(str,x.evidence_refs or [])),"grounding":x.grounding_status} for x in s.scalars(select(ReportSection).where(ReportSection.report_id==report.id).order_by(ReportSection.section_order))],"paid_requests":[{"status":x.status,"reserved_usd":str(x.reserved_usd),"actual_usd":str(x.actual_usd)} for x in s.scalars(select(PersonalPaidRequest).where(PersonalPaidRequest.run_id==rid))]}
 print(json.dumps(result,sort_keys=True,default=str));assert run.state=="succeeded"
engine.dispose()
'''
keys=sys.argv[1:] or ['celery2','child','ui_retry'];values={}
for key in keys:
 code='RUN_ID='+repr(R['parity_run_id'])+'\n'+CODE
 result=M['py'](R,key,code,transport='celery' if key.startswith('celery') else 'subprocess')
 value=json.loads(result.stdout);values[key]=value;M['write'](key+'-normalized-output.json',value)
first=values[keys[0]]
comparison={key:{'equal':value==first,'sha256':hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'different_fields':[k for k in set(first)|set(value) if first.get(k)!=value.get(k)]} for key,value in values.items()}
M['write']('transport-parity-accepted.json',{'fixture':'phase-2-offline-workflow-v1','baseline_registered_task':'workers.personal_tasks.run_personal_daily','data':comparison,'excluded_differences':['generated report and telemetry UUIDs','claim/publication wall-clock timestamps','transport label/generation'],'credentials_blank':True,'ordinary_paid_activation':False})
print(json.dumps(comparison,indent=2));assert all(x['equal'] for x in comparison.values())
