"""Export only published action IDs and authoritative decisions; never credentials."""
import json,sqlite3,time,os
from pathlib import Path
BROKER='/var/lib/ops-broker/state.db'
BRIDGE='/var/lib/ops-native-approval/state.db'
TARGET=Path('/run/ops-approvals/state.json')
def export():
    with sqlite3.connect('file:'+BROKER+'?mode=ro',uri=True) as b, sqlite3.connect('file:'+BRIDGE+'?mode=ro',uri=True) as z:
        b.row_factory=sqlite3.Row
        publications=dict(z.execute("SELECT publication_key,message_id FROM publications WHERE kind='approval' AND status='published'"))
        items={}
        for row in b.execute("SELECT id,status,approval_deadline,created_at FROM actions WHERE action_class='C'"):
            if row['id'] not in publications:continue
            decision=b.execute('SELECT decision FROM approvals WHERE action_id=? ORDER BY created_at LIMIT 1',(row['id'],)).fetchone()
            items[str(publications[row['id']])]={'action_id':row['id'],'status':row['status'],'deadline':row['approval_deadline'],'created_at':row['created_at'],'decision':decision[0] if decision else None}
    target=TARGET.with_suffix('.next');target.write_text(json.dumps({'updated_at':time.time(),'items':items}));target.chmod(0o640);os.replace(target,TARGET)
if __name__=='__main__':
    while True:
        try:export()
        except Exception as error:print(type(error).__name__, str(error), flush=True) # no data values
        time.sleep(2)
