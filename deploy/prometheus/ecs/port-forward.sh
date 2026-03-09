#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# port-forward.sh — SSM port-forward to the Prometheus Fargate task
# ---------------------------------------------------------------------------
# Usage:
#   ./port-forward.sh                                    # defaults
#   ./port-forward.sh <cluster-name> <service> [port]
# ---------------------------------------------------------------------------
set -euo pipefail

CLUSTER="${1:-rastir-server}"
SERVICE="${2:-prometheus}"
LOCAL_PORT="${3:-9090}"
REMOTE_PORT="$LOCAL_PORT"

echo "Finding running task for service $SERVICE in cluster: $CLUSTER ..."

TASK_ARN=$(aws ecs list-tasks \
  --cluster "$CLUSTER" \
  --service-name "$SERVICE" \
  --desired-status RUNNING \
  --query 'taskArns[0]' \
  --output text)

if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "ERROR: No running tasks found for service $SERVICE in cluster $CLUSTER"
  exit 1
fi

TASK_ID="${TASK_ARN##*/}"
echo "Task: $TASK_ID"
echo "Forwarding localhost:$LOCAL_PORT → $REMOTE_PORT ..."

aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK_ID}_${TASK_ID%%_*}" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"$REMOTE_PORT\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
