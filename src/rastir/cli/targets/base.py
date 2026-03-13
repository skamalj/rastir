"""Base class for deployment targets."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from rastir.cli.config import DeployConfig


class DeployTarget(ABC):
    """Abstract base class for deployment targets."""
    
    # Target name (local, k8s, aws, azure, gcp)
    name: str = ""
    
    # Fixed components per target
    trace_backend: str = ""
    trace_collector: str = ""
    
    def __init__(
        self,
        config: DeployConfig,
        server_config_path: str,
        deploy_dir: Path,
        dry_run: bool = False,
    ):
        """Initialize target.
        
        Args:
            config: Deployment configuration
            server_config_path: Path to rastir-server-config.yaml
            deploy_dir: Path to deploy/ directory with templates
            dry_run: If True, show what would be done without executing
        """
        self.config = config
        self.server_config_path = server_config_path
        self.deploy_dir = deploy_dir
        self.dry_run = dry_run
    
    @abstractmethod
    def start(self) -> int:
        """Deploy/create resources. Returns exit code."""
        pass
    
    @abstractmethod
    def stop(self) -> int:
        """Remove/destroy resources. Returns exit code."""
        pass
    
    @abstractmethod
    def status(self) -> int:
        """Check deployment health. Returns exit code."""
        pass
    
    @abstractmethod
    def logs(self) -> int:
        """Tail logs. Returns exit code."""
        pass
    
    def get_stack_info(self) -> List[str]:
        """Return info about the fixed stack for this target."""
        return [
            f"Target: {self.name}",
            f"Trace Backend: {self.trace_backend}",
            f"Trace Collector: {self.trace_collector}",
            "Metrics: Prometheus",
            "Dashboards: Grafana",
        ]
