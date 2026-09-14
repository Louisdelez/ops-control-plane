from pathlib import Path
import copy
import runpy
import stat
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('variant', ['observed', 'changed_enablement', 'wrong_receipt', 'unsafe_receipt'])
def test_thermal_stop_is_accounted_without_rewriting_history(tmp_path, monkeypatch, variant):
    ns = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'artifacts/repair-m/prepare-release-m.py'))
    fn = ns['verify_rollback_with_recorded_thermal_stop']
    g = fn.__globals__
    original = {'release_id': 'atlas-api-zulip-2026.09.09.16', 'local_inference_before': {'ollama.service': {'loaded': True, 'active': True, 'enabled': True}}}
    saved = copy.deepcopy(original)
    current = {'ollama.service': {'loaded': True, 'active': False, 'enabled': variant != 'changed_enablement'}}
    receipt = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFREG | (0o666 if variant == 'unsafe_receipt' else 0o600), st_uid=0), read_text=lambda: 'unexpected' if variant == 'wrong_receipt' else '2026-09-09T16:56:25+02:00 temperature_c=93.000 threshold_c=90\n')
    monkeypatch.setitem(g, 'Path', lambda _: receipt)
    monkeypatch.setitem(g, 'STATE', tmp_path)
    monkeypatch.setitem(g, 'command', lambda *args: None)
    verified = []
    prior = {'local_inference_snapshot': lambda: current, 'verify_terminal_rollback': lambda m: verified.append(m)}
    if variant == 'observed':
        fn(prior, original)
        assert verified[0]['local_inference_before'] == current
        assert (tmp_path / 'post-rollback-thermal-stop.json').exists()
    else:
        with pytest.raises(RuntimeError):
            fn(prior, original)
        assert not verified
    assert original == saved
