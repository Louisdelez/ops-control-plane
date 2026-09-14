"""Compatibility repair for MCP 2 typed annotations; preserve fail-closed gating."""
from pathlib import Path
import ast,hashlib,json,os,shutil,time
from types import SimpleNamespace
assert os.geteuid()==0
p=Path('/var/lib/hermes/hermes-agent/tools/mcp_tool.py');s=p.read_text()
old='        hint = getattr(annotations, "readOnlyHint", None)'
new='        hint = getattr(annotations, "readOnlyHint", getattr(annotations, "read_only_hint", None))'
assert s.count(old)==1
changed=s.replace(old,new)
node=next(n for n in ast.parse(changed).body if isinstance(n,ast.FunctionDef) and n.name=='_annotation_read_only_hint')
namespace={'Any':object};exec(compile(ast.Module(body=[node],type_ignores=[]),str(p),'exec'),namespace)
f=namespace['_annotation_read_only_hint']
for annotations,expected in [(None,False),({},False),({'readOnlyHint':True},True),({'readOnlyHint':'true'},False),(SimpleNamespace(readOnlyHint=False),False),(SimpleNamespace(read_only_hint=True),True),(SimpleNamespace(read_only_hint=False),False),(SimpleNamespace(read_only_hint='true'),False)]:
 assert f(SimpleNamespace(annotations=annotations)) is expected
archive=Path('/var/lib/ops-native-archives')/('hermes-mcp2-'+str(time.time_ns()));archive.mkdir(mode=0o700);shutil.copy2(p,archive/'mcp_tool.py')
p.write_text(changed)
result={'archive':str(archive),'compatibility':'MCP 2 read_only_hint','fail_closed_cases_verified':8,'trust_changed':False,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
(archive/'result.json').write_text(json.dumps(result));print(json.dumps(result))
