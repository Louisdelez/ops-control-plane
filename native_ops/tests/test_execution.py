import json
from ops_native.execution import execution_mode

def test_unknown_missing_and_corrupt_mode_fail_closed(tmp_path):
    p=tmp_path/'mode.json'
    assert execution_mode(p)=='cli'
    for value in ['{', 'null', '[]', '{"mode":"anything"}']:
        p.write_text(value)
        assert execution_mode(p)=='cli'

def test_explicit_modes_are_read_live(tmp_path):
    p=tmp_path/'mode.json'
    for mode in ['api','cli','hybrid','cli']:
        p.write_text(json.dumps({'mode':mode}))
        assert execution_mode(p)==mode
