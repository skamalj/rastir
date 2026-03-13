"""AWS deployment target using Terraform (ECS Fargate).

Fixed stack:
    - Rastir Server (ECS Fargate)
    - ADOT Collector (ECS Sidecar)
    - X-Ray (AWS managed - trace backend)
    - Prometheus (ECS Fargate)
    - Grafana (ECS Fargate)
"""

from pathlib import Path
from typing import Any, Dict

from rastir.cli.config import DeployConfig
from rastir.cli.targets.terraform_base import TerraformTarget


class AwsTarget(TerraformTarget):
    """AWS deployment using Terraform (ECS Fargate)."""
    
    name = "aws"
    terraform_subdir = "aws"
    trace_backend = "X-Ray"
    trace_collector = "ADOT"
    
    def _get_tfvars(self) -> Dict[str, Any]:
        """Generate terraform variables from config."""
        return {
            "aws_region": self.config.aws.region,
            "vpc_id": self.config.aws.vpc_id,
            "subnet_ids": self.config.aws.subnet_ids,
            "assign_public_ip": self.config.aws.assign_public_ip,
            "stack_name": "rastir",
        }
    
    def _print_endpoints(self) -> None:
        """Print AWS deployment info."""
        print()
        print("─" * 60)
        print("  Rastir AWS Stack (ECS Fargate)")
        print("─" * 60)
        print(f"  Region: {self.config.aws.region}")
        print(f"  VPC: {self.config.aws.vpc_id}")
        print()
        print("  Services will be accessible via:")
        print("    - Rastir Server: Via ALB (see terraform output)")
        print("    - Grafana: Via ALB (see terraform output)")
        print("    - Traces: AWS X-Ray Console")
        print("─" * 60)
        print()
    
    def _print_log_instructions(self) -> None:
        """Print AWS log access instructions."""
        print("  - CloudWatch Logs: AWS Console > CloudWatch > Log groups")
        print(f"  - Filter by: /ecs/rastir-*")
        print("  - Or use: aws logs tail /ecs/rastir-server --follow")
