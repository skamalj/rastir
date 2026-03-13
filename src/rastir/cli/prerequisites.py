"""Prerequisite checking for deployment targets.

Verifies required tools and credentials are available.
"""

import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ToolCheck:
    """Result of checking a tool."""
    name: str
    required: bool
    found: bool
    version: str = ""
    path: str = ""
    install_hint: str = ""


@dataclass
class CredentialCheck:
    """Result of checking credentials."""
    name: str
    configured: bool
    detail: str = ""
    configure_hint: str = ""


@dataclass
class PrerequisiteResult:
    """Overall prerequisite check result."""
    target: str
    tools: List[ToolCheck]
    credentials: List[CredentialCheck]
    
    @property
    def all_passed(self) -> bool:
        """Check if all prerequisites are met."""
        tools_ok = all(t.found for t in self.tools if t.required)
        creds_ok = all(c.configured for c in self.credentials)
        return tools_ok and creds_ok


def _run_command(cmd: List[str], timeout: int = 5) -> Tuple[bool, str]:
    """Run a command and return (success, output)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False, ""


def _check_tool(
    name: str,
    version_cmd: Optional[List[str]] = None,
    install_hint: str = "",
) -> ToolCheck:
    """Check if a tool is installed."""
    path = shutil.which(name)
    found = path is not None
    version = ""
    
    if found and version_cmd:
        success, output = _run_command(version_cmd)
        if success:
            # Extract version from output (usually first line)
            version = output.split("\n")[0] if output else ""
    
    return ToolCheck(
        name=name,
        required=True,
        found=found,
        version=version,
        path=path or "",
        install_hint=install_hint,
    )


def check_docker() -> ToolCheck:
    """Check Docker installation."""
    return _check_tool(
        "docker",
        version_cmd=["docker", "--version"],
        install_hint=(
            "Install Docker:\n"
            "  macOS:   brew install --cask docker\n"
            "  Linux:   https://docs.docker.com/engine/install/\n"
            "  Windows: https://docs.docker.com/desktop/windows/install/"
        ),
    )


def check_docker_compose() -> ToolCheck:
    """Check Docker Compose installation (v2 plugin or standalone)."""
    # First check docker compose (v2 plugin)
    success, output = _run_command(["docker", "compose", "version"])
    if success:
        return ToolCheck(
            name="docker compose",
            required=True,
            found=True,
            version=output,
            path="docker compose",
            install_hint="",
        )
    
    # Fallback to docker-compose (standalone)
    tool = _check_tool(
        "docker-compose",
        version_cmd=["docker-compose", "--version"],
        install_hint=(
            "Docker Compose is included with Docker Desktop.\n"
            "For Linux: https://docs.docker.com/compose/install/"
        ),
    )
    tool.name = "docker compose"
    return tool


def check_kubectl() -> ToolCheck:
    """Check kubectl installation."""
    return _check_tool(
        "kubectl",
        version_cmd=["kubectl", "version", "--client", "--short"],
        install_hint=(
            "Install kubectl:\n"
            "  macOS:   brew install kubectl\n"
            "  Linux:   https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/\n"
            "  Windows: https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/"
        ),
    )


def check_helm() -> ToolCheck:
    """Check Helm installation."""
    return _check_tool(
        "helm",
        version_cmd=["helm", "version", "--short"],
        install_hint=(
            "Install Helm:\n"
            "  macOS:   brew install helm\n"
            "  Linux:   https://helm.sh/docs/intro/install/\n"
            "  Windows: choco install kubernetes-helm"
        ),
    )


def check_terraform() -> ToolCheck:
    """Check Terraform installation."""
    return _check_tool(
        "terraform",
        version_cmd=["terraform", "version"],
        install_hint=(
            "Install Terraform:\n"
            "  macOS:   brew install terraform\n"
            "  Linux:   https://developer.hashicorp.com/terraform/downloads\n"
            "  Windows: choco install terraform"
        ),
    )


def check_aws_cli() -> ToolCheck:
    """Check AWS CLI installation."""
    return _check_tool(
        "aws",
        version_cmd=["aws", "--version"],
        install_hint=(
            "Install AWS CLI:\n"
            "  macOS:   brew install awscli\n"
            "  Linux:   pip install awscli\n"
            "  Windows: https://awscli.amazonaws.com/AWSCLIV2.msi"
        ),
    )


def check_azure_cli() -> ToolCheck:
    """Check Azure CLI installation."""
    return _check_tool(
        "az",
        version_cmd=["az", "version", "--output", "tsv"],
        install_hint=(
            "Install Azure CLI:\n"
            "  macOS:   brew install azure-cli\n"
            "  Linux:   curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash\n"
            "  Windows: https://aka.ms/installazurecliwindows"
        ),
    )


def check_gcloud_cli() -> ToolCheck:
    """Check Google Cloud CLI installation."""
    return _check_tool(
        "gcloud",
        version_cmd=["gcloud", "version"],
        install_hint=(
            "Install Google Cloud CLI:\n"
            "  All platforms: https://cloud.google.com/sdk/docs/install"
        ),
    )


def check_aws_credentials() -> CredentialCheck:
    """Check AWS credentials are configured."""
    success, output = _run_command(["aws", "sts", "get-caller-identity"], timeout=10)
    if success:
        return CredentialCheck(
            name="AWS credentials",
            configured=True,
            detail="Credentials configured",
        )
    return CredentialCheck(
        name="AWS credentials",
        configured=False,
        configure_hint=(
            "Configure AWS credentials:\n"
            "  aws configure\n"
            "  # or export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
        ),
    )


def check_azure_credentials() -> CredentialCheck:
    """Check Azure credentials are configured."""
    success, _ = _run_command(["az", "account", "show"], timeout=10)
    if success:
        return CredentialCheck(
            name="Azure credentials",
            configured=True,
            detail="Logged in",
        )
    return CredentialCheck(
        name="Azure credentials",
        configured=False,
        configure_hint="Run: az login",
    )


def check_gcp_credentials() -> CredentialCheck:
    """Check GCP credentials are configured."""
    success, output = _run_command(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], timeout=10)
    if success and output:
        return CredentialCheck(
            name="GCP credentials",
            configured=True,
            detail=f"Account: {output.split()[0] if output else 'configured'}",
        )
    return CredentialCheck(
        name="GCP credentials",
        configured=False,
        configure_hint="Run: gcloud auth login",
    )


def check_kubeconfig() -> CredentialCheck:
    """Check kubectl is configured with a cluster."""
    success, output = _run_command(["kubectl", "config", "current-context"], timeout=5)
    if success and output:
        return CredentialCheck(
            name="Kubernetes context",
            configured=True,
            detail=f"Context: {output}",
        )
    return CredentialCheck(
        name="Kubernetes context",
        configured=False,
        configure_hint=(
            "Configure kubectl:\n"
            "  kubectl config use-context <context-name>\n"
            "  # or set KUBECONFIG environment variable"
        ),
    )


def check_docker_running() -> CredentialCheck:
    """Check Docker daemon is running."""
    success, _ = _run_command(["docker", "info"], timeout=10)
    if success:
        return CredentialCheck(
            name="Docker daemon",
            configured=True,
            detail="Running",
        )
    return CredentialCheck(
        name="Docker daemon",
        configured=False,
        configure_hint="Start Docker Desktop or the Docker daemon",
    )


def check_prerequisites(target: str) -> PrerequisiteResult:
    """Check all prerequisites for a deployment target.
    
    Args:
        target: Deployment target (local, k8s, aws, azure, gcp)
        
    Returns:
        PrerequisiteResult with tool and credential checks
    """
    tools: List[ToolCheck] = []
    credentials: List[CredentialCheck] = []
    
    if target == "local":
        tools.append(check_docker())
        tools.append(check_docker_compose())
        credentials.append(check_docker_running())
    
    elif target == "k8s":
        tools.append(check_kubectl())
        tools.append(check_helm())
        credentials.append(check_kubeconfig())
    
    elif target == "aws":
        tools.append(check_terraform())
        tools.append(check_aws_cli())
        credentials.append(check_aws_credentials())
    
    elif target == "azure":
        tools.append(check_terraform())
        tools.append(check_azure_cli())
        credentials.append(check_azure_credentials())
    
    elif target == "gcp":
        tools.append(check_terraform())
        tools.append(check_gcloud_cli())
        credentials.append(check_gcp_credentials())
    
    return PrerequisiteResult(
        target=target,
        tools=tools,
        credentials=credentials,
    )


def print_prerequisite_result(result: PrerequisiteResult) -> None:
    """Print prerequisite check results to stdout."""
    print(f"\nChecking prerequisites for target: {result.target}")
    print("─" * 50)
    
    # Print tool checks
    for tool in result.tools:
        status = "✓" if tool.found else "✗"
        if tool.found:
            version_info = f"  {tool.version}" if tool.version else ""
            print(f"  {status} {tool.name}{version_info}")
        else:
            print(f"  {status} {tool.name}  (missing)")
    
    # Print credential checks
    for cred in result.credentials:
        status = "✓" if cred.configured else "✗"
        detail = f"  ({cred.detail})" if cred.detail and cred.configured else ""
        print(f"  {status} {cred.name}{detail}")
    
    print()
    
    # Print installation hints for missing tools
    missing_tools = [t for t in result.tools if not t.found and t.required]
    if missing_tools:
        print("Missing tools:")
        for tool in missing_tools:
            print(f"\n{tool.name}:")
            print(tool.install_hint)
    
    # Print credential hints
    missing_creds = [c for c in result.credentials if not c.configured]
    if missing_creds:
        print("\nMissing credentials:")
        for cred in missing_creds:
            print(f"\n{cred.name}:")
            print(cred.configure_hint)
