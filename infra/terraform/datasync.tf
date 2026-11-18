# AWS DataSync from an on-premises NFS share to the landing bucket, for shares too large or
# too busy for a one-time upload. Needs a DataSync agent deployed next to the share first.

data "aws_iam_policy_document" "datasync_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["datasync.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "datasync" {
  count              = var.enable_datasync ? 1 : 0
  name               = "${local.name}-datasync"
  assume_role_policy = data.aws_iam_policy_document.datasync_trust.json
}

resource "aws_iam_role_policy" "datasync" {
  count = var.enable_datasync ? 1 : 0
  role  = aws_iam_role.datasync[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"]
      Resource = [aws_s3_bucket.landing.arn]
      }, {
      Effect   = "Allow"
      Action   = ["s3:AbortMultipartUpload", "s3:DeleteObject", "s3:GetObject", "s3:ListMultipartUploadParts", "s3:PutObject", "s3:GetObjectTagging", "s3:PutObjectTagging"]
      Resource = ["${aws_s3_bucket.landing.arn}/*"]
      }, {
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
      Resource = [aws_kms_key.landing.arn]
    }]
  })
}

resource "aws_datasync_location_nfs" "share" {
  count           = var.enable_datasync ? 1 : 0
  server_hostname = var.datasync_server
  subdirectory    = var.datasync_nfs_path
  on_prem_config {
    agent_arns = var.datasync_agent_arns
  }
}

resource "aws_datasync_location_s3" "landing" {
  count         = var.enable_datasync ? 1 : 0
  s3_bucket_arn = aws_s3_bucket.landing.arn
  subdirectory  = "/landing/datasync"
  s3_config {
    bucket_access_role_arn = aws_iam_role.datasync[0].arn
  }
}

resource "aws_cloudwatch_log_group" "datasync" {
  count             = var.enable_datasync ? 1 : 0
  name              = "/aws/datasync/${local.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.landing.arn
}

resource "aws_datasync_task" "share" {
  count                    = var.enable_datasync ? 1 : 0
  name                     = "${local.name}-share"
  source_location_arn      = aws_datasync_location_nfs.share[0].arn
  destination_location_arn = aws_datasync_location_s3.landing[0].arn
  cloudwatch_log_group_arn = aws_cloudwatch_log_group.datasync[0].arn
  options {
    verify_mode            = "POINT_IN_TIME_CONSISTENT"
    transfer_mode          = "CHANGED"
    preserve_deleted_files = "PRESERVE"
    log_level              = "TRANSFER"
  }
}
