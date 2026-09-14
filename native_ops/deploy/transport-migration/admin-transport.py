"""Administrative transport used only inside approved deterministic runbooks."""
import os,pwd,stat,subprocess
from pathlib import Path
ALIASES={'edge-vps':'vps','prod':'prod','nas':'nas','gamebox':'gamebox'}
def command(resource,alternate=False):
 if os.geteuid()!=0 or resource not in ALIASES:raise ValueError('Invalid administrative transport')
 subprocess.run(['/usr/bin/systemctl','start','ops-remote-admin-secrets.service'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=40)
 key=Path('/run/ops-remote-admin/id_ed25519');meta=key.lstat();account=pwd.getpwnam('opsremoteadmin')
 if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=account.pw_uid or meta.st_mode&0o077:raise ValueError('Administrative identity unavailable')
 alias=ALIASES[resource];route=[]
 if alternate:
  if resource=='gamebox':alias='gamebox-remote'
  elif resource=='prod':route=['-J','vps','-o','HostName=198.51.100.4','-o','HostKeyAlias=192.0.2.65']
  else:raise ValueError('No reviewed alternate')
 return ['/usr/sbin/runuser','-u','opsremoteadmin','--','/usr/bin/env','-i','HOME=/var/lib/ops-remote-admin','USER=opsremoteadmin','LOGNAME=opsremoteadmin','PATH=/usr/bin:/bin','LANG=C','/usr/bin/ssh','-F','/etc/ops-remote-admin/ssh_config','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ClearAllForwardings=yes','-o','PermitLocalCommand=no','-o','RemoteCommand=none','-o','ConnectTimeout=8','-o','ConnectionAttempts=1','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=1',*route,alias,'/usr/bin/true']
