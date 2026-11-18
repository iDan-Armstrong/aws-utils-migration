# AWS DMS for databases that are large or must stay live until cutover (full load + CDC).
# Geometry note: DMS moves PostGIS and Oracle SDO_GEOMETRY as LOBs. Source tables need a primary
# or unique key, target geometry columns must be nullable, and PostGIS must exist on the target.

resource "aws_dms_replication_subnet_group" "this" {
  count                                = var.enable_dms ? 1 : 0
  replication_subnet_group_id          = "${local.name}-dms"
  replication_subnet_group_description = "awsmig DMS subnets"
  subnet_ids                           = var.private_subnet_ids
}

resource "aws_dms_replication_instance" "this" {
  count                       = var.enable_dms ? 1 : 0
  replication_instance_id     = "${local.name}-dms"
  replication_instance_class  = "dms.r5.large"
  allocated_storage           = 200
  kms_key_arn                 = aws_kms_key.landing.arn
  publicly_accessible         = false
  multi_az                    = var.environment == "prod"
  replication_subnet_group_id = aws_dms_replication_subnet_group.this[0].id
  vpc_security_group_ids      = var.enable_rds ? [aws_security_group.db[0].id] : []
}

data "aws_iam_policy_document" "dms_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["dms.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "dms_secrets" {
  count              = var.enable_dms ? 1 : 0
  name               = "${local.name}-dms-secrets"
  assume_role_policy = data.aws_iam_policy_document.dms_trust.json
}

resource "aws_iam_role_policy" "dms_secrets" {
  count = var.enable_dms ? 1 : 0
  role  = aws_iam_role.dms_secrets[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = compact([var.dms_source_secret_arn, var.enable_rds ? aws_db_instance.target[0].master_user_secret[0].secret_arn : ""])
      }, {
      Effect   = "Allow"
      Action   = ["kms:Decrypt"]
      Resource = [aws_kms_key.landing.arn]
    }]
  })
}

resource "aws_dms_endpoint" "source" {
  count                           = var.enable_dms ? 1 : 0
  endpoint_id                     = "${local.name}-source"
  endpoint_type                   = "source"
  engine_name                     = var.dms_source_engine
  database_name                   = var.dms_source_database
  secrets_manager_arn             = var.dms_source_secret_arn
  secrets_manager_access_role_arn = aws_iam_role.dms_secrets[0].arn
  ssl_mode                        = "require"
  kms_key_arn                     = aws_kms_key.landing.arn
}

resource "aws_dms_endpoint" "target" {
  count                           = var.enable_dms && var.enable_rds ? 1 : 0
  endpoint_id                     = "${local.name}-target"
  endpoint_type                   = "target"
  engine_name                     = "postgres"
  database_name                   = "postgres"
  secrets_manager_arn             = aws_db_instance.target[0].master_user_secret[0].secret_arn
  secrets_manager_access_role_arn = aws_iam_role.dms_secrets[0].arn
  ssl_mode                        = "require"
  kms_key_arn                     = aws_kms_key.landing.arn
}

resource "aws_dms_replication_task" "full_load_cdc" {
  count                    = var.enable_dms && var.enable_rds ? 1 : 0
  replication_task_id      = "${local.name}-full-load-cdc"
  migration_type           = "full-load-and-cdc"
  replication_instance_arn = aws_dms_replication_instance.this[0].replication_instance_arn
  source_endpoint_arn      = aws_dms_endpoint.source[0].endpoint_arn
  target_endpoint_arn      = aws_dms_endpoint.target[0].endpoint_arn
  start_replication_task   = false
  table_mappings = coalesce(var.dms_table_mappings, jsonencode({
    rules = [{
      rule-type      = "selection"
      rule-id        = "1"
      rule-name      = "all-gis"
      object-locator = { schema-name = "gis", table-name = "%" }
      rule-action    = "include"
    }]
  }))
  replication_task_settings = jsonencode({
    TargetMetadata = {
      SupportLobs        = true
      FullLobMode        = false
      LimitedSizeLobMode = true
      LobMaxSize         = 2048
    }
    FullLoadSettings = { TargetTablePrepMode = "DO_NOTHING" }
    Logging          = { EnableLogging = true }
    ValidationSettings = {
      EnableValidation = true
      ValidationMode   = "ROW_LEVEL"
    }
  })
}
