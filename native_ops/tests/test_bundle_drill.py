import importlib.util,io,tarfile
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('drill',Path(__file__).parents[1]/'deploy/verify-recovery-bundle.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def archive(path,name,kind=tarfile.REGTYPE):
 with tarfile.open(path,'w') as t:
  info=tarfile.TarInfo(name);info.type=kind;info.size=1 if kind==tarfile.REGTYPE else 0;info.linkname='/etc/passwd' if kind==tarfile.SYMTYPE else '';t.addfile(info,io.BytesIO(b'x') if info.size else None)
@pytest.mark.parametrize('name,kind',[('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('file',tarfile.SYMTYPE)])
def test_reject_unsafe_members(tmp_path,name,kind):
 a=tmp_path/'a.tar';archive(a,name,kind)
 with pytest.raises(ValueError):m.extract(a,tmp_path/'out',{name})
def test_exact_member_set_and_permissions(tmp_path):
 a=tmp_path/'a.tar';archive(a,'broker/state.db')
 with pytest.raises(ValueError):m.extract(a,tmp_path/'wrong',{'unexpected'})
 m.extract(a,tmp_path/'out',{'broker/state.db'})
 p=tmp_path/'out/broker/state.db';assert p.read_bytes()==b'x' and p.stat().st_mode&0o777==0o600
