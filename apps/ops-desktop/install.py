#!/usr/bin/python3
"""Install the built application and desktop entry for the workstation owner."""
from pathlib import Path
import os,shutil,subprocess,hashlib
root=Path(__file__).resolve().parent
binary=root/'src-tauri/target/release/ops-desktop'
if not binary.is_file():raise SystemExit('Build the release binary first.')
base=Path.home()/'.local'
lib=base/'lib/ops-desktop';lib.mkdir(parents=True,exist_ok=True)
target=lib/'ops-desktop'
if target.is_file() and target.read_bytes()!=binary.read_bytes():
    archive=lib/'archive';archive.mkdir(exist_ok=True)
    digest=hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    saved=archive/('ops-desktop-'+digest)
    if not saved.exists():shutil.copy2(target,saved)
shutil.copyfile(binary,lib/'ops-desktop.next');(lib/'ops-desktop.next').chmod(0o755)
os.replace(lib/'ops-desktop.next',target)
bin_dir=base/'bin';bin_dir.mkdir(parents=True,exist_ok=True)
launcher=bin_dir/'ops-desktop'
launcher.write_text('#!/usr/bin/env bash\nset -e\nsystemctl --user start hermes-dashboard.service\nexec "'+str(target)+'" "$@"\n');launcher.chmod(0o755)
icons=base/'share/icons/hicolor/128x128/apps';icons.mkdir(parents=True,exist_ok=True)
shutil.copyfile(root/'src-tauri/icons/icon.png',icons/'ch.ops-user.ops-desktop.png')
apps=base/'share/applications';apps.mkdir(parents=True,exist_ok=True)
entry=apps/'ch.ops-user.ops-desktop.desktop'
entry.write_text('[Desktop Entry]\nType=Application\nName=Ops\nComment=Atlas, Zulip, OpenBao et Hermes\nExec='+str(launcher)+'\nIcon=ch.ops-user.ops-desktop\nTerminal=false\nCategories=Utility;\nKeywords=Atlas;Zulip;OpenBao;Hermes;Ops;\nStartupNotify=true\nStartupWMClass=ops-desktop\n')
entry.chmod(0o644)
if shutil.which('update-desktop-database'):subprocess.run(['update-desktop-database',str(apps)],check=True)
print('Application installed:',entry)
