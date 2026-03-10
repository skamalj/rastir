#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Rastir Kubernetes (Helm) deployment
# ---------------------------------------------------------------------------
# Usage:
#   ./deploy.sh                                  # install with defaults (Tempo)
#   ./deploy.sh --set traceBackend=xray          # AWS X-Ray
#   ./deploy.sh --set traceBackend=appinsights   # Azure
#   ./deploy.sh --set traceBackend=cloudtrace    # GCP
#   ./deploy.sh uninstall                        # remove
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RELEASE_NAME="rastir"

if [[ "${1:-}" == "uninstall" ]]; then
  helm uninstall "$RELEASE_NAME" 2>/dev/null || true
  echo "✓ Rastir uninstalled."
  exit 0
fi

# Generate dashboard ConfigMap from JSON files
echo "── Generating dashboard ConfigMap..."
bash generate-dashboard-configmap.sh | kubectl apply -f -

echo "── Installing Rastir Helm chart..."
echo "  Release:  $RELEASE_NAME"
echo "  Values:   values.yaml"
echo "──────────────────────────────────────────────────────────"

helm upgrade --install "$RELEASE_NAME" . "$@"

echo ""
echo "✓ Rastir stack deployed."
echo ""
echo "  kubectl port-forward svc/grafana 3000:3000 -n rastir"
echo "  kubectl port-forward svc/rastir-server 8080:8080 -n rastir"
echo "  kubectl port-forward svc/prometheus 9090:9090 -n rastir"
