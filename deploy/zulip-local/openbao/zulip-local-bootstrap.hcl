# Bootstrap may inspect/replace only the exact bridge object. It cannot list or
# read any other application secret.
path "kv-infra-shared/data/zulip/bot" {
  capabilities = ["create", "read", "update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
