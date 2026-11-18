# Which route into AWS

awsmig handles one-time moves it can verify end to end. For anything that has to keep
running, is too big for the link, or has a managed AWS service built for it, the plan hands
the item to that service and the Terraform in `infra/terraform` creates it.

| Situation | Route | Why |
|---|---|---|
| GIS files and Esri services | awsmig: GeoParquet to S3, then RDS PostGIS | Geometry stays exact, the coordinate system is recorded, every row is verified. Esri layers are paged by object ID so the server limit cannot truncate them. |
| Database table, one-time copy, up to about 50 GB | awsmig: database copy | Pages by primary key, carries geometry as WKB, swaps the finished table into place atomically, verifies by fingerprint. |
| Database too large, or must stay live until cutover | AWS DMS full load + CDC | Ongoing replication keeps downtime to the cutover window. DMS data validation can run alongside. |
| Different source engine (SQL Server, Oracle, MySQL) | DMS Schema Conversion or AWS SCT, then DMS | Converts tables, views and code; DMS then moves the data. |
| Files that fit the link | awsmig: upload to S3 | Multipart upload, SHA-256 per file, KMS encryption, marking tag. |
| File share over about 1 TB, millions of files, or an ongoing sync | AWS DataSync | Parallel, incremental, with integrity checks; one task can fill a 10 Gbps link. |
| Partners keep sending files | AWS Transfer Family (SFTP into S3) | A managed SFTP endpoint, with a FIPS security policy, that writes straight into the landing bucket. |
| More than a week over the available link | Offline transfer | AWS Snowball Edge is no longer available to new customers. AWS points new customers to AWS Data Transfer Terminal or AWS Partner devices, or to Direct Connect plus DataSync. |

## Spatial data and DMS

AWS DMS can move PostGIS geometry between PostgreSQL engines and Oracle `SDO_GEOMETRY` into
PostGIS. It treats geometry columns as LOBs. Source tables need a primary or unique key, target
geometry columns must be nullable (restore `NOT NULL` afterwards), and PostGIS must be
installed on the target before the task starts. Size the LOB settings to the largest geometry.

Sources:
[AWS DMS with spatial data between PostgreSQL engines](https://aws.amazon.com/blogs/database/replicate-spatial-data-using-aws-dms-and-amazon-rds-for-postgresql),
[Oracle spatial columns to PostGIS with DMS](https://aws.amazon.com/blogs/database/migrate-spatial-columns-from-oracle-to-amazon-aurora-postgresql-or-amazon-rds-for-postgresql-using-aws-dms),
[Snowball Edge availability change](https://docs.aws.amazon.com/snowball/latest/developer-guide/snowball-edge-availability-change.html).

## The time estimate

`hours = bytes × 8 ÷ (link Mbps × 1,000,000 × share of the link awsmig may use) ÷ 3600`

Set `network.mbps` to what the site can actually push to AWS, not the contract speed, and
`network.utilization` to the share that will not disrupt the office.
