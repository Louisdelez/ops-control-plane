from pathlib import Path
import pwd,json
for path in ['/etc/credstore.encrypted/ops-native-facade-token','/etc/credstore.encrypted/ops-native-api-role-id','/etc/credstore.encrypted/ops-native-api-secret-id','/etc/ops-native-model/config.json','/etc/ops-native-model/agent.hcl']:
 print(path,Path(path).exists())
try:print('SERVICE_USER',pwd.getpwnam('opsnativeai').pw_uid)
except KeyError:print('SERVICE_USER_MISSING')
c=json.loads(Path('/etc/ops-native/config.json').read_text());print('GENERATION',c.get('generation_enabled'))
