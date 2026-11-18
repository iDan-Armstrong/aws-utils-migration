"""Databases through SQLAlchemy: PostgreSQL/PostGIS, SQL Server, Oracle, MySQL, SQLite.

The connection URL comes from an environment variable named in the job file (url_env), so
credentials never sit in the job file or the repository. Drivers needed per engine:
postgresql+psycopg (bundled), mssql+pyodbc, oracle+oracledb, mysql+pymysql.
"""
from __future__ import annotations

import fnmatch
import os

import sqlalchemy as sa

from ..arrowutil import coerce_rows, normalize_type, schema_for
from ..model import Column, Item
from ..sensitive import scan


def _q(dialect, name: str) -> str:
    return dialect.identifier_preparer.quote(name)


class DatabaseSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        env = cfg.get("url_env")
        url = os.environ.get(env or "", cfg.get("url"))
        if not url:
            raise ValueError(f"source {self.name}: set the environment variable {env}")
        self.engine = sa.create_engine(url, future=True)
        self.dialect = self.engine.dialect
        self.include = cfg.get("tables") or ["*"]
        self.exclude = cfg.get("exclude") or []
        self.schemas = cfg.get("schemas")
        self.batch_size = int(cfg.get("batch_size", 20_000))

    @property
    def engine_name(self) -> str:
        return self.dialect.name   # postgresql, mssql, oracle, mysql, sqlite

    def _geometry_columns(self, conn) -> dict:
        """(schema, table) -> {column: (type, srid)} for PostGIS; other engines detect by type name."""
        out = {}
        if self.engine_name == "postgresql":
            try:
                rows = conn.execute(sa.text("select f_table_schema, f_table_name, f_geometry_column, type, srid from geometry_columns")).all()
                for s, t, c, ty, srid in rows:
                    out.setdefault((s, t), {})[c] = (ty, srid)
            except Exception:
                conn.rollback()
        return out

    def discover(self) -> list[Item]:
        insp = sa.inspect(self.engine)
        items = []
        with self.engine.connect() as conn:
            gcols = self._geometry_columns(conn)
            schemas = self.schemas or [insp.default_schema_name]
            for schema in schemas:
                for table in insp.get_table_names(schema=schema):
                    full = f"{schema}.{table}" if schema else table
                    if not any(fnmatch.fnmatch(table, p) or fnmatch.fnmatch(full, p) for p in self.include):
                        continue
                    if any(fnmatch.fnmatch(table, p) or fnmatch.fnmatch(full, p) for p in self.exclude):
                        continue
                    if table in ("spatial_ref_sys",) or table.startswith(("awsmig_", "geometry_columns")):
                        continue
                    cols, geom, gtype, srid = [], None, None, None
                    g = gcols.get((schema, table), {})
                    for c in insp.get_columns(table, schema=schema):
                        st = str(c["type"])
                        nt = "geometry" if c["name"] in g else normalize_type(st)
                        if nt == "geometry" and geom is None:
                            geom = c["name"]
                            gtype, srid = g.get(c["name"], (st, None))
                        cols.append(Column(c["name"], nt, st, bool(c.get("nullable", True))))
                    pk = (insp.get_pk_constraint(table, schema=schema) or {}).get("constrained_columns") or []
                    qt = (f"{_q(self.dialect, schema)}." if schema else "") + _q(self.dialect, table)
                    rows = conn.execute(sa.text(f"select count(*) from {qt}")).scalar()
                    size = None
                    if self.engine_name == "postgresql":
                        size = conn.execute(sa.text("select pg_total_relation_size(cast(:t as regclass))"), {"t": qt}).scalar()
                    sample_cols = [c.name for c in cols if c.type not in ("geometry", "binary")]
                    sample = {}
                    if sample_cols:
                        sel = ", ".join(_q(self.dialect, c) for c in sample_cols)
                        res = conn.execute(sa.text(f"select {sel} from {qt}" + self._limit(200))).all()
                        sample = {c: [r[i] for r in res] for i, c in enumerate(sample_cols)}
                    items.append(Item(source=self.name, name=full if self.schemas else table, kind="table", rows=int(rows),
                                      bytes=size, columns=cols, geometry=geom, geometry_type=gtype,
                                      srid=int(srid) if srid else None, primary_key=pk,
                                      flags=scan(sample_cols, sample),
                                      extra={"schema": schema, "table": table, "engine": self.engine_name}))
        return items

    def _limit(self, n: int) -> str:
        if self.engine_name == "mssql":
            return ""  # handled with TOP in callers that need it
        if self.engine_name == "oracle":
            return f" fetch first {n} rows only"
        return f" limit {n}"

    def _geom_expr(self, col: str) -> str:
        q = _q(self.dialect, col)
        return {"postgresql": f"ST_AsBinary({q})", "mssql": f"{q}.STAsBinary()", "oracle": f"SDO_UTIL.TO_WKBGEOMETRY({q})",
                "mysql": f"ST_AsBinary({q})"}.get(self.engine_name, q)

    def batches(self, item: Item, start: int = 0):
        """Yield (batch_index, RecordBatch). Pages by primary key when there is one, so reads are stable and resumable."""
        schema, table = item.extra["schema"], item.extra["table"]
        qt = (f"{_q(self.dialect, schema)}." if schema else "") + _q(self.dialect, table)
        sel = ", ".join(f"{self._geom_expr(c.name)} as geom" if c.name == item.geometry else _q(self.dialect, c.name) for c in item.columns)
        arrow_schema = schema_for(item.columns, item.geometry)
        pk = item.primary_key
        idx, last = 0, None
        with self.engine.connect() as conn:
            if pk:
                order = ", ".join(_q(self.dialect, k) for k in pk)
                while True:
                    where = ""
                    params = {}
                    if last is not None:
                        lhs = "(" + ", ".join(_q(self.dialect, k) for k in pk) + ")"
                        rhs = "(" + ", ".join(f":k{i}" for i in range(len(pk))) + ")"
                        where = f" where {lhs} > {rhs}"
                        params = {f"k{i}": v for i, v in enumerate(last)}
                    sql = f"select {sel} from {qt}{where} order by {order}" + self._limit(self.batch_size)
                    if self.engine_name == "mssql":
                        sql = f"select top {self.batch_size} {sel} from {qt}{where} order by {order}"
                    rows = conn.execute(sa.text(sql), params).mappings().all()
                    if not rows:
                        break
                    last = tuple(rows[-1][k] for k in pk)
                    if idx >= start:
                        yield idx, coerce_rows([dict(r) for r in rows], arrow_schema)
                    idx += 1
                    if len(rows) < self.batch_size:
                        break
            else:
                res = conn.execution_options(stream_results=True).execute(sa.text(f"select {sel} from {qt}"))
                while True:
                    rows = res.mappings().fetchmany(self.batch_size)
                    if not rows:
                        break
                    if idx >= start:
                        yield idx, coerce_rows([dict(r) for r in rows], arrow_schema)
                    idx += 1
