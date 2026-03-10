output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "rastir_service_connect_endpoint" {
  value = "http://rastir-server:${var.rastir_port}"
}

output "prometheus_service_connect_endpoint" {
  value = "http://prometheus:9090"
}

output "grafana_service_connect_endpoint" {
  value = "http://grafana:3000"
}
