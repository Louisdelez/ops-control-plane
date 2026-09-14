import base64
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT=Path(__file__).resolve().parents[1]/'artifacts/recovery-patch'


def load():
    return runpy.run_path(str(ROOT/'install-recovery-g'))


def test_g_manifest_helper_and_unit_are_bound():
    g=load()
    helper=(ROOT/'control-plane-bootstrap.g').read_bytes()
    namespace=runpy.run_path(str(ROOT/'control-plane-bootstrap.g'))
    manifest=json.loads((ROOT/'release-manifest.g.v1.json').read_bytes())
    assert g['sha'](helper)==g['NEW_HELPER']==manifest['bootstrap']['helper_sha256']
    assert g['sha']((ROOT/'release-manifest.g.v1.json').read_bytes())==g['NEW_MANIFEST']
    assert namespace['RESUME_PATCH_CONTRACT']==manifest['bootstrap']['resume_patch']
    assert g['sha'](namespace['NETWORK_BASELINE_UNIT_BYTES'])==g['NEW_UNIT']
    assert b'CapabilityBoundingSet=CAP_NET_ADMIN CAP_SYS_PTRACE\n' in namespace['NETWORK_BASELINE_UNIT_BYTES']
    assert b'ReadWritePaths=' not in namespace['NETWORK_BASELINE_UNIT_BYTES']


def test_wal_migration_preserves_phase_and_all_other_fields():
    g=load()
    old={'phase':'start-planned','status':'applying','resume_helper_sha256':g['OLD_HELPER'],
         'resume_manifest_sha256':g['OLD_MANIFEST'],'unit_sha256':g['OLD_UNIT'],'transaction_id':'existing',
         'created_at':123,'updated_at':124,'completed_at':None}
    new=g['migrate_wal'](old)
    changed={k for k in old if old[k]!=new[k]}
    assert changed=={'resume_helper_sha256','resume_manifest_sha256','unit_sha256'}
    with pytest.raises(RuntimeError):
        g['migrate_wal'](dict(old,phase='committed'))


@pytest.mark.parametrize('prefix',range(6))
def test_every_interrupted_publication_prefix_can_resume(prefix,monkeypatch):
    g=load()
    namespace=g['reconcile'].__globals__
    files={}
    for path,(mode,old,new,name) in g['TARGETS'].items():
        fname=name.replace('.g','.f')
        if name=='network-baseline.g.service':
            before=runpy.run_path(str(ROOT/'control-plane-bootstrap.f'))['NETWORK_BASELINE_UNIT_BYTES']
        else:
            before=(ROOT/fname).read_bytes()
        files[path]={'old':base64.b64encode(before).decode(),'new':base64.b64encode((ROOT/name).read_bytes()).decode(),'mode':mode}
    old={'phase':'start-planned','status':'applying','resume_helper_sha256':g['OLD_HELPER'],
         'resume_manifest_sha256':g['OLD_MANIFEST'],'unit_sha256':g['OLD_UNIT']}
    files[str(g['WAL'])]={'old':base64.b64encode(g['canonical'](old)).decode(),
                         'new':base64.b64encode(g['canonical'](g['migrate_wal'](old))).decode(),'mode':0o600}
    plan={'schema':1,'status':'prepared','files':files,'marker_sha256':g['sha'](b'marker'),'standard_sha256':g['sha'](b'standard')}
    disk={str(g['MARKER']):b'marker',str(g['STANDARD']):b'standard',str(g['PLAN']):g['canonical'](plan)}
    for i,(path,record) in enumerate(files.items()):
        disk[path]=base64.b64decode(record['new' if i<prefix else 'old'])
    monkeypatch.setitem(namespace,'read',lambda path,mode:disk[str(path)])
    monkeypatch.setitem(namespace,'atomic',lambda path,raw,mode:disk.__setitem__(str(path),raw))
    monkeypatch.setitem(namespace,'pinned_namespace',lambda _: {
        '_read_corrective_marker':lambda: {},'_reviewed_manifest':lambda: ({},g['NEW_MANIFEST']),
        '_read_network_baseline_frontier':lambda *a,**k: {},
    })
    monkeypatch.setitem(namespace,'subprocess',SimpleNamespace(run=lambda *a,**k:None))
    g['reconcile']()
    assert json.loads(disk[str(g['PLAN'])])['status']=='complete'
    for path,record in files.items():
        assert disk[path]==base64.b64decode(record['new'])
    g['reconcile']()


def test_inactive_reset_does_not_call_systemctl_but_validates_identity(monkeypatch):
    g=runpy.run_path(str(ROOT/'control-plane-bootstrap.g'))
    ns=g['_network_baseline_systemctl'].__globals__
    checks=[]
    monkeypatch.setitem(ns,'_validate_network_baseline_unit',lambda **kw:checks.append(kw))
    monkeypatch.setitem(ns,'_strict_systemctl_properties',lambda unit:{'ActiveState':'inactive'})
    def unexpected(*a,**k):
        raise AssertionError('reset-failed must not run for an inactive unit')
    monkeypatch.setitem(ns,'subprocess',SimpleNamespace(run=unexpected))
    g['_network_baseline_systemctl']('reset-failed',g['NETWORK_BASELINE_UNIT_NAME'])
    assert checks==[{'require_active':False}]
