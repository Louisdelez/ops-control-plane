# Production deployment assets

These files are consumed by `scripts/install-broker.sh`; they are not written
to the host merely by cloning this repository.

The production topology deliberately differs from the development TCP example:

```text
zulipbridge -> /run/ops-broker/api.sock (0660, opsbroker:opsbroker-api)
                    |
                    v
              ops-broker.service (UID opsbroker)
                    |
                    v
          /var/lib/ops-broker/state.db (0600)
```

Only `zulipbridge` is added to `opsbroker-api`. The service receives the socket
as systemd descriptor 3; Uvicorn never creates or chmods it. The socket unit is
disabled by default and must be enabled explicitly with `--enable-api` after the
authenticated bridge exists.

Production forces `OPS_BROKER_API_MODE=approvals-only` in both the managed
environment and the unit. The socket exposes health, the exact fixed-identity
approval publication/decision routes, and four narrow `zulip-mobile` mission
routes: create, redacted status, resume-if-paused and one bounded human-answer
record. Generic action creation,
execution, incidents, runbook listing, audit endpoints and API documentation do
not exist in that mode. The publication view is read-only,
limited to 50 non-expired pending class C records per page, and returns only a
UUID, scrubbed summary/impact/rollback, and deadline. The `zulip-mobile` RBAC
identity has project membership plus mission status/record writes only: no
runbook or action class. Thus a compromised `zulipbridge` process can at worst
enqueue a bounded mission, reopen a paused one, or add a bounded record; it
cannot create or execute an action.
Non-bridge identities use MCP stdio.

Codex does not join the API group and cannot read SQLite. User `ops-user` may run
one root-owned, argument-free wrapper as `opsbroker`; that wrapper clears the
environment and fixes `OPS_BROKER_MCP_ACTOR_ID=codex-supervised`. There is still
no MCP approval or secret tool.

The restart sudoers policy enumerates the three complete helper command lines.
The helper repeats the unit allowlist and uses `systemctl --no-block restart`
without a shell. `NoNewPrivileges` is intentionally absent only from the main
broker unit because it would prevent this exact sudo transition. Verification
and backup units use `NoNewPrivileges=yes` and an empty capability set.

The backup command uses SQLite's online backup API, converts the result to a
standalone rollback-journal database, runs `integrity_check`, verifies the audit
hash chain, fsyncs, and atomically renames it. Fourteen daily backups are kept in
`/var/backups/ops-broker`, mode `0700`; files are mode `0600`.

The verifier and backup source connections use SQLite `mode=ro` plus
`query_only`. Their units nevertheless allow the `opsbroker` identity to write
inside its own state directory because SQLite may need to maintain `-shm`/`-wal`
coordination files even for a logically read-only WAL snapshot. No other service
identity gains state-directory access.

On success or failure, a separate root-owned and heavily sandboxed oneshot
atomically writes `/var/lib/node-exporter/textfile/broker-backup.prom`. Running
the publisher as root preserves a root-owned, non-writable textfile directory;
the unit has an empty capability set, a read-only system image, one explicit
writable path, and cannot see the broker state or backups. It exposes only
`ops_broker_backup_last_success_timestamp_seconds` and
`ops_broker_backup_last_verify_ok`. Neither `node-exporter` nor `opsbroker` is
granted write access to the textfile directory.

The installer uses standard Fedora locations and runs `restorecon`; it never
uses persistent `chcon` overrides or generates a policy from observed denials.
The broker remains in the normal system-service SELinux domain until a reviewed
dedicated policy is supplied. Inspect AVCs rather than weakening enforcement.
