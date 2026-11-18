"""Small helpers for moving rows as Apache Arrow batches."""
from __future__ import annotations

import re

import pyarrow as pa

from .model import Column

ARROW_TYPES = {
    "integer": pa.int64(), "float": pa.float64(), "text": pa.string(), "boolean": pa.bool_(),
    "date": pa.date32(), "datetime": pa.timestamp("us"), "binary": pa.binary(), "geometry": pa.binary(),
    "json": pa.string(),
}


def normalize_type(t: str) -> str:
    t = (t or "").lower()
    if any(k in t for k in ("geometry", "geography", "sdo_geometry", "wkb")):
        return "geometry"
    if t in ("bool", "boolean", "bit"):
        return "boolean"
    if re.search(r"int|serial|long", t) and "interval" not in t and "point" not in t:
        return "integer"
    if re.search(r"float|double|real|numeric|decimal|number|money", t):
        return "float"
    if "timestamp" in t or "datetime" in t:
        return "datetime"
    if t == "date":
        return "date"
    if re.search(r"bytea|blob|binary|image|raw", t):
        return "binary"
    if "json" in t:
        return "json"
    return "text"


def schema_for(columns: list[Column], geometry: str | None, geom_name: str = "geom") -> pa.Schema:
    fields = []
    for c in columns:
        if c.name == geometry:
            fields.append(pa.field(geom_name, pa.binary()))
        else:
            fields.append(pa.field(c.name, ARROW_TYPES.get(c.type, pa.string())))
    return pa.schema(fields)


def safe_name(name: str) -> str:
    """A lowercase identifier safe for PostgreSQL and S3 keys."""
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    if not s or s[0].isdigit():
        s = "t_" + s
    return s[:63]


def coerce_rows(rows: list[dict], schema: pa.Schema) -> pa.RecordBatch:
    """Build a batch, coercing values the source typed loosely (text dates, numeric strings)."""
    import datetime as dt
    cols = {}
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_timestamp(f.type):
            vals = [dt.datetime.fromisoformat(v) if isinstance(v, str) else v for v in vals]
        elif pa.types.is_date32(f.type):
            vals = [dt.date.fromisoformat(v[:10]) if isinstance(v, str) else (v.date() if isinstance(v, dt.datetime) else v) for v in vals]
        elif pa.types.is_floating(f.type):
            vals = [float(v) if v is not None and not isinstance(v, float) else v for v in vals]
        elif pa.types.is_integer(f.type):
            vals = [int(v) if v is not None and (isinstance(v, bool) or not isinstance(v, int)) else v for v in vals]
        elif pa.types.is_string(f.type):
            vals = [v if v is None or isinstance(v, str) else str(v) for v in vals]
        elif pa.types.is_binary(f.type):
            vals = [bytes(v) if v is not None else None for v in vals]
        cols[f.name] = pa.array(vals, type=f.type)
    return pa.RecordBatch.from_pydict(cols, schema=schema)
