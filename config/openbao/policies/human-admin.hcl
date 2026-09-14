# Broad operational administration, without raw storage, generate-root or rekey.
path "kv-minecraft/*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list"]
}

path "kv-infra-shared/*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list"]
}

path "kv-network-shared/*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list"]
}

path "kv-monitoring-shared/*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list"]
}

path "kv-backup-shared/*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list"]
}

# Maintain the reviewed policies and role-scoped AppRoles after the initial
# root token is revoked.  No capability is granted on sys/raw, rekey,
# generate-root, seal or unseal endpoints.
path "sys/policies/acl" {
  capabilities = ["read", "list"]
}

path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "auth/approle/role" {
  capabilities = ["read", "list"]
}

path "auth/approle/role/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "auth/approle/role/*/role-id" {
  capabilities = ["read"]
}

path "auth/approle/role/*/secret-id" {
  capabilities = ["create", "update"]
}

path "auth/approle/role/*/secret-id-accessor/lookup" {
  capabilities = ["update"]
}

path "auth/approle/role/*/secret-id-accessor/destroy" {
  capabilities = ["update"]
}

path "sys/mounts" {
  capabilities = ["read"]
}

path "sys/mounts/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "sys/auth" {
  capabilities = ["read"]
}

path "sys/audit" {
  capabilities = ["read"]
}

# Allow the named human administrator to rotate only their own password.
path "auth/userpass/users/ops-user/password" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
