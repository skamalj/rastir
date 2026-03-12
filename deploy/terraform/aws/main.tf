# ---------------------------------------------------------------------------
# Rastir Observability Stack — AWS ECS Fargate
# ---------------------------------------------------------------------------
# Deploys:
#   1. Rastir Server  (Fargate task)
#   2. ADOT Collector (sidecar — traces → X-Ray)
#   3. Prometheus     (Fargate task + EFS)
#   4. Grafana        (Fargate task)
#
# Usage:
#   cd deploy/terraform/aws
#   cp terraform.tfvars.example terraform.tfvars  # edit
#   terraform init
#   terraform apply
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = var.tags
  }
}

# ── Modules ───────────────────────────────────────────────────────────────

module "rastir_server" {
  source        = "../modules/rastir-server"
  image         = var.rastir_image
  port          = var.rastir_port
  cpu           = var.rastir_cpu
  memory        = var.rastir_memory
  otlp_endpoint = "http://localhost:4318" # ADOT sidecar
  extra_env     = var.rastir_env
}

module "otel_collector" {
  source        = "../modules/otel-collector"
  image         = var.adot_image
  trace_backend = "xray"
  aws_region    = var.aws_region
}

module "prometheus" {
  source                  = "../modules/prometheus"
  image                   = var.prometheus_image
  rastir_server_endpoint  = "rastir-server:${var.rastir_port}"
  storage_size_gb         = var.prometheus_storage_gb
}

module "grafana" {
  source           = "../modules/grafana"
  image            = var.grafana_image
  admin_password   = var.grafana_admin_password
  prometheus_url   = "http://prometheus:9090"
  tempo_url        = ""  # X-Ray used instead of Tempo
  xray_datasource  = { region = var.aws_region }
}

# ── Data ──────────────────────────────────────────────────────────────────

data "aws_vpc" "this" {
  id = var.vpc_id
}

# ── ECS Cluster + Service Discovery ──────────────────────────────────────

resource "aws_service_discovery_http_namespace" "this" {
  name        = var.stack_name
  description = "Service Connect namespace for Rastir stack"
}

resource "aws_ecs_cluster" "this" {
  name = var.stack_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  service_connect_defaults {
    namespace = aws_service_discovery_http_namespace.this.arn
  }
}

# ── Security Group ────────────────────────────────────────────────────────

resource "aws_security_group" "rastir" {
  name_prefix = "${var.stack_name}-"
  vpc_id      = var.vpc_id
  description = "Rastir stack - intra-VPC communication"

  ingress {
    description = "Rastir server"
    from_port   = var.rastir_port
    to_port     = var.rastir_port
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  ingress {
    description = "Prometheus"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  ingress {
    description = "Grafana"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }

  ingress {
    description = "EFS (NFS)"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── IAM — Execution Role ─────────────────────────────────────────────────

resource "aws_iam_role" "execution" {
  name = "${var.stack_name}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ── IAM — Task Role (ADOT → X-Ray + SSM Exec) ──────────────────────────

resource "aws_iam_role" "task" {
  name = "${var.stack_name}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_xray" {
  name = "xray-access"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
        "xray:BatchGetTraces",
        "xray:GetTraceSummaries",
        "xray:GetTraceGraph",
        "xray:GetGroups",
        "xray:GetGroup",
        "xray:GetTimeSeriesServiceStatistics",
        "xray:GetInsightSummaries",
        "xray:GetServiceGraph",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "task_ssm" {
  name = "ssm-exec"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}

# ── EFS for Prometheus ────────────────────────────────────────────────────

resource "aws_efs_file_system" "prometheus" {
  creation_token = "${var.stack_name}-prometheus"
  encrypted      = true

  tags = { Name = "${var.stack_name}-prometheus" }
}

resource "aws_efs_mount_target" "prometheus" {
  count           = length(var.subnet_ids)
  file_system_id  = aws_efs_file_system.prometheus.id
  subnet_id       = var.subnet_ids[count.index]
  security_groups = [aws_security_group.rastir.id]
}

resource "aws_efs_access_point" "prometheus" {
  file_system_id = aws_efs_file_system.prometheus.id
  posix_user {
    uid = 65534  # nobody
    gid = 65534
  }
  root_directory {
    path = "/prometheus"
    creation_info {
      owner_uid   = 65534
      owner_gid   = 65534
      permissions = "755"
    }
  }
}

# ── CloudWatch Log Groups ────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "rastir" {
  name              = "/ecs/${var.stack_name}/rastir"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "adot" {
  name              = "/ecs/${var.stack_name}/adot"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "prometheus" {
  name              = "/ecs/${var.stack_name}/prometheus"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "grafana" {
  name              = "/ecs/${var.stack_name}/grafana"
  retention_in_days = 14
}

# ── Task Definition: Rastir Server + ADOT sidecar ────────────────────────

locals {
  rastir_config = module.rastir_server.container_config
  otel_config   = module.otel_collector.container_config

  # Prometheus recording rules & alerts
  rules_dir                = "${path.module}/../../../deploy/docker/rules"
  prometheus_rules_content = file("${local.rules_dir}/rastir-sre-rules.yml")
  prometheus_alerts_content = file("${local.rules_dir}/alerts.yaml")

  # Grafana dashboards — compressed for env var transport
  dashboard_dir = "${path.module}/../../../grafana/dashboards"
  dashboard_files = {
    for f in fileset(local.dashboard_dir, "*.json") :
    replace(f, ".json", "") => base64gzip(
      replace(file("${local.dashboard_dir}/${f}"), "$${DS_PROMETHEUS}", "prometheus")
    )
  }
}

resource "aws_ecs_task_definition" "rastir" {
  family                   = "${var.stack_name}-rastir"
  cpu                      = 1024  # rastir + ADOT sidecar
  memory                   = 2048
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "rastir-server"
      image     = local.rastir_config.image
      essential = true
      portMappings = [{
        containerPort = local.rastir_config.port
        protocol      = "tcp"
        name          = "rastir-http"
      }]
      environment = [for k, v in local.rastir_config.environment : { name = k, value = v }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.rastir.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "rastir"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import httpx; httpx.get('http://localhost:${local.rastir_config.port}/health').raise_for_status()\""]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 10
      }
    },
    {
      name      = "adot-collector"
      image     = local.otel_config.image
      essential = false
      environment = [{
        name  = "AOT_CONFIG_CONTENT"
        value = module.otel_collector.collector_config
      }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.adot.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "adot"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "wget -qO- http://localhost:13133/ || exit 1"]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 10
      }
    }
  ])
}

# ── Task Definition: Prometheus ──────────────────────────────────────────

resource "aws_ecs_task_definition" "prometheus" {
  family                   = "${var.stack_name}-prometheus"
  cpu                      = var.prometheus_cpu
  memory                   = var.prometheus_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  volume {
    name = "prometheus-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.prometheus.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.prometheus.id
        iam             = "DISABLED"
      }
    }
  }

  volume {
    name = "prometheus-config"
  }

  container_definitions = jsonencode([
    {
      name      = "prometheus"
      image     = module.prometheus.container_config.image
      essential = true
      portMappings = [{
        containerPort = 9090
        protocol      = "tcp"
        name          = "prometheus-http"
      }]
      entryPoint = ["/bin/sh", "-c"]
      command = [
        "echo \"$PROMETHEUS_CONFIG\" > /etc/prometheus/prometheus.yml && mkdir -p /etc/prometheus/rules && echo \"$PROMETHEUS_RULES\" > /etc/prometheus/rules/rastir-sre-rules.yml && echo \"$PROMETHEUS_ALERTS\" > /etc/prometheus/rules/alerts.yaml && exec /bin/prometheus --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time=30d --web.enable-lifecycle --enable-feature=exemplar-storage"
      ]
      environment = [
        { name = "PROMETHEUS_CONFIG", value = module.prometheus.prometheus_config },
        { name = "PROMETHEUS_RULES",  value = local.prometheus_rules_content },
        { name = "PROMETHEUS_ALERTS", value = local.prometheus_alerts_content },
      ]
      mountPoints = [
        { sourceVolume = "prometheus-data", containerPath = "/prometheus", readOnly = false },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.prometheus.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "prometheus"
        }
      }
    }
  ])
}

# ── EFS for Grafana ───────────────────────────────────────────────────────

resource "aws_efs_file_system" "grafana" {
  creation_token = "${var.stack_name}-grafana"
  encrypted      = true

  tags = { Name = "${var.stack_name}-grafana" }
}

resource "aws_efs_mount_target" "grafana" {
  count           = length(var.subnet_ids)
  file_system_id  = aws_efs_file_system.grafana.id
  subnet_id       = var.subnet_ids[count.index]
  security_groups = [aws_security_group.rastir.id]
}

resource "aws_efs_access_point" "grafana" {
  file_system_id = aws_efs_file_system.grafana.id
  posix_user {
    uid = 472  # grafana user
    gid = 0
  }
  root_directory {
    path = "/grafana"
    creation_info {
      owner_uid   = 472
      owner_gid   = 0
      permissions = "755"
    }
  }
}

# ── Task Definition: Grafana ─────────────────────────────────────────────

resource "aws_ecs_task_definition" "grafana" {
  family                   = "${var.stack_name}-grafana"
  cpu                      = var.grafana_cpu
  memory                   = var.grafana_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  volume {
    name = "grafana-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.grafana.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.grafana.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name      = "grafana"
      image     = module.grafana.container_config.image
      essential = true
      entryPoint = ["/bin/sh", "-c"]
      command = [
        "echo \"$GRAFANA_DATASOURCES\" > /etc/grafana/provisioning/datasources/datasources.yaml && echo \"$GRAFANA_DASHBOARDS_PROVIDER\" > /etc/grafana/provisioning/dashboards/dashboards.yaml && mkdir -p /var/lib/grafana/dashboards && for var in $(env | grep '^DASH_' | cut -d= -f1); do fname=$(echo $var | sed 's/^DASH_//' | tr '_' '-'); printenv $var | base64 -d | gunzip > /var/lib/grafana/dashboards/$fname.json; done && exec /run.sh"
      ]
      portMappings = [{
        containerPort = 3000
        protocol      = "tcp"
        name          = "grafana-http"
      }]
      environment = concat(
        [for k, v in module.grafana.container_config.environment : { name = k, value = v }],
        [
          { name = "GRAFANA_DATASOURCES",        value = module.grafana.datasources_config },
          { name = "GRAFANA_DASHBOARDS_PROVIDER", value = module.grafana.dashboards_provider_config },
        ],
        [for name, content in local.dashboard_files : { name = "DASH_${replace(name, "-", "_")}", value = content }]
      )
      mountPoints = [
        { sourceVolume = "grafana-data", containerPath = "/var/lib/grafana", readOnly = false },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.grafana.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "grafana"
        }
      }
    }
  ])
}

# ── ECS Services ─────────────────────────────────────────────────────────

resource "aws_ecs_service" "rastir" {
  name                   = "rastir-server"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.rastir.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.rastir.id]
    assign_public_ip = var.assign_public_ip
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "rastir-http"
      client_alias {
        port     = var.rastir_port
        dns_name = "rastir-server"
      }
    }
  }
}

resource "aws_ecs_service" "prometheus" {
  name                   = "prometheus"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.prometheus.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.rastir.id]
    assign_public_ip = var.assign_public_ip
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "prometheus-http"
      client_alias {
        port     = 9090
        dns_name = "prometheus"
      }
    }
  }

  depends_on = [aws_efs_mount_target.prometheus]
}

resource "aws_ecs_service" "grafana" {
  name                   = "grafana"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.grafana.arn
  desired_count          = 1
  launch_type            = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.rastir.id]
    assign_public_ip = var.assign_public_ip
  }

  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_http_namespace.this.arn
    service {
      port_name = "grafana-http"
      client_alias {
        port     = 3000
        dns_name = "grafana"
      }
    }
  }

  depends_on = [aws_efs_mount_target.grafana]
}
