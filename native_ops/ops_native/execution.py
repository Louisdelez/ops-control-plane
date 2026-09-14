"""Live, fail-closed selection of model execution paths. Contains no credentials."""
import json
from pathlib import Path
MODE_FILE=Path('/etc/ops-execution-mode.json')
def execution_mode(path=MODE_FILE):
    try:
        data=json.loads(path.read_text())
        return data['mode'] if isinstance(data,dict) and data.get('mode') in {'cli','api','hybrid'} else 'cli'
    except (OSError,ValueError,TypeError):return 'cli'
def api_allowed():return execution_mode() in {'api','hybrid'}
