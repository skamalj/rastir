# ---------------------------------------------------------------------------
# Terraform Module: Grafana
# ---------------------------------------------------------------------------
# Outputs container config for Grafana with pre-provisioned datasources.

variable "image" {
  description = "Grafana container image"
  type        = string
  default     = "grafana/grafana:latest"
}

variable "port" {
  description = "Grafana port"
  type        = number
  default     = 3000
}

variable "admin_user" {
  description = "Grafana admin username"
  type        = string
  default     = "admin"
}

variable "admin_password" {
  description = "Grafana admin password"
  type        = string
  default     = "admin"
  sensitive   = true
}

variable "prometheus_url" {
  description = "Prometheus datasource URL"
  type        = string
  default     = "http://prometheus:9090"
}

variable "tempo_url" {
  description = "Tempo datasource URL (empty to disable)"
  type        = string
  default     = "http://tempo:3200"
}

variable "xray_datasource" {
  description = "AWS X-Ray datasource config. Set region to enable. Replaces Tempo for exemplar trace links."
  type = object({
    region = string
  })
  default = null
}

output "container_config" {
  value = {
    image  = var.image
    port   = var.port
    environment = {
      GF_SECURITY_ADMIN_USER     = var.admin_user
      GF_SECURITY_ADMIN_PASSWORD = var.admin_password
      GF_USERS_ALLOW_SIGN_UP     = "false"
    }
  }
}

locals {
  trace_datasource_uid = var.xray_datasource != null ? "xray" : "tempo"
}

output "datasources_config" {
  description = "Grafana datasources provisioning YAML"
  value = yamlencode({
    apiVersion = 1
    datasources = concat([
      {
        name      = "Prometheus"
        type      = "prometheus"
        access    = "proxy"
        url       = var.prometheus_url
        isDefault = true
        uid       = "prometheus"
        editable  = true
        jsonData = {
          httpMethod = "POST"
          exemplarTraceIdDestinations = [{
            name           = "trace_id"
            datasourceUid  = local.trace_datasource_uid
            urlDisplayLabel = "View Trace"
          }]
        }
      }],
      var.xray_datasource != null ? [{
        name     = "X-Ray"
        type     = "grafana-x-ray-datasource"
        access   = "proxy"
        uid      = "xray"
        editable = true
        jsonData = {
          defaultRegion = var.xray_datasource.region
          authType      = "default"
        }
      }] : [],
      var.tempo_url != "" ? [{
        name     = "Tempo"
        type     = "tempo"
        access   = "proxy"
        url      = var.tempo_url
        uid      = "tempo"
        editable = true
        jsonData = {
          tracesToMetrics = {
            datasourceUid      = "prometheus"
            spanStartTimeShift = "-1h"
            spanEndTimeShift   = "1h"
          }
          nodeGraph = { enabled = true }
        }
      }] : []
    )
  })
}

output "dashboards_provider_config" {
  description = "Grafana dashboards provisioning YAML"
  value = yamlencode({
    apiVersion = 1
    providers = [{
      name                = "Rastir"
      orgId               = 1
      type                = "file"
      disableDeletion     = false
      updateIntervalSeconds = 30
      allowUiUpdates      = true
      options = {
        path                     = "/var/lib/grafana/dashboards"
        foldersFromFilesStructure = false
      }
    }]
  })
}
