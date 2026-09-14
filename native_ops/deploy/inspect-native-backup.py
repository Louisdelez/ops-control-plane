import subprocess
r=subprocess.run(['docker','exec','zulip-standard-zulip-1','sh','-c',"sed -n '650,786p' /sbin/entrypoint.sh"],capture_output=True,text=True,check=True);print(r.stdout)
