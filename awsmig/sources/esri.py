"""Esri feature services: ArcGIS Online, ArcGIS Enterprise and any ArcGIS REST FeatureServer or MapServer layer.

Reads every feature by object ID in pages no larger than the layer's maxRecordCount, so
large layers come down completely (a plain where=1=1 query stops at the server limit).
Attachments are listed and downloaded too. A token, if the service needs one, is read
from the environment variable named in token_env.
"""
from __future__ import annotations

import datetime as dt
import os
import time

import requests
import shapely
from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon

from ..arrowutil import coerce_rows, schema_for
from ..model import Column, Item
from ..sensitive import scan

FIELD_TYPES = {
    "esriFieldTypeOID": "integer", "esriFieldTypeInteger": "integer", "esriFieldTypeSmallInteger": "integer",
    "esriFieldTypeBigInteger": "integer", "esriFieldTypeDouble": "float", "esriFieldTypeSingle": "float",
    "esriFieldTypeString": "text", "esriFieldTypeDate": "datetime", "esriFieldTypeDateOnly": "date",
    "esriFieldTypeGUID": "text", "esriFieldTypeGlobalID": "text", "esriFieldTypeXML": "text",
}


def _ring_area(r):
    return 0.5 * sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1] for i in range(len(r) - 1))


def esri_geometry(g: dict | None):
    """Esri JSON geometry to a Shapely geometry. Clockwise rings are exteriors."""
    if not g:
        return None
    if "x" in g:
        return None if g["x"] is None else Point(g["x"], g["y"])
    if "points" in g:
        return MultiPoint(g["points"])
    if "paths" in g:
        return LineString(g["paths"][0]) if len(g["paths"]) == 1 else MultiLineString(g["paths"])
    if "rings" in g:
        polys = []
        for r in g["rings"]:
            if len(r) < 4:
                continue
            if _ring_area(r) <= 0:          # clockwise: a new exterior
                polys.append([r, []])
            elif polys:
                polys[-1][1].append(r)
            else:                           # a hole before any exterior: treat as exterior
                polys.append([r, []])
        shapes = [Polygon(e, h) for e, h in polys]
        return shapes[0] if len(shapes) == 1 else MultiPolygon(shapes)
    return None


class EsriSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.urls = cfg["layers"] if isinstance(cfg.get("layers"), list) else [cfg["url"]]
        self.token = os.environ.get(cfg["token_env"]) if cfg.get("token_env") else None
        self.out_sr = cfg.get("out_sr")
        self.attachments = bool(cfg.get("attachments", True))
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "awsmig/0.1"
        self.timeout = int(cfg.get("timeout", 60))

    def _get(self, url: str, params: dict, post: bool = False) -> dict:
        p = {"f": "json", **params}
        if self.token:
            p["token"] = self.token
        for attempt in range(6):
            try:
                r = self.s.post(url, data=p, timeout=self.timeout) if post else self.s.get(url, params=p, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                d = r.json()
                if isinstance(d, dict) and "error" in d:
                    code = d["error"].get("code")
                    if code in (498, 499):
                        raise PermissionError(f"{url}: token rejected or missing ({code})")
                    if code in (500, 503, 504) and attempt < 5:
                        raise requests.HTTPError(str(d["error"]))
                    raise RuntimeError(f"{url}: {d['error'].get('message')} {d['error'].get('details') or ''}")
                return d
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
                if attempt == 5:
                    raise
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError("unreachable")

    def discover(self) -> list[Item]:
        items = []
        for url in self.urls:
            meta = self._get(url, {})
            oid = meta.get("objectIdField") or next((f["name"] for f in meta["fields"] if f["type"] == "esriFieldTypeOID"), "OBJECTID")
            cols = [Column(f["name"], FIELD_TYPES.get(f["type"], "text"), f["type"]) for f in meta["fields"]
                    if f["type"] not in ("esriFieldTypeGeometry", "esriFieldTypeBlob", "esriFieldTypeRaster")]
            geom = None
            if meta.get("geometryType"):
                geom = "geom"
                cols.append(Column("geom", "geometry", meta["geometryType"]))
            count = self._get(url + "/query", {"where": "1=1", "returnCountOnly": "true"})["count"]
            sr = (meta.get("extent") or {}).get("spatialReference") or meta.get("sourceSpatialReference") or {}
            srid = self.out_sr or sr.get("latestWkid") or sr.get("wkid")
            sample = {}
            try:
                d = self._get(url + "/query", {"where": "1=1", "outFields": "*", "returnGeometry": "false", "resultRecordCount": 200})
                sample = {c.name: [f["attributes"].get(c.name) for f in d.get("features", [])] for c in cols if c.type != "geometry"}
            except Exception:
                pass
            name = meta.get("name") or url.rstrip("/").split("/")[-1]
            items.append(Item(source=self.name, name=name, kind="layer", rows=int(count), columns=cols, geometry=geom,
                              geometry_type=meta.get("geometryType"), srid=int(srid) if srid else None, primary_key=[oid],
                              flags=scan([c.name for c in cols if c.type != "geometry"], sample),
                              extra={"url": url, "oid": oid, "max_records": int(meta.get("maxRecordCount") or 1000),
                                     "has_attachments": bool(meta.get("hasAttachments")),
                                     "date_fields": [c.name for c in cols if c.type in ("datetime", "date")]}))
        return items

    def object_ids(self, item: Item) -> list[int]:
        d = self._get(item.extra["url"] + "/query", {"where": "1=1", "returnIdsOnly": "true"}, post=True)
        return sorted(d.get("objectIds") or [])

    def batches(self, item: Item, start: int = 0):
        url, page = item.extra["url"], item.extra["max_records"]
        ids = self.object_ids(item)
        schema = schema_for(item.columns, item.geometry)
        dates = set(item.extra.get("date_fields", []))
        for idx, i in enumerate(range(0, len(ids), page)):
            if idx < start:
                continue
            chunk = ids[i:i + page]
            params = {"objectIds": ",".join(map(str, chunk)), "outFields": "*", "returnGeometry": "true"}
            if self.out_sr:
                params["outSR"] = self.out_sr
            d = self._get(url + "/query", params, post=True)
            rows = []
            for f in d.get("features", []):
                a = dict(f.get("attributes") or {})
                for k in dates:
                    if isinstance(a.get(k), (int, float)):
                        a[k] = dt.datetime(1970, 1, 1) + dt.timedelta(milliseconds=a[k])
                if item.geometry:
                    g = esri_geometry(f.get("geometry"))
                    a["geom"] = shapely.to_wkb(g) if g is not None else None
                rows.append(a)
            if len(rows) != len(chunk):
                raise RuntimeError(f"{item.name}: asked for {len(chunk)} features, got {len(rows)}")
            yield idx, coerce_rows(rows, schema)

    def attachments_for(self, item: Item):
        """Yield (object id, attachment info, bytes) for every attachment on the layer."""
        if not (self.attachments and item.extra.get("has_attachments")):
            return
        url = item.extra["url"]
        ids = self.object_ids(item)
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            d = self._get(url + "/queryAttachments", {"objectIds": ",".join(map(str, chunk)), "returnMetadata": "true"}, post=True)
            for grp in d.get("attachmentGroups", []):
                oid = grp.get("parentObjectId")
                for att in grp.get("attachmentInfos", []):
                    params = {"token": self.token} if self.token else {}
                    r = self.s.get(f"{url}/{oid}/attachments/{att['id']}", params=params, timeout=self.timeout)
                    r.raise_for_status()
                    yield oid, att, r.content
