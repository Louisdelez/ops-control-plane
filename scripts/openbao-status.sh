#!/usr/bin/env bash
set -euo pipefail
export BAO_ADDR=${BAO_ADDR:-https://127.0.0.1:8200}
export BAO_CACERT=${BAO_CACERT:-/etc/pki/ca-trust/source/anchors/openbao-local.crt}
bao status -format=json | jq '{initialized,sealed,version,storage_type,ha_enabled}'
