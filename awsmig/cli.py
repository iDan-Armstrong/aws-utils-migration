"""awsmig command line.

    awsmig discover job.yml     take stock of every source: items, rows, sizes, schema, sensitive fields
    awsmig plan job.yml         choose a route into AWS for each item, with reasons and an estimate
    awsmig run job.yml          move what awsmig moves itself; hand the rest to the AWS service named
    awsmig verify job.yml       compare source and target row by row (fingerprints, checksums, geometry)
    awsmig all job.yml          discover, plan, run, verify and write the report
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, load_job
from .run import Runner


def _size(n):
    if not n:
        return ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def _filter(items, only):
    return [i for i in items if not only or i.source in only or i.key in only]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="awsmig", description="Data migration into AWS with verification.")
    p.add_argument("--version", action="version", version=f"awsmig {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "plan", "run", "verify", "all"):
        s = sub.add_parser(name)
        s.add_argument("job")
        s.add_argument("--only", nargs="*", help="limit to these sources or items (source/name)")
        if name in ("run", "all"):
            s.add_argument("--force", action="store_true", help="migrate again even if already migrated")
        if name in ("verify", "all"):
            s.add_argument("--deep", action="store_true", help="re-download files from S3 and hash them again")
    a = p.parse_args(argv)
    try:
        job = load_job(a.job)
    except ConfigError as e:
        print(f"job file problem: {e}", file=sys.stderr)
        return 2
    r = Runner(job)
    try:
        if a.cmd == "discover":
            items = _filter(r.discover(a.only), a.only)
            for i in items:
                geo = f" {i.geometry_type} EPSG:{i.srid}" if i.geometry else ""
                print(f"{i.key:<48} {i.kind:<6} {i.rows if i.rows is not None else '':>9} {_size(i.bytes):>9}{geo}")
                for f in i.flags:
                    print(f"{'':<4}! {f}")
            return 0
        items = _filter(r.inventory() if a.cmd != "all" else r.discover(a.only), a.only)
        routes = r.plan(items)
        if a.cmd == "plan":
            for rt in routes:
                est = f"~{rt.estimate_hours:.1f} h" if rt.estimate_hours else ""
                print(f"{rt.item}\n    {rt.method} [{rt.aws_service}] {est}")
                for why in rt.reasons:
                    print(f"      - {why}")
            return 0
        results = []
        if a.cmd in ("run", "all"):
            print(f"Running {job.name} ({len(items)} items)")
            results = r.run(items, routes, force=a.force)
        vres = []
        if a.cmd in ("verify", "all"):
            print("Verifying")
            vres = r.verify(items, deep=a.deep)
        report = r.write_report(r.manifest(items, routes, results, vres))
        print(f"Report: {report}")
        failed = [x for x in results if x.status == "failed"] + [x for x in vres if x.status == "mismatch"]
        return 1 if failed else 0
    finally:
        r.close()


if __name__ == "__main__":
    sys.exit(main())
