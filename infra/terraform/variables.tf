variable "name" {
  type    = string
  default = "awsmig"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "region" {
  description = "us-gov-west-1 or us-gov-east-1 for GovCloud"
  type        = string
  default     = "us-gov-west-1"
}

variable "use_fips_endpoints" {
  description = "Use FIPS 140 validated endpoints for every AWS API call"
  type        = bool
  default     = true
}

variable "data_marking" {
  description = "Handling marking stamped on every resource, for example CUI"
  type        = string
  default     = "CUI"
}

variable "vpc_id" {
  type    = string
  default = null
}

variable "private_subnet_ids" {
  type    = list(string)
  default = []
}

variable "runner_principal_arns" {
  description = "IAM principals (for example an SSO role) allowed to assume the awsmig runner role"
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  type    = number
  default = 365
}

# ---- optional pieces ---------------------------------------------------------------

variable "enable_rds" {
  description = "Create the RDS for PostgreSQL target with PostGIS"
  type        = bool
  default     = false
}

variable "rds_instance_class" {
  type    = string
  default = "db.r6g.large"
}

variable "rds_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "allowed_db_cidrs" {
  description = "Networks allowed to reach the database (the runner and the DMS subnets)"
  type        = list(string)
  default     = []
}

variable "enable_dms" {
  description = "Create a DMS replication instance, endpoints and task for large or live databases"
  type        = bool
  default     = false
}

variable "dms_source_engine" {
  description = "postgres, sqlserver, oracle or mysql"
  type        = string
  default     = "sqlserver"
}

variable "dms_source_secret_arn" {
  description = "Secrets Manager secret holding the source database host, port, username and password"
  type        = string
  default     = null
}

variable "dms_source_database" {
  type    = string
  default = null
}

variable "dms_table_mappings" {
  description = "DMS table mapping JSON. The default copies every table in schema gis."
  type        = string
  default     = null
}

variable "enable_datasync" {
  description = "Create a DataSync task from an on-premises NFS or SMB share to the landing bucket"
  type        = bool
  default     = false
}

variable "datasync_agent_arns" {
  type    = list(string)
  default = []
}

variable "datasync_server" {
  type    = string
  default = null
}

variable "datasync_nfs_path" {
  type    = string
  default = "/exports/records"
}

variable "enable_transfer_family" {
  description = "Create an SFTP endpoint that writes to the landing bucket, for partners that keep sending files"
  type        = bool
  default     = false
}
