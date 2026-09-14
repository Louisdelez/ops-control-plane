# The automated backup identity may only download one encrypted Raft snapshot
# and revoke its own short-lived token.  No write endpoint is granted.
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
