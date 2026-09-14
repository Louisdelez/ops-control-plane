# Zulip Ops bridge V1

This directory contains an offline-testable, fail-closed bridge for four
bounded workflows:

- validate human `✅`/`❌` reactions and submit the decision to the local Ops
  broker;
- publish non-expired class C approval requests and Alertmanager
  firing/resolution updates as short Zulip messages;
- publish the short deterministic `daily/latest.json` report once per content
  hash.
- accept explicit mobile mission/status/report commands from freshly verified
  allowlisted humans in the configured Ops stream.

A checkout changes nothing on the host. The installer copies a root-owned
runtime and units, then leaves the bridge and its private Alertmanager query
socket disabled and inactive. It never creates a populated credential file.

## Trust boundaries

An event becomes an approval only when all of these checks succeed:

1. `ZULIP_REALM_URL` is one HTTPS origin and the credential resolves to the
   exact active numeric `ZULIP_BOT_USER_ID`.
2. Zulip resolves every configured stream name to its configured numeric ID.
3. The event is an `add` of the official Unicode `✅` (`check`, `2705`) or `❌`
   (`cross_mark`, `274c`), never a realm emoji with a similar name.
4. Its numeric `user_id` is allowlisted. A fresh user lookup must return that
   same ID as active, non-bot and non-stub. Names, email and message text are
   never identity.
5. The fetched message ID, bot sender ID, stream name/ID and topic all match
   configuration. Its content contains exactly one
   `OPS-APPROVAL-V1:<action-uuid-v4>` marker.
6. The decision goes only through `/run/ops-broker/api.sock`; the broker again
   checks its human actor allowlist, separation of requester/approver, class,
   deadline and status.

The broker production UDS remains narrow. It exposes health, approval
POST/publication, and four mobile mission routes to fixed
`X-Actor-ID: zulip-mobile`: create, redacted status, resume-if-paused and a
bounded attributed answer record. It
exposes `GET /v1/zulip/approvals/pending` to the fixed
`X-Actor-ID: zulip-publisher`. That read is ordered and limited to 1–50 records
per page. It returns only action UUID, scrubbed summary/impact/rollback and
deadline for non-expired `class=C,status=pending_approval` rows. It exposes no
reason, requester, arbitrary parameters, argv, result, generic action creation
or execution route. The `zulip-mobile` RBAC role has no runbook or action class.

## Commandes mobiles V1

The same queue accepts only commands starting with the literal `!ops`, in any
topic of the configured approval stream. The event message is fetched again;
its numeric sender and stream identity must match, and the sender must resolve
freshly as an active non-bot member of `ZULIP_APPROVER_USER_IDS`. Display names,
emails and text never establish identity.

```text
!ops mission <project-id> <titre>
!ops statut <mission-uuid>
!ops reprendre <mission-uuid>
!ops répondre <mission-uuid> <texte>
!ops rapport
!ops capture <mission-uuid>
```

Creation and resume use deterministic UUID request IDs derived from the realm,
queue and immutable event ID. Replies have a durable publication intent and a
unique marker, so a crash or ambiguous Zulip POST is reconciled before retry.
`répondre` adds one bounded, attributed human decision record to the mission;
it cannot execute any action. Status replies expose only mission ID, project,
title, high-level state, queue
state and update time; free-form context, paths, resources, model traces and
action parameters never cross the bridge.

`!ops rapport` uploads only the protected, at-most-64-KiB
`/var/lib/ops-reports/daily/latest.json`. `!ops capture` maps the canonical
mission UUID to the single fixed path
`/var/lib/ops-reports/captures/<uuid>.png`; it rejects symlinks, writable files,
non-PNG data and files over 5 MiB. It does not run a screenshot command. Another
reviewed deterministic workflow must create that capture first.

Both human allowlists must agree: Zulip ID `42` is `42` in
`ZULIP_APPROVER_USER_IDS` and `zulip:42` in the broker action policy. Missing
actor IDs, credentials or any stream name/ID/topic stops activation or startup.

## Producers and durable idempotence

Before every Zulip POST, SQLite stores an intent keyed by the immutable action
ID, alert fingerprint/episode, or report SHA-256. The row is marked published
only after Zulip returns a numeric message ID. After an ambiguous timeout or
crash, the bridge searches the exact configured stream/topic for its canonical
marker, revalidates bot sender and numeric stream IDs, and only sends when no
accepted prior message exists. Credentials and full message bodies are never
stored.

Approval text, Alertmanager labels/annotations and daily summary text are
normalized, stripped of control characters, bounded and neutralized for Zulip
mentions/marker injection. Long reports stay outside chat.

Alertmanager is polled read-only via its v2 API. The bridge connects only to
`/run/zulip-alertmanager-query/api.sock` (mode `0660`, group `zulipbridge`). A
separate hardened `systemd-socket-proxyd` forwards that UDS to the already-local
Alertmanager endpoint at `127.0.0.1:9093`; it is restricted to localhost. The
bridge has no HTTP server, webhook endpoint or TCP listener. A successful,
fully validated snapshot may create firing messages and resolve alerts missing
from the next snapshot. A timeout or malformed snapshot never causes a false
resolution. Alert identity is SHA-256 over canonical labels plus the start time
for the episode.

The daily producer reads only `/var/lib/ops-reports/daily/latest.json`. It uses
`O_NOFOLLOW`, rejects writable/non-regular or over-64-KiB files, accepts only a
timezone-aware recent generation time and a summary of at most 500 characters,
and publishes no detail fields. `zulipbridge` receives only the `ops-readers`
group needed for this read.

## Credentials and configuration

The bridge never reads `zuliprc`, YAML secrets or a checked-in credential
file. The service receives a RoleID and SecretID only through systemd
`LoadCredentialEncrypted=` blobs sealed with `host+tpm2`. The root-owned
`zulip-openbao-launcher` runs as the unprivileged `zulipbridge` identity. It
logs in only against `https://127.0.0.1:8200`, reads exactly
`kv-infra-shared/zulip/bot`, validates the complete object, revokes its token,
then uses `execve` with a newly constructed allowlisted environment. No
plaintext dotenv file is created; neither the OpenBao token nor AppRole
material reaches the bridge process.

The `zulip-bridge` AppRole is loopback-bound, has no default policy, allows one
KV read plus `revoke-self`, and issues 60-second service tokens limited to two
uses. Its SecretID is limited to 1024 logins and 30 days. The encrypted
SecretID blob's mtime is the local issuance/rotation reference used for
25-day warning and 29-day critical monitoring; an earlier use-count exhaustion
still fails closed. Rotate it with the same provisioner before day 30.

The one KV object uses lower-case field names. Required fields are:

```text
realm_url, bot_email, api_key, bot_user_id, approver_user_ids
approval_stream, approval_stream_id, approval_topic
alert_stream, alert_stream_id, alert_topic
daily_stream, daily_stream_id, daily_topic
```

Only these optional fields are accepted: `ca_bundle`,
`poll_timeout_seconds`, `retry_max_seconds`, `producer_poll_seconds`, and
`pending_page_size`. An unknown, missing, duplicated-user, out-of-range or
control-character-bearing value blocks launch. `config/example.env` is a
non-secret schema aid only; never populate or version a copy.

Required explicit scopes are:

```text
ZULIP_APPROVER_USER_IDS
ZULIP_APPROVAL_STREAM / ZULIP_APPROVAL_STREAM_ID / ZULIP_APPROVAL_TOPIC
ZULIP_ALERT_STREAM / ZULIP_ALERT_STREAM_ID / ZULIP_ALERT_TOPIC
ZULIP_DAILY_STREAM / ZULIP_DAILY_STREAM_ID / ZULIP_DAILY_TOPIC
```

Socket, state and report paths are pinned in the reviewed unit. Optional poll,
retry, page-size and public CA settings are documented in `config/example.env`.

## Offline verification and installation

Tests use in-process HTTP transports and temporary SQLite databases. They make
no network request and require no secret:

```bash
python3 -m venv .venv
.venv/bin/pip install --requirement requirements-dev.txt
.venv/bin/pytest
bash -n ../../scripts/install-zulip-bridge.sh
systemd-analyze verify systemd/zulip-approval-bridge.service \
  systemd/zulip-alertmanager-query.service \
  systemd/zulip-alertmanager-query.socket
```

The idempotent host installer installs but deliberately stops/disables all
bridge units:

```bash
sudo ./scripts/install-zulip-bridge.sh
sudo ./scripts/install-zulip-bridge.sh --check
```

Explicit activation is refused unless the OpenBao environment is complete,
the three encrypted AppRole blobs are root-owned, decryptable and younger than
30 days, the fixed broker socket is active and writable by `zulipbridge`,
Alertmanager is active, the socket proxy exists, and both supplementary groups
are present. The installer never creates those blobs. After the single Zulip
KV object has been entered through a non-logged human OpenBao session, issue
and encrypt the runtime AppRole without starting anything:

```bash
sudo ./scripts/install-zulip-bridge.sh
sudo /usr/local/sbin/provision-zulip-openbao
```

The provisioner requires the exact pre-existing KV schema and reviewed
AppRole, confirms one login/read/revoke cycle, encrypts RoleID, SecretID and
its accessor with `systemd-creds --with-key=host+tpm2`, revokes the previous
accessor during rotation, and displays no credential. It does not initialize,
unseal or write application secrets to OpenBao and leaves all units disabled.

Only after reviewing the numeric user/stream IDs, the broker actor allowlist,
and the activation preflight:

```bash
sudo ./scripts/install-broker.sh --enable-api
sudo ./scripts/install-zulip-bridge.sh --check
sudo ./scripts/install-zulip-bridge.sh --activate
```

## Limites V1

- Alert delivery is bounded polling, not an Alertmanager webhook. This avoids
  adding an unauthenticated TCP receiver; detection remains Prometheus/
  Alertmanager's job, not the LLM or bridge.
- Reactions are the approval UI; richer buttons and interactive cards are not
  implemented.
- The bridge publishes short summaries and only the two bounded attachment
  classes above. It does not expose full broker actions, generate monitoring
  conclusions, take screenshots itself, or change Alertmanager state.
- It does not provision a Zulip organization/bot, initialize or unseal
  OpenBao, populate the Zulip KV object, start the broker/monitoring stack, or
  activate itself.
