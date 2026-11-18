"""Job files: one YAML file describes the sources, the AWS targets and how to reach them.

Secrets never go in the job file. Database URLs, tokens and passwords are read from
environment variables named in the file (``url_env``, ``token_env``, ``password_env``).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MARKINGS = ("PUBLIC", "CUI", "CUI//SP-PRVCY", "PCII", "FOUO")


class ConfigError(ValueError):
    pass


@dataclass
class AwsSettings:
    partition: str = "aws"            # aws | aws-us-gov
    region: str = "us-east-1"
    fips: bool = False                # use FIPS 140 validated endpoints
    profile: str | None = None
    endpoint_url: str | None = None   # set for a simulated environment (moto, LocalStack)
    kms_key: str | None = None        # key id, ARN or alias for SSE-KMS; None means SSE-S3

    def check(self) -> None:
        if self.partition not in ("aws", "aws-us-gov"):
            raise ConfigError("aws.partition must be aws or aws-us-gov")
        if self.partition == "aws-us-gov" and not self.region.startswith("us-gov-"):
            raise ConfigError(f"region {self.region} is not a GovCloud region")
        if self.partition == "aws" and self.region.startswith("us-gov-"):
            raise ConfigError("a GovCloud region needs partition aws-us-gov")


@dataclass
class Job:
    name: str
    marking: str
    aws: AwsSettings
    s3_bucket: str
    s3_prefix: str
    postgis_url_env: str | None
    postgis_schema: str
    sources: list[dict[str, Any]]
    network_mbps: float = 100.0
    network_utilization: float = 0.7
    state_dir: Path = field(default_factory=lambda: Path(".awsmig"))
    path: Path | None = None

    def source(self, name: str) -> dict[str, Any]:
        for s in self.sources:
            if s["name"] == name:
                return s
        raise ConfigError(f"no source named {name!r}")

    @property
    def postgis_url(self) -> str | None:
        if not self.postgis_url_env:
            return None
        url = os.environ.get(self.postgis_url_env)
        if not url:
            raise ConfigError(f"environment variable {self.postgis_url_env} is not set")
        return url


SOURCE_TYPES = {"gis_file", "database", "files", "sftp", "esri"}


def _expand(value: Any, base: Path) -> Any:
    if isinstance(value, str) and value.startswith("./"):
        return str((base / value).resolve())
    return value


def load_job(path: str | Path) -> Job:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError("job file must be a mapping")
    for key in ("job", "target", "sources"):
        if key not in raw:
            raise ConfigError(f"job file is missing '{key}'")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", raw["job"]):
        raise ConfigError("job name: lowercase letters, digits and hyphens")
    marking = raw.get("marking", "PUBLIC")
    if marking not in MARKINGS:
        raise ConfigError(f"marking must be one of {', '.join(MARKINGS)}")
    aws = AwsSettings(**(raw.get("aws") or {}))
    aws.check()
    if aws.endpoint_url is None and os.environ.get("AWSMIG_ENDPOINT_URL"):
        aws.endpoint_url = os.environ["AWSMIG_ENDPOINT_URL"]
    tgt = raw["target"]
    s3 = tgt.get("s3") or {}
    if "bucket" not in s3:
        raise ConfigError("target.s3.bucket is required")
    pg = tgt.get("postgis") or {}
    base = path.parent
    sources = []
    names = set()
    for s in raw["sources"]:
        if s.get("type") not in SOURCE_TYPES:
            raise ConfigError(f"source type must be one of {sorted(SOURCE_TYPES)}")
        if not s.get("name") or s["name"] in names:
            raise ConfigError("every source needs a unique name")
        names.add(s["name"])
        sources.append({k: _expand(v, base) for k, v in s.items()})
    net = raw.get("network") or {}
    return Job(name=raw["job"], marking=marking, aws=aws, s3_bucket=s3["bucket"],
               s3_prefix=(s3.get("prefix") or raw["job"]).strip("/"),
               postgis_url_env=pg.get("url_env"), postgis_schema=pg.get("schema", "landing"),
               sources=sources, network_mbps=float(net.get("mbps", 100)),
               network_utilization=float(net.get("utilization", 0.7)),
               state_dir=Path(raw.get("state_dir", base / ".awsmig")), path=path)
