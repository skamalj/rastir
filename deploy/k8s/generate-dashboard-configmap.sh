#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Generate grafana-dashboards ConfigMap from JSON files
# ---------------------------------------------------------------------------
# Usage:
#   ./generate-dashboard-configmap.sh
#   # Then: helm install rastir . (dashboards will be applied via post-install)
#
# Or pipe directly:
#   ./generate-dashboard-configmap.sh | kubectl apply -f -
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="${SCRIPT_DIR}/../../grafana/dashboards"
NAMESPACE="${1:-rastir}"

if [[ ! -d "$DASHBOARD_DIR" ]]; then
  echo "ERROR: Dashboard directory not found: $DASHBOARD_DIR" >&2
  exit 1
fi

cat <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboards
  namespace: ${NAMESPACE}
  labels:
    app: grafana
data:
EOF

for f in "$DASHBOARD_DIR"/*.json; do
  name="$(basename "$f")"
  echo "  ${name}: |"
  sed 's/^/    /' "$f"
done
