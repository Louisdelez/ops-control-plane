# Native Ops integration

Additive integration through official Hermes CLI, Zulip REST and OpenBao Agent,
plus the existing broker and memory MCP servers. Official product source and
internal databases are not patched. The broker remains the mission authority;
connector SQLite stores intake/delivery state.

Installed version: 0.1.7 (verified wheel). A real Zulip request can be resumed via supervised MCP,
observed through an authorized runbook and answered in its original topic with
no Qwen/DeepSeek call. Codex and Claude use distinct fixed identities. See
[operating workflow](docs/PILOTAGE.md).

The approval bridge uses a dedicated bot and owner ID, private channels and
OpenBao Agent credentials. It accepts native Zulip reactions and explicit
mobile commands. A genuine human approval remains a separate acceptance test.
Ambiguous delivery and conflicting handoffs are not blindly replayed.

Provider model calls remain disabled by user decision. The model facade and
API Agent units are now installed but inactive; real Qwen/DeepSeek, embedding and reranking
acceptance waits until keys are added. Deterministic memory indexing is not a
local AI model. Phone VPN/TLS/push and broader project execution remain outside
the completed pilot tests.

Tests: `PYTHONPATH=native_ops:orchestrator/src broker/.venv/bin/python -m pytest -q native_ops/tests`.
The native bridge has its own existing tests under bridges/zulip/tests.
Proofs and remaining requirements are recorded in
`/home/ops-user/standard-install-review/AVANCEMENT-NATIF-2026-09-10.md`.

Native connector backups use SQLite online snapshots and isolated restore
checks. They complement, and do not replace, product-specific broker, memory,
OpenBao and Zulip recovery procedures.

Current delivery: [native consolidation](../docs/status.md). The earlier [audit](../docs/status.md) remains a dated snapshot. Runtime modules now match their installed wheel RECORD.

The continuous CLI pilot routes the five configured projects and follows its own approved actions without another model call. The API profile exposes four mission-scoped broker tools under hermes-native. The connector reports terminal action states and never treats a missing approval as authorization. Ambiguous execution or delivery requires review.
