"""GCP deployment target using Terraform (Cloud Run).

Fixed stack:
    - Rastir Server (Cloud Run)
    - OTel Collector (Cloud Run)
    - Cloud Trace (GCP managed - trace backend)
    - Prometheus (Compute Engine)
    - Grafana (Cloud Run)
"""

from pathlib import Path
from typing import Any, Dict

from rastir.cli.config import DeployConfig
from rastir.cli.targets.terraform_base import TerraformTarget


class GcpTarget(TerraformTarget):
    """GCP deployment using Terraform (Cloud Run)."""
    
    name = "gcp"
    terraform_subdir = "gcp"
    trace_backend = "Cloud Trace"
    trace_collector = "OTel Collector"
    
    def _get_tfvars(self) -> Dict[str, Any]:
        """Generate terraform variables from config."""
        return {
            "project_id": self.config.gcp.project_id,
            "region": self.config.gcp.region,
            "stack_name": "rastir",
        }
    
    def _print_endpoints(self) -> None:
        """Print GCP deployment info."""
        print()
        print("─" * 60)
        print("  Rastir GCP Stack (Cloud Run)")
        print("─" * 60)
        print(f"  Project: {self.config.gcp.project_id}")
        print(f"  Region: {self.config.gcp.region}")
        print()
        print("  Services will be accessible via:")
        print("    - Rastir Server: Cloud Run URL (see terraform output)")
        print("    - Grafana: Cloud Run URL (see terraform output)")
        print("    - Traces: GCP Console > Cloud Trace")
        print("─" * 60)
        print()
    
    def _print_log_instructions(self) -> None:
        """Print GCP log access instructions."""
        print("  - GCP Console > Cloud Run > rastir-server > Logs")
        print("  - Or use: gcloud run services logs read rastir-server")
