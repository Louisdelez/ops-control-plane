# One exact KV v2 document contains the bot credential and all reviewed
# numeric/message scopes.  The launcher has no list or metadata capability.
path "kv-infra-shared/data/zulip/bot" {
  capabilities = ["read"]
}

# The launcher refuses to exec the bridge until this revocation succeeds.
# It deliberately cannot inspect or renew its short-lived token.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
