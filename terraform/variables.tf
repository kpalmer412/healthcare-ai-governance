##############################################################################
# healthcare-ai-governance / terraform / variables.tf
##############################################################################

variable "aws_region" {
    description = "AWS region for all resources"
    type        = string
    default     = "us-east-1"
}

variable "project_name" {
    description = "Project name — used for resource naming and tagging"
    type        = string
    default     = "healthcare-ai-governance"
}

variable "environment" {
    description = "Deployment environment"
    type        = string
    validation {
        condition     = contains(["dev", "staging", "prod"], var.environment)
        error_message = "environment must be dev, staging, or prod."
    }
}

variable "tags" {
    description = "Common tags for all resources"
    type        = map(string)
    default = {
        Project     = "healthcare-ai-governance"
        ManagedBy   = "Terraform"
    }
}