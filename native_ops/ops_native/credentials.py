"""Consume systemd credentials without logging or weakening source permissions."""
import os,stat
from pathlib import Path

def read_credential(name):
    if not name or '/' in name or name in {'.','..'}:raise ValueError('invalid credential name')
    directory=Path(os.environ['CREDENTIALS_DIRECTORY'])
    if not str(directory).startswith('/run/credentials/'):raise ValueError('systemd credential directory required')
    fd=os.open(directory/name,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        s=os.fstat(fd);mode=stat.S_IMODE(s.st_mode)
        allowed=mode==0o400 or (mode==0o440 and (s.st_gid in {os.getgid(),*os.getgroups()} or (s.st_uid==0 and s.st_gid==0)))
        if not stat.S_ISREG(s.st_mode) or not allowed or s.st_uid not in {0,os.getuid()} or not 1<=s.st_size<=4096:
            raise ValueError('invalid systemd credential metadata')
        value=os.read(fd,4097).decode('ascii').strip()
    finally:os.close(fd)
    if not value or len(value)>4096 or any(c.isspace() for c in value):raise ValueError('invalid credential format')
    return value
