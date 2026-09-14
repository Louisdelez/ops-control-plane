import json
from decimal import Decimal
from pathlib import Path

CONFIG=Path(__file__).resolve().parents[1]/'deploy/model-config.json'

def test_api_only_and_combined_budget_ceiling():
    c=json.loads(CONFIG.read_text())
    providers=[p for role in c['roles'].values() for p in role]
    assert all(p['location']=='remote' for p in providers)
    assert {p['model'] for p in providers if p['enabled']}=={'deepseek-flash'}
    assert sum(Decimal(a['budget']['daily_cost_usd']) for a in c['provider_accounts'])==Decimal('1')
    assert sum(Decimal(a['budget']['monthly_cost_usd']) for a in c['provider_accounts'])==Decimal('20')

def test_prices_match_selected_endpoint_scope_and_peak_reservation():
    c=json.loads(CONFIG.read_text())
    q=c['roles']['ROLE_TINY'][0]
    assert q['base_url']=='https://dashscope-intl.aliyuncs.com/compatible-mode/v1'
    assert q['price_tiers'][0]['input_price_per_million_usd']=='0.030000'
    assert q['price_tiers'][0]['output_price_per_million_usd']=='0.130000'
    d=c['roles']['ROLE_LOCAL_OPS'][0]
    assert d['model']=='deepseek-flash'
    assert d['input_price_per_million_usd']=='0.300000'
    assert d['output_price_per_million_usd']=='1.200000'


def test_default_requires_deepseek_only():
    root=CONFIG.parent
    assert "KEYS=(Path('/run/ops-native-model/deepseek'),)" in (root/'ops-execution-mode').read_text()
    agent=(root/'agent.hcl').read_text()
    assert '/llm/deepseek' in agent and '/llm/qwen' not in agent
    unit=(root/'ops-native-model.service').read_text()
    assert 'ENABLE_DEEPSEEK=1' in unit and 'ENABLE_QWEN=0' in unit
    proxy=(root.parent/'ops_native/model_proxy.py').read_text()
    assert "provider_ids=['native-deepseek-flash']" in proxy
