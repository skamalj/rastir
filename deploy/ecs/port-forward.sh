#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# port-forward.sh — SSM port-forward to a running Rastir Fargate task
# ---------------------------------------------------------------------------
# Usage:
#   ./port-forward.sh                          # defaults: stack=rastir-server, port=8080
#   ./port-forward.sh <cluster-name> [port]
#
# After running, access Rastir at http://localhost:<port>
# ---------------------------------------------------------------------------
set -euo pipefail

CLUSTER="${1:-rastir-server}"
LOCAL_PORT="${2:-8080}"
REMOTE_PORT="$LOCAL_PORT"

echo "Finding running task in cluster: $CLUSTER ..."

# Get the first running task ARN
TASK_ARN=$(aws ecs list-tasks \
  --cluster "$CLUSTER" \
  --service-name rastir-server \
  --desired-status RUNNING \
  --query 'taskArns[0]' \
  --output text)

if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "ERROR: No running tasks found in cluster $CLUSTER"
  exit 1
fi

# Get the runtime ID for the rastir-server container
TASK_ID=$(echo "$TASK_ARN" | awk -F'/' '{print $NF}')
RUNTIME_ID=$(aws ecs describe-tasks \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN" \
  --query "tasks[0].containers[?name=='rastir-server'].runtimeId" \
  --output text)

TARGET="ecs:${CLUSTER}_${TASK_ID}_${RUNTIME_ID}"

echo "Task:   $TASK_ID"
echo "Target: $TARGET"
echo ""
echo "Opening port-forward: localhost:${LOCAL_PORT} → task:${REMOTE_PORT}"
echo "Press Ctrl+C to stop."
echo ""

aws ssm start-session \
  --target "$TARGET" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"${REMOTE_PORT}\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
