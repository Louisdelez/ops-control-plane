vault {
  address = "https://127.0.0.1:8200"
  ca_cert = "/etc/pki/ca-trust/source/anchors/openbao-local.crt"
}
auto_auth {
  method "approle" {
    mount_path = "auth/approle"
    config = {
      role_id_file_path = "/run/credentials/ops-native-secrets.service/ops-native-api-role-id"
      secret_id_file_path = "/run/credentials/ops-native-secrets.service/ops-native-api-secret-id"
      remove_secret_id_file_after_reading = false
    }
  }
}
template_config {
  exit_on_retry_failure = true
  static_secret_render_interval = "5m"
}
template {
  contents = "{{- with secret \"kv-infra-shared/data/llm/deepseek\" -}}{{ .Data.data.api_key }}{{- end -}}"
  destination = "/run/ops-native-model/deepseek"
  perms = "0600"
  backup = false
  create_dest_dirs = false
  error_on_missing_key = true
}
