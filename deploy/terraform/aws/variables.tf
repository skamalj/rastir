# ---------------------------------------------------------------------------
# Terraform Variables — Rastir AWS Deployment
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "stack_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "rastir"
}

variable "vpc_id" {
  description = "VPC ID to deploy into"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for Fargate tasks"
  type        = list(string)
}

variable "assign_public_ip" {
  description = "Assign public IP to Fargate tasks (needed for public subnets without NAT)"
  type        = bool
  default     = false
}

# --- Rastir Server ---

variable "rastir_image" {
  description = "Rastir server container image"
  type        = string
  default     = "719030485523.dkr.ecr.ap-south-1.amazonaws.com/rastir-server:latest"
}

variable "rastir_cpu" {
  description = "Rastir server CPU units"
  type        = number
  default     = 512
}

variable "rastir_memory" {
  description = "Rastir server memory (MB)"
  type        = number
  default     = 1024
}

variable "rastir_port" {
  description = "Rastir server port"
  type        = number
  default     = 8080
}

variable "rastir_env" {
  description = "Extra environment variables for the Rastir server"
  type        = map(string)
  default     = {}
}

# --- ADOT Collector ---

variable "adot_image" {
  description = "ADOT Collector image (must be in ECR for private VPC)"
  type        = string
  default     = "719030485523.dkr.ecr.ap-south-1.amazonaws.com/adot-collector:latest"
}

# --- Prometheus ---

variable "prometheus_image" {
  description = "Prometheus image"
  type        = string
  default     = "719030485523.dkr.ecr.ap-south-1.amazonaws.com/prom:latest"
}

variable "prometheus_cpu" {
  description = "Prometheus CPU units"
  type        = number
  default     = 512
}

variable "prometheus_memory" {
  description = "Prometheus memory (MB)"
  type        = number
  default     = 1024
}

variable "prometheus_storage_gb" {
  description = "EFS storage for Prometheus data (GB)"
  type        = number
  default     = 50
}

# --- Grafana ---

variable "grafana_image" {
  description = "Grafana image"
  type        = string
  default     = "719030485523.dkr.ecr.ap-south-1.amazonaws.com/grafana:latest"
}

variable "grafana_cpu" {
  description = "Grafana CPU units"
  type        = number
  default     = 256
}

variable "grafana_memory" {
  description = "Grafana memory (MB)"
  type        = number
  default     = 512
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
