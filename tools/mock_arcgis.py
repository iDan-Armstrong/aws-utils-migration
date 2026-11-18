"""A small stand-in for an ArcGIS REST FeatureServer, for tests and demos.

Serves layers from a GeoPackage at /arcgis/rest/services/<service>/FeatureServer/<n> with
the parts of the REST API awsmig uses: layer metadata, query (count, ids, paging by object
ID with maxRecordCount), queryAttachments and attachment downloads. It also enforces a
token when one is given, so the token handling path gets exercised.

    python tools/mock_arcgis.py data.gpkg --layers wells well_pads leases --port 8765 --token demo-token
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import geopandas as gpd
from shapely.geometry.polygon import orient

TYPES = {"int64": "esriFieldTypeInteger", "int32": "esriFieldTypeInteger", "float64": "esriFieldTypeDouble",
         "bool": "esriFieldTypeSmallInteger", "object": "esriFieldTypeString", "datetime64[ms]": "esriFieldTypeDate"}
GEOM = {"Point": "esriGeometryPoint", "MultiPoint": "esriGeometryMultipoint", "LineString": "esriGeometryPolyline",
        "MultiLineString": "esriGeometryPolyline", "Polygon": "esriGeometryPolygon", "MultiPolygon": "esriGeometryPolygon"}


def to_esri(g):
    if g is None or g.is_empty:
        return None
    t = g.geom_type
    if t == "Point":
        return {"x": g.x, "y": g.y}
    if t == "MultiPoint":
        return {"points": [[p.x, p.y] for p in g.geoms]}
    if t == "LineString":
        return {"paths": [list(map(list, g.coords))]}
    if t == "MultiLineString":
        return {"paths": [list(map(list, line.coords)) for line in g.geoms]}
    polys = [g] if t == "Polygon" else list(g.geoms)
    rings = []
    for p in polys:
        p = orient(p, sign=-1.0)       # Esri: exterior clockwise, holes counterclockwise
        rings.append([list(c) for c in p.exterior.coords])
        rings += [[list(c) for c in r.coords] for r in p.interiors]
    return {"rings": rings}


class Layer:
    def __init__(self, gpkg, name, lid, max_records, with_attachments):
        df = gpd.read_file(gpkg, layer=name)
        df = df.reset_index(drop=True)
        df.insert(0, "OBJECTID", range(1, len(df) + 1))
        self.df, self.name, self.id, self.max = df, name, lid, max_records
        self.wkid = df.crs.to_epsg() if df.crs else None
        self.fields = [{"name": c, "type": "esriFieldTypeOID" if c == "OBJECTID" else TYPES.get(str(df[c].dtype), "esriFieldTypeString"), "alias": c}
                       for c in df.columns if c != "geometry"]
        gt = df.geometry.geom_type.dropna()
        self.gtype = GEOM.get(gt.iloc[0]) if len(gt) else None
        self.attachments = {}
        if with_attachments:
            for oid in df["OBJECTID"][::2]:
                body = f"%PDF-1.4\n% permit scan for {name} feature {oid}\n".encode() + hashlib.sha256(str(oid).encode()).digest() * 40
                self.attachments[int(oid)] = [{"id": 1, "name": f"permit_{oid}.pdf", "contentType": "application/pdf", "size": len(body), "data": body}]

    def meta(self):
        b = self.df.total_bounds
        return {"id": self.id, "name": self.name, "type": "Feature Layer", "geometryType": self.gtype, "objectIdField": "OBJECTID",
                "maxRecordCount": self.max, "hasAttachments": bool(self.attachments), "fields": self.fields,
                "extent": {"xmin": b[0], "ymin": b[1], "xmax": b[2], "ymax": b[3], "spatialReference": {"wkid": self.wkid, "latestWkid": self.wkid}}}

    def feature(self, row):
        attrs = {}
        for f in self.fields:
            v = row[f["name"]]
            if v is None or (isinstance(v, float) and v != v):
                v = None
            elif f["type"] == "esriFieldTypeDate":
                v = int(v.timestamp() * 1000)
            elif hasattr(v, "item"):
                v = v.item()
            if isinstance(v, bool):
                v = int(v)
            attrs[f["name"]] = v
        return {"attributes": attrs, "geometry": to_esri(row["geometry"])}


def make_handler(service, layers, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, ctype="application/json", raw=None):
            body = raw if raw is not None else json.dumps(obj, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _params(self):
            q = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                q.update({k: v[-1] for k, v in parse_qs(self.rfile.read(n).decode()).items()})
            return q

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            p = self._params()
            parts = urlparse(self.path).path.strip("/").split("/")
            base = ["arcgis", "rest", "services", service, "FeatureServer"]
            if parts[:5] != base:
                return self._send({"error": {"code": 400, "message": "Invalid URL"}})
            if token and p.get("token") != token:
                return self._send({"error": {"code": 499, "message": "Token Required"}})
            if len(parts) == 5:
                return self._send({"layers": [{"id": lyr.id, "name": lyr.name} for lyr in layers.values()]})
            lyr = layers.get(int(parts[5]))
            if lyr is None:
                return self._send({"error": {"code": 400, "message": "Invalid layer"}})
            rest = parts[6:]
            if not rest:
                return self._send(lyr.meta())
            if rest == ["query"]:
                df = lyr.df
                if p.get("objectIds"):
                    ids = {int(x) for x in p["objectIds"].split(",")}
                    df = df[df["OBJECTID"].isin(ids)]
                if p.get("returnCountOnly") == "true":
                    return self._send({"count": len(df)})
                if p.get("returnIdsOnly") == "true":
                    return self._send({"objectIdFieldName": "OBJECTID", "objectIds": df["OBJECTID"].tolist()})
                off = int(p.get("resultOffset", 0))
                cnt = min(int(p.get("resultRecordCount", lyr.max)), lyr.max)
                page = df.iloc[off:off + cnt]
                feats = [lyr.feature(r) for _, r in page.iterrows()]
                if p.get("returnGeometry") == "false":
                    for f in feats:
                        f.pop("geometry")
                return self._send({"features": feats, "exceededTransferLimit": off + cnt < len(df),
                                   "spatialReference": {"wkid": lyr.wkid}})
            if rest == ["queryAttachments"]:
                ids = [int(x) for x in p.get("objectIds", "").split(",") if x]
                groups = [{"parentObjectId": i, "attachmentInfos": [{k: v for k, v in a.items() if k != "data"} for a in lyr.attachments[i]]}
                          for i in ids if i in lyr.attachments]
                return self._send({"attachmentGroups": groups})
            if len(rest) == 3 and rest[1] == "attachments":
                for a in lyr.attachments.get(int(rest[0]), []):
                    if a["id"] == int(rest[2]):
                        return self._send(None, a["contentType"], a["data"])
            return self._send({"error": {"code": 404, "message": "Not found"}})
    return H


def serve(gpkg, layer_names, port=8765, token=None, max_records=1000, service="OG_Field", attachments=("well_pads",)):
    layers = {i: Layer(gpkg, n, i, max_records, n in attachments) for i, n in enumerate(layer_names)}
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(service, layers, token))
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{port}/arcgis/rest/services/{service}/FeatureServer"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("gpkg")
    ap.add_argument("--layers", nargs="+", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token")
    ap.add_argument("--max-records", type=int, default=1000)
    a = ap.parse_args()
    srv, url = serve(a.gpkg, a.layers, a.port, a.token, a.max_records)
    print("serving", url)
    threading.Event().wait()
