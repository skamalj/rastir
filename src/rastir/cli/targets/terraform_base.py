"""Base class for Terraform-based deployment targets."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from rastir.cli.config import DeployConfig
from rastir.cli.targets.base import DeployTarget


class TerraformTarget(DeployTarget):
    """Base class for Terraform-based deployments (AWS, Azure, GCP)."""
    
    # Subclasses set this to the terraform directory name (aws, azure, gcp)
    terraform_subdir: str = ""
    
    def __init__(
        self,
        config: DeployConfig,
        server_config_path: str,
        deploy_dir: Path,
        dry_run: bool = False,
    ):
        super().__init__(config, server_config_path, deploy_dir, dry_run)
        self.terraform_dir = deploy_dir / "terraform" / self.terraform_subdir
    
    def _get_tfvars(self) -> Dict[str, Any]:
        """Return terraform variables. Override in subclass."""
        return {}
    
    def _write_tfvars(self) -> Path:
        """Write terraform.tfvars.json from config."""
        tfvars = self._get_tfvars()
        tfvars_path = self.terraform_dir / "terraform.tfvars.json"
        
        if self.dry_run:
            print(f"\n[DRY RUN] Would write {tfvars_path}:")
            print(json.dumps(tfvars, indent=2))
            return tfvars_path
        
        with open(tfvars_path, "w") as f:
            json.dump(tfvars, f, indent=2)
        
        return tfvars_path
    
    def _run_terraform(self, args: List[str], capture: bool = False) -> int:
        """Run a terraform command."""
        cmd = ["terraform"] + args
        
        if self.dry_run and args[0] != "init":
            print(f"[DRY RUN] Would execute: {' '.join(cmd)}")
            return 0
        
        try:
            if capture:
                result = subprocess.run(
                    cmd,
                    cwd=self.terraform_dir,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(result.stdout)
                else:
                    print(result.stderr)
                return result.returncode
            else:
                result = subprocess.run(cmd, cwd=self.terraform_dir)
                return result.returncode
        except FileNotFoundError:
            print("Error: terraform not found")
            return 1
        except KeyboardInterrupt:
            return 130
    
    def _print_endpoints(self) -> None:
        """Print service endpoints. Override in subclass."""
        pass
    
    def start(self) -> int:
        """Deploy using Terraform."""
        print(f"\nDeploying Rastir to {self.name}...")
        
        # Print stack info
        for line in self.get_stack_info():
            print(f"  {line}")
        print()
        
        # Write tfvars from config
        print("  Generating terraform.tfvars.json from config...")
        self._write_tfvars()
        
        # Initialize terraform
        print("  Initializing Terraform...")
        ret = self._run_terraform(["init", "-upgrade"])
        if ret != 0:
            return ret
        
        self._print_endpoints()
        
        # Apply
        print("  Applying Terraform configuration...")
        ret = self._run_terraform(["apply", "-auto-approve"])
        
        if ret == 0 and not self.dry_run:
            print(f"\n✓ Rastir deployed to {self.name}.")
            print("\nOutputs:")
            self._run_terraform(["output"], capture=True)
        
        return ret
    
    def stop(self) -> int:
        """Destroy Terraform resources."""
        print(f"\nDestroying Rastir on {self.name}...")
        
        ret = self._run_terraform(["destroy", "-auto-approve"])
        
        if ret == 0 and not self.dry_run:
            print("✓ Rastir infrastructure destroyed.")
        
        return ret
    
    def status(self) -> int:
        """Show Terraform state."""
        print(f"\nRastir {self.name} status:")
        print("─" * 50)
        
        # Show terraform state
        print("\nTerraform resources:")
        ret = self._run_terraform(["state", "list"])
        
        if ret != 0:
            print("  (No state found - not deployed?)")
            return 0
        
        print("\nOutputs:")
        self._run_terraform(["output"], capture=True)
        
        return 0
    
    def logs(self) -> int:
        """Logs are not available via Terraform."""
        print(f"\nLogs for {self.name} are not available via CLI.")
        print("Access logs through the cloud provider's console:")
        self._print_log_instructions()
        return 0
    
    def _print_log_instructions(self) -> None:
        """Print instructions for accessing logs. Override in subclass."""
        pass
