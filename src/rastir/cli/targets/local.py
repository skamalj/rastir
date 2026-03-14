"""Local deployment target using Docker Compose.

Fixed stack:
    - Rastir Server (built from source or image)
    - OTel Collector
    - Tempo (trace backend)
    - Prometheus
    - Grafana
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List

from rastir.cli.config import DeployConfig
from rastir.cli.targets.base import DeployTarget


class LocalTarget(DeployTarget):
    """Local deployment using Docker Compose."""
    
    name = "local"
    trace_backend = "Tempo"
    trace_collector = "OTel Collector"
    
    def __init__(
        self,
        config: DeployConfig,
        server_config_path: str,
        deploy_dir: Path,
        dry_run: bool = False,
    ):
        super().__init__(config, server_config_path, deploy_dir, dry_run)
        self.docker_dir = deploy_dir / "docker"
    
    def _get_compose_cmd(self) -> List[str]:
        """Get docker compose command (v2 or v1)."""
        # Try v2 first
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
        # Fallback to v1
        return ["docker-compose"]
    
    def _run_compose(self, args: List[str], cwd: Path = None) -> int:
        """Run a docker compose command."""
        cmd = self._get_compose_cmd() + args
        
        if self.dry_run:
            print(f"[DRY RUN] Would execute: {' '.join(cmd)}")
            return 0
        
        # Run from docker directory
        work_dir = cwd or self.docker_dir
        
        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                env={**os.environ, "COMPOSE_PROJECT_NAME": "rastir"},
            )
            return result.returncode
        except FileNotFoundError:
            print("Error: Docker Compose not found")
            return 1
        except KeyboardInterrupt:
            return 130
    
    def _print_endpoints(self) -> None:
        """Print service endpoints."""
        print()
        print("─" * 55)
        print("  Rastir Observability Stack")
        print("─" * 55)
        print("  Rastir Server  : http://localhost:8080")
        print("  Grafana        : http://localhost:3000  (admin/admin)")
        print("  Prometheus     : http://localhost:9090")
        print("  Tempo          : http://localhost:3200")
        print("  OTLP gRPC      : localhost:4317")
        print("  OTLP HTTP      : localhost:4318")
        print("─" * 55)
        print()
    
    def start(self) -> int:
        """Deploy the local stack using docker compose."""
        print(f"\nDeploying Rastir to {self.name}...")
        
        # Print stack info
        for line in self.get_stack_info():
            print(f"  {line}")
        print()
        
        prometheus_mode = self.config.prometheus.mode
        grafana_mode = self.config.grafana.mode
        
        # Build compose arguments
        # --wait ensures all services are healthy/started before returning
        compose_args = ["up", "-d", "--build", "--wait"]
        
        # If prometheus or grafana is external, we need to handle that
        # For now, we deploy everything in local mode
        if prometheus_mode == "external":
            print("  Note: prometheus.mode=external - skipping Prometheus deployment")
            compose_args.extend(["--scale", "prometheus=0"])
        
        if grafana_mode == "external":
            print("  Note: grafana.mode=external - skipping Grafana deployment")
            compose_args.extend(["--scale", "grafana=0"])
        
        self._print_endpoints()
        
        ret = self._run_compose(compose_args)
        
        if ret == 0 and not self.dry_run:
            print("✓ Stack is up. Run 'rastir deploy local logs' to tail output.")
        
        return ret
    
    def stop(self) -> int:
        """Tear down the local stack."""
        print(f"\nStopping Rastir on {self.name}...")
        
        ret = self._run_compose(["down", "--remove-orphans"])
        
        if ret == 0 and not self.dry_run:
            print("✓ Stack torn down.")
        
        return ret
    
    def status(self) -> int:
        """Check local stack status."""
        print(f"\nRastir {self.name} status:")
        print("─" * 50)
        
        return self._run_compose(["ps"])
    
    def logs(self) -> int:
        """Tail local stack logs."""
        print(f"\nTailing Rastir {self.name} logs (Ctrl+C to stop)...")
        
        return self._run_compose(["logs", "-f"])
