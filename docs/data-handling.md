# Data handling: CUI, GovCloud and the audit trail

This repository holds code and synthetic data only. Real migrations of controlled data run
inside an authorized environment, under that system's security plan.

## What awsmig does by default

* **Marking.** The job's marking (for example `CUI`) is written to every S3 object's metadata
  and tags, to each PostGIS table's comment, and to the lineage table, so the marking travels
  with the data.
* **Personal data scan.** Discovery checks column names and sampled values for names,
  addresses, emails, phone numbers, tax or Social Security numbers and account numbers. The
  findings appear in the plan and the report so a person confirms the marking (for example
  `CUI//SP-PRVCY`) before the run.
* **Encryption in transit.** Every AWS call uses TLS; the Terraform bucket policy refuses plain
  HTTP and uploads without KMS encryption. With `fips: true` the SDK uses FIPS 140 validated
  endpoints. PostgreSQL connections should use `sslmode=verify-full`; the RDS parameter group
  forces SSL.
* **Encryption at rest.** S3 objects use SSE-KMS with the landing key (rotation on). RDS
  storage, Performance Insights, DMS and DataSync logs use the same key.
* **No secrets in files.** Job files name environment variables; database URLs, tokens and
  passwords come from the environment, ideally filled from AWS Secrets Manager or an IAM
  Identity Center session. RDS uses IAM database authentication for the loader role.
* **Least privilege.** The runner role can list the landing bucket, read and write objects in
  it, use the landing key, and connect to the database as `awsmig_loader`. Nothing else.
* **Audit trail.** `audit.jsonl` records who ran what, when, under which marking, with counts
  and outcomes. The run manifest and HTML report are copied to `_awsmig/manifests/` in S3.
  Pair it with CloudTrail data events on the landing bucket for an independent record.

## GovCloud notes

* Set `aws.partition: aws-us-gov` and a GovCloud region; awsmig refuses a mismatched pair.
* ARNs in the Terraform use the `aws_partition` data source, so the same code serves both
  partitions.
* Check that each managed service you route to is offered in your GovCloud region and inside
  your system's authorization boundary before planning around it.

## Mapping to NIST SP 800-53 control families (for the security plan author)

| Family | Where awsmig or its Terraform helps |
|---|---|
| AC Access control | Runner role, IAM database authentication, bucket policy |
| AU Audit and accountability | audit.jsonl, run manifests in S3, S3 access logs, RDS and DataSync logs |
| CM Configuration management | Terraform as the configuration baseline; job files under version control |
| IA Identification and authentication | Role assumption, no long-lived keys in job files |
| MP Media protection | Marking on objects and tables; KMS at rest |
| SC System and communications protection | TLS-only policy, FIPS endpoints, SSE-KMS |
| SI System and information integrity | Fingerprint and checksum verification of every item |

This table helps the person writing the security plan. It is not an assessment or an
authorization.
