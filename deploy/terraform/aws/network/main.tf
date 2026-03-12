# ---------------------------------------------------------------------------
# Rastir AWS Network — Private VPC with VPC Endpoints
# ---------------------------------------------------------------------------
# Creates a fully private network for ECS Fargate deployment:
#   - VPC with DNS support
#   - 2 private subnets (multi-AZ)
#   - VPC endpoints for all AWS services needed by Fargate
#   - No NAT gateway, no internet gateway
#
# Usage:
#   cd deploy/terraform/aws/network
#   cp terraform.tfvars.example terraform.tfvars  # edit
#   terraform init && terraform apply
#
# Then use the outputs as inputs for the main Rastir ECS stack:
#   cd ../
#   terraform apply -var="vpc_id=$(terraform -chdir=network output -raw vpc_id)" \
#                   -var='subnet_ids=["subnet-...", "subnet-..."]'
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

# ── Data Sources ──────────────────────────────────────────────────────────

data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = length(var.availability_zones) > 0 ? var.availability_zones : slice(data.aws_availability_zones.available.names, 0, length(var.subnet_cidrs))
}

# ── VPC ───────────────────────────────────────────────────────────────────

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.stack_name}-vpc" }
}

# ── Private Subnets ───────────────────────────────────────────────────────

resource "aws_subnet" "private" {
  count             = length(var.subnet_cidrs)
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.subnet_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = { Name = "${var.stack_name}-private-${local.azs[count.index]}" }
}

# ── Route Table (local-only, no internet route) ──────────────────────────

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${var.stack_name}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ── Security Group for VPC Endpoints ─────────────────────────────────────

resource "aws_security_group" "vpce" {
  name_prefix = "${var.stack_name}-vpce-"
  vpc_id      = aws_vpc.this.id
  description = "Allow HTTPS from VPC to VPC Endpoints"

  ingress {
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.stack_name}-vpce-sg" }
}

# ── VPC Endpoints — Interface (PrivateLink) ──────────────────────────────
# Required for fully private Fargate:
#   - ecr.api / ecr.dkr   — pull container images
#   - logs                 — CloudWatch Logs (awslogs driver)
#   - ecs / ecs-agent / ecs-telemetry — ECS control plane
#   - xray                 — ADOT collector → X-Ray
#   - ssmmessages          — ECS Exec (enable_execute_command)
#   - elasticfilesystem    — EFS mount targets

locals {
  interface_endpoints = [
    "ecr.api",
    "ecr.dkr",
    "logs",
    "ecs",
    "ecs-agent",
    "ecs-telemetry",
    "xray",
    "ssmmessages",
    "elasticfilesystem",
  ]
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(local.interface_endpoints)

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpce.id]

  tags = { Name = "${var.stack_name}-vpce-${each.key}" }
}

# ── VPC Endpoint — Gateway (S3) ──────────────────────────────────────────
# ECR stores image layers in S3; gateway endpoints are free.

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${var.stack_name}-vpce-s3" }
}
