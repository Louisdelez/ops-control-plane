# The memory secret resolver performs one exact KV v2 read, then must revoke
# its two-use token before it can publish the Qdrant key into /run.
path "kv-infra-shared/data/memory/qdrant" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
