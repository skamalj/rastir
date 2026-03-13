"""Rastir CLI — Deployment and Management Commands.

Usage:
    rastir deploy <target> <action> [options]

Targets:
    local    Docker Compose (local development)
    k8s      Kubernetes via Helm
    aws      AWS ECS Fargate via Terraform
    azure    Azure Container Instances via Terraform
    gcp      GCP Cloud Run via Terraform

Actions:
    start    Deploy/create resources
    stop     Remove/destroy resources
    status   Check deployment health
    logs     Tail logs (local/k8s only)
    check    Validate prerequisites and config
"""

import argparse
import sys
from typing import List, Optional

from rastir.cli.deploy import run_deploy


def create_parser() -> argparse.ArgumentParser:
    """Create the main CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="rastir",
        description="Rastir — LLM & Agent Observability CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Deploy command
    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Deploy Rastir observability stack",
        description="Deploy Rastir to various targets (local, k8s, aws, azure, gcp)",
    )
    deploy_parser.add_argument(
        "target",
        choices=["local", "k8s", "aws", "azure", "gcp"],
        help="Deployment target",
    )
    deploy_parser.add_argument(
        "action",
        choices=["start", "stop", "status", "logs", "check"],
        help="Action to perform",
    )
    deploy_parser.add_argument(
        "--deploy-config", "-d",
        default="rastir-deploy.yaml",
        help="Path to deployment config (default: rastir-deploy.yaml)",
    )
    deploy_parser.add_argument(
        "--server-config", "-s",
        default="rastir-server-config.yaml",
        help="Path to server config (default: rastir-server-config.yaml)",
    )
    deploy_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deployed without applying",
    )

    return parser


def main(args: Optional[List[str]] = None) -> int:
    """Main CLI entry point."""
    parser = create_parser()
    parsed = parser.parse_args(args)

    if parsed.command is None:
        parser.print_help()
        return 1

    if parsed.command == "deploy":
        return run_deploy(
            target=parsed.target,
            action=parsed.action,
            deploy_config=parsed.deploy_config,
            server_config=parsed.server_config,
            dry_run=parsed.dry_run,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
