"""Choose how each item gets into AWS, and say why.

The rules are deliberately plain so a reviewer can argue with them. awsmig moves what it
can move well itself (GIS layers, one-time database copies of moderate size, file folders
that fit the network, Esri services). Anything better served by an AWS managed service is
routed there with the steps to set it up, and the Terraform in infra/ creates that service.

Facts behind the rules (checked against AWS documentation, November 2026):
* AWS Snowball Edge is no longer available to new customers. AWS points new customers to
  AWS DataSync for online transfer, AWS Data Transfer Terminal or AWS Partner devices for
  physical transfer.
* AWS DMS migrates PostGIS geometry between PostgreSQL engines and Oracle SDO_GEOMETRY to
  PostGIS. It treats geometry as LOBs, needs a primary or unique key on the source table,
  nullable geometry columns on the target, and PostGIS installed on the target first.
"""
from __future__ import annotations

from .config import Job
from .model import Item, Route

GB = 1024 ** 3
TB = 1024 ** 4


def transfer_hours(nbytes: int | None, job: Job) -> float | None:
    if not nbytes:
        return None
    bits_per_sec = job.network_mbps * 1e6 * job.network_utilization
    return nbytes * 8 / bits_per_sec / 3600


def route(item: Item, src: dict, job: Job, engine: str | None = None) -> Route:
    hrs = transfer_hours(item.bytes, job)
    t = src["type"]
    gov = job.aws.partition == "aws-us-gov"
    reasons, steps = [], []
    if item.flags:
        reasons.append(f"{len(item.flags)} column(s) look like personal or controlled data; confirm the marking ({job.marking}) before moving")

    if t in ("gis_file", "esri"):
        reasons.append("GIS layer: awsmig keeps geometry exact (WKB), records the coordinate system, and verifies every row")
        if t == "esri":
            reasons.append(f"service is paged by object ID in blocks of {item.extra.get('max_records', 1000)}, so the server limit cannot truncate it")
            if item.extra.get("has_attachments"):
                reasons.append("attachments are copied to S3 under attachments/<layer>/<objectid>/")
        steps = ["Write GeoParquet to the S3 landing zone (SSE-KMS, SHA-256 checksums, marking tag)",
                 "Load into RDS PostGIS in one transaction with a spatial index",
                 "Verify row count, content fingerprint, geometry validity and extent against the source"]
        return Route(item.key, "awsmig: GeoParquet to S3 + load to RDS PostGIS", "Amazon S3, Amazon RDS for PostgreSQL", True, hrs, reasons, steps)

    if t == "database":
        size = item.bytes or 0
        homogeneous = engine == "postgresql"
        if size > 50 * GB or src.get("cdc"):
            reasons.append(f"table is {size / GB:.0f} GB" if size > 50 * GB else "source must stay live during cutover (cdc: true)")
            if homogeneous:
                reasons.append("PostgreSQL to PostgreSQL: AWS DMS full load + change data capture keeps downtime to the cutover window; native pg_dump/pg_restore is simpler if a maintenance window allows")
            else:
                reasons.append(f"{engine} to PostgreSQL: convert the schema with DMS Schema Conversion (or AWS SCT), then DMS full load + CDC")
            if item.geometry:
                reasons.append("spatial column: DMS moves geometry as LOBs; needs a primary or unique key, nullable target geometry, PostGIS installed first")
            steps = ["terraform apply infra/terraform with enable_dms = true",
                     "Pre-create the target tables (geometry nullable) or let DMS create them",
                     "Start the task: full load, then ongoing replication until cutover",
                     "Run `awsmig verify` at cutover to compare counts and fingerprints"]
            return Route(item.key, "AWS DMS full load + CDC", "AWS Database Migration Service", False, hrs, reasons, steps)
        reasons.append(f"{size / GB:.2f} GB one-time copy: awsmig pages by primary key, carries geometry as WKB, and verifies the result"
                       if size else "one-time copy: awsmig pages by primary key, carries geometry as WKB, and verifies the result")
        if not item.primary_key:
            reasons.append("no primary key: rows are streamed in one pass, so a failed run restarts this table from the beginning")
        steps = ["Snapshot to GeoParquet in the S3 landing zone", "Load into RDS PostGIS (atomic swap)", "Verify fingerprints, column by column"]
        return Route(item.key, "awsmig: database copy to RDS PostGIS + Parquet snapshot", "Amazon RDS for PostgreSQL, Amazon S3", True, hrs, reasons, steps)

    if t in ("files", "sftp"):
        size = item.bytes or 0
        if hrs and hrs > 7 * 24:
            reasons.append(f"about {hrs / 24:.0f} days over a {job.network_mbps:g} Mbps link at {job.network_utilization:.0%} use")
            reasons.append("AWS Snowball Edge is no longer offered to new customers; use AWS Data Transfer Terminal or an AWS Partner device, or add AWS Direct Connect for DataSync"
                           + (" (check which options serve GovCloud)" if gov else ""))
            steps = ["Copy to encrypted drives and upload at an AWS Data Transfer Terminal, or order a Partner device",
                     "Run `awsmig verify` against S3 when the upload lands"]
            return Route(item.key, "Offline transfer (Data Transfer Terminal or Partner device)", "AWS Data Transfer Terminal", False, hrs, reasons, steps)
        if size > TB or (item.rows or 0) > 1_000_000 or src.get("ongoing"):
            reasons.append("large share or ongoing sync: AWS DataSync runs parallel, incremental transfers with built-in integrity checks")
            steps = ["Deploy a DataSync agent near the share", "terraform apply with enable_datasync = true", "Schedule the task; verify with `awsmig verify`"]
            return Route(item.key, "AWS DataSync", "AWS DataSync", False, hrs, reasons, steps)
        if t == "sftp" and src.get("ongoing"):
            reasons.append("partners keep sending files: AWS Transfer Family gives them an SFTP endpoint that writes straight to S3")
        reasons.append(f"{item.rows} files, {size / GB:.2f} GB: direct multipart upload with per-file SHA-256")
        steps = ["Upload each file to S3 (SSE-KMS, SHA-256 checksum, marking tag)", "Skip files already uploaded with the same hash", "Verify size and hash for every file"]
        return Route(item.key, "awsmig: upload to S3", "Amazon S3", True, hrs, reasons, steps)

    return Route(item.key, "manual review", "n/a", False, hrs, ["unrecognized source"], [])
