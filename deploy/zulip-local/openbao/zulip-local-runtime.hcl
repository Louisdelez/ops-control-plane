# One render cycle: read the exact server object, then revoke the token.
path "kv-infra-shared/data/zulip/server" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
