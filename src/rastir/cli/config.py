"""Deploy configuration loading and validation.

Handles rastir-deploy.yaml configuration file.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class PrometheusExternalConfig:
    """External Prometheus configuration."""
    endpoint: str = ""
    config_method: str = "file"  # file | operator
    scrape_config_path: str = ""
    rules_path: str = ""
    reload_endpoint: str = ""


@dataclass
class PrometheusConfig:
    """Prometheus configuration."""
    mode: str = "deploy"  # deploy | external
    retention: str = "30d"
    storage: str = "50Gi"
    external: PrometheusExternalConfig = field(default_factory=PrometheusExternalConfig)


@dataclass
class GrafanaExternalConfig:
    """External Grafana configuration."""
    endpoint: str = ""
    api_key: str = ""


@dataclass
class GrafanaConfig:
    """Grafana configuration."""
    mode: str = "deploy"  # deploy | external
    admin_password: str = "admin"
    external: GrafanaExternalConfig = field(default_factory=GrafanaExternalConfig)


@dataclass
class LocalTargetConfig:
    """Local (Docker Compose) target configuration."""
    pass


@dataclass
class K8sTargetConfig:
    """Kubernetes target configuration."""
    namespace: str = "rastir"


@dataclass
class AwsTargetConfig:
    """AWS target configuration."""
    region: str = "us-east-1"
    vpc_id: str = ""
    subnet_ids: List[str] = field(default_factory=list)
    assign_public_ip: bool = False


@dataclass
class AzureTargetConfig:
    """Azure target configuration."""
    location: str = "eastus"
    resource_group: str = "rastir-rg"


@dataclass
class GcpTargetConfig:
    """GCP target configuration."""
    project_id: str = ""
    region: str = "us-central1"


@dataclass
class DeployConfig:
    """Main deployment configuration."""
    server_config: str = "rastir-server-config.yaml"
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    grafana: GrafanaConfig = field(default_factory=GrafanaConfig)
    local: LocalTargetConfig = field(default_factory=LocalTargetConfig)
    k8s: K8sTargetConfig = field(default_factory=K8sTargetConfig)
    aws: AwsTargetConfig = field(default_factory=AwsTargetConfig)
    azure: AzureTargetConfig = field(default_factory=AzureTargetConfig)
    gcp: GcpTargetConfig = field(default_factory=GcpTargetConfig)


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand environment variables in config values."""
    if isinstance(value, str):
        # Expand ${VAR} patterns
        if value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            return os.environ.get(env_var, value)
        return value
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def load_deploy_config(config_path: str) -> DeployConfig:
    """Load deployment configuration from YAML file.
    
    Args:
        config_path: Path to rastir-deploy.yaml
        
    Returns:
        DeployConfig instance
        
    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config is invalid
    """
    path = Path(config_path)
    
    if not path.exists():
        # Return defaults if no config file
        return DeployConfig()
    
    with open(path) as f:
        raw_config = yaml.safe_load(f) or {}
    
    # Expand environment variables
    raw_config = _expand_env_vars(raw_config)
    
    config = DeployConfig()
    
    # Parse server_config reference
    if "server_config" in raw_config:
        config.server_config = raw_config["server_config"]
    
    # Parse prometheus config
    if "prometheus" in raw_config:
        prom = raw_config["prometheus"]
        config.prometheus.mode = prom.get("mode", "deploy")
        config.prometheus.retention = prom.get("retention", "30d")
        config.prometheus.storage = prom.get("storage", "50Gi")
        if "external" in prom:
            ext = prom["external"]
            config.prometheus.external.endpoint = ext.get("endpoint", "")
            config.prometheus.external.config_method = ext.get("config_method", "file")
            config.prometheus.external.scrape_config_path = ext.get("scrape_config_path", "")
            config.prometheus.external.rules_path = ext.get("rules_path", "")
            config.prometheus.external.reload_endpoint = ext.get("reload_endpoint", "")
    
    # Parse grafana config
    if "grafana" in raw_config:
        graf = raw_config["grafana"]
        config.grafana.mode = graf.get("mode", "deploy")
        config.grafana.admin_password = graf.get("admin_password", "admin")
        if "external" in graf:
            ext = graf["external"]
            config.grafana.external.endpoint = ext.get("endpoint", "")
            config.grafana.external.api_key = ext.get("api_key", "")
    
    # Parse targets
    targets = raw_config.get("targets", {})
    
    if "local" in targets:
        pass  # No config needed for local
    
    if "k8s" in targets:
        k8s = targets["k8s"]
        config.k8s.namespace = k8s.get("namespace", "rastir")
    
    if "aws" in targets:
        aws = targets["aws"]
        config.aws.region = aws.get("region", "us-east-1")
        config.aws.vpc_id = aws.get("vpc_id", "")
        config.aws.subnet_ids = aws.get("subnet_ids", [])
        config.aws.assign_public_ip = aws.get("assign_public_ip", False)
    
    if "azure" in targets:
        azure = targets["azure"]
        config.azure.location = azure.get("location", "eastus")
        config.azure.resource_group = azure.get("resource_group", "rastir-rg")
    
    if "gcp" in targets:
        gcp = targets["gcp"]
        config.gcp.project_id = gcp.get("project_id", "")
        config.gcp.region = gcp.get("region", "us-central1")
    
    return config


def validate_config_for_target(config: DeployConfig, target: str) -> List[str]:
    """Validate configuration for a specific target.
    
    Returns list of error messages. Empty list means valid.
    """
    errors = []
    
    if target == "aws":
        if not config.aws.vpc_id:
            errors.append("aws.vpc_id is required")
        if not config.aws.subnet_ids:
            errors.append("aws.subnet_ids is required (at least one subnet)")
    
    elif target == "gcp":
        if not config.gcp.project_id:
            errors.append("gcp.project_id is required")
    
    # Validate external mode configs
    if config.prometheus.mode == "external":
        if not config.prometheus.external.endpoint:
            errors.append("prometheus.external.endpoint is required when mode is 'external'")
    
    if config.grafana.mode == "external":
        if not config.grafana.external.endpoint:
            errors.append("grafana.external.endpoint is required when mode is 'external'")
        if not config.grafana.external.api_key:
            errors.append("grafana.external.api_key is required when mode is 'external'")
    
    return errors
