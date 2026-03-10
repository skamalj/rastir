# ---------------------------------------------------------------------------
# Terraform Variables — Rastir Azure Deployment
# ---------------------------------------------------------------------------

variable "location" {
  description = "Azure region"
  type        = string
  default     = "eastus"
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
  default     = "rastir-rg"
}

variable "stack_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "rastir"
}

# --- Networking ---

variable "vnet_address_space" {
  description = "VNet address space"
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_address_prefix" {
  description = "Subnet address prefix (delegated to ACI)"
  type        = string
  default     = "10.0.1.0/24"
}

# --- Rastir Server ---

variable "rastir_image" {
  description = "Rastir server container image"
  type        = string
  default     = "ghcr.io/skamalj/rastir-server:latest"
}

variable "rastir_cpu" {
  description = "Rastir server CPU cores"
  type        = number
  default     = 1
}

variable "rastir_memory_gb" {
  description = "Rastir server memory (GB)"
  type        = number
  default     = 1
}

variable "rastir_port" {
  description = "Rastir server port"
  type        = number
  default     = 8080
}

variable "rastir_env" {
  description = "Extra environment variables"
  type        = map(string)
  default     = {}
}

# --- OTel Collector ---

variable "appinsights_connection_string" {
  description = "Azure Application Insights connection string for trace export"
  type        = string
  sensitive   = true
}

# --- Prometheus ---

variable "prometheus_image" {
  description = "Prometheus image"
  type        = string
  default     = "prom/prometheus:latest"
}

variable "prometheus_storage_gb" {
  description = "Prometheus persistent storage size (GB)"
  type        = number
  default     = 50
}

# --- Grafana ---

variable "grafana_image" {
  description = "Grafana image"
  type        = string
  default     = "grafana/grafana:latest"
}

variable "grafana_admin_password" {
  description = "Grafana admin password"
  type        = string
  default     = "admin"
  sensitive   = true
}

# --- Tags ---

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default = {
    Project   = "rastir"
    ManagedBy = "terraform"
  }
}
