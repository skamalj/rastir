# ---------------------------------------------------------------------------
# Terraform Module: OTel Collector
# ---------------------------------------------------------------------------
# Generates collector configuration for different trace backends.

variable "image" {
  description = "OTel Collector image"
  type        = string
  default     = "otel/opentelemetry-collector-contrib:latest"
}

variable "trace_backend" {
  description = "Trace backend: tempo, xray, appinsights, or cloudtrace"
  type        = string
  default     = "tempo"
  validation {
    condition     = contains(["tempo", "xray", "appinsights", "cloudtrace"], var.trace_backend)
    error_message = "trace_backend must be one of: tempo, xray, appinsights, cloudtrace"
  }
}

variable "tempo_endpoint" {
  description = "Tempo gRPC endpoint (used when trace_backend=tempo)"
  type        = string
  default     = "tempo:4317"
}

variable "aws_region" {
  description = "AWS region (used when trace_backend=xray)"
  type        = string
  default     = "us-east-1"
}

variable "azure_connection_string" {
  description = "Azure Application Insights connection string"
  type        = string
  default     = ""
  sensitive   = true
}

variable "gcp_project_id" {
  description = "GCP project ID (used when trace_backend=cloudtrace)"
  type        = string
  default     = ""
}

# Use ADOT image for AWS, standard contrib image for everything else
locals {
  collector_image = var.trace_backend == "xray" ? coalesce(var.image, "public.ecr.aws/aws-observability/aws-otel-collector:latest") : var.image

  configs = {
    tempo = yamlencode({
      receivers = {
        otlp = {
          protocols = {
            grpc = { endpoint = "0.0.0.0:4317" }
            http = { endpoint = "0.0.0.0:4318" }
          }
        }
      }
      exporters = {
        "otlp/tempo" = {
          endpoint = var.tempo_endpoint
          tls      = { insecure = true }
        }
      }
      extensions = {
        health_check = { endpoint = "0.0.0.0:13133" }
      }
      service = {
        extensions = ["health_check"]
        pipelines = {
          traces = {
            receivers = ["otlp"]
            exporters = ["otlp/tempo"]
          }
        }
      }
    })

    xray = yamlencode({
      receivers = {
        otlp = {
          protocols = {
            grpc = { endpoint = "0.0.0.0:4317" }
            http = { endpoint = "0.0.0.0:4318" }
          }
        }
      }
      exporters = {
        awsxray = { region = var.aws_region }
      }
      extensions = {
        health_check = { endpoint = "0.0.0.0:13133" }
      }
      service = {
        extensions = ["health_check"]
        pipelines = {
          traces = {
            receivers = ["otlp"]
            exporters = ["awsxray"]
          }
        }
      }
    })

    appinsights = yamlencode({
      receivers = {
        otlp = {
          protocols = {
            grpc = { endpoint = "0.0.0.0:4317" }
            http = { endpoint = "0.0.0.0:4318" }
          }
        }
      }
      exporters = {
        azuremonitor = {
          connection_string = var.azure_connection_string
        }
      }
      extensions = {
        health_check = { endpoint = "0.0.0.0:13133" }
      }
      service = {
        extensions = ["health_check"]
        pipelines = {
          traces = {
            receivers = ["otlp"]
            exporters = ["azuremonitor"]
          }
        }
      }
    })

    cloudtrace = yamlencode({
      receivers = {
        otlp = {
          protocols = {
            grpc = { endpoint = "0.0.0.0:4317" }
            http = { endpoint = "0.0.0.0:4318" }
          }
        }
      }
      exporters = {
        googlecloud = {
          project = var.gcp_project_id
        }
      }
      extensions = {
        health_check = { endpoint = "0.0.0.0:13133" }
      }
      service = {
        extensions = ["health_check"]
        pipelines = {
          traces = {
            receivers = ["otlp"]
            exporters = ["googlecloud"]
          }
        }
      }
    })
  }
}

output "container_config" {
  value = {
    image = local.collector_image
    grpc_port     = 4317
    http_port     = 4318
    health_port   = 13133
  }
}

output "collector_config" {
  description = "OTel Collector YAML configuration"
  value       = local.configs[var.trace_backend]
}

output "trace_backend" {
  value = var.trace_backend
}
