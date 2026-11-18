# RDS for PostgreSQL target with PostGIS. Encrypted, TLS only, IAM authentication, backups.
# After apply, connect once and run: create extension postgis; create role awsmig_loader login; grant rds_iam to awsmig_loader;

resource "aws_db_subnet_group" "target" {
  count      = var.enable_rds ? 1 : 0
  name       = "${local.name}-target"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "db" {
  count       = var.enable_rds ? 1 : 0
  name        = "${local.name}-db"
  description = "PostgreSQL from the migration runner and DMS"
  vpc_id      = var.vpc_id

  ingress {
    description = "PostgreSQL"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.allowed_db_cidrs
  }
}

resource "aws_db_parameter_group" "pg16" {
  count  = var.enable_rds ? 1 : 0
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
}

resource "aws_db_instance" "target" {
  count                               = var.enable_rds ? 1 : 0
  identifier                          = "${local.name}-target"
  engine                              = "postgres"
  engine_version                      = "16"
  instance_class                      = var.rds_instance_class
  allocated_storage                   = var.rds_allocated_storage_gb
  max_allocated_storage               = var.rds_allocated_storage_gb * 4
  storage_type                        = "gp3"
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.landing.arn
  username                            = "awsmig_admin"
  manage_master_user_password         = true
  iam_database_authentication_enabled = true
  db_subnet_group_name                = aws_db_subnet_group.target[0].name
  vpc_security_group_ids              = [aws_security_group.db[0].id]
  parameter_group_name                = aws_db_parameter_group.pg16[0].name
  publicly_accessible                 = false
  multi_az                            = var.environment == "prod"
  backup_retention_period             = 14
  deletion_protection                 = true
  copy_tags_to_snapshot               = true
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.landing.arn
  final_snapshot_identifier           = "${local.name}-target-final"
  auto_minor_version_upgrade          = true
}
