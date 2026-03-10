# ---------------------------------------------------------------------------
# Rastir Observability Stack — GCP Cloud Run
# ---------------------------------------------------------------------------
# Deploys:
#   1. Rastir Server  (Cloud Run service)
#   2. OTel Collector (Cloud Run sidecar — traces → Cloud Trace)
#   3. Prometheus     (GCE VM — Cloud Run is stateless, needs disk)
#   4. Grafana        (Cloud Run service)
#
# Usage:
#   cd deploy/terraform/gcp
#   cp terraform.tfvars.example terraform.tfvars  # edit
#   terraform init
#   terraform apply
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── Modules ───────────────────────────────────────────────────────────────

module "rastir_server" {
  source        = "../modules/rastir-server"
  image         = var.rastir_image
  port          = var.rastir_port
  otlp_endpoint = "http://localhost:4318"
  extra_env     = var.rastir_env
}

module "otel_collector" {
  source         = "../modules/otel-collector"
  trace_backend  = "cloudtrace"
  gcp_project_id = var.project_id
}

module "prometheus" {
  source                 = "../modules/prometheus"
  image                  = var.prometheus_image
  rastir_server_endpoint = "localhost:${var.rastir_port}"
  storage_size_gb        = var.prometheus_disk_size_gb
}

module "grafana" {
  source         = "../modules/grafana"
  image          = var.grafana_image
  admin_password = var.grafana_admin_password
  prometheus_url = "http://${google_compute_instance.prometheus.network_interface[0].network_ip}:9090"
  tempo_url      = ""  # Cloud Trace used instead of Tempo
}

# ── APIs ──────────────────────────────────────────────────────────────────

resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "cloudtrace" {
  service            = "cloudtrace.googleapis.com"
  disable_on_destroy = false
}

# ── VPC & Connector ──────────────────────────────────────────────────────

resource "google_compute_network" "this" {
  name                    = "${var.stack_name}-vpc"
  auto_create_subnetworks = true
}

resource "google_vpc_access_connector" "this" {
  name          = "${var.stack_name}-connector"
  region        = var.region
  ip_cidr_range = var.vpc_connector_cidr
  network       = google_compute_network.this.name
}

# ── Firewall — allow internal traffic ─────────────────────────────────────

resource "google_compute_firewall" "internal" {
  name    = "${var.stack_name}-allow-internal"
  network = google_compute_network.this.name

  allow {
    protocol = "tcp"
    ports    = ["8080", "9090", "3000", "4317", "4318"]
  }

  source_ranges = ["10.0.0.0/8", var.vpc_connector_cidr]
}

# ── Service Account ──────────────────────────────────────────────────────

resource "google_service_account" "rastir" {
  account_id   = "${var.stack_name}-sa"
  display_name = "Rastir stack service account"
}

resource "google_project_iam_member" "trace_writer" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.rastir.email}"
}

# ── Cloud Run: Rastir Server + OTel Collector sidecar ─────────────────────

resource "google_cloud_run_v2_service" "rastir" {
  name     = "${var.stack_name}-server"
  location = var.region
  labels   = var.labels

  template {
    service_account = google_service_account.rastir.email

    vpc_access {
      connector = google_vpc_access_connector.this.id
      egress    = "ALL_TRAFFIC"
    }

    containers {
      name  = "rastir-server"
      image = module.rastir_server.container_config.image

      ports {
        container_port = var.rastir_port
      }

      dynamic "env" {
        for_each = module.rastir_server.container_config.environment
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        http_get {
          path = "/health"
          port = var.rastir_port
        }
        initial_delay_seconds = 5
        period_seconds        = 10
      }

      liveness_probe {
        http_get {
          path = "/health"
          port = var.rastir_port
        }
        period_seconds = 15
      }

      resources {
        limits = {
          cpu    = var.rastir_cpu
          memory = var.rastir_memory
        }
      }
    }

    containers {
      name  = "otel-collector"
      image = module.otel_collector.container_config.image

      env {
        name  = "OTEL_CONFIG"
        value = module.otel_collector.collector_config
      }

      resources {
        limits = {
          cpu    = "0.5"
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.run, google_project_service.cloudtrace]
}

# ── GCE: Prometheus (needs persistent disk) ───────────────────────────────

resource "google_compute_disk" "prometheus" {
  name = "${var.stack_name}-prometheus-data"
  type = "pd-ssd"
  size = var.prometheus_disk_size_gb
  zone = "${var.region}-a"
}

resource "google_compute_instance" "prometheus" {
  name         = "${var.stack_name}-prometheus"
  machine_type = "e2-small"
  zone         = "${var.region}-a"
  labels       = var.labels

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
      size  = 10
    }
  }

  attached_disk {
    source      = google_compute_disk.prometheus.self_link
    device_name = "prometheus-data"
  }

  network_interface {
    network = google_compute_network.this.name
  }

  service_account {
    email  = google_service_account.rastir.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    gce-container-declaration = yamlencode({
      spec = {
        containers = [{
          name  = "prometheus"
          image = module.prometheus.container_config.image
          args  = module.prometheus.container_config.command
          volumeMounts = [{
            name      = "prometheus-data"
            mountPath = "/prometheus"
          }]
        }]
        volumes = [{
          name = "prometheus-data"
          gcePersistentDisk = {
            pdName = "prometheus-data"
            fsType = "ext4"
          }
        }]
      }
    })
  }

  depends_on = [google_project_service.compute]
}

# ── Cloud Run: Grafana ────────────────────────────────────────────────────

resource "google_cloud_run_v2_service" "grafana" {
  name     = "${var.stack_name}-grafana"
  location = var.region
  labels   = var.labels

  template {
    vpc_access {
      connector = google_vpc_access_connector.this.id
      egress    = "ALL_TRAFFIC"
    }

    containers {
      name  = "grafana"
      image = module.grafana.container_config.image

      ports {
        container_port = 3000
      }

      dynamic "env" {
        for_each = module.grafana.container_config.environment
        content {
          name  = env.key
          value = env.value
        }
      }

      resources {
        limits = {
          cpu    = "0.5"
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.run, google_compute_instance.prometheus]
}
