# API routing review — 2026-09-10

User decision: Qwen and DeepSeek, lowest capable price first; no local inference.

Qwen3.7 Flash: official model page distinguishes Singapore/International pricing
from Global pricing. The staged Singapore endpoint uses $0.030/M input and
$0.130/M output up to 32k input tokens, then the documented higher tiers.
Source: https://www.alibabacloud.com/help/en/model-studio/qwen3-7-flash

DeepSeek's current official page says `deepseek-flash` selects V4.1 Flash.
Old `deepseek-v4-flash` aliases now target that model and its current prices.
The staged reservation uses peak, non-cache prices $0.30/M input, $1.20/M output.
Actual off-peak/cache billing can be lower; conservative ledger accounting is
intentional until those discounts are independently validated.
Source: https://api-docs.deepseek.com/quick_start/pricing/
The official HTML was retrieved with curl after the browser tool timed out;
a copy is in the local review directory. No provider account has yet been
validated and no paid inference was performed in this implementation turn.

The deployment config enables exactly these two API candidates. Other required
router role slots remain disabled. There is no Ollama endpoint. The existing
router service is unchanged; this configuration is for the separate native pilot.

Proposed initial safety ceilings: $1/day and $20/month combined across both
provider accounts ($0.50/day and $10/month each), before taxes; $0.05 per provider
mission reservation and three CLI tool rounds maximum. These are staged defaults,
not evidence of a configured paid account. No unlimited fallback.

The existing multi-provider facade compares conservative request costs before
choosing a provider. Once an upstream request may have been sent, it does not
silently retry another provider after an ambiguous failure. The worker now permits one explicit needs_reasoning escalation to DeepSeek
thinking mode. Both rounds retain the same mission budget identity; tests cover
this HTTP scope, rejection of missing scope, and exhaustion of the single
escalation. Actual provider behavior still requires a bounded live API test.
