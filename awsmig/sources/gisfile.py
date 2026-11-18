"""GIS files: GeoPackage, shapefile, file geodatabase, GeoJSON, CSV, and anything else GDAL reads."""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pyarrow as pa
import pyogrio

from ..arrowutil import normalize_type
from ..model import Column, Item
from ..sensitive import scan

EXTS = {".gpkg", ".shp", ".geojson", ".json", ".csv", ".fgb", ".kml", ".gml", ".sqlite", ".tab"}


def _srid(crs) -> int | None:
    if not crs:
        return None
    m = re.search(r"EPSG:(\d+)", str(crs))
    return int(m.group(1)) if m else None


class GisFileSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.path = Path(cfg["path"])
        self.include = cfg.get("layers")          # optional list of layer names or globs
        self.batch_size = int(cfg.get("batch_size", 50_000))

    def _datasets(self) -> list[Path]:
        p = self.path
        if p.is_file() or p.suffix.lower() == ".gdb":
            return [p]
        out = []
        for f in sorted(p.rglob("*")):
            if f.suffix.lower() == ".gdb" and f.is_dir():
                out.append(f)
            elif f.is_file() and f.suffix.lower() in EXTS and ".gdb" not in f.parts[-2:-1]:
                out.append(f)
        return out

    def _wanted(self, layer: str) -> bool:
        return not self.include or any(fnmatch.fnmatch(layer, pat) for pat in self.include)

    def discover(self) -> list[Item]:
        items = []
        for ds in self._datasets():
            for layer, gtype in pyogrio.list_layers(ds):
                if not self._wanted(layer):
                    continue
                info = pyogrio.read_info(ds, layer=layer)
                cols = [Column(n, normalize_type(str(t)), str(t)) for n, t in zip(info["fields"], info["dtypes"])]
                geom = None
                if gtype and info.get("geometry_type"):
                    geom = "geom"
                    cols.append(Column("geom", "geometry", str(info["geometry_type"])))
                sample = {}
                try:
                    df = pyogrio.read_dataframe(ds, layer=layer, max_features=200, read_geometry=False)
                    sample = {c: df[c].tolist() for c in df.columns}
                except Exception:
                    pass
                name = layer if len(self._datasets()) == 1 else f"{ds.stem}.{layer}"
                layers_in_ds = len(pyogrio.list_layers(ds))
                items.append(Item(source=self.name, name=name, kind="layer", rows=int(info["features"]),
                                  bytes=ds.stat().st_size if ds.is_file() and layers_in_ds == 1 else None, columns=cols,
                                  geometry=geom, geometry_type=str(info.get("geometry_type")) if geom else None,
                                  srid=_srid(info.get("crs")),
                                  flags=scan([c.name for c in cols if c.type != "geometry"], sample),
                                  extra={"dataset": str(ds), "layer": layer, "driver": info.get("driver")}))
        return items

    def batches(self, item: Item, start: int = 0):
        """Yield (batch_index, RecordBatch). Geometry arrives as WKB in a column named geom."""
        ds, layer = item.extra["dataset"], item.extra["layer"]
        idx = 0
        with pyogrio.raw.open_arrow(ds, layer=layer, batch_size=self.batch_size, use_pyarrow=True) as (meta, reader):
            gname = meta.get("geometry_name") or "wkb_geometry"
            for batch in reader:
                if idx >= start:
                    if gname in batch.schema.names:
                        i = batch.schema.get_field_index(gname)
                        batch = batch.set_column(i, pa.field("geom", pa.binary()), batch.column(i).cast(pa.binary()))
                    yield idx, batch
                idx += 1
