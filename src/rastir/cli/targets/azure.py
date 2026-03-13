"""Azure deployment target using Terraform (Container Instances).

Fixed stack:
    - Rastir Server (Azure Container Instances)
    - OTel Collector (ACI)
    - Application Insights (Azure managed - trace backend)
    - Prometheus (ACI)
    - Grafana (ACI)
"""

from pathlib import Path
from typing import Any, Dict

from rastir.cli.config import DeployConfig
from rastir.cli.targets.terraform_base import TerraformTarget


class AzureTarget(TerraformTarget):
    """Azure deployment using Terraform (Container Instances)."""
    
    name = "azure"
    terraform_subdir = "azure"
    trace_backend = "Application Insights"
    trace_collector = "OTel Collector"
    
    def _get_tfvars(self) -> Dict[str, Any]:
        """Generate terraform variables from config."""
        return {
            "location": self.config.azure.location,
            "resource_group_name": self.config.azure.resource_group,
            "stack_name": "rastir",
        }
    
    def _print_endpoints(self) -> None:
        """Print Azure deployment info."""
        print()
        print("─" * 60)
        print("  Rastir Azure Stack (Container Instances)")
        print("─" * 60)
        print(f"  Location: {self.config.azure.location}")
        print(f"  Resource Group: {self.config.azure.resource_group}")
        print()
        print("  Services will be accessible via:")
        print("    - Rastir Server: Container group public IP")
        print("    - Grafana: Container group public IP")
        print("    - Traces: Azure Portal > Application Insights")
        print("─" * 60)
        print()
    
    def _print_log_instructions(self) -> None:
        """Print Azure log access instructions."""
        print("  - Azure Portal > Container Instances > rastir-server > Logs")
        print("  - Or use: az container logs --resource-group {rg} --name rastir-server")
