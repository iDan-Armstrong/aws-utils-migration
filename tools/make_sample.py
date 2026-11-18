"""Build a small, entirely made-up oil and gas dataset for tests and the demo.

Everything here is synthetic: the grid is placed on an imaginary meridian, and the owner
names, emails and tax IDs are generated. It deliberately includes the awkward cases a real
migration meets: an invalid (self-intersecting) polygon, a null geometry, non-ASCII text,
dates, a table with no primary key, and columns that look like personal data.

    python tools/make_sample.py out_dir
"""
from __future__ import annotations

import datetime as dt
import random
import sqlite3
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon, box

FIRST = ["Ada", "Pearl", "June", "Edna", "Walt", "Otis", "Ruth", "Hal", "Ines", "José", "Zoë", "Amos"]
LAST = ["Castell", "Sayre", "Cordray", "Barlow", "Mertz", "Okafor", "Lindqvist", "Nguyen", "Reyes", "Haddad"]


def build(out: Path, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    epsg = 32614
    x0, y0, mile = 560000.0, 3960000.0, 1609.344
    sections, tracts, leases, wells, pads = [], [], [], [], []
    for r in range(4):
        for c in range(4):
            sec = r * 4 + c + 1
            sx, sy = x0 + c * mile, y0 + (3 - r) * mile
            sections.append({"sec": sec, "label": f"Sec {sec}", "geometry": box(sx, sy, sx + mile, sy + mile)})
            for qi, (dx, dy) in enumerate([(0.5, 0.5), (0, 0.5), (0, 0), (0.5, 0)]):
                q = ["NE", "NW", "SW", "SE"][qi]
                g = box(sx + dx * mile, sy + dy * mile, sx + (dx + 0.5) * mile, sy + (dy + 0.5) * mile)
                tid = f"T-{sec:02d}-{q}"
                tracts.append({"tract_id": tid, "sec": sec, "aliquot": q, "gross_acres": 160.0, "category": rnd.choice(["Fee", "Fee", "State", "Federal"]), "geometry": g})
                for k in range(rnd.randint(1, 3)):
                    eff = dt.date(2019, 1, 1) + dt.timedelta(days=rnd.randint(0, 1800))
                    leases.append({"lease_id": f"L-{tid}-{k}", "tract_id": tid,
                                   "lessor": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", "lessee": rnd.choice(["Redbud Energy", "Cimarron Operating"]),
                                   "effective_date": eff.isoformat(), "primary_term_yrs": rnd.choice([3, 5]), "royalty": rnd.choice([0.125, 0.1875, 0.25]),
                                   "net_acres": round(160 / (k + 1), 3), "geometry": g})
    # the awkward cases
    leases[3]["geometry"] = Polygon([(x0, y0), (x0 + 500, y0 + 500), (x0 + 500, y0), (x0, y0 + 500)])  # bow-tie: invalid
    leases[5]["geometry"] = None
    leases[7]["lessor"] = "Zoë Ångström-Núñez"
    for i in range(6):
        px, py = x0 + rnd.uniform(0.2, 3.8) * mile, y0 + rnd.uniform(0.2, 3.8) * mile
        pads.append({"pad_id": f"PAD-{i + 1}", "operator": "Redbud Energy", "well_count": 2, "geometry": Point(px, py)})
        for w in range(2):
            wells.append({"api": f"35-073-{24000 + i * 10 + w}", "pad_id": f"PAD-{i + 1}", "status": rnd.choice(["Producing", "Permitted", "Drilling"]),
                          "spud_date": (dt.date(2023, 3, 1) + dt.timedelta(days=rnd.randint(0, 600))).isoformat(),
                          "lateral_ft": rnd.choice([5000, 7500, 10000]),
                          "geometry": LineString([(px, py), (px + rnd.choice([-1, 1]) * 1500, py - 2800)])})
    gpkg = out / "sample_og.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    for name, rows in [("sections", sections), ("mineral_tracts", tracts), ("leases", leases), ("well_pads", pads), ("wells", wells)]:
        df = gpd.GeoDataFrame(rows, geometry="geometry", crs=epsg)
        if "effective_date" in df:
            df["effective_date"] = gpd.pd.to_datetime(df["effective_date"]).dt.date
        df.to_file(gpkg, layer=name, driver="GPKG")

    # a legacy land records system (SQLite stands in for SQL Server or Oracle in tests)
    db = out / "land_records.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.execute("create table owners (owner_id integer primary key, name text, owner_email text, tax_id text, mailing_address text, created_on date)")
    con.execute("create table instruments (instrument_id text primary key, tract_id text, instrument_type text, recorded_date date, book integer, page integer, grantor text, grantee text, consideration real)")
    con.execute("create table payment_log (tract_id text, paid_on date, amount real, memo text)")  # no primary key
    for i in range(1, 121):
        n = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        con.execute("insert into owners values (?,?,?,?,?,?)", (i, n, n.lower().replace(" ", ".") + "@example.org", f"9{rnd.randint(10,99)}-{rnd.randint(70,88)}-{rnd.randint(1000,9999)}",
                                                                f"{rnd.randint(100, 9999)} County Road {rnd.randint(1, 60)}", (dt.date(2015, 1, 1) + dt.timedelta(days=i * 11)).isoformat()))
    for t in tracts:
        for s in range(rnd.randint(2, 5)):
            con.execute("insert into instruments values (?,?,?,?,?,?,?,?,?)", (f"{t['tract_id']}-I{s + 1}", t["tract_id"], rnd.choice(["Warranty Deed", "Mineral Deed", "Oil and Gas Lease", "Affidavit of Heirship"]),
                        (dt.date(1950, 1, 1) + dt.timedelta(days=rnd.randint(0, 26000))).isoformat(), rnd.randint(100, 900), rnd.randint(1, 600),
                        f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", round(rnd.uniform(10, 5000), 2)))
        for m in range(2):
            con.execute("insert into payment_log values (?,?,?,?)", (t["tract_id"], (dt.date(2024, 1, 15) + dt.timedelta(days=m * 30)).isoformat(), round(rnd.uniform(50, 900), 2), "delay rental"))
    con.commit()
    con.close()

    # a file share and an SFTP drop of scanned documents, well logs and seismic
    share = out / "file_share"
    for sub in ("lease_scans", "well_logs", "seismic"):
        (share / sub).mkdir(parents=True, exist_ok=True)
    for lease in leases[:40]:
        (share / "lease_scans" / f"{lease['lease_id']}.pdf").write_bytes(b"%PDF-1.4\n% synthetic scan\n" + rnd.randbytes(rnd.randint(8_000, 40_000)))
    for w in wells:
        las = "~Version\nVERS. 2.0\n~Well\nAPI. " + w["api"] + "\n~Curve\nDEPT.FT\nGR.GAPI\n~A\n" + "\n".join(f"{d} {rnd.uniform(20, 150):.2f}" for d in range(5000, 9000, 2))
        (share / "well_logs" / f"{w['api']}.las").write_text(las)
    (share / "seismic" / "sample_survey_line01.sgy").write_bytes(rnd.randbytes(3_000_000))
    sftp = out / "sftp_drop"
    (sftp / "inbound" / "2026-11").mkdir(parents=True, exist_ok=True)
    for i in range(12):
        (sftp / "inbound" / "2026-11" / f"field_ticket_{i + 1:03d}.csv").write_text("ticket,pad,volume_bbl\n" + "\n".join(f"{i}-{k},PAD-{k % 6 + 1},{rnd.uniform(1, 400):.1f}" for k in range(200)))
    return {"gpkg": str(gpkg), "sqlite": str(db), "share": str(share), "sftp": str(sftp)}


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data")))
