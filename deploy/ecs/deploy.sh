#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Deploy Rastir to ECS Fargate
# ---------------------------------------------------------------------------
# Reads config.json (preferred) or legacy config.env, resolves secrets, and
# deploys the CloudFormation stack.
#
# Usage:
#   ./deploy.sh                   # tries ./config.json then ./config.env
#   ./deploy.sh my-config.json    # JSON config
#   ./deploy.sh my-config.env     # legacy flat config
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Resolve config file ──────────────────────────────────────────────────
CONFIG_FILE="${1:-}"
if [[ -z "$CONFIG_FILE" ]]; then
  if [[ -f "${SCRIPT_DIR}/config.json" ]]; then
    CONFIG_FILE="${SCRIPT_DIR}/config.json"
  elif [[ -f "${SCRIPT_DIR}/config.env" ]]; then
    CONFIG_FILE="${SCRIPT_DIR}/config.env"
  else
    echo "ERROR: No config.json or config.env found in ${SCRIPT_DIR}"
    echo "       Copy config.json.example → config.json and edit it first."
    exit 1
  fi
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config file not found: $CONFIG_FILE"
  exit 1
fi

# Container environment variables and secrets
declare -A DEPLOY_PARAMS
ENV_JSON="[]"
SECRETS_JSON="[]"

# ── JSON config parser ───────────────────────────────────────────────────
# Flattens the "server" section into RASTIR_SERVER_<SECTION>_<KEY> env vars.
# Complex values (dicts/lists) become _JSON env vars instead of being
# flattened, so config.py can parse them via the _JSON env var path.
parse_json_config() {
  local config_file="$1"
  read -r ENV_JSON SECRETS_JSON <<< "$(python3 -c "
import json, sys

with open('$config_file') as f:
    cfg = json.load(f)

env_vars = []
secrets = []

# --- server section → RASTIR_SERVER_* env vars ---
server = cfg.get('server', {})
for section_name, section_val in server.items():
    section_upper = section_name.upper()
    if isinstance(section_val, dict):
        for key, val in section_val.items():
            key_upper = key.upper()
            if isinstance(val, (dict, list)):
                # Complex nested value → _JSON env var
                env_name = f'RASTIR_SERVER_{section_upper}_{key_upper}_JSON'
                env_vars.append({'name': env_name, 'value': json.dumps(val)})
            else:
                env_name = f'RASTIR_SERVER_{section_upper}_{key_upper}'
                env_vars.append({'name': env_name, 'value': str(val).lower() if isinstance(val, bool) else str(val)})
    else:
        # Top-level scalar (port, host)
        env_name = f'RASTIR_SERVER_{section_upper}'
        env_vars.append({'name': env_name, 'value': str(section_val).lower() if isinstance(section_val, bool) else str(section_val)})

# --- secrets section → ECS secrets ---
for name, ref in cfg.get('secrets', {}).items():
    if ref.startswith('secretsmanager:'):
        secrets.append({'name': name, 'valueFrom': ref[len('secretsmanager:'):]})
    elif ref.startswith('ssm:'):
        secrets.append({'name': name, 'valueFrom': ref[len('ssm:'):]})

print(json.dumps(env_vars), end=' ')
print(json.dumps(secrets))
")"

  # Extract deploy params
  local deploy_json
  deploy_json="$(python3 -c "
import json
with open('$config_file') as f:
    d = json.load(f).get('deploy', {})
for k, v in d.items():
    print(f'{k.upper()}={v}')
")"
  while IFS='=' read -r k v; do
    [[ -n "$k" ]] && DEPLOY_PARAMS["$k"]="$v"
  done <<< "$deploy_json"
}

# ── Legacy config.env parser ─────────────────────────────────────────────
parse_env_config() {
  local config_file="$1"
  local DEPLOY_KEYS="AWS_REGION STACK_NAME VPC_ID SUBNET_IDS FARGATE_CPU FARGATE_MEMORY DESIRED_COUNT RASTIR_IMAGE AMP_REMOTE_WRITE_ENDPOINT AMP_REGION RASTIR_SERVER_PORT"

  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    value="${value%%#*}"
    value="${value%"${value##*[![:space:]]}"}"

    is_deploy=false
    for dk in $DEPLOY_KEYS; do
      if [[ "$key" == "$dk" ]]; then
        DEPLOY_PARAMS["$key"]="$value"
        is_deploy=true
        break
      fi
    done
    $is_deploy && continue

    if [[ "$value" == secretsmanager:* ]]; then
      arn="${value#secretsmanager:}"
      SECRETS_JSON=$(echo "$SECRETS_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d.append({'name':'$key','valueFrom':'$arn'})
json.dump(d,sys.stdout)")
    elif [[ "$value" == ssm:* ]]; then
      arn="${value#ssm:}"
      SECRETS_JSON=$(echo "$SECRETS_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d.append({'name':'$key','valueFrom':'$arn'})
json.dump(d,sys.stdout)")
    elif [[ "$value" == env:* ]]; then
      var_name="${value#env:}"
      resolved="${!var_name:-}"
      if [[ -z "$resolved" ]]; then
        echo "WARNING: env:$var_name is not set in the environment — skipping $key"
        continue
      fi
      ENV_JSON=$(echo "$ENV_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d.append({'name':'$key','value':'$resolved'})
json.dump(d,sys.stdout)")
    else
      ENV_JSON=$(echo "$ENV_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d.append({'name':'$key','value':'''$value'''})
json.dump(d,sys.stdout)")
    fi
  done < <(grep -v '^\s*$' "$config_file" | grep -v '^\s*#')
}

# ── Parse ─────────────────────────────────────────────────────────────────
if [[ "$CONFIG_FILE" == *.json ]]; then
  echo "Reading JSON config: $CONFIG_FILE"
  parse_json_config "$CONFIG_FILE"
else
  echo "Reading legacy config.env: $CONFIG_FILE"
  parse_env_config "$CONFIG_FILE"
fi

# ── Defaults ──────────────────────────────────────────────────────────────
REGION="${DEPLOY_PARAMS[AWS_REGION]:-us-east-1}"
STACK="${DEPLOY_PARAMS[STACK_NAME]:-rastir-server}"
PORT="${DEPLOY_PARAMS[RASTIR_SERVER_PORT]:-8080}"

# Always inject RASTIR_SERVER_HOST into container env
ENV_JSON=$(echo "$ENV_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d.append({'name':'RASTIR_SERVER_HOST','value':'0.0.0.0'})
json.dump(d,sys.stdout)")

echo ""
echo "── Rastir ECS Deploy ──────────────────────────────────────────"
echo "  Stack:   $STACK"
echo "  Region:  $REGION"
echo "  Image:   ${DEPLOY_PARAMS[RASTIR_IMAGE]:-ghcr.io/skamalj/rastir-server:latest}"
echo "  CPU/Mem: ${DEPLOY_PARAMS[FARGATE_CPU]:-512} / ${DEPLOY_PARAMS[FARGATE_MEMORY]:-1024}"
echo "  Tasks:   ${DEPLOY_PARAMS[DESIRED_COUNT]:-1}"
echo "  AMP:     ${DEPLOY_PARAMS[AMP_REMOTE_WRITE_ENDPOINT]:-NOT SET}"
echo "  Env vars:  $(echo "$ENV_JSON" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
echo "  Secrets:   $(echo "$SECRETS_JSON" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
echo "───────────────────────────────────────────────────────────────"
echo ""

# ── Deploy ────────────────────────────────────────────────────────────────
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "${SCRIPT_DIR}/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    VpcId="${DEPLOY_PARAMS[VPC_ID]}" \
    SubnetIds="${DEPLOY_PARAMS[SUBNET_IDS]}" \
    RastirImage="${DEPLOY_PARAMS[RASTIR_IMAGE]:-ghcr.io/skamalj/rastir-server:latest}" \
    FargateCpu="${DEPLOY_PARAMS[FARGATE_CPU]:-512}" \
    FargateMemory="${DEPLOY_PARAMS[FARGATE_MEMORY]:-1024}" \
    DesiredCount="${DEPLOY_PARAMS[DESIRED_COUNT]:-1}" \
    AmpRemoteWriteEndpoint="${DEPLOY_PARAMS[AMP_REMOTE_WRITE_ENDPOINT]}" \
    AmpRegion="${DEPLOY_PARAMS[AMP_REGION]:-$REGION}" \
    RastirServerPort="$PORT" \
    RastirEnvJson="$ENV_JSON" \
    RastirSecretsJson="$SECRETS_JSON"

echo ""
echo "✓ Stack deployed: $STACK"
echo "  Port-forward:  ./port-forward.sh $STACK $PORT"
echo "  Internal URL:  http://rastir-server:$PORT"
