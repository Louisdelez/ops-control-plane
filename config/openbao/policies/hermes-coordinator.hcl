# Quarantine policy retained only to neutralize already-issued legacy tokens.
# Atlas may retain quarantined AppRoles for rollback; this grants no secret read,
# token lookup or renewal capability.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
