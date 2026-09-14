"""Add one exact SNI route, preserving existing sites; archive and roll back on failure."""
from pathlib import Path
import subprocess,shutil,hashlib,json,time
BASE=Path('/var/lib/ops-approvals-publication');HAP=Path('/etc/haproxy/haproxy.cfg')
DOMAIN='autorisations.example.org'
def run(cmd,timeout=45):
 r=subprocess.run(cmd,capture_output=True,timeout=timeout)
 if r.returncode:raise RuntimeError('Command failed: '+cmd[0])
 return r.stdout
assert not BASE.exists(),'Existing publication requires review'
BASE.mkdir(mode=0o700)
shutil.copy2(HAP,BASE/'haproxy.before')
http=Path('/etc/nginx/conf.d/ops-approvals-http.conf');https=Path('/etc/nginx/conf.d/ops-approvals-https.conf')
assert not http.exists() and not https.exists()
webroot=Path('/var/lib/ops-approvals-acme');webroot.mkdir(mode=0o755)
try:
 http.write_text('''server {
 listen 80;
 server_name autorisations.example.org;
 location /.well-known/acme-challenge/ { root /var/lib/ops-approvals-acme; }
 location / { return 301 https://autorisations.example.org$request_uri; }
}
''')
 run(['nginx','-t']);run(['systemctl','reload','nginx'])
 run(['certbot','certonly','--non-interactive','--webroot','-w',str(webroot),'--cert-name','ops-autorisations-delez-ovh','-d',DOMAIN],timeout=150)
 https.write_text('''server {
 listen 127.0.0.1:8547 ssl proxy_protocol;
 server_name autorisations.example.org;
 ssl_certificate /etc/letsencrypt/live/ops-autorisations-delez-ovh/fullchain.pem;
 ssl_certificate_key /etc/letsencrypt/live/ops-autorisations-delez-ovh/privkey.pem;
 ssl_protocols TLSv1.2 TLSv1.3;
 client_max_body_size 8k;
 add_header Strict-Transport-Security "max-age=31536000" always;
 location / {
   proxy_pass http://127.0.0.1:19128;
   proxy_set_header Host autorisations.example.org;
   proxy_set_header X-Forwarded-Proto https;
   proxy_set_header X-Forwarded-For $proxy_protocol_addr;
   proxy_read_timeout 45s;
   proxy_http_version 1.1;
   proxy_set_header Connection "";
 }
}
''')
 run(['nginx','-t']);run(['systemctl','reload','nginx'])
 text=HAP.read_text();assert text== (BASE/'haproxy.before').read_text()
 assert 'ops_approvals' not in text
 lines=text.splitlines(keepends=True);indices=[i for i,l in enumerate(lines) if l.strip()=='default_backend nas'];assert len(indices)==1
 lines.insert(indices[0],'    acl h_ops_approvals req.ssl_sni -i autorisations.example.org\n    use_backend ops_approvals if h_ops_approvals\n')
 staged=HAP.with_suffix('.ops-approvals-next');staged.write_text(''.join(lines)+'\nbackend ops_approvals\n    mode tcp\n    server approvals 127.0.0.1:8547 send-proxy-v2\n')
 run(['haproxy','-c','-f',str(staged)]);staged.replace(HAP);run(['systemctl','reload','haproxy'])
 run(['curl','--fail','--silent','--max-time','15','https://'+DOMAIN+'/healthz'])
 (BASE/'state.json').write_text(json.dumps({'status':'published','domain':DOMAIN,'at':time.time(),'haproxy_sha256':hashlib.sha256(HAP.read_bytes()).hexdigest()}))
 print('PUBLISHED_AND_HEALTHY')
except Exception:
 shutil.copy2(BASE/'haproxy.before',HAP)
 http.unlink(missing_ok=True);https.unlink(missing_ok=True)
 run(['nginx','-t']);run(['systemctl','reload','nginx']);run(['haproxy','-c','-f',str(HAP)]);run(['systemctl','reload','haproxy'])
 (BASE/'state.json').write_text(json.dumps({'status':'rolled_back'}))
 raise
