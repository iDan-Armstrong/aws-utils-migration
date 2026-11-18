"""Core data structures shared by sources, targets, planning and verification."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Column:
    name: str
    type: str                 # normalized type: integer, float, text, boolean, date, datetime, binary, geometry, json
    source_type: str = ""     # the type as the source reported it
    nullable: bool = True


@dataclass
class Item:
    """One thing to migrate: a table, a GIS layer, a file, or a folder of files."""
    source: str               # name of the source block in the job file
    name: str                 # table, layer or relative path
    kind: str                 # table | layer | file
    rows: int | None = None
    bytes: int | None = None
    columns: list[Column] = field(default_factory=list)
    geometry: str | None = None       # geometry column name
    geometry_type: str | None = None
    srid: int | None = None
    primary_key: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)       # sensitive-data findings
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}/{self.name}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        d = dict(d)
        d["columns"] = [Column(**c) for c in d.get("columns", [])]
        return cls(**d)


@dataclass
class Route:
    """How one item gets into AWS, and why."""
    item: str
    method: str               # e.g. "awsmig: GeoParquet to S3 + load to RDS PostGIS"
    aws_service: str          # the AWS service doing the heavy lifting
    executable: bool          # True when `awsmig run` performs it; False when it is handed to an AWS service
    estimate_hours: float | None
    reasons: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


@dataclass
class Check:
    name: str
    ok: bool
    source: Any = None
    target: Any = None
    note: str = ""


@dataclass
class ItemResult:
    item: str
    status: str               # migrated | skipped | failed | verified | mismatch | handed-off
    rows: int | None = None
    bytes: int | None = None
    seconds: float | None = None
    destinations: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
