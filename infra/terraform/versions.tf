# Landing zone and managed migration services for awsmig.
# Written to plan and validate; nothing here has been applied.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

provider "aws" {
  region            = var.region
  use_fips_endpoint = var.use_fips_endpoints

  default_tags {
    tags = {
      Project     = "awsmig"
      Environment = var.environment
      DataMarking = var.data_marking
      ManagedBy   = "terraform"
    }
  }
}

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  name      = "${var.name}-${var.environment}"
  partition = data.aws_partition.current.partition
  account   = data.aws_caller_identity.current.account_id
}
