---
title: Deployment
nav_order: 12
---

# Deployment Guide

Rastir ships with production-ready deployment configs for local development, three major clouds, and Kubernetes.

## Stack Components

Every deployment target deploys the same four components:

| Component | Role |
|-----------|------|
| **Rastir Server** | Receives telemetry, exposes `/metrics`, serves OTLP |
| **OTel Collector** | Forwards traces to the trace backend |
| **Prometheus** | Scrapes `/metrics` from Rastir, evaluates SRE rules |
| **Grafana** | Pre-provisioned dashboards + datasources |

The only thing that varies per target is the **trace backend**:

| Target | Trace Backend |
|--------|--------------|
| Local (Docker Compose) | Tempo (deployed as 5th container) |
| AWS | X-Ray (managed) |
| Azure | Application Insights (managed) |
| GCP | Cloud Trace (managed) |
| Kubernetes | Configurable: Tempo (default), X-Ray, App Insights, or Cloud Trace |

---

## Docker Compose (Local) {#docker-compose}

The fastest way to get the full stack running locally.

### Prerequisites

- Docker and Docker Compose

### Deploy

```bash
cd deploy/docker
./deploy.sh          # brings up all 5 services
```

### What's Running

| Service | URL |
|---------|-----|
| Rastir Server | `http://localhost:8080` |
| Grafana | `http://localhost:3000` (admin/admin) |
| Prometheus | `http://localhost:9090` |
| Tempo | `http://localhost:3200` |
| OTLP gRPC | `localhost:4317` |
| OTLP HTTP | `localhost:4318` |

### Commands

```bash
./deploy.sh          # start
./deploy.sh down     # stop
./deploy.sh logs     # tail logs
./deploy.sh restart  # restart all
```

---

## AWS (ECS Fargate) {#aws}

Deploys to ECS Fargate via Terraform. Traces go to AWS X-Ray via ADOT (AWS Distro for OpenTelemetry).

### Prerequisites

- Terraform >= 1.5
- AWS CLI configured with sufficient permissions
- A VPC with subnets

### Deploy

```bash
cd deploy/terraform/aws
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your VPC, subnets, etc.
./deploy.sh          # terraform init + apply
```

### Architecture

- **Rastir Server + ADOT** — single Fargate task, ADOT as sidecar
- **Prometheus** — Fargate task with EFS for persistent TSDB storage
- **Grafana** — Fargate task
- **Service Connect** — internal service discovery (services find each other by name)

### Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `vpc_id` | VPC to deploy into | (required) |
| `subnet_ids` | Subnet IDs for Fargate | (required) |
| `rastir_image` | Rastir server container image | `ghcr.io/skamalj/rastir-server:latest` |
| `rastir_cpu` / `rastir_memory` | Server resources | 512 / 1024 |
| `grafana_admin_password` | Grafana admin password | `admin` |

### Commands

```bash
./deploy.sh          # apply
./deploy.sh plan     # plan only
./deploy.sh destroy  # tear down
```

---

## Azure (Container Instances) {#azure}

Deploys to Azure Container Instances via Terraform. Traces go to Application Insights.

### Prerequisites

- Terraform >= 1.5
- Azure CLI (`az login`)
- An Application Insights connection string

### Deploy

```bash
cd deploy/terraform/azure
cp terraform.tfvars.example terraform.tfvars
# Edit: set appinsights_connection_string, location, etc.
./deploy.sh
```

### Architecture

- **Rastir Server + OTel Collector** — ACI container group, collector as sidecar
- **Prometheus** — ACI container group with Azure File Share for storage
- **Grafana** — ACI container group
- **VNet** — all groups communicate over a private VNet

### Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `location` | Azure region | `eastus` |
| `appinsights_connection_string` | App Insights connection string | (required) |
| `grafana_admin_password` | Grafana admin password | `admin` |

---

## GCP (Cloud Run) {#gcp}

Deploys to Cloud Run via Terraform. Traces go to Cloud Trace.

### Prerequisites

- Terraform >= 1.5
- `gcloud` CLI authenticated
- A GCP project with Cloud Run and Cloud Trace APIs enabled

### Deploy

```bash
cd deploy/terraform/gcp
cp terraform.tfvars.example terraform.tfvars
# Edit: set project_id, region, etc.
./deploy.sh
```

### Architecture

- **Rastir Server + OTel Collector** — Cloud Run multi-container service
- **Prometheus** — GCE VM with persistent SSD (Cloud Run is stateless)
- **Grafana** — Cloud Run service
- **VPC Connector** — allows Cloud Run to reach the Prometheus VM

### Key Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `project_id` | GCP project ID | (required) |
| `region` | GCP region | `us-central1` |
| `grafana_admin_password` | Grafana admin password | `admin` |

---

## Kubernetes (Helm) {#kubernetes}

A Helm chart that works on any Kubernetes cluster (EKS, AKS, GKE, on-prem). By default traces go to Tempo; set `traceBackend` to switch.

### Prerequisites

- `kubectl` connected to a cluster
- Helm 3

### Deploy

```bash
cd deploy/k8s
./deploy.sh                                  # default: Tempo
./deploy.sh --set traceBackend=xray          # AWS X-Ray
./deploy.sh --set traceBackend=appinsights   # Azure
./deploy.sh --set traceBackend=cloudtrace    # GCP
```

### Access

```bash
kubectl port-forward svc/grafana 3000:3000 -n rastir
kubectl port-forward svc/rastir-server 8080:8080 -n rastir
kubectl port-forward svc/prometheus 9090:9090 -n rastir
```

### Key Values

| Value | Description | Default |
|-------|-------------|---------|
| `traceBackend` | `tempo`, `xray`, `appinsights`, `cloudtrace` | `tempo` |
| `rastirServer.image.tag` | Rastir image tag | `latest` |
| `prometheus.storage.size` | Prometheus PVC size | `50Gi` |
| `grafana.adminPassword` | Grafana admin password | `admin` |

### Uninstall

```bash
./deploy.sh uninstall
```

---

## Directory Structure

```
deploy/
  docker/                         # Docker Compose (local)
    docker-compose.yml
    deploy.sh
    otel-collector-config.yaml
    prometheus.yml
    tempo.yaml
    provisioning/                 # Grafana datasources + dashboards
    dashboards -> ../../grafana/dashboards  (symlink)
    rules/                        # SRE rules + alerts (symlinks)

  terraform/
    modules/                      # Shared Terraform modules
      rastir-server/
      otel-collector/
      prometheus/
      grafana/
    aws/                          # ECS Fargate + ADOT → X-Ray
    azure/                        # ACI + OTel → App Insights
    gcp/                          # Cloud Run + OTel → Cloud Trace

  k8s/                            # Helm chart
    Chart.yaml
    values.yaml
    deploy.sh
    templates/
```
