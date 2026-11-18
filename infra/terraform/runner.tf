# Role the awsmig command line runs as: write to the landing prefix, use the key, nothing else.

data "aws_iam_policy_document" "runner_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = length(var.runner_principal_arns) > 0 ? var.runner_principal_arns : ["arn:${local.partition}:iam::${local.account}:root"]
    }
  }
}

resource "aws_iam_role" "runner" {
  name                 = "${local.name}-runner"
  assume_role_policy   = data.aws_iam_policy_document.runner_trust.json
  max_session_duration = 43200
}

data "aws_iam_policy_document" "runner" {
  statement {
    sid       = "ListLanding"
    actions   = ["s3:ListBucket", "s3:GetBucketVersioning", "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock"]
    resources = [aws_s3_bucket.landing.arn]
  }
  statement {
    sid       = "ReadWriteLanding"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:PutObjectTagging", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${aws_s3_bucket.landing.arn}/*"]
  }
  statement {
    sid       = "UseLandingKey"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.landing.arn]
  }
  dynamic "statement" {
    for_each = var.enable_rds ? [1] : []
    content {
      sid       = "ConnectToRdsWithIam"
      actions   = ["rds-db:connect"]
      resources = ["arn:${local.partition}:rds-db:${var.region}:${local.account}:dbuser:${aws_db_instance.target[0].resource_id}/awsmig_loader"]
    }
  }
}

resource "aws_iam_role_policy" "runner" {
  name   = "awsmig-runner"
  role   = aws_iam_role.runner.id
  policy = data.aws_iam_policy_document.runner.json
}
