"""Read-only native facade ledger projection. No prompts, keys or DB writes."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

CONFIG=Path('/etc/ops-native-model/config.json')
DATABASE=Path('/var/lib/ops-native-model/budget.sqlite3')

def snapshot(config_path=CONFIG, database_path=DATABASE):
    result={'available':False,'source':'native-facade','providers':[]}
    if not database_path.is_file():
        return {**result,'reason':'not_initialized'}
    config=json.loads(config_path.read_text())
    now=datetime.now(timezone.utc)
    with sqlite3.connect(database_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
        db.row_factory=sqlite3.Row
        for role,providers in config['roles'].items():
            for provider in providers:
                if not provider['id'].startswith('native-'):continue
                periods={}
                for period,pattern in [('daily',now.strftime('%Y-%m-%d%')),('monthly',now.strftime('%Y-%m%'))]:
                    row=db.execute('''SELECT COUNT(*) calls,
                        COALESCE(SUM(CASE WHEN status='completed' THEN actual_input_tokens+actual_output_tokens ELSE reserved_input_tokens+reserved_output_tokens END),0) tokens,
                        COALESCE(SUM(CASE WHEN status='completed' THEN actual_cost_microusd ELSE reserved_cost_microusd END),0) cost
                        FROM usage_reservations WHERE provider_id=? AND created_at LIKE ?''',(provider['id'],pattern)).fetchone()
                    periods[period]=dict(row)
                budget=provider['budget']
                limits={p:{'calls':budget[p+'_calls'],'tokens':budget[p+'_tokens'],
                    'cost_microusd':int(Decimal(budget[p+'_cost_usd'])*1000000)} for p in ['daily','monthly']}
                result['providers'].append({'provider':provider['id'],'role':role,'location':'remote','usage':periods,'limits':limits})
    return {**result,'available':True,'observed_at':now.isoformat()}

def main():
    try:result=snapshot()
    except (OSError,ValueError,KeyError,sqlite3.Error):result={'available':False,'source':'native-facade','reason':'unavailable','providers':[]}
    print(json.dumps(result))

if __name__=='__main__':main()
