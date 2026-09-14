import os
from pathlib import Path
import runpy
import pytest

@pytest.mark.parametrize('variant', ['valid', 'extra', 'symlink', 'executable', 'wrong_version', 'oversized'])
def test_generated_metadata_is_bounded_and_not_executable(tmp_path, variant):
    ns = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/extract-hermes-source.py'))
    metadata = tmp_path / 'hermes_agent.egg-info'
    metadata.mkdir(mode=0o750)
    for name in ('PKG-INFO', 'dependency_links.txt', 'entry_points.txt', 'requires.txt', 'top_level.txt', 'SOURCES.txt'):
        (metadata / name).write_bytes(b'Name: hermes-agent\nVersion: 0.21.0\n' if name == 'PKG-INFO' else b'generated\n')
        (metadata / name).chmod(0o640)
    target = metadata / 'SOURCES.txt'
    if variant == 'extra':
        (metadata / 'unexpected.py').write_text('pass')
    elif variant == 'symlink':
        target.unlink(); target.symlink_to(metadata / 'PKG-INFO')
    elif variant == 'executable':
        target.chmod(0o750)
    elif variant == 'wrong_version':
        (metadata / 'PKG-INFO').write_text('Name: hermes-agent\nVersion: 99.0\n')
    elif variant == 'oversized':
        target.write_bytes(b'x' * (256 * 1024 + 1))
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if variant == 'valid':
            ns['verify_generated_egg_info'](fd, (os.getuid(), os.getgid()))
        else:
            with pytest.raises((ns['ArchiveError'], OSError)):
                ns['verify_generated_egg_info'](fd, (os.getuid(), os.getgid()))
    finally:
        os.close(fd)
