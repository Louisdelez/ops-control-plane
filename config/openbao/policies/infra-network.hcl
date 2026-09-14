path "kv-network-shared/data/automation/*" {
  capabilities = ["read"]
}

path "kv-network-shared/metadata/automation/*" {
  capabilities = ["read", "list"]
}
