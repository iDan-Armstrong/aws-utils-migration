"""Amazon S3 landing zone: encrypted, checksummed, tagged with the data marking."""
from __future__ import annotations

import base64
import hashlib
import io
import json

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from ..config import Job

MB = 1024 * 1024


def session(job: Job):
    return boto3.Session(profile_name=job.aws.profile) if job.aws.profile else boto3.Session()


def client(job: Job, service: str = "s3"):
    cfg = Config(region_name=job.aws.region, retries={"max_attempts": 10, "mode": "adaptive"},
                 use_fips_endpoint=bool(job.aws.fips and not job.aws.endpoint_url),
                 s3={"addressing_style": "path"} if job.aws.endpoint_url else None)
    return session(job).client(service, config=cfg, endpoint_url=job.aws.endpoint_url)


def geoparquet_metadata(table: pa.Table, geom: str, geometry_type: str | None, srid: int | None, bbox=None) -> dict:
    col = {"encoding": "WKB", "geometry_types": []}
    if srid and srid != 4326:
        try:
            from pyproj import CRS
            col["crs"] = CRS.from_epsg(srid).to_json_dict()
        except Exception:
            col["crs"] = {"id": {"authority": "EPSG", "code": srid}}
    if bbox:
        col["bbox"] = bbox
    return {"version": "1.1.0", "primary_column": geom, "columns": {geom: col}}


class S3Target:
    def __init__(self, job: Job):
        self.job = job
        self.bucket = job.s3_bucket
        self.s3 = client(job)
        self.transfer = TransferConfig(multipart_threshold=64 * MB, multipart_chunksize=64 * MB, max_concurrency=8)

    def _extra(self, sha256_hex: str | None, source: str, content_type: str | None = None) -> dict:
        extra = {"Metadata": {"awsmig-job": self.job.name, "awsmig-source": source[:1000], "awsmig-marking": self.job.marking},
                 "Tagging": f"marking={self.job.marking.replace('/', '-')}&job={self.job.name}",
                 "ChecksumAlgorithm": "SHA256"}
        if sha256_hex:
            extra["Metadata"]["awsmig-sha256"] = sha256_hex
        if self.job.aws.kms_key:
            extra.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self.job.aws.kms_key)
        else:
            extra["ServerSideEncryption"] = "AES256"
        if content_type:
            extra["ContentType"] = content_type
        return extra

    def key(self, *parts: str) -> str:
        return "/".join([self.job.s3_prefix, *[p.strip("/") for p in parts if p]])

    def put_bytes(self, key: str, data: bytes, source: str, content_type: str | None = None) -> dict:
        sha = hashlib.sha256(data).hexdigest()
        self.s3.upload_fileobj(io.BytesIO(data), self.bucket, key, ExtraArgs=self._extra(sha, source, content_type), Config=self.transfer)
        return {"key": key, "bytes": len(data), "sha256": sha}

    def put_stream(self, key: str, fileobj, source: str, sha256_hex: str, size: int) -> dict:
        self.s3.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=self._extra(sha256_hex, source), Config=self.transfer)
        return {"key": key, "bytes": size, "sha256": sha256_hex}

    def put_parquet(self, key: str, table: pa.Table, source: str, geo: dict | None = None) -> dict:
        if geo:
            md = dict(table.schema.metadata or {})
            md[b"geo"] = json.dumps(geo).encode()
            table = table.replace_schema_metadata(md)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd")
        return self.put_bytes(key, buf.getvalue(), source, "application/vnd.apache.parquet")

    def put_json(self, key: str, obj, source: str = "awsmig") -> dict:
        return self.put_bytes(key, json.dumps(obj, indent=1, default=str).encode(), source, "application/json")

    def head(self, key: str) -> dict | None:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except self.s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def read_parquet(self, key: str) -> pa.Table:
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return pq.read_table(io.BytesIO(body))

    def stream_sha256(self, key: str) -> str:
        h = hashlib.sha256()
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        for chunk in iter(lambda: body.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()

    def list(self, prefix: str) -> list[dict]:
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += r.get("Contents", [])
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]

    def bucket_posture(self) -> dict:
        """Report the bucket settings a reviewer will ask about: encryption, versioning, public access."""
        out = {}
        try:
            enc = self.s3.get_bucket_encryption(Bucket=self.bucket)["ServerSideEncryptionConfiguration"]["Rules"][0]
            out["default_encryption"] = enc["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        except Exception:
            out["default_encryption"] = "not set"
        try:
            out["versioning"] = self.s3.get_bucket_versioning(Bucket=self.bucket).get("Status", "Disabled")
        except Exception:
            out["versioning"] = "unknown"
        try:
            pab = self.s3.get_public_access_block(Bucket=self.bucket)["PublicAccessBlockConfiguration"]
            out["public_access_blocked"] = all(pab.values())
        except Exception:
            out["public_access_blocked"] = False
        return out


def b64_to_hex(b64: str) -> str:
    return base64.b64decode(b64).hex()
