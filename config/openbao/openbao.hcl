ui = false

api_addr     = "https://127.0.0.1:8200"
cluster_addr = "https://127.0.0.1:8201"

storage "raft" {
  path    = "/var/lib/openbao/raft"
  node_id = "dell-ops-01"
}

listener "tcp" {
  address         = "127.0.0.1:8200"
  cluster_address = "127.0.0.1:8201"

  tls_cert_file   = "/etc/openbao.d/tls/server.crt"
  tls_key_file    = "/run/credentials/openbao.service/tls.key"
  tls_min_version = "tls12"
  tls_max_version = "tls13"

  disable_unauthed_rekey_endpoints         = true
  disable_unauthed_generate_root_endpoints = true
}

telemetry {
  prometheus_retention_time = "30s"
  disable_hostname          = true
}

# OpenBao 2.4+ manages audit devices declaratively.  This guarantees that
# auditing is present before any authenticated bootstrap operation and cannot
# be removed through the API.
audit "file" "file" {
  description = "Dell Ops tamper-evident API audit trail"
  options {
    file_path    = "/var/log/openbao/audit.json"
    format       = "json"
    log_raw      = "false"
    hmac_accessor = "true"
    mode         = "0600"
  }
}
