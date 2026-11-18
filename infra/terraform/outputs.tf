output "landing_bucket" {
  value = aws_s3_bucket.landing.bucket
}

output "kms_key_alias" {
  value = aws_kms_alias.landing.name
}

output "runner_role_arn" {
  value = aws_iam_role.runner.arn
}

output "rds_endpoint" {
  value = var.enable_rds ? aws_db_instance.target[0].endpoint : null
}

output "sftp_endpoint" {
  value = var.enable_transfer_family ? aws_transfer_server.sftp[0].endpoint : null
}
