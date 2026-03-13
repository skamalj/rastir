"""Deploy command implementation.

Orchestrates prerequisite checking, config loading, and target execution.
"""

import sys
from pathlib import Path
from typing import Optional

from rastir.cli.config import (
    DeployConfig,
    load_deploy_config,
    validate_config_for_target,
)
from rastir.cli.prerequisites import (
    check_prerequisites,
    print_prerequisite_result,
)
from rastir.cli.targets.base import DeployTarget
from rastir.cli.targets.local import LocalTarget
from rastir.cli.targets.k8s import K8sTarget
from rastir.cli.targets.aws import AwsTarget
from rastir.cli.targets.azure import AzureTarget
from rastir.cli.targets.gcp import GcpTarget


def _find_deploy_dir() -> Optional[Path]:
    """Find the deploy directory.
    
    Searches in order:
    1. ./deploy (current directory)
    2. Package installation directory
    """
    # Check current directory
    cwd_deploy = Path.cwd() / "deploy"
    if cwd_deploy.exists():
        return cwd_deploy
    
    # Check relative to this file (for development)
    package_deploy = Path(__file__).parent.parent.parent.parent / "deploy"
    if package_deploy.exists():
        return package_deploy
    
    return None


def _get_target(
    target_name: str,
    config: DeployConfig,
    server_config_path: str,
    deploy_dir: Path,
    dry_run: bool,
) -> DeployTarget:
    """Get the appropriate target implementation."""
    targets = {
        "local": LocalTarget,
        "k8s": K8sTarget,
        "aws": AwsTarget,
        "azure": AzureTarget,
        "gcp": GcpTarget,
    }
    
    target_class = targets.get(target_name)
    if not target_class:
        raise ValueError(f"Unknown target: {target_name}")
    
    return target_class(
        config=config,
        server_config_path=server_config_path,
        deploy_dir=deploy_dir,
        dry_run=dry_run,
    )


def run_deploy(
    target: str,
    action: str,
    deploy_config: str,
    server_config: str,
    dry_run: bool,
) -> int:
    """Run the deploy command.
    
    Args:
        target: Deployment target (local, k8s, aws, azure, gcp)
        action: Action to perform (start, stop, status, logs, check)
        deploy_config: Path to rastir-deploy.yaml
        server_config: Path to rastir-server-config.yaml
        dry_run: If True, show what would be done
        
    Returns:
        Exit code (0 = success)
    """
    # Check prerequisites first
    prereq_result = check_prerequisites(target)
    print_prerequisite_result(prereq_result)
    
    if not prereq_result.all_passed:
        return 1
    
    # Load config
    try:
        config = load_deploy_config(deploy_config)
    except Exception as e:
        print(f"Error loading config '{deploy_config}': {e}")
        return 1
    
    # Resolve server config path
    if config.server_config:
        server_config_path = config.server_config
    else:
        server_config_path = server_config
    
    # Check server config exists
    if not Path(server_config_path).exists():
        print(f"Warning: Server config not found: {server_config_path}")
        print("  Using defaults. Create rastir-server-config.yaml for custom settings.")
    
    # Validate config for target
    if action in ("start", "check"):
        errors = validate_config_for_target(config, target)
        if errors:
            print("\nConfiguration errors:")
            for error in errors:
                print(f"  ✗ {error}")
            return 1
    
    # Find deploy directory
    deploy_dir = _find_deploy_dir()
    if deploy_dir is None:
        print("Error: Could not find deploy/ directory.")
        print("  Run from the rastir project root, or ensure deploy/ is in the current directory.")
        return 1
    
    # If check action, we're done after validation
    if action == "check":
        print(f"\n✓ Prerequisites and configuration valid for target: {target}")
        print(f"  Deploy directory: {deploy_dir}")
        print(f"  Server config: {server_config_path}")
        return 0
    
    # Get target implementation
    try:
        target_impl = _get_target(
            target_name=target,
            config=config,
            server_config_path=server_config_path,
            deploy_dir=deploy_dir,
            dry_run=dry_run,
        )
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    
    # Execute action
    actions = {
        "start": target_impl.start,
        "stop": target_impl.stop,
        "status": target_impl.status,
        "logs": target_impl.logs,
    }
    
    action_fn = actions.get(action)
    if not action_fn:
        print(f"Error: Unknown action: {action}")
        return 1
    
    return action_fn()
