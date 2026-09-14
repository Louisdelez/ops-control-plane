# Exact runtime read for the single Unix identity hermesd. The helper emits
# only the local facade bearer token to multiplexed profiles and additionally
# emits API_SERVER_KEY to the default gateway profile. Vendor credentials are
# deliberately unavailable to Hermes.
path "kv-infra-shared/data/hermes/gateway" {
  capabilities = ["read"]
}

# The helper must revoke every short-lived login before emitting a value.  It
# deliberately has no lookup-self or renew-self capability.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
