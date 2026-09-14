import json,sqlite3
from datetime import datetime,timezone
from ops_native.budget_status import snapshot

def test_uninitialized_ledger_is_not_reported_as_zero(tmp_path):
    assert snapshot(tmp_path/'missing-config',tmp_path/'missing-db')['reason']=='not_initialized'

def test_native_costs_include_unsettled_reservations_without_writing(tmp_path):
    config=tmp_path/'config.json';db=tmp_path/'budget.sqlite3'
    config.write_text(json.dumps({'roles':{'ROLE_FAST':[{'id':'native-qwen-flash','budget':{'daily_calls':100,'monthly_calls':1000,'daily_tokens':10000,'monthly_tokens':100000,'daily_cost_usd':'1','monthly_cost_usd':'20'}}]}}))
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE usage_reservations (provider_id,status,created_at,actual_input_tokens,actual_output_tokens,reserved_input_tokens,reserved_output_tokens,actual_cost_microusd,reserved_cost_microusd)')
        now=datetime.now(timezone.utc).isoformat()
        c.execute('INSERT INTO usage_reservations VALUES (?,?,?,?,?,?,?,?,?)',('native-qwen-flash','completed',now,20,10,100,100,30,200))
        c.execute('INSERT INTO usage_reservations VALUES (?,?,?,?,?,?,?,?,?)',('native-qwen-flash','uncertain',now,None,None,100,50,None,250))
    before=db.read_bytes();result=snapshot(config,db)
    assert result['available']
    assert result['providers'][0]['usage']['monthly']=={'calls':2,'tokens':180,'cost':280}
    assert result['providers'][0]['limits']['monthly']['cost_microusd']==20000000
    assert db.read_bytes()==before
