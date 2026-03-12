# ---------------------------------------------------------------------------
# Variables — Rastir AWS Network Stack
# ---------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "stack_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "rastir"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_cidrs" {
  description = "CIDR blocks for private subnets (one per AZ)"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "availability_zones" {
  description = "Availability zones for subnets. Must match length of subnet_cidrs."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default = {
    Project   = "rastir"
    ManagedBy = "terraform"
  }
}
