"""Discover, plan, migrate and verify, with a state file so an interrupted run picks up where it stopped."""
from __future__ import annotations

import getpass
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from . import __version__
from .arrowutil import safe_name
from .config import Job
from .fingerprint import Fingerprint
from .model import Check, Item, ItemResult, Route
from .plan import route
from .sources import open_source
from .targets.s3 import S3Target, geoparquet_metadata


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Audit:
    """Append-only JSON Lines log of every action, for the system's audit trail."""

    def __init__(self, job: Job):
        self.path = job.state_dir / job.name / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.job = job
        self.user = os.environ.get("AWSMIG_OPERATOR") or getpass.getuser()

    def log(self, action: str, **kw):
        rec = {"ts": now(), "job": self.job.name, "operator": self.user, "action": action, "marking": self.job.marking, **kw}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


class State:
    def __init__(self, job: Job):
        self.path = job.state_dir / job.name / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {"items": {}}

    def item(self, key: str) -> dict:
        return self.d["items"].setdefault(key, {})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=1, default=str))
        tmp.replace(self.path)


class Runner:
    def __init__(self, job: Job, log=print):
        self.job = job
        self.log = log
        self.audit = Audit(job)
        self.state = State(job)
        self._sources = {}
        self._s3 = None
        self._pg = None

    # ---- connections -------------------------------------------------------------
    def source(self, name: str):
        if name not in self._sources:
            self._sources[name] = open_source(self.job.source(name))
        return self._sources[name]

    @property
    def s3(self) -> S3Target:
        if self._s3 is None:
            self._s3 = S3Target(self.job)
        return self._s3

    @property
    def pg(self):
        if self._pg is None and self.job.postgis_url_env:
            from .targets.postgis import PostGISTarget
            self._pg = PostGISTarget(self.job.postgis_url, self.job.postgis_schema)
        return self._pg

    def close(self):
        for s in self._sources.values():
            if hasattr(s, "close"):
                s.close()
        if self._pg:
            self._pg.close()

    # ---- discover and plan -------------------------------------------------------
    def discover(self, only: list[str] | None = None) -> list[Item]:
        items = []
        for s in self.job.sources:
            if only and s["name"] not in only:
                continue
            found = self.source(s["name"]).discover()
            self.log(f"{s['name']}: {len(found)} item(s)")
            items += found
        path = self.job.state_dir / self.job.name / "inventory.json"
        path.write_text(json.dumps([i.to_dict() for i in items], indent=1, default=str))
        self.audit.log("discover", items=len(items), flagged=sum(1 for i in items if i.flags))
        return items

    def inventory(self) -> list[Item]:
        path = self.job.state_dir / self.job.name / "inventory.json"
        if not path.exists():
            return self.discover()
        return [Item.from_dict(d) for d in json.loads(path.read_text())]

    def plan(self, items: list[Item]) -> list[Route]:
        out = []
        for it in items:
            src = self.job.source(it.source)
            engine = getattr(self.source(it.source), "engine_name", None) if src["type"] == "database" else None
            out.append(route(it, src, self.job, engine))
        self.audit.log("plan", routes=[{"item": r.item, "method": r.method} for r in out])
        return out

    # ---- run ---------------------------------------------------------------------
    def run(self, items: list[Item], routes: list[Route], force: bool = False) -> list[ItemResult]:
        results = []
        for it, r in zip(items, routes):
            st = self.state.item(it.key)
            if not r.executable:
                st.update(status="handed-off", method=r.method, at=now())
                results.append(ItemResult(it.key, "handed-off", it.rows, it.bytes))
                self.log(f"  {it.key}: handed off to {r.aws_service}")
                continue
            if st.get("status") == "migrated" and not force:
                results.append(ItemResult(it.key, "skipped", st.get("rows"), st.get("bytes"), destinations=st.get("destinations", [])))
                self.log(f"  {it.key}: already migrated (use --force to redo)")
                continue
            t0 = time.time()
            try:
                if it.kind == "files":
                    res = self._run_files(it, st)
                else:
                    res = self._run_table(it, st)
                res.seconds = round(time.time() - t0, 2)
                st.update(status="migrated", rows=res.rows, bytes=res.bytes, destinations=res.destinations, at=now(), seconds=res.seconds)
                self.audit.log("migrate", item=it.key, rows=res.rows, bytes=res.bytes, destinations=res.destinations)
                self.log(f"  {it.key}: {res.rows} rows/files in {res.seconds}s")
            except Exception as e:
                res = ItemResult(it.key, "failed", error=f"{type(e).__name__}: {e}", seconds=round(time.time() - t0, 2))
                st.update(status="failed", error=res.error, at=now())
                self.audit.log("error", item=it.key, error=res.error)
                self.log(f"  {it.key}: FAILED {res.error}")
            self.state.save()
            results.append(res)
        return results

    def _prefix(self, it: Item) -> str:
        return self.s3.key(safe_name(it.source), safe_name(it.name.replace(".", "_")))

    def _run_table(self, it: Item, st: dict) -> ItemResult:
        src = self.source(it.source)
        prefix = self._prefix(it)
        parts = st.setdefault("parts", {}) if st.get("status") != "migrated" else {}
        st["parts"] = parts
        fp = Fingerprint([("geom" if c.type == "geometry" else c.name) for c in it.columns], "geom" if it.geometry else None)
        rows = nbytes = 0
        start = len(parts)
        if start:
            self.log(f"  {it.key}: resuming at part {start}")
        geo = None
        for idx, batch in src.batches(it, start=start):
            table = pa.Table.from_batches([batch])
            if it.geometry and geo is None:
                geo = geoparquet_metadata(table, "geom", it.geometry_type, it.srid)
            put = self.s3.put_parquet(f"{prefix}/part-{idx:05d}.parquet", table, it.key, geo)
            parts[str(idx)] = {"key": put["key"], "rows": table.num_rows, "sha256": put["sha256"]}
            self.state.save()
        # fingerprint and total from every part (including parts written by an earlier, interrupted run)
        for k in sorted(parts, key=int):
            t = self.s3.read_parquet(parts[k]["key"])
            fp.add_rows(t.to_pylist())
            rows += t.num_rows
        nbytes = sum(int(self.s3.head(p["key"])["ContentLength"]) for p in parts.values())
        fpd = fp.to_dict()
        manifest = {"item": it.to_dict(), "parts": parts, "fingerprint": fpd, "written": now(), "job": self.job.name, "marking": self.job.marking}
        self.s3.put_json(f"{prefix}/_manifest.json", manifest)
        dest = [f"s3://{self.job.s3_bucket}/{prefix}/"]
        if self.pg is not None:
            def from_s3():
                for k in sorted(parts, key=int):
                    for b in self.s3.read_parquet(parts[k]["key"]).to_batches():
                        yield 0, b
            n = self.pg.load(it, from_s3(), self.job.name, self.job.marking, fpd["fingerprint"])
            if n != rows:
                raise RuntimeError(f"PostGIS load wrote {n} rows, expected {rows}")
            dest.append(f"postgis:{self.job.postgis_schema}.{self.pg.table_name(it)}")
        st["fingerprint"] = fpd
        if it.extra.get("has_attachments"):
            att = self._attachments(it, src, prefix)
            if att:
                dest.append(f"s3://{self.job.s3_bucket}/{prefix}/attachments/ ({att} files)")
        return ItemResult(it.key, "migrated", rows, nbytes, destinations=dest)

    def _attachments(self, it, src, prefix) -> int:
        n = 0
        for oid, att, data in src.attachments_for(it):
            self.s3.put_bytes(f"{prefix}/attachments/{oid}/{att['id']}-{safe_name(att['name'].rsplit('.', 1)[0])}.{att['name'].rsplit('.', 1)[-1]}",
                              data, f"{it.key}#{oid}", att.get("contentType"))
            n += 1
        return n

    def _run_files(self, it: Item, st: dict) -> ItemResult:
        src = self.source(it.source)
        done = st.setdefault("files", {})
        total = 0
        for f in it.extra["files"]:
            rel = f["path"]
            prev = done.get(rel)
            if prev and prev.get("size") == f["size"] and prev.get("mtime") == f["mtime"]:
                total += f["size"]
                continue
            sha = src.sha256(rel)
            key = f"{self.job.s3_prefix}/{safe_name(it.source)}/{rel}"
            with src.open(rel) as fh:
                self.s3.put_stream(key, fh, f"{it.source}:{rel}", sha, f["size"])
            done[rel] = {"key": key, "size": f["size"], "mtime": f["mtime"], "sha256": sha}
            total += f["size"]
            self.state.save()
        return ItemResult(it.key, "migrated", len(it.extra["files"]), total, destinations=[f"s3://{self.job.s3_bucket}/{self.job.s3_prefix}/{safe_name(it.source)}/{it.name if it.name != '(root)' else ''}"])

    # ---- verify ------------------------------------------------------------------
    def verify(self, items: list[Item], deep: bool = False) -> list[ItemResult]:
        out = []
        for it in items:
            st = self.state.item(it.key)
            if st.get("status") not in ("migrated", "verified", "mismatch"):
                out.append(ItemResult(it.key, st.get("status") or "not-run"))
                continue
            checks = self._verify_files(it, st, deep) if it.kind == "files" else self._verify_table(it, st)
            ok = all(c.ok for c in checks)
            st.update(verify_status="verified" if ok else "mismatch", verified_at=now(),
                      checks=[c.__dict__ for c in checks])
            self.state.save()
            self.audit.log("verify", item=it.key, ok=ok, failed=[c.name for c in checks if not c.ok])
            self.log(f"  {it.key}: {'verified' if ok else 'MISMATCH'} ({sum(c.ok for c in checks)}/{len(checks)} checks)")
            out.append(ItemResult(it.key, "verified" if ok else "mismatch", st.get("rows"), st.get("bytes"), checks=checks,
                                  destinations=st.get("destinations", [])))
        return out

    def _verify_table(self, it: Item, st: dict) -> list[Check]:
        src = self.source(it.source)
        cols = [("geom" if c.type == "geometry" else c.name) for c in it.columns]
        g = "geom" if it.geometry else None
        sfp = Fingerprint(cols, g)
        for _, b in src.batches(it):
            sfp.add_batch(b)
        s = sfp.to_dict()
        checks = [Check("source row count matches discovery", s["rows"] == it.rows, it.rows, s["rows"],
                        "" if s["rows"] == it.rows else "the source changed since discovery")]
        # re-read every Parquet part from S3 now, rather than trusting what the run recorded
        lfp = Fingerprint(cols, g)
        for p in sorted((st.get("parts") or {}).values(), key=lambda x: x["key"]):
            lfp.add_rows(self.s3.read_parquet(p["key"]).to_pylist())
        lake = lfp.to_dict()
        checks.append(Check("S3 rows", lake.get("rows") == s["rows"], s["rows"], lake.get("rows")))
        checks.append(Check("S3 content fingerprint", lake.get("fingerprint") == s["fingerprint"], s["fingerprint"], lake.get("fingerprint")))
        if lake.get("fingerprint") != s["fingerprint"]:
            bad = [c for c in cols if (lake.get("columns") or {}).get(c) != s["columns"][c]]
            checks[-1].note = "columns that differ: " + ", ".join(bad)
        if self.pg is not None:
            tfp = Fingerprint(cols, g)
            for rows in self.pg.rows(it):
                tfp.add_rows(rows)
            t = tfp.to_dict()
            checks.append(Check("PostGIS rows", t["rows"] == s["rows"], s["rows"], t["rows"]))
            c = Check("PostGIS content fingerprint", t["fingerprint"] == s["fingerprint"], s["fingerprint"], t["fingerprint"])
            if not c.ok:
                c.note = "columns that differ: " + ", ".join(k for k in cols if t["columns"][k] != s["columns"][k])
            checks.append(c)
            if g:
                checks.append(Check("PostGIS coordinate system", (self.pg.srid(it) or 0) == (it.srid or 0), it.srid, self.pg.srid(it)))
                checks.append(Check("geometry extent", t["bbox"] == s["bbox"], s["bbox"], t["bbox"]))
                checks.append(Check("invalid geometries carried over unchanged", t["invalid_geometries"] == s["invalid_geometries"],
                                    s["invalid_geometries"], t["invalid_geometries"],
                                    f"{s['invalid_geometries']} invalid in the source; fix with ST_MakeValid after review" if s["invalid_geometries"] else ""))
        return checks

    def _verify_files(self, it: Item, st: dict, deep: bool) -> list[Check]:
        src = self.source(it.source)
        done = st.get("files", {})
        missing, size_bad, hash_bad, deep_bad = [], [], [], []
        for f in it.extra["files"]:
            rec = done.get(f["path"])
            h = self.s3.head(rec["key"]) if rec else None
            if not h:
                missing.append(f["path"])
                continue
            if int(h["ContentLength"]) != f["size"]:
                size_bad.append(f["path"])
            src_sha = src.sha256(f["path"])
            if h.get("Metadata", {}).get("awsmig-sha256") != src_sha:
                hash_bad.append(f["path"])
            if deep and self.s3.stream_sha256(rec["key"]) != src_sha:
                deep_bad.append(f["path"])
        n = len(it.extra["files"])
        checks = [Check("every file present in S3", not missing, n, n - len(missing), ", ".join(missing[:10])),
                  Check("sizes match", not size_bad, 0, len(size_bad), ", ".join(size_bad[:10])),
                  Check("SHA-256 matches the source", not hash_bad, 0, len(hash_bad), ", ".join(hash_bad[:10]))]
        if deep:
            checks.append(Check("S3 object re-read and re-hashed", not deep_bad, 0, len(deep_bad), ", ".join(deep_bad[:10])))
        return checks

    # ---- report ------------------------------------------------------------------
    def manifest(self, items, routes, results, verify_results=None) -> dict:
        vr = {r.item: r for r in (verify_results or [])}
        rr = {r.item: r for r in results}
        posture = {}
        try:
            posture = self.s3.bucket_posture()
        except Exception as e:
            posture = {"error": str(e)}
        doc = {
            "awsmig_version": __version__, "job": self.job.name, "marking": self.job.marking, "generated": now(),
            "operator": self.audit.user, "aws": {"partition": self.job.aws.partition, "region": self.job.aws.region,
                                                 "fips": self.job.aws.fips, "simulated": bool(self.job.aws.endpoint_url),
                                                 "kms_key": self.job.aws.kms_key, "bucket": self.job.s3_bucket, "bucket_posture": posture},
            "network": {"mbps": self.job.network_mbps, "utilization": self.job.network_utilization},
            "items": [],
        }
        for it, r in zip(items, routes):
            res = rr.get(it.key)
            v = vr.get(it.key)
            doc["items"].append({
                "key": it.key, "source_type": self.job.source(it.source)["type"], "kind": it.kind, "rows": it.rows, "bytes": it.bytes,
                "geometry_type": it.geometry_type, "srid": it.srid, "flags": it.flags,
                "route": r.__dict__, "status": res.status if res else self.state.item(it.key).get("status", "planned"),
                "seconds": res.seconds if res else None, "destinations": res.destinations if res else [],
                "error": res.error if res else None,
                "verification": None if not v else {"status": v.status, "checks": [c.__dict__ for c in v.checks]},
            })
        return doc

    def write_report(self, doc: dict) -> Path:
        from .report import html_report
        d = self.job.state_dir / self.job.name
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        jp = d / f"run-{stamp}.json"
        jp.write_text(json.dumps(doc, indent=1, default=str))
        hp = d / f"run-{stamp}.html"
        hp.write_text(html_report(doc))
        try:
            self.s3.put_json(self.s3.key("_awsmig", "manifests", f"run-{stamp}.json"), doc)
            self.s3.put_bytes(self.s3.key("_awsmig", "manifests", f"run-{stamp}.html"), hp.read_bytes(), "awsmig", "text/html")
        except Exception as e:
            self.log(f"could not copy the report to S3: {e}")
        self.audit.log("report", path=str(jp))
        return hp
