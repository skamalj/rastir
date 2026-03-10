# ---------------------------------------------------------------------------
# Rastir Observability Stack — Azure Container Instances
# ---------------------------------------------------------------------------
# Deploys:
#   1. Rastir Server  (ACI container group)
#   2. OTel Collector (sidecar — traces → Application Insights)
#   3. Prometheus     (ACI container group + Azure File Share)
#   4. Grafana        (ACI container group)
#
# Usage:
#   cd deploy/terraform/azure
#   cp terraform.tfvars.example terraform.tfvars  # edit
#   terraform init
#   terraform apply
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
}

# ── Modules ───────────────────────────────────────────────────────────────

module "rastir_server" {
  source        = "../modules/rastir-server"
  image         = var.rastir_image
  port          = var.rastir_port
  cpu           = var.rastir_cpu
  memory        = var.rastir_memory_gb * 1024
  otlp_endpoint = "http://localhost:4318"
  extra_env     = var.rastir_env
}

module "otel_collector" {
  source                  = "../modules/otel-collector"
  trace_backend           = "appinsights"
  azure_connection_string = var.appinsights_connection_string
}

module "prometheus" {
  source                 = "../modules/prometheus"
  image                  = var.prometheus_image
  rastir_server_endpoint = "${var.stack_name}-rastir.${var.stack_name}-vnet:${var.rastir_port}"
  storage_size_gb        = var.prometheus_storage_gb
}

module "grafana" {
  source         = "../modules/grafana"
  image          = var.grafana_image
  admin_password = var.grafana_admin_password
  prometheus_url = "http://${var.stack_name}-prometheus.${var.stack_name}-vnet:9090"
  tempo_url      = ""  # App Insights used instead of Tempo
}

# ── Resource Group ────────────────────────────────────────────────────────

resource "azurerm_resource_group" "this" {
  name     = var.resource_group_name
  location = var.location
  tags     = var.tags
}

# ── Networking ────────────────────────────────────────────────────────────

resource "azurerm_virtual_network" "this" {
  name                = "${var.stack_name}-vnet"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  address_space       = [var.vnet_address_space]
}

resource "azurerm_subnet" "aci" {
  name                 = "${var.stack_name}-aci-subnet"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.subnet_address_prefix]

  delegation {
    name = "aci-delegation"
    service_delegation {
      name    = "Microsoft.ContainerInstance/containerGroups"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_network_profile" "aci" {
  name                = "${var.stack_name}-aci-profile"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location

  container_network_interface {
    name = "aci-nic"
    ip_configuration {
      name      = "aci-ip"
      subnet_id = azurerm_subnet.aci.id
    }
  }
}

# ── Storage for Prometheus ────────────────────────────────────────────────

resource "azurerm_storage_account" "this" {
  name                     = replace("${var.stack_name}storage", "-", "")
  resource_group_name      = azurerm_resource_group.this.name
  location                 = azurerm_resource_group.this.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = var.tags
}

resource "azurerm_storage_share" "prometheus" {
  name               = "prometheus-data"
  storage_account_id = azurerm_storage_account.this.id
  quota              = var.prometheus_storage_gb
}

# ── Container Group: Rastir Server + OTel Collector ──────────────────────

resource "azurerm_container_group" "rastir" {
  name                = "${var.stack_name}-rastir"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  os_type             = "Linux"
  network_profile_id  = azurerm_network_profile.aci.id
  restart_policy      = "Always"
  tags                = var.tags

  container {
    name   = "rastir-server"
    image  = module.rastir_server.container_config.image
    cpu    = var.rastir_cpu
    memory = var.rastir_memory_gb

    ports {
      port     = var.rastir_port
      protocol = "TCP"
    }

    dynamic "environment_variables" {
      for_each = module.rastir_server.container_config.environment
      content {
        name  = environment_variables.key
        value = environment_variables.value
      }
    }

    liveness_probe {
      http_get {
        path = "/health"
        port = var.rastir_port
      }
      initial_delay_seconds = 10
      period_seconds        = 15
    }
  }

  container {
    name   = "otel-collector"
    image  = module.otel_collector.container_config.image
    cpu    = 0.5
    memory = 0.5

    environment_variables = {
      OTEL_CONFIG = module.otel_collector.collector_config
    }

    ports {
      port     = 4317
      protocol = "TCP"
    }

    ports {
      port     = 4318
      protocol = "TCP"
    }
  }
}

# ── Container Group: Prometheus ──────────────────────────────────────────

resource "azurerm_container_group" "prometheus" {
  name                = "${var.stack_name}-prometheus"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  os_type             = "Linux"
  network_profile_id  = azurerm_network_profile.aci.id
  restart_policy      = "Always"
  tags                = var.tags

  container {
    name   = "prometheus"
    image  = module.prometheus.container_config.image
    cpu    = 1
    memory = 1

    ports {
      port     = 9090
      protocol = "TCP"
    }

    commands = module.prometheus.container_config.command

    volume {
      name                 = "prometheus-data"
      mount_path           = "/prometheus"
      storage_account_name = azurerm_storage_account.this.name
      storage_account_key  = azurerm_storage_account.this.primary_access_key
      share_name           = azurerm_storage_share.prometheus.name
    }
  }

  depends_on = [azurerm_container_group.rastir]
}

# ── Container Group: Grafana ─────────────────────────────────────────────

resource "azurerm_container_group" "grafana" {
  name                = "${var.stack_name}-grafana"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  os_type             = "Linux"
  network_profile_id  = azurerm_network_profile.aci.id
  restart_policy      = "Always"
  tags                = var.tags

  container {
    name   = "grafana"
    image  = module.grafana.container_config.image
    cpu    = 0.5
    memory = 0.5

    ports {
      port     = 3000
      protocol = "TCP"
    }

    dynamic "environment_variables" {
      for_each = module.grafana.container_config.environment
      content {
        name  = environment_variables.key
        value = environment_variables.value
      }
    }
  }

  depends_on = [azurerm_container_group.prometheus]
}
