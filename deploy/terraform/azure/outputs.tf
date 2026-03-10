output "rastir_ip" {
  value = azurerm_container_group.rastir.ip_address
}

output "prometheus_ip" {
  value = azurerm_container_group.prometheus.ip_address
}

output "grafana_ip" {
  value = azurerm_container_group.grafana.ip_address
}

output "grafana_url" {
  value = "http://${azurerm_container_group.grafana.ip_address}:3000"
}
