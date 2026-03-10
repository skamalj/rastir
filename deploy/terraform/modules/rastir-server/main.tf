# ---------------------------------------------------------------------------
# Terraform Module: Rastir Server
# ---------------------------------------------------------------------------
# Reusable module that outputs container definitions for the Rastir server.
# Each cloud root module calls this to get consistent configuration.

variable "image" {
  description = "Rastir server container image"
  type        = string
  default     = "ghcr.io/skamalj/rastir-server:latest"
}

variable "port" {
  description = "Rastir server port"
  type        = number
  default     = 8080
}

variable "otlp_endpoint" {
  description = "OTLP endpoint for the OTel collector"
  type        = string
  default     = "http://localhost:4318"
}

variable "extra_env" {
  description = "Additional environment variables"
  type        = map(string)
  default     = {}
}

variable "cpu" {
  description = "CPU units (cloud-specific meaning)"
  type        = number
  default     = 512
}

variable "memory" {
  description = "Memory in MB"
  type        = number
  default     = 1024
}

output "container_config" {
  description = "Standardized container configuration"
  value = {
    image  = var.image
    port   = var.port
    cpu    = var.cpu
    memory = var.memory
    environment = merge({
      RASTIR_SERVER_HOST                  = "0.0.0.0"
      RASTIR_SERVER_PORT                  = tostring(var.port)
      RASTIR_SERVER_EXPORTER_OTLP_ENDPOINT = var.otlp_endpoint
      RASTIR_SERVER_EXEMPLARS_ENABLED     = "true"
      RASTIR_SERVER_LOGGING_STRUCTURED    = "true"
    }, var.extra_env)
    health_check = {
      path     = "/health"
      port     = var.port
      interval = 15
      timeout  = 5
      retries  = 3
    }
  }
}
