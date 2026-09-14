# Ops Orchestrator

`ops-orchestrator` is the API-first model-routing boundary of the Dell
operations control plane. It keeps deterministic tools first, selects the
cheapest eligible provider within the required capability role, enforces
persistent per-provider, shared-account and per-request budgets, and records a
hash-linked model trace. Validated outcomes and a median of at least five
completed-call latencies feed a fully audited, combined ±15% value adjustment
inside the selected capability tier; urgency changes only the reviewed latency
target and its bounded weight. The raw catalogue is never rewritten. It never
executes an operations command.

Every accepted result for an important task (`HIGH`, `CRITICAL`, or impact at
least 0.75) must also pass an independent model check. The verifier is a remote
OpenAI-compatible provider with both a different provider account and a
different model family. It receives a bounded, explicitly untrusted executor
result, consumes the normal budgets/call/iteration limits, and emits an explicit
`agree|disagree|uncertain` decision. Disagreement is terminal; technical
failures may use a ranked alternative only while all global bounds still hold.
No available agreement means human escalation, never implicit validation.

The public v2 catalogue in `../catalog/model-catalog.v2.json` contains all 58
normalized cards from the supplied 2026 catalogue. It is exposed page by page
over the Unix API and MCP, with capability-first, cost-first route previews.
Catalogue presence never implies activation. A deterministic generator links a
card only when its exact model id, official-source allowlist, token prices,
provider endpoint, authentication scheme and request protocol all pass review.
It currently adds 9 API deployments to the 5 statically configured cards (14
invocable cards total, 16 catalogue-linked deployments). Every other card
exposes a precise fail-closed reason: unverified price/id/API contract, or
quarantine. The 7 original Qwen/DeepSeek deployments remain unchanged.
`qwen-coder-api` is separately and explicitly
declared auxiliary because its exact Flash identifier is absent from the seed;
the generic embedding and reranking backups are auxiliary for the same
non-catalogue-binding purpose.

The versioned API-first runtime configuration contains no Ollama provider. Its
generated layer currently covers four Z.ai GLM models, four OpenAI GPT models
through a bounded Responses adapter, and Kimi K3 through its dedicated request
dialect. Generic OpenAI-compatible providers receive only a minimal common
payload; Anthropic native parsing and authentication are separately bounded.
Its prioritized generative tiers use Qwen and DeepSeek APIs. `ROLE_LOCAL_OPS` tries Alibaba-hosted
DeepSeek before Qwen Plus and direct DeepSeek; the Alibaba-hosted path shares the
statically pinned Model Studio Global endpoint, activation flag, and Qwen key,
so it adds no third provider secret or hidden endpoint variable. `ROLE_REASONING`
uses DeepSeek V4 Pro; Qwen3.8 Max is isolated
as `ROLE_PREMIUM`, reached only after a cheaper capable tier fails, except that
critical risk may justify going there directly while still requiring human
approval. It is not part of `ROLE_CODER`. Endpoints and private key-file paths
are provided through the environment, and a remote call additionally requires
`remote_allowed=true`. Optional embedding and reranking providers are also
remote. Local hash/lexical memory remains deterministic and is not a model.

The daemon exposes HTTP/1.1 over a permission-restricted Unix socket only. Its
accepted sockets have a five-second I/O deadline and non-blocking admission to
a maximum of 16 request threads; the loopback Hermes facade uses the same
limits, including for blocked SSE writes. Direct balance reads for DeepSeek,
Moonshot and StepFun run in a stateless daemon under the dedicated
`opsfinance` UID. Its `0660` Unix socket lives in a `0750 opsfinance:opsfinance`
directory and accepts only the expected `opsorchestrator` peer UID. The main
daemon proxies only an authorized account identifier, revalidates every
normalized field, and is the sole writer of snapshots to SQLite; it never
receives the Moonshot or StepFun keys. The Hermes facade deliberately shares
the main daemon's Qwen/DeepSeek ledger trust domain, but its systemd mount
namespace makes the finance socket inaccessible.
The main daemon receives exact per-account credential files. Every provider
credential, including Qwen and DeepSeek, has an empty systemd fallback so a
zero-key installation can start locally; the provider secret reader rejects an
empty fallback as unavailable and cannot issue a provider request. Credential
values never enter configuration, catalogue or WebView.
See `../docs/orchestrator-v1.md` for the protocol, activation procedure and
security invariants.
