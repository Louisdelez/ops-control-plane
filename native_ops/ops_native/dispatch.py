"""Deterministic backend selection; mode changes never reassign accepted work."""
def select_backend(content, mode, available_tools=None):
    requested = content.split(maxsplit=1)[0] if content.strip() else ''
    explicit = {'!codex':'codex', '!claude':'claude', '!api':'api'}.get(requested)
    if explicit:
        if (explicit == 'api' and mode == 'cli') or (explicit != 'api' and mode == 'api'):
            return 'manual'
        return explicit
    if mode=='cli' and available_tools is not None:
        return next((tool for tool in ('codex','claude') if tool in available_tools),None)
    return 'codex' if mode == 'cli' else 'api' if mode in {'api','hybrid'} else 'manual'

PROJECTS=('infra-shared','minecraft','network-shared','monitoring-shared','backup-shared')

def select_project(topic):
    if topic.startswith('[') and ']' in topic:
        project=topic[1:topic.index(']')]
        if project not in PROJECTS:raise ValueError('Unknown Ops project')
        return project
    return 'infra-shared'


def available_cli(path=None):
    import json,stat,time
    from pathlib import Path
    path=Path('/run/ops-native-cli-accounts.json') if path is None else path
    try:
        metadata=path.lstat()
        age=time.time()-metadata.st_mtime
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=0 or metadata.st_mode&0o022 or not 0<=age<=180 or metadata.st_size>4096:return ()
        value=json.loads(path.read_text())
        return tuple(tool for tool in ('codex','claude') if value.get(tool) is True)
    except (OSError,ValueError,AttributeError):return ()
