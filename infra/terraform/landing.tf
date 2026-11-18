# ---- KMS key for everything at rest ----------------------------------------------

resource "aws_kms_key" "landing" {
  description             = "${local.name} landing zone"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "landing" {
  name          = "alias/${local.name}-landing"
  target_key_id = aws_kms_key.landing.key_id
}

# ---- access log bucket -------------------------------------------------------------

resource "aws_s3_bucket" "logs" {
  bucket = "${local.name}-access-logs-${local.account}"
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

# ---- landing bucket ----------------------------------------------------------------

resource "aws_s3_bucket" "landing" {
  bucket = "${local.name}-landing-${local.account}"
}

resource "aws_s3_bucket_public_access_block" "landing" {
  bucket                  = aws_s3_bucket.landing.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "landing" {
  bucket = aws_s3_bucket.landing.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.landing.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_logging" "landing" {
  bucket        = aws_s3_bucket.landing.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "landing/"
}

resource "aws_s3_bucket_lifecycle_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

data "aws_iam_policy_document" "landing" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.landing.arn, "${aws_s3_bucket.landing.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "DenyUnencryptedUploads"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.landing.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

resource "aws_s3_bucket_policy" "landing" {
  bucket     = aws_s3_bucket.landing.id
  policy     = data.aws_iam_policy_document.landing.json
  depends_on = [aws_s3_bucket_public_access_block.landing]
}
