#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Deploy Rastir to ECS Fargate
# ---------------------------------------------------------------------------
# Reads config.env, resolves secrets, and deploys the CloudFormation stack.
#
# Usage:
#   ./deploy.sh                 # uses ./config.env
#   ./deploy.sh my-config.env   # uses a custom config file
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${1:-${SCRIPT_DIR}/config.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config file not found: $CONFIG_FILE"
  echo "       Copy config.env.example → config.env and edit it first."
  exit 1
fi

# ── Parse config.env ──────────────────────────────────────────────────────

# Deployment parameters (consumed by this script / CloudFormation)
declare -A DEPLOY_PARAMS
DEPLOY_KEYS="AWS_REGION STACK_NAME VPC_ID SUBNET_IDS FARGATE_CPU FARGATE_MEMORY DESIRED_COUNT RASTIR_IMAGE AMP_REMOTE_WRITE_ENDPOINT AMP_REGION RASTIR_SERVER_PORT"

# Container environment variables and secrets
ENV_JSON="[]"
SECRETS_JSON="[]"

while IFS='=' read -r key value; do
  # Skip comments and blank lines
  [[ -z "$key" || "$key" =~ ^# ]] && continue

  # Strip inline comments and whitespace
  value="${value%%#*}"
  value="${value%"${value##*[![:space:]]}"}"

  # Check if this is a deployment parameter
  is_deploy=false
  for dk in $DEPLOY_KEYS; do
    if [[ "$key" == "$dk" ]]; then
      DEPLOY_PARAMS["$key"]="$value"
      is_deploy=true
      break
    fi
  done
  $is_deploy && continue

  # Determine value source
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
    # Plain value
    ENV_JSON=$(echo "$ENV_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
d.append({'name':'$key','value':'''$value'''})
json.dump(d,sys.stdout)")
  fi

done < <(grep -v '^\s*$' "$CONFIG_FILE" | grep -v '^\s*#')

# ── Defaults ──────────────────────────────────────────────────────────────
REGION="${DEPLOY_PARAMS[AWS_REGION]:-us-east-1}"
STACK="${DEPLOY_PARAMS[STACK_NAME]:-rastir-server}"
PORT="${DEPLOY_PARAMS[RASTIR_SERVER_PORT]:-8080}"

# Always inject RASTIR_SERVER_HOST and PORT into container env
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
