# ---------------------------------------------------------------------------
# Terraform Module: Prometheus
# ---------------------------------------------------------------------------
# Outputs container config and configuration file content for Prometheus.

variable "rastir_server_endpoint" {
  description = "Rastir server endpoint for scraping (host:port)"
  type        = string
  default     = "rastir-server:8080"
}

variable "image" {
  description = "Prometheus container image"
  type        = string
  default     = "prom/prometheus:latest"
}

variable "port" {
  description = "Prometheus port"
  type        = number
  default     = 9090
}

variable "retention" {
  description = "TSDB retention period"
  type        = string
  default     = "30d"
}

variable "storage_size_gb" {
  description = "Persistent storage size in GB"
  type        = number
  default     = 50
}

output "container_config" {
  value = {
    image  = var.image
    port   = var.port
    command = [
      "--config.file=/etc/prometheus/prometheus.yml",
      "--storage.tsdb.path=/prometheus",
      "--storage.tsdb.retention.time=${var.retention}",
      "--web.enable-lifecycle",
      "--enable-feature=exemplar-storage",
    ]
    storage_size_gb = var.storage_size_gb
  }
}

output "prometheus_config" {
  description = "prometheus.yml content"
  value = yamlencode({
    global = {
      scrape_interval    = "15s"
      evaluation_interval = "15s"
    }
    rule_files = ["/etc/prometheus/rules/*.yml", "/etc/prometheus/rules/*.yaml"]
    scrape_configs = [{
      job_name     = "rastir-server"
      metrics_path = "/metrics"
      honor_labels = true
      static_configs = [{
        targets = [var.rastir_server_endpoint]
      }]
    }]
  })
}
