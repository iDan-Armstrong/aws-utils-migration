"""End to end against a mocked AWS (moto), with every source type.

PostGIS is exercised when AWSMIG_TEST_PG points at a database with PostGIS available
(CI provides one); otherwise the run lands in S3 only.
"""
import json
import os
import socket
import sys
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from make_sample import build  # noqa: E402
from mock_arcgis import serve as serve_arcgis  # noqa: E402
from mock_sftp import serve as serve_sftp  # noqa: E402

from awsmig.config import load_job  # noqa: E402
from awsmig.run import Runner  # noqa: E402

REGION = "us-gov-west-1"
BUCKET = "awsmig-test-landing"


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    work = tmp_path_factory.mktemp("e2e")
    data = build(work / "sample")
    _, arc = serve_arcgis(data["gpkg"], ["wells", "well_pads", "leases"], port=_port(), token="t0ken", max_records=50)
    sport = _port()
    serve_sftp(data["sftp"], sport, "field", "pw-123")
    os.environ.update({"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "TEST_ARC_TOKEN": "t0ken",
                       "TEST_SFTP_PW": "pw-123", "TEST_LAND_DB": f"sqlite:///{data['sqlite']}"})
    job = {"job": "e2e-test", "marking": "CUI", "state_dir": str(work / "state"),
           "aws": {"partition": "aws-us-gov", "region": REGION, "fips": True, "kms_key": "alias/test"},
           "target": {"s3": {"bucket": BUCKET, "prefix": "landing"}},
           "sources": [
               {"name": "gis", "type": "gis_file", "path": data["gpkg"], "batch_size": 40},
               {"name": "land", "type": "database", "url_env": "TEST_LAND_DB", "batch_size": 50},
               {"name": "esri", "type": "esri", "layers": [f"{arc}/0", f"{arc}/1", f"{arc}/2"], "token_env": "TEST_ARC_TOKEN"},
               {"name": "share", "type": "files", "path": data["share"]},
               {"name": "drop", "type": "sftp", "host": "127.0.0.1", "port": sport, "username": "field", "password_env": "TEST_SFTP_PW", "path": "/"},
           ]}
    if os.environ.get("AWSMIG_TEST_PG"):
        job["target"]["postgis"] = {"url_env": "AWSMIG_TEST_PG", "schema": "awsmig_test"}
    p = work / "job.yml"
    p.write_text(yaml.safe_dump(job))
    return p


def _bucket():
    kms = boto3.client("kms", region_name=REGION)
    k = kms.create_key()["KeyMetadata"]["KeyId"]
    kms.create_alias(AliasName="alias/test", TargetKeyId=k)
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})


@mock_aws
def test_full_run_verifies_every_item(env):
    _bucket()
    job = load_job(env)
    r = Runner(job, log=lambda *_: None)
    items = r.discover()
    kinds = {i.source for i in items}
    assert kinds == {"gis", "land", "esri", "share", "drop"}
    routes = r.plan(items)
    res = r.run(items, routes)
    assert [x.item for x in res if x.status != "migrated"] == []
    v = r.verify(items)
    bad = {x.item: [c.name for c in x.checks if not c.ok] for x in v if x.status != "verified"}
    assert bad == {}
    # Esri paging: 128 leases at 50 per request, and attachments landed
    esri_leases = next(i for i in items if i.key == "esri/leases")
    assert esri_leases.rows == 128
    s3 = boto3.client("s3", region_name=REGION)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix="landing/esri/well_pads/attachments/")["Contents"]]
    assert len(keys) == 3
    head = s3.head_object(Bucket=BUCKET, Key=keys[0])
    assert head["Metadata"]["awsmig-marking"] == "CUI" and len(head["Metadata"]["awsmig-sha256"]) == 64
    assert head["ServerSideEncryption"] == "aws:kms"
    # sensitive columns were flagged
    owners = next(i for i in items if i.key == "land/owners")
    assert any("tax_id" in f for f in owners.flags)
    doc = r.manifest(items, routes, res, v)
    assert json.dumps(doc, default=str)
    r.close()


@mock_aws
def test_tampering_in_s3_is_caught(env, tmp_path):
    _bucket()
    job = load_job(env)
    job.state_dir = tmp_path / "state"
    r = Runner(job, log=lambda *_: None)
    items = [i for i in r.discover(["gis"]) if i.name == "leases"]
    r.run(items, r.plan(items))
    # change one value in one part
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq
    st = r.state.item(items[0].key)
    key = st["parts"]["0"]["key"]
    s3 = boto3.client("s3", region_name=REGION)
    t = pq.read_table(io.BytesIO(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()))
    royalty = t.column("royalty").to_pylist()
    royalty[0] = royalty[0] + 0.01
    t = t.set_column(t.schema.get_field_index("royalty"), "royalty", pa.array(royalty))
    buf = io.BytesIO()
    pq.write_table(t, buf)
    s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue())
    v = r.verify(items)[0]
    assert v.status == "mismatch"
    failed = [c for c in v.checks if not c.ok]
    assert failed and "royalty" in failed[0].note
    r.close()


@mock_aws
def test_interrupted_run_resumes(env, tmp_path, monkeypatch):
    _bucket()
    job = load_job(env)
    job.state_dir = tmp_path / "state"
    r = Runner(job, log=lambda *_: None)
    items = [i for i in r.discover(["gis"]) if i.name == "leases"]
    routes = r.plan(items)
    from awsmig.targets import s3 as s3mod
    real = s3mod.S3Target.put_parquet
    calls = {"n": 0}

    def flaky(self, key, table, source, geo=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ConnectionError("network dropped")
        return real(self, key, table, source, geo)

    monkeypatch.setattr(s3mod.S3Target, "put_parquet", flaky)
    assert r.run(items, routes)[0].status == "failed"
    assert len(r.state.item(items[0].key)["parts"]) == 2
    monkeypatch.setattr(s3mod.S3Target, "put_parquet", real)
    res = r.run(items, routes)[0]
    assert res.status == "migrated" and res.rows == items[0].rows
    assert r.verify(items)[0].status == "verified"
    r.close()
