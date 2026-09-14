from pathlib import Path
import os,shutil,subprocess
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane/native_ops/deploy')
for source,destination,mode in [('backup-zulip-native.py','/usr/local/libexec/ops-zulip-backup.py',0o755),('ops-zulip-backup.service','/etc/systemd/system/ops-zulip-backup.service',0o644),('ops-zulip-backup.timer','/etc/systemd/system/ops-zulip-backup.timer',0o644)]:
 p=Path(destination)
 if p.is_symlink():raise RuntimeError('Symlink refused')
 shutil.copyfile(root/source,p);p.chmod(mode)
subprocess.run(['systemd-analyze','verify','/etc/systemd/system/ops-zulip-backup.service','/etc/systemd/system/ops-zulip-backup.timer'],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','ops-zulip-backup.timer'],check=True)
subprocess.run(['systemctl','start','ops-zulip-backup.service'],check=True)
print('ZULIP_BACKUP_TIMER_AND_SERVICE_VERIFIED')
