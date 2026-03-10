#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Rastir local stack (Docker Compose)
# ---------------------------------------------------------------------------
# Usage:
#   ./deploy.sh          # bring up the full stack
#   ./deploy.sh down     # tear down
#   ./deploy.sh logs     # tail all logs
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ACTION="${1:-up}"

case "$ACTION" in
  up)
    echo "── Rastir Local Stack ──────────────────────────────────"
    echo "  Rastir Server : http://localhost:8080"
    echo "  Grafana       : http://localhost:3000  (admin/admin)"
    echo "  Prometheus    : http://localhost:9090"
    echo "  Tempo         : http://localhost:3200"
    echo "  OTLP gRPC    : localhost:4317"
    echo "  OTLP HTTP    : localhost:4318"
    echo "──────────────────────────────────────────────────────────"
    docker compose up -d --build
    echo ""
    echo "✓ Stack is up. Run './deploy.sh logs' to tail output."
    ;;
  down)
    docker compose down
    echo "✓ Stack torn down."
    ;;
  logs)
    docker compose logs -f
    ;;
  restart)
    docker compose restart
    ;;
  *)
    echo "Usage: $0 {up|down|logs|restart}"
    exit 1
    ;;
esac
