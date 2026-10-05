"""Frozen offline component checks; no external provider connections permitted."""
import contextlib,json,runpy,socket,sys
from pathlib import Path
V=runpy.run_path(str(Path(__file__).with_name('verify.py')));OUT=V['OUT'];ROOT=V['ROOT']
import pytest
paths=sorted(str(p.relative_to(ROOT)) for p in (ROOT/'tests/unit').glob('test_personal*.py'))
paths+=['tests/unit/test_http_rss_provider.py','tests/unit/test_episode_seed.py','tests/unit/test_health_readiness.py','tests/unit/test_intelligence_api.py','tests/unit/test_report_api.py','tests/unit/test_report_worker.py']
paths=[p for p in paths if (ROOT/p).exists()];before=V['snapshot']('accepted-units-source-before')
original=socket.socket.connect
original_ex=socket.socket.connect_ex
def guarded(self,address):
 if self.family in (socket.AF_INET,socket.AF_INET6):
  import ipaddress
  assert ipaddress.ip_address(address[0]).is_loopback,'external unit network blocked'
 return original(self,address)
def guarded_ex(self,address):
 if self.family in (socket.AF_INET,socket.AF_INET6):
  import ipaddress
  assert ipaddress.ip_address(address[0]).is_loopback,'external unit network blocked'
 return original_ex(self,address)
socket.socket.connect=guarded;socket.socket.connect_ex=guarded_ex
args=['-q','-p','no:cacheprovider','--junitxml='+str(OUT/'accepted-units.xml'),*paths]
V['write']('accepted-units-command.json',{'args':args,'credentials_blank':True,'paid_gate':False,'network':'loopback synthetic HTTP servers only; external sockets blocked'})
try:
 with (OUT/'accepted-units.log').open('w') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):code=int(pytest.main(args))
finally:socket.socket.connect=original;socket.socket.connect_ex=original_ex
after=V['snapshot']('accepted-units-source-after');drift=sorted(k for k in set(before['hashes'])|set(after['hashes']) if before['hashes'].get(k)!=after['hashes'].get(k));V['write']('accepted-units-result.json',{'exit_code':code,'source_drift':drift,'protected_unchanged':before['protected_hashes']==after['protected_hashes']})
print((OUT/'accepted-units.log').read_text()[-6000:]);assert not drift;assert before['protected_hashes']==after['protected_hashes'];sys.exit(code)
