"""Kubernetes deployment target using Helm.

Fixed stack:
    - Rastir Server
    - OTel Collector
    - Tempo (trace backend)
    - Prometheus
    - Grafana
"""

import os
import subprocess
from pathlib import Path
from typing import List

from rastir.cli.config import DeployConfig
from rastir.cli.targets.base import DeployTarget


class K8sTarget(DeployTarget):
    """Kubernetes deployment using Helm."""
    
    name = "k8s"
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
        self.helm_dir = deploy_dir / "k8s"
        self.release_name = "rastir"
        self.namespace = config.k8s.namespace
    
    def _run_helm(self, args: List[str]) -> int:
        """Run a helm command."""
        cmd = ["helm"] + args
        
        if self.dry_run:
            print(f"[DRY RUN] Would execute: {' '.join(cmd)}")
            return 0
        
        try:
            result = subprocess.run(cmd, cwd=self.helm_dir)
            return result.returncode
        except FileNotFoundError:
            print("Error: helm not found")
            return 1
        except KeyboardInterrupt:
            return 130
    
    def _run_kubectl(self, args: List[str]) -> int:
        """Run a kubectl command."""
        cmd = ["kubectl"] + args
        
        if self.dry_run:
            print(f"[DRY RUN] Would execute: {' '.join(cmd)}")
            return 0
        
        try:
            result = subprocess.run(cmd)
            return result.returncode
        except FileNotFoundError:
            print("Error: kubectl not found")
            return 1
        except KeyboardInterrupt:
            return 130
    
    def _print_endpoints(self) -> None:
        """Print service access instructions."""
        print()
        print("─" * 60)
        print("  Rastir Kubernetes Stack")
        print("─" * 60)
        print(f"  Namespace: {self.namespace}")
        print()
        print("  Access services via port-forward:")
        print(f"    kubectl port-forward svc/rastir-server 8080:8080 -n {self.namespace}")
        print(f"    kubectl port-forward svc/grafana 3000:3000 -n {self.namespace}")
        print(f"    kubectl port-forward svc/prometheus 9090:9090 -n {self.namespace}")
        print("─" * 60)
        print()
    
    def start(self) -> int:
        """Deploy using Helm."""
        print(f"\nDeploying Rastir to {self.name}...")
        
        # Print stack info
        for line in self.get_stack_info():
            print(f"  {line}")
        print()
        
        # Build helm arguments
        helm_args = [
            "upgrade", "--install",
            self.release_name,
            ".",
            "--namespace", self.namespace,
            "--create-namespace",
            "--set", "traceBackend=tempo",
        ]
        
        # Handle external prometheus/grafana
        if self.config.prometheus.mode == "external":
            print("  Note: prometheus.mode=external - skipping Prometheus deployment")
            helm_args.extend(["--set", "prometheus.enabled=false"])
        
        if self.config.grafana.mode == "external":
            print("  Note: grafana.mode=external - skipping Grafana deployment")
            helm_args.extend(["--set", "grafana.enabled=false"])
        
        self._print_endpoints()
        
        ret = self._run_helm(helm_args)
        
        if ret == 0 and not self.dry_run:
            print(f"✓ Rastir deployed to namespace '{self.namespace}'")
        
        return ret
    
    def stop(self) -> int:
        """Uninstall Helm release."""
        print(f"\nStopping Rastir on {self.name}...")
        
        ret = self._run_helm([
            "uninstall",
            self.release_name,
            "--namespace", self.namespace,
        ])
        
        if ret == 0 and not self.dry_run:
            print("✓ Rastir uninstalled.")
        
        return ret
    
    def status(self) -> int:
        """Check Kubernetes deployment status."""
        print(f"\nRastir {self.name} status (namespace: {self.namespace}):")
        print("─" * 50)
        
        # Show helm release status
        print("\nHelm release:")
        self._run_helm(["status", self.release_name, "--namespace", self.namespace])
        
        # Show pods
        print("\nPods:")
        return self._run_kubectl([
            "get", "pods",
            "-n", self.namespace,
            "-l", f"app.kubernetes.io/instance={self.release_name}",
        ])
    
    def logs(self) -> int:
        """Tail Kubernetes logs."""
        print(f"\nTailing Rastir {self.name} logs (Ctrl+C to stop)...")
        
        return self._run_kubectl([
            "logs",
            "-f",
            "-n", self.namespace,
            "-l", f"app.kubernetes.io/instance={self.release_name}",
            "--all-containers=true",
        ])
