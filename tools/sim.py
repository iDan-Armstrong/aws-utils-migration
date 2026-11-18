"""Simulated AWS environment for trying awsmig end to end without an AWS account.

Starts:
  * moto server: S3 and KMS on a local endpoint, with a bucket set up the way the Terraform
    in infra/terraform sets up the real one (KMS default encryption, versioning, public access
    blocked, TLS-only policy)
  * PostgreSQL + PostGIS: an "on-premises" source database and an "RDS" target database
    (a local cluster, or the URLs in AWSMIG_SIM_SOURCE_DB / AWSMIG_SIM_TARGET_DB)
  * a mock ArcGIS REST FeatureServer with paging, a token and attachments
  * a mock SFTP server

Then writes a job file and, with `run`, executes `awsmig all` against it.

    python tools/sim.py run --data sample            # synthetic data built by make_sample.py
    python tools/sim.py run --data /path/og_aoi.gpkg  # your own GeoPackage as the GIS source
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

PG_BIN = Path(os.environ.get("PG_BIN", "/usr/lib/postgresql/16/bin"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port: int, timeout: float = 30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} did not open")


def start_moto(port: int):
    p = subprocess.Popen([sys.executable, "-m", "moto.server", "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_port(port)
    return p


def setup_aws(endpoint: str, region: str, bucket: str) -> str:
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "simulated")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "simulated")
    kms = boto3.client("kms", region_name=region, endpoint_url=endpoint)
    key = kms.create_key(Description="awsmig landing zone (simulated)")["KeyMetadata"]["KeyId"]
    kms.create_alias(AliasName="alias/awsmig-landing", TargetKeyId=key)
    s3 = boto3.client("s3", region_name=region, endpoint_url=endpoint)
    kw = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
    s3.create_bucket(Bucket=bucket, **kw)
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": key}, "BucketKeyEnabled": True}]})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={k: True for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    part = "aws-us-gov" if region.startswith("us-gov") else "aws"
    if not endpoint.startswith("https"):
        # moto enforces bucket policies, and the local endpoint is plain HTTP, so a TLS-only
        # policy would refuse every request here. The real bucket gets it from Terraform.
        return key
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:{part}:s3:::{bucket}", f"arn:{part}:s3:::{bucket}/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
    return key


def _as_pg(cmd: list[str], **kw):
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--"] + cmd
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def start_postgres(work: Path, port: int):
    if os.geteuid() == 0:
        # the postgres user must own the cluster and reach its folder
        work = Path(os.environ.get("AWSMIG_PG_DIR", "/var/lib/postgresql/awsmig-sim"))
    data = work / "pgdata"
    if data.exists():
        shutil.rmtree(data)
    work.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        shutil.chown(work, "postgres")
    _as_pg([str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"])
    _as_pg([str(PG_BIN / "pg_ctl"), "-D", str(data), "-o", f"-p {port} -k {work} -c listen_addresses=127.0.0.1", "-l", str(work / "pg.log"), "-w", "start"])
    for db in ("onprem_gis", "rds_target"):
        _as_pg([str(PG_BIN / "createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", db])
    return data


def stop_postgres(data: Path):
    try:
        _as_pg([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"])
    except Exception:
        pass


def seed_source_db(url: str, gpkg: str, layers: list[str], extra_csv: dict[str, str] | None = None):
    import geopandas as gpd
    import pandas as pd
    import sqlalchemy as sa
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("create extension if not exists postgis"))
        c.execute(sa.text("create schema if not exists gis"))
    for name in layers:
        df = gpd.read_file(gpkg, layer=name)
        df = df.rename_geometry("shape")
        df.insert(0, "objectid", range(1, len(df) + 1))
        df.to_postgis(name, eng, schema="gis", if_exists="replace", index=False)
        with eng.begin() as c:
            c.execute(sa.text(f'alter table gis."{name}" add primary key (objectid)'))
    for name, path in (extra_csv or {}).items():
        df = pd.read_csv(path)
        df.to_sql(name, eng, schema="gis", if_exists="replace", index=False)
        key = df.columns[0]
        if df[key].is_unique:
            with eng.begin() as c:
                c.execute(sa.text(f'alter table gis."{name}" add primary key ("{key}")'))
    eng.dispose()


def write_job(work: Path, endpoint: str, region: str, bucket: str, gpkg: str, sqlite: str | None, share: str,
              arcgis_url: str, arcgis_layers: list[int], sftp_port: int, sftp_root: str, gis_layers: list[str] | None,
              db_tables: list[str], job_name: str = "og-field-to-govcloud", cdc: bool = False) -> Path:
    import yaml
    part = "aws-us-gov" if region.startswith("us-gov") else "aws"
    sources = [
        {"name": "field_gis", "type": "gis_file", "path": gpkg, **({"layers": gis_layers} if gis_layers else {})},
        {"name": "onprem_geodb", "type": "database", "url_env": "AWSMIG_SOURCE_DB", "schemas": ["gis"], "tables": db_tables, **({"cdc": True} if cdc else {})},
    ]
    if sqlite:
        sources.append({"name": "land_records", "type": "database", "url_env": "AWSMIG_LAND_RECORDS_DB"})
    sources += [
        {"name": "esri_field_service", "type": "esri", "layers": [f"{arcgis_url}/{i}" for i in arcgis_layers], "token_env": "AWSMIG_ARCGIS_TOKEN"},
        {"name": "records_share", "type": "files", "path": share, **({"ongoing": True} if cdc else {})},
        {"name": "partner_sftp", "type": "sftp", "host": "127.0.0.1", "port": sftp_port, "username": "field", "password_env": "AWSMIG_SFTP_PASSWORD", "path": "/"},
    ]
    job = {"job": job_name, "marking": "CUI", "state_dir": str(work / "state"),
           "aws": {"partition": part, "region": region, "fips": True, "endpoint_url": endpoint, "kms_key": "alias/awsmig-landing"},
           "target": {"s3": {"bucket": bucket, "prefix": "landing/og-field"}, "postgis": {"url_env": "AWSMIG_TARGET_DB", "schema": "landing"}},
           "network": {"mbps": 200, "utilization": 0.6}, "sources": sources}
    p = work / "job.yml"
    p.write_text(yaml.safe_dump(job, sort_keys=False))
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "up"])
    ap.add_argument("--data", default="sample", help="'sample' or a GeoPackage path")
    ap.add_argument("--work", default=".sim")
    ap.add_argument("--region", default="us-gov-west-1")
    ap.add_argument("--job-name", default="og-field-to-govcloud")
    ap.add_argument("--cutover", action="store_true", help="mark the database as live (DMS + CDC) and the share as ongoing (DataSync)")
    ap.add_argument("--extra-csv", nargs="*", default=[], help="name=path CSV tables to add to the source database")
    a = ap.parse_args()
    work = Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    from make_sample import build
    from mock_arcgis import serve as serve_arcgis
    from mock_sftp import serve as serve_sftp
    sample = build(work / "sample")
    gpkg = sample["gpkg"] if a.data == "sample" else a.data
    if a.data == "sample":
        db_layers, esri_layers = ["mineral_tracts", "leases"], ["wells", "well_pads", "leases"]
    else:
        db_layers = ["mineral_tracts", "lease_status", "drilling_units", "gathering_lines", "seismic_permits"]
        esri_layers = ["wells", "well_pads", "leases", "pipeline_stations"]
    procs, pg = [], None
    try:
        mport = free_port()
        procs.append(start_moto(mport))
        endpoint = f"http://127.0.0.1:{mport}"
        bucket = "awsmig-landing-sim"
        setup_aws(endpoint, a.region, bucket)
        if os.environ.get("AWSMIG_SIM_SOURCE_DB"):
            src_url, tgt_url = os.environ["AWSMIG_SIM_SOURCE_DB"], os.environ["AWSMIG_SIM_TARGET_DB"]
        else:
            port = free_port()
            pg = start_postgres(work / "pg", port)
            src_url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/onprem_gis"
            tgt_url = f"postgresql://postgres@127.0.0.1:{port}/rds_target"
        extra = dict(x.split("=", 1) for x in a.extra_csv)
        seed_source_db(src_url, gpkg, db_layers, extra)
        token = "sim-token-" + os.urandom(4).hex()
        srv, arc_url = serve_arcgis(gpkg, esri_layers, port=free_port(), token=token, max_records=1000)
        sport = free_port()
        sftp_pw = "sim-" + os.urandom(6).hex()
        serve_sftp(sample["sftp"], sport, "field", sftp_pw)
        env = {"AWSMIG_SOURCE_DB": src_url, "AWSMIG_TARGET_DB": tgt_url, "AWSMIG_LAND_RECORDS_DB": f"sqlite:///{sample['sqlite']}",
               "AWSMIG_ARCGIS_TOKEN": token, "AWSMIG_SFTP_PASSWORD": sftp_pw, "AWS_ACCESS_KEY_ID": "simulated", "AWS_SECRET_ACCESS_KEY": "simulated"}
        os.environ.update(env)
        job = write_job(work, endpoint, a.region, bucket, gpkg, sample["sqlite"], sample["share"], arc_url, list(range(len(esri_layers))),
                        sport, sample["sftp"], None, ["*"], a.job_name, a.cutover)
        print(f"Simulated environment ready. Job file: {job}")
        if a.cmd == "up":
            print("\n".join(f"export {k}='{v}'" for k, v in env.items()))
            print("Press Ctrl+C to stop.")
            while True:
                time.sleep(3600)
        from awsmig.cli import main as cli
        code = cli(["all", str(job)])
        print(f"awsmig exit code {code}")
        return code
    finally:
        for p in procs:
            p.terminate()
        if pg:
            stop_postgres(pg)


if __name__ == "__main__":
    sys.exit(main())
