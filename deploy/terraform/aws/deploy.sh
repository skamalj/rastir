#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Rastir AWS (ECS Fargate) via Terraform
# ---------------------------------------------------------------------------
# Usage:
#   cp terraform.tfvars.example terraform.tfvars  # edit first
#   ./deploy.sh          # plan + apply
#   ./deploy.sh plan     # plan only
#   ./deploy.sh destroy  # tear down
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ACTION="${1:-apply}"

if [[ ! -f terraform.tfvars ]]; then
  echo "ERROR: terraform.tfvars not found."
  echo "       Copy terraform.tfvars.example → terraform.tfvars and edit it."
  exit 1
fi

terraform init -upgrade

case "$ACTION" in
  plan)
    terraform plan
    ;;
  apply)
    terraform apply -auto-approve
    echo ""
    echo "✓ Rastir AWS stack deployed."
    terraform output
    ;;
  destroy)
    terraform destroy -auto-approve
    echo "✓ Rastir AWS stack destroyed."
    ;;
  *)
    echo "Usage: $0 {plan|apply|destroy}"
    exit 1
    ;;
esac
