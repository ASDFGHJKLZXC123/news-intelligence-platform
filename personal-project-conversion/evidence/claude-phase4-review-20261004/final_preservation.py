import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).resolve().parent
SKIP={'.git','.venv','venv','__pycache__','.pytest_cache','.ruff_cache','node_modules','.claude'}
before=json.loads((OUT/'baseline-file-hashes.json').read_text())
after={}
links=[]
for folder,dirs,names in os.walk(ROOT):
    current=Path(folder)
    dirs[:]=[n for n in dirs if n not in SKIP and current/n!=OUT]
    for name in names:
        path=current/name
        if path.is_symlink():
            links.append(str(path.relative_to(ROOT)))
            continue
        after[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
added=sorted(after.keys()-before.keys())
removed=sorted(before.keys()-after.keys())
changed=sorted(k for k in before.keys()&after.keys() if before[k]!=after[k])
identity=json.loads((OUT/'source-snapshot.json').read_text())
snapshot=Path(identity['snapshot']).resolve()
before_links={x['path'] for x in identity['excluded_sensitive_or_generated_files'] if x.get('reason')=='symlink not copied'}
new_links=sorted(set(links)-before_links)
missing_links=sorted(before_links-set(links))
tools=collections.Counter()
outside=[]
errors=[]
for line in (OUT/'claude-events.jsonl').read_text().splitlines():
    try: event=json.loads(line)
    except json.JSONDecodeError: continue
    for block in event.get('message',{}).get('content',[]):
        if block.get('type')=='tool_use':
            name=block.get('name');tools[name]+=1
            fields=block.get('input',{})
            target=fields.get('file_path') or fields.get('path')
            if target:
                path=Path(target)
                if not path.is_absolute(): path=snapshot/path
                if not path.resolve().is_relative_to(snapshot):outside.append({'tool':name,'target':target})
        if block.get('type')=='tool_result' and block.get('is_error'):
            errors.append(str(block.get('content',''))[:250])
status=json.loads((OUT/'claude-result.json').read_text())
result={'checked_utc':dt.datetime.now(dt.UTC).isoformat(),'original_file_count':len(before),'new_original_paths_outside_audit_folder':added,'missing_original_paths':removed,'changed_original_paths':changed,'observed_original_symlinks':links,'new_original_symlink_paths':new_links,'missing_original_symlink_paths':missing_links,'actual_claude_tool_counts':dict(tools),'unexpected_tools':sorted(set(tools)-{'Read','Glob','Grep'}),'observed_tool_targets_outside_snapshot':outside,'tool_errors':errors,'protected_Git_controls':status['controls'],'exact_temporary_snapshot_removed':status.get('exact_temporary_snapshot_removed'),'private_files_excluded':True,'tests_executed_by_claude':False,'passed':not added and not removed and not changed and not new_links and not missing_links and not outside and not (set(tools)-{'Read','Glob','Grep'}) and all(status['controls'].values())}
(OUT/'final-preservation.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
assert result['passed']
