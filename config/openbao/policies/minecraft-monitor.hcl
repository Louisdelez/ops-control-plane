path "kv-minecraft/data/monitoring/*" {
  capabilities = ["read"]
}

path "kv-minecraft/metadata/monitoring/*" {
  capabilities = ["read", "list"]
}

path "kv-minecraft/data/hosts/*" {
  capabilities = ["read"]
}

path "kv-minecraft/metadata/hosts/*" {
  capabilities = ["read", "list"]
}
