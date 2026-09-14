path "kv-minecraft/data/deploy/*" {
  capabilities = ["create", "read", "update", "patch"]
}

path "kv-minecraft/metadata/deploy/*" {
  capabilities = ["read", "list"]
}
