# ops-broker V0

`ops-broker` is the small, fail-closed execution boundary between Hermes (or a
supervised MCP client) and operational runbooks. It stores missions, incidents,
actions, approvals, bounded-action budgets, and a hash-linked event journal in
SQLite.

This V0 is deliberately not a secret broker. There is no route or MCP tool for
reading a credential, token, SSH key, or OpenBao value. Future runbook helpers
may obtain short-lived credentials under their own operating-system identity;
the broker must never return those values.

## Security properties

- RBAC defaults to deny. Grants name an exact actor, project, runbook and action
  class. Mission/incident status writes use separate explicit lifecycle
  capabilities; a project read grant alone never grants them.
- Class A executes immediately after RBAC and parameter validation.
- Class B executes immediately only while its durable, namespaced budget has
  capacity. The budget is reserved before execution, so a failed attempt still
  counts. The supplied restart runbook permits one attempt per incident/service
  per hour.
- Class C is durable but cannot execute until a different, allowlisted human has
  approved it. Approvals expire and are idempotent by request UUID and source
  event ID.
- YAML runbooks provide an `argv` list. There is no shell, no command string,
  and no partial argument interpolation. Executables and sudo helpers must match
  exact absolute paths in `config/executables.yaml`.
- Subprocess output is bounded and redacted before persistence or response.
- Every state change plus action/approval authorization refusal is appended to
  a SHA-256 hash chain, atomically with the related durable state change.
- The HTTP launcher refuses non-loopback bind addresses. The MCP server uses
  local stdio.

The event chain is tamper-evident, not immutable: an attacker able to rewrite
both the database and an external reference can rebuild it. Periodically anchor
the reported head hash in a separate, append-only system before calling this a
compliance log.

## Layout and configuration

The default control-plane root is the parent of this directory:

```text
ops-control-plane/
  policies/actions.yaml       # global A/B/C policy and human approver IDs
  runbooks/**/*.yaml          # operational procedures
  broker/config/rbac.yaml     # exact actor grants
  broker/config/executables.yaml
  broker/src/ops_broker/
```

The shipped `policies/actions.yaml` intentionally has an empty
`authorized_actor_ids` list. Therefore approvals fail closed until real,
stable Zulip user IDs are added by the deployment/configuration owner. Do not
use display names or email addresses as approval identities.

Supported path overrides are:

```text
OPS_CONTROL_PLANE_ROOT
OPS_BROKER_DATABASE
OPS_BROKER_RUNBOOKS
OPS_BROKER_ACTION_POLICY
OPS_BROKER_RBAC_POLICY
OPS_BROKER_EXECUTABLES_POLICY
OPS_BROKER_MCP_ACTOR_ID       # fixed identity for one MCP server process
OPS_BROKER_FD                 # inherited systemd socket descriptor
OPS_BROKER_SOCKET             # direct Unix socket for development only
OPS_BROKER_API_MODE           # approvals-only (default) or explicit dev full
OPS_BROKER_HOST               # loopback only; default 127.0.0.1
OPS_BROKER_PORT               # default 8788
```

No environment variable accepts a secret.

## Development and verification

From this directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,mcp]'
.venv/bin/pytest
```

For a local smoke run, put the database somewhere writable and start the
launcher (do not invoke a generic `uvicorn` command with a public bind):

```bash
export OPS_BROKER_DATABASE="$PWD/.local-state/broker.db"
export OPS_BROKER_API_MODE=full
.venv/bin/ops-broker
```

The production default is `/var/lib/ops-broker/state.db`. Its directory should
belong only to the dedicated broker service account. A new database is created
with mode `0600`; its parent is created with mode `0700` when absent.

Verify a stopped or live database without changing its records:

```bash
.venv/bin/ops-broker-verify --database .local-state/broker.db
```

The verifier refuses a missing path instead of silently creating a database.

## Production installation on Fedora

The repository contains an idempotent host installer, but this working tree
does not execute it automatically:

```bash
cd /home/ops-user/ops-control-plane
sudo ./scripts/install-broker.sh
```

It creates a root-owned virtual environment under `/opt/ops-broker`, installs
root-owned helpers and exact sudoers entries, initializes private SQLite state,
and enables the audit-verification and coherent-backup timers. It does **not**
enable an HTTP/TCP listener or the Unix API socket. Once an authenticated Zulip
bridge is deployed, explicit activation is:

```bash
sudo ./scripts/install-broker.sh --enable-api
```

Production uses systemd socket activation at `/run/ops-broker/api.sock`, not
`127.0.0.1:8788`. The socket is `0660` inside a `0770` directory and its group
contains only `zulipbridge`. Production also forces `approvals-only`: health,
the fixed-identity bounded approval routes, and the narrow mobile mission
intake/status/resume/answer routes exist, with API schemas/docs disabled. The read returns only action ID,
scrubbed summary/impact/rollback, and deadline for non-expired pending class C
actions (at most 50 per page). A bridge process can enqueue a mission in a
granted project or reopen one paused, but it cannot create or execute an action
by forging an MCP actor ID. The
development TCP launcher and full route set require an explicit
`OPS_BROKER_API_MODE=full` and remain for isolated tests only.

The installer seeds `/etc/ops-broker/{actions,rbac,executables}.yaml` once and
preserves those operator-managed files on later runs. Updated safe defaults are
placed under `/opt/ops-broker/defaults`. No approver is added: class C continues
to fail closed until real authenticated Zulip IDs are deliberately configured.
After a schema/RBAC release, review and merge new fields from the defaults;
omitting lifecycle capabilities is safe and leaves every status write denied.

Codex supervised mode uses the argument-free root-owned wrapper:

```bash
sudo -u opsbroker -g opsbroker -- \
  /usr/local/libexec/ops-broker/ops-broker-mcp-codex
```

Only local user `ops-user` receives that exact sudo transition. Neither `ops-user`
nor Hermes receives filesystem access to `state.db` or its backups.

Useful deployment checks:

```bash
systemctl status ops-broker-audit-verify.timer ops-broker-backup.timer
systemctl list-timers 'ops-broker-*'
sudo -u opsbroker /opt/ops-broker/venv/bin/ops-broker-verify \
  --database /var/lib/ops-broker/state.db
sudo ausearch -m AVC -ts recent
```

See `deploy/README.md` for the trust boundaries and hardening trade-offs.

## HTTP API

All actor-scoped requests carry `X-Actor-ID`. Mutating request bodies also carry
`actor_id`, and the two values must match. Every creation has a caller-generated
UUID `request_id` for safe retry.

The production `approvals-only` UDS exposes exactly:

```text
GET  /healthz
GET  /v1/zulip/approvals/pending   # X-Actor-ID: zulip-publisher; limit 1..50
POST /v1/zulip/missions            # X-Actor-ID: zulip-mobile
GET  /v1/zulip/missions/{id}/status
POST /v1/zulip/missions/{id}/resume
POST /v1/zulip/missions/{id}/answers
POST /v1/actions/{id}/approvals
```

The following full API is a development interface only and requires explicit
`OPS_BROKER_API_MODE=full`:

```text
GET  /healthz
GET  /v1/audit/verify
GET  /v1/runbooks
POST /v1/missions
GET  /v1/missions/{id}
POST /v1/incidents
GET  /v1/incidents/{id}
POST /v1/actions
GET  /v1/actions/{id}
POST /v1/actions/{id}/execute
GET  /v1/approvals/pending
POST /v1/actions/{id}/approvals
```

Example class C request:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-ID: infra-network' \
  -d '{
    "actor_id":"infra-network",
    "request_id":"00000000-0000-4000-8000-000000000001",
    "runbook_id":"shared.change-request.v1",
    "parameters":{
      "summary":"Add the reviewed DNS record",
      "impact":"No interruption expected",
      "rollback":"Remove the new record"
    },
    "reason":"Approved change proposal"
  }' \
  http://127.0.0.1:8788/v1/actions
```

For the Zulip bridge, derive `actor_id` from the authenticated Zulip event and
inject the same value into the header and body. Never trust an actor ID supplied
by chat text. Store the immutable Zulip event ID as `source_event_id`; replaying
it is then harmless, while reuse for another action is rejected. A bridge must
also reject bot reactions and validate the target stream/topic/message before
calling the broker.

The header/body comparison is an identity-consistency check, not standalone
authentication. Production therefore combines a group-restricted Unix socket,
an isolated `zulipbridge` account and an approvals-only route set. Never expose
the full development API to the bridge or any TCP listener.

## MCP (official Python SDK)

The optional adapter uses the official `mcp` Python package and stdio transport:

```bash
export OPS_BROKER_DATABASE=/absolute/private/path/broker.db
export OPS_BROKER_MCP_ACTOR_ID=codex-supervised
.venv/bin/ops-broker-mcp
```

Configure Codex/Claude Code to launch that exact command with the required
non-secret path and actor variables. Each MCP process is bound to the configured
actor; a tool call claiming another identity is denied. The MCP adapter exposes
authorized runbook discovery, mission/incident creation and resumption, action
request/status/execution, and audit-chain verification. It intentionally exposes
no approval tool and no secret tool. Human approval remains a separate trusted
HTTP-bridge operation.

Incident and mission resumption uses these bounded tools:

```text
list_open_incidents(actor_id, project_id=None, limit=20)
list_open_missions(actor_id, project_id=None, limit=20)
get_incident(actor_id, incident_id)
get_mission(actor_id, mission_id)
list_actions_for_incident(actor_id, incident_id, limit=20)
list_actions_for_mission(actor_id, mission_id, limit=20)
set_incident_status(actor_id, request_id, incident_id, status)
set_mission_status(actor_id, request_id, mission_id, status)
```

Open-record lists accept only limits from 1 through 50, sort by `updated_at` descending and
return fixed public fields. Without `project_id`, they combine only projects in
the actor's RBAC grants; an explicit ungranted project is denied. “Open” means
non-terminal here: missions include `open` and `paused`, while incidents include
`open` and `monitoring`. Terminal states are `completed` and `resolved`.
Linked-action lists require access to the parent record's project, order by
most recent creation and return only bounded state metadata plus exit/timing
flags. They omit reasons, parameters, requesters and stdout/stderr; use the
single-action tool deliberately when deeper output is actually needed.
Lifecycle changes require both a project grant and the matching
`mission.status.write` or `incident.status.write` capability. Mission
`completed` and incident `resolved` are terminal and cannot be reopened in V0.
A same-status request is a durable no-op and does not move `updated_at`. Caller
UUIDs provide idempotency: a replay identifies itself with `replayed: true`,
preserves the original transition in `status`, and separately reports the
entity's present `current_status`. Changed and unchanged requests append a
corresponding event to the audit chain.
These tools are MCP-only in production; the approvals-only UDS route set is
unchanged.

## Operational limits of V0

- Populate real Zulip approver IDs before expecting class C to progress.
- Install the allowlisted helpers and narrow sudoers rules separately; this
  repository does not modify the host.
- Back up the SQLite database and its WAL coherently, and test restoration.
- Add external event-head anchoring before broader production rollout, and
  preserve a verified backup before every future schema migration.
- Add startup reconciliation for an action left in `running` if the broker host
  loses power after the helper exits but before its result is committed.
- Treat changes to policies, runbooks and executable allowlists as reviewed
  releases. Restart the broker after changing them; they load at process start.
