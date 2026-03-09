#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Deploy Rastir Prometheus to ECS Fargate with EFS storage
# ---------------------------------------------------------------------------
# Reads config.json and deploys the CloudFormation stack.
#
# Usage:
#   ./deploy.sh                   # uses ./config.json
#   ./deploy.sh my-config.json    # custom config
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Resolve config file ──────────────────────────────────────────────────
CONFIG_FILE="${1:-}"
if [[ -z "$CONFIG_FILE" ]]; then
  if [[ -f "${SCRIPT_DIR}/config.json" ]]; then
    CONFIG_FILE="${SCRIPT_DIR}/config.json"
  else
    echo "ERROR: No config.json found in ${SCRIPT_DIR}"
    echo "       Copy config.json.example → config.json and edit it first."
    exit 1
  fi
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "ERROR: Config file not found: $CONFIG_FILE"
  exit 1
fi

# ── Parse config ─────────────────────────────────────────────────────────
get_val() {
  python3 -c "
import json, sys
with open('$CONFIG_FILE') as f:
    d = json.load(f).get('deploy', {})
print(d.get('$1', '${2:-}'))
"
}

REGION="$(get_val aws_region us-east-1)"
STACK="$(get_val stack_name rastir-prometheus)"
VPC_ID="$(get_val vpc_id)"
VPC_CIDR="$(get_val vpc_cidr 172.31.0.0/16)"
SUBNET_IDS="$(get_val subnet_ids)"
ASSIGN_PUBLIC_IP="$(get_val assign_public_ip DISABLED)"
FARGATE_CPU="$(get_val fargate_cpu 512)"
FARGATE_MEMORY="$(get_val fargate_memory 2048)"
DESIRED_COUNT="$(get_val desired_count 1)"
PROMETHEUS_IMAGE="$(get_val prometheus_image prom/prometheus:latest)"
PROMETHEUS_PORT="$(get_val prometheus_port 9090)"
RASTIR_CLUSTER="$(get_val rastir_cluster_name rastir-server)"
RASTIR_NS_ARN="$(get_val rastir_service_connect_namespace_arn)"
RASTIR_ENDPOINT="$(get_val rastir_server_endpoint rastir-server:8080)"
S3_CONFIG_URI="$(get_val s3_config_uri)"

echo ""
echo "── Rastir Prometheus Deploy ───────────────────────────────────"
echo "  Stack:       $STACK"
echo "  Region:      $REGION"
echo "  Image:       $PROMETHEUS_IMAGE"
echo "  CPU/Mem:     $FARGATE_CPU / $FARGATE_MEMORY"
echo "  Tasks:       $DESIRED_COUNT"
echo "  ECS Cluster: $RASTIR_CLUSTER"
echo "  S3 Config:   ${S3_CONFIG_URI:-<none — using image defaults>}"
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
    VpcId="$VPC_ID" \
    VpcCidr="$VPC_CIDR" \
    SubnetIds="$SUBNET_IDS" \
    AssignPublicIp="$ASSIGN_PUBLIC_IP" \
    FargateCpu="$FARGATE_CPU" \
    FargateMemory="$FARGATE_MEMORY" \
    DesiredCount="$DESIRED_COUNT" \
    RastirClusterName="$RASTIR_CLUSTER" \
    RastirServiceConnectNamespaceArn="$RASTIR_NS_ARN" \
    RastirServerEndpoint="$RASTIR_ENDPOINT" \
    PrometheusPort="$PROMETHEUS_PORT" \
    PrometheusImage="$PROMETHEUS_IMAGE" \
    S3ConfigUri="$S3_CONFIG_URI"

echo ""
echo "✓ Stack deployed: $STACK"
echo "  Port-forward:  ./port-forward.sh $RASTIR_CLUSTER prometheus $PROMETHEUS_PORT"
echo "  Internal URL:  http://prometheus:$PROMETHEUS_PORT"
