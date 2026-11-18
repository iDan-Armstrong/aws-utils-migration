# AWS Transfer Family SFTP endpoint that writes straight into the landing bucket.

resource "aws_transfer_server" "sftp" {
  count                  = var.enable_transfer_family ? 1 : 0
  identity_provider_type = "SERVICE_MANAGED"
  protocols              = ["SFTP"]
  domain                 = "S3"
  endpoint_type          = "PUBLIC"
  security_policy_name   = "TransferSecurityPolicy-FIPS-2025-03"
  logging_role           = aws_iam_role.transfer_logging[0].arn
}

data "aws_iam_policy_document" "transfer_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["transfer.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "transfer_logging" {
  count              = var.enable_transfer_family ? 1 : 0
  name               = "${local.name}-transfer-logging"
  assume_role_policy = data.aws_iam_policy_document.transfer_trust.json
}

resource "aws_iam_role_policy_attachment" "transfer_logging" {
  count      = var.enable_transfer_family ? 1 : 0
  role       = aws_iam_role.transfer_logging[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSTransferLoggingAccess"
}
