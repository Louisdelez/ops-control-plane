# Twenty-two exact KV v2 reads followed by mandatory revoke-self consume all 23
# uses of the ops-orchestrator-runtime token. Provider objects are optional;
# the Hermes facade token is mandatory. No enumeration, renewal or broad prefix
# capability is granted.
path "kv-infra-shared/data/llm/qwen" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/deepseek" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/z-ai" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/mistral-ai" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/minimax" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/google" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/cohere" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/moonshot" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/tencent" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/xai" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/openai" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/bytedance" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/anthropic" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/nvidia" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/aws" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/meta" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/microsoft" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/xiaomi" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/baidu" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/baichuan" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/llm/providers/stepfun" {
  capabilities = ["read"]
}

path "kv-infra-shared/data/hermes/gateway" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
