"""Source readers. Each one discovers items and reads them as Arrow batches or files."""
from __future__ import annotations


def open_source(cfg: dict):
    t = cfg["type"]
    if t == "gis_file":
        from .gisfile import GisFileSource
        return GisFileSource(cfg)
    if t == "database":
        from .database import DatabaseSource
        return DatabaseSource(cfg)
    if t in ("files", "sftp"):
        from .files import FileSource
        return FileSource(cfg)
    if t == "esri":
        from .esri import EsriSource
        return EsriSource(cfg)
    raise ValueError(f"unknown source type {t}")
