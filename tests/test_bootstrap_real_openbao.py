from pathlib import Path
import runpy
import shutil
import pytest

@pytest.mark.skipif(shutil.which('bao') is None, reason='OpenBao binary is unavailable')
@pytest.mark.parametrize('local_ids,partial', [(False,False),(True,False),(False,True)])
def test_real_approle_migration_and_rollback(local_ids,partial):
    ns=runpy.run_path(str(Path(__file__).parent/'helpers/real_openbao.py'))
    ns['scenario'](local_ids,partial)
