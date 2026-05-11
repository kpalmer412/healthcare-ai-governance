##############################################################################
# healthcare-ai-governance / terraform / backend.tf
##############################################################################

terraform {
  backend "s3" {
    bucket  = "terraform-state-hcls-ai-governance-491074939705"
    key     = "healthcare-ai-governance/terraform.tfstate"
    region  = "us-east-1"
    encrypt = true
  }
}
