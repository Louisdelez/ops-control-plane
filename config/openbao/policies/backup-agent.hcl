path "kv-backup-shared/data/jobs/*" {
  capabilities = ["read"]
}

path "kv-backup-shared/metadata/jobs/*" {
  capabilities = ["read", "list"]
}

path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}
