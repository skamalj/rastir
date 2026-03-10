# ---------------------------------------------------------------------------
# Terraform Variables — Rastir GCP Deployment
# ---------------------------------------------------------------------------

variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "stack_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "rastir"
}

# --- Networking ---

variable "vpc_connector_cidr" {
  description = "CIDR range for the Serverless VPC Access connector"
  type        = string
  default     = "10.8.0.0/28"
}

# --- Rastir Server ---

variable "rastir_image" {
  description = "Rastir server container image"
  type        = string
  default     = "ghcr.io/skamalj/rastir-server:latest"
}

variable "rastir_cpu" {
  description = "Rastir server CPU (e.g., '1' or '2')"
  type        = string
  default     = "1"
}

variable "rastir_memory" {
  description = "Rastir server memory (e.g., '1Gi')"
  type        = string
  default     = "1Gi"
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

# --- Prometheus ---

variable "prometheus_image" {
  description = "Prometheus image"
  type        = string
  default     = "prom/prometheus:latest"
}

variable "prometheus_disk_size_gb" {
  description = "Prometheus persistent disk size (GB)"
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

# --- Labels ---

variable "labels" {
  description = "Labels to apply to all resources"
  type        = map(string)
  default = {
    project    = "rastir"
    managed-by = "terraform"
  }
}
