output "rastir_url" {
  value = google_cloud_run_v2_service.rastir.uri
}

output "grafana_url" {
  value = google_cloud_run_v2_service.grafana.uri
}

output "prometheus_ip" {
  value = google_compute_instance.prometheus.network_interface[0].network_ip
}
