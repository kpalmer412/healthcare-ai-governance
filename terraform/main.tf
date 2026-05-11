##############################################################################
# healthcare-ai-governance / terraform / main.tf
#
# Terraform parity build — mirrors the CloudFormation nested stack deployment.
# Demonstrates IaC flexibility: both CloudFormation (primary) and Terraform
# (secondary) can deploy the same governance architecture.
#
# Usage:
#   terraform init
#   terraform plan -var-file="environments/dev.tfvars"
#   terraform apply -var-file="environments/dev.tfvars"
##############################################################################

terraform {
  required_version = ">= 1.7.0"
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
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
      Owner       = "palmer-consulting"
    }
  }
}

##############################################################################
# Data Sources
##############################################################################

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

##############################################################################
# KMS Customer Managed Key
# Encrypts DynamoDB, S3, CloudWatch Logs, SNS
##############################################################################

resource "aws_kms_key" "governance" {
  description             = "CMK for ${var.project_name} — encrypts all data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EnableRootAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchLogs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-cmk-${var.environment}"
  }
}

resource "aws_kms_alias" "governance" {
  name          = "alias/${var.project_name}-${var.environment}"
  target_key_id = aws_kms_key.governance.key_id
}

##############################################################################
# DynamoDB Audit Log Table
# Stores all inference audit records — no raw PHI, hashes only
# 90-day TTL enforced at item level for HIPAA retention compliance
##############################################################################

resource "aws_dynamodb_table" "audit_log" {
  name         = "${var.project_name}-audit-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "request_id"

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "request_id"
    type = "S"
  }

  # TTL — items expire after 90 days automatically
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Point-in-time recovery for audit integrity
  point_in_time_recovery {
    enabled = true
  }

  # Encryption at rest with CMK
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.governance.arn
  }

  deletion_protection_enabled = true

  tags = {
    Name        = "${var.project_name}-audit-${var.environment}"
    HIPAARelevant = "true"
  }
}

##############################################################################
# S3 Bucket — Drift Detection Baseline Embeddings
# Stores baseline centroid JSON used by drift_detector Lambda
# Chosen over OpenSearch Serverless — eliminates ~$350/month idle cost
##############################################################################

resource "aws_s3_bucket" "drift_baseline" {
  bucket = "${var.project_name}-drift-baselines-${var.environment}"

  tags = {
    Name    = "${var.project_name}-drift-baselines-${var.environment}"
    Purpose = "DriftDetectionBaseline"
  }
}

resource "aws_s3_bucket_versioning" "drift_baseline" {
  bucket = aws_s3_bucket.drift_baseline.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "drift_baseline" {
  bucket = aws_s3_bucket.drift_baseline.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.governance.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "drift_baseline" {
  bucket                  = aws_s3_bucket.drift_baseline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

##############################################################################
# SSM Parameter — Active Guardrail Version
# Allows zero-downtime guardrail rotation without code redeployment
##############################################################################

resource "aws_ssm_parameter" "guardrail_version" {
  name        = "/healthcare-ai-governance/bedrock/guardrail-version"
  type        = "String"
  value       = "DRAFT"
  description = "Active Bedrock Guardrail version — update to rotate without redeployment"

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}

##############################################################################
# CloudWatch Log Groups
# KMS encrypted, 90-day retention enforced by variable validation
##############################################################################

resource "aws_cloudwatch_log_group" "guardrail_proxy" {
  name              = "/aws/lambda/${var.project_name}-guardrail-proxy-${var.environment}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.governance.arn
}

resource "aws_cloudwatch_log_group" "drift_detector" {
  name              = "/aws/lambda/${var.project_name}-drift-detector-${var.environment}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.governance.arn
}

resource "aws_cloudwatch_log_group" "step_functions" {
  name              = "/aws/states/${var.project_name}-clinical-review-${var.environment}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.governance.arn
}

##############################################################################
# Outputs
##############################################################################

output "kms_key_arn" {
  description = "CMK ARN for encryption at rest"
  value       = aws_kms_key.governance.arn
}

output "audit_table_name" {
  description = "DynamoDB audit log table name"
  value       = aws_dynamodb_table.audit_log.name
}

output "drift_baseline_bucket" {
  description = "S3 bucket for drift detection baselines"
  value       = aws_s3_bucket.drift_baseline.bucket
}

output "guardrail_version_parameter" {
  description = "SSM parameter name for active guardrail version"
  value       = aws_ssm_parameter.guardrail_version.name
}
