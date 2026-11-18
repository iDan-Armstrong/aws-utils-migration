"""Amazon RDS for PostgreSQL (or Aurora PostgreSQL) with PostGIS.

Each item loads into a staging table inside one transaction and is renamed into place only
after every row is in, so a failed load never leaves a half-filled table behind. A lineage
table records what was loaded, from where, under which marking, with its fingerprint.
"""
from __future__ import annotations

import json

import psycopg
import shapely
from psycopg import sql

from ..arrowutil import safe_name
from ..model import Item

PG_TYPES = {"integer": "bigint", "float": "double precision", "text": "text", "boolean": "boolean",
            "date": "date", "datetime": "timestamp", "binary": "bytea", "json": "jsonb"}


def _copy_value(v, t: str):
    if v is None:
        return None
    if t == "binary":
        return bytes(v)
    if t == "json" and not isinstance(v, str):
        return json.dumps(v)
    return v


class PostGISTarget:
    def __init__(self, url: str, schema: str):
        if url.startswith("postgresql+psycopg://"):
            url = "postgresql://" + url.split("://", 1)[1]
        self.url = url
        self.schema = schema
        self.conn = psycopg.connect(url, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("select exists(select 1 from pg_extension where extname='postgis')")
            if not cur.fetchone()[0]:
                cur.execute("create extension if not exists postgis")
            cur.execute(sql.SQL("create schema if not exists {}").format(sql.Identifier(schema)))
            cur.execute(sql.SQL("""create table if not exists {}.awsmig_lineage (
                id bigserial primary key, job text, item text, target_table text, rows bigint,
                fingerprint text, marking text, source_detail jsonb, loaded_at timestamptz default now())""").format(sql.Identifier(schema)))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def table_name(self, item: Item) -> str:
        """<source>__<item>, so two sources with a layer of the same name never overwrite each other."""
        return safe_name(f"{item.source}__{item.name.replace('.', '_')}")

    def load(self, item: Item, batches, job_name: str, marking: str, fingerprint: str | None = None) -> int:
        """Load all batches into <schema>.<table>, replacing it atomically. Returns rows loaded."""
        tname = self.table_name(item)
        stage = safe_name("awsmig_stage_" + tname)[:63]
        cols = [c for c in item.columns]
        srid = item.srid or 0
        defs = []
        for c in cols:
            if c.type == "geometry":
                defs.append(sql.SQL("{} geometry(Geometry, {})").format(sql.Identifier("geom"), sql.Literal(srid)))
            else:
                defs.append(sql.SQL("{} {}").format(sql.Identifier(c.name), sql.SQL(PG_TYPES.get(c.type, "text"))))
        names = ["geom" if c.type == "geometry" else c.name for c in cols]
        types = [c.type for c in cols]
        S = sql.Identifier(self.schema)
        total = 0
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("drop table if exists {}.{}").format(S, sql.Identifier(stage)))
            cur.execute(sql.SQL("create table {}.{} ({})").format(S, sql.Identifier(stage), sql.SQL(", ").join(defs)))
            copy_sql = sql.SQL("copy {}.{} ({}) from stdin").format(S, sql.Identifier(stage), sql.SQL(", ").join(map(sql.Identifier, names)))
            with cur.copy(copy_sql) as cp:
                for _, batch in batches:
                    d = batch.to_pydict()
                    n = batch.num_rows
                    for i in range(n):
                        row = []
                        for name, t in zip(names, types):
                            v = d.get(name, [None] * n)[i]
                            if t == "geometry" and v is not None:
                                g = shapely.from_wkb(bytes(v))
                                if srid:
                                    g = shapely.set_srid(g, srid)
                                v = shapely.to_wkb(g, hex=True, include_srid=bool(srid))
                            row.append(_copy_value(v, t))
                        cp.write_row(row)
                    total += n
            if item.geometry:
                cur.execute(sql.SQL("create index on {}.{} using gist (geom)").format(S, sql.Identifier(stage)))
            pk = [k for k in item.primary_key if k in names]
            if pk:
                cur.execute(sql.SQL("alter table {}.{} add primary key ({})").format(S, sql.Identifier(stage), sql.SQL(", ").join(map(sql.Identifier, pk))))
            cur.execute(sql.SQL("drop table if exists {}.{}").format(S, sql.Identifier(tname)))
            cur.execute(sql.SQL("alter table {}.{} rename to {}").format(S, sql.Identifier(stage), sql.Identifier(tname)))
            cur.execute(sql.SQL("comment on table {}.{} is {}").format(S, sql.Identifier(tname), sql.Literal(f"Loaded by awsmig job {job_name}; marking {marking}; source {item.key}")))
            cur.execute(sql.SQL("insert into {}.awsmig_lineage (job, item, target_table, rows, fingerprint, marking, source_detail) values (%s,%s,%s,%s,%s,%s,%s)").format(S),
                        (job_name, item.key, f"{self.schema}.{tname}", total, fingerprint, marking,
                         json.dumps({k: v for k, v in item.extra.items() if k != "files"}, default=str)))
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("analyze {}.{}").format(S, sql.Identifier(tname)))
        self.conn.commit()
        return total

    def rows(self, item: Item, batch: int = 20000):
        """Read a loaded table back as dict rows, geometry as WKB, column names as in the source."""
        tname = self.table_name(item)
        names = ["geom" if c.type == "geometry" else c.name for c in item.columns]
        sel = sql.SQL(", ").join(sql.SQL("ST_AsBinary(geom) as geom") if n == "geom" else sql.Identifier(n) for n in names)
        with self.conn.cursor(name="awsmig_read") as cur:
            cur.itersize = batch
            cur.execute(sql.SQL("select {} from {}.{}").format(sel, sql.Identifier(self.schema), sql.Identifier(tname)))
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    break
                yield [dict(zip(names, r)) for r in chunk]
        self.conn.commit()

    def srid(self, item: Item) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute("select srid from geometry_columns where f_table_schema=%s and f_table_name=%s", (self.schema, self.table_name(item)))
            r = cur.fetchone()
        self.conn.commit()
        return r[0] if r else None
