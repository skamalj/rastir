# ---------------------------------------------------------------------------
# Outputs — Rastir AWS Network Stack
# ---------------------------------------------------------------------------
# Feed these into the main Rastir ECS stack:
#   vpc_id     → var.vpc_id
#   subnet_ids → var.subnet_ids
# ---------------------------------------------------------------------------

output "vpc_id" {
  description = "VPC ID for the Rastir deployment"
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "VPC CIDR block"
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs (use as subnet_ids in the ECS stack)"
  value       = aws_subnet.private[*].id
}

output "vpce_security_group_id" {
  description = "Security group ID attached to VPC endpoints"
  value       = aws_security_group.vpce.id
}
