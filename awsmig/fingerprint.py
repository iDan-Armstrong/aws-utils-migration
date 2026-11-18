"""Order-independent content fingerprints, so source and target can be compared exactly.

Every row is turned into a canonical string (same value, same text, whatever the database
or file format), hashed to 64 bits, and the hashes are summed modulo 2**64. Two copies of a
table give the same fingerprint only if they hold the same multiset of rows. A per-column
fingerprint is kept too, so a mismatch can be traced to the column that changed.

Geometry is compared by its normalized WKB: parsed with Shapely and written back out in one
byte order, without SRID, so ISO WKB from a GeoPackage and EWKB from PostGIS compare equal
when the coordinates are identical.
"""
from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import math

import shapely

MASK = (1 << 64) - 1


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).digest(), "big")


def canon(v) -> str:
    if v is None:
        return "\x00N"
    if isinstance(v, bool):
        return "T" if v else "F"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return str(v)


def canon_geom(wkb) -> str:
    if wkb is None:
        return "\x00N"
    g = shapely.from_wkb(bytes(wkb))
    if g is None or g.is_empty:
        return "EMPTY"
    return shapely.to_wkb(g, hex=True, byte_order=1, include_srid=False)


class Fingerprint:
    def __init__(self, columns: list[str], geometry: str | None = None):
        self.columns = sorted(columns)
        self.geometry = geometry
        self.rows = 0
        self.total = 0
        self.per_col = {c: 0 for c in self.columns}
        self.invalid_geoms = 0
        self.null_geoms = 0
        self.bbox = [math.inf, math.inf, -math.inf, -math.inf]

    def add_rows(self, rows: list[dict]) -> None:
        for r in rows:
            parts = []
            for c in self.columns:
                v = r.get(c)
                if c == self.geometry:
                    s = canon_geom(v)
                    if v is None:
                        self.null_geoms += 1
                    else:
                        g = shapely.from_wkb(bytes(v))
                        if g is not None and not g.is_empty:
                            if not g.is_valid:
                                self.invalid_geoms += 1
                            b = g.bounds
                            self.bbox = [min(self.bbox[0], b[0]), min(self.bbox[1], b[1]),
                                         max(self.bbox[2], b[2]), max(self.bbox[3], b[3])]
                else:
                    s = canon(v)
                self.per_col[c] = (self.per_col[c] + _h(c + "=" + s)) & MASK
                parts.append(s)
            self.total = (self.total + _h("\x1f".join(parts))) & MASK
            self.rows += 1

    def add_batch(self, batch) -> None:
        self.add_rows(batch.to_pylist())

    def to_dict(self) -> dict:
        bbox = None if self.bbox[0] == math.inf else [round(x, 6) for x in self.bbox]
        return {"rows": self.rows, "fingerprint": f"{self.total:016x}",
                "columns": {c: f"{v:016x}" for c, v in self.per_col.items()},
                "invalid_geometries": self.invalid_geoms, "null_geometries": self.null_geoms, "bbox": bbox}
