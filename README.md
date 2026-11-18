# aws-utils-migration

`awsmig` moves data into AWS from the places it usually lives (databases, GIS files, file
shares, SFTP servers and Esri feature services), and then proves it arrived intact.

It works in four steps, each one a command:

| Step | Command | What happens |
|---|---|---|
| Take stock | `awsmig discover job.yml` | Lists every table, layer and folder with rows, size, columns, geometry type and coordinate system. Flags columns that look like personal data (names, emails, tax IDs, addresses) so the marking is decided before anything moves. |
| Plan | `awsmig plan job.yml` | Picks a route into AWS for each item and says why: awsmig itself, AWS DMS, AWS DataSync, AWS Transfer Family, or an offline transfer. Estimates the transfer time from the site's bandwidth. |
| Migrate | `awsmig run job.yml` | Moves what awsmig handles: Parquet (GeoParquet for spatial data) into an encrypted S3 landing zone, then an atomic load into RDS for PostgreSQL with PostGIS. Interrupted runs resume where they stopped. |
| Prove it | `awsmig verify job.yml` | Re-reads the source, the S3 copy and the database, and compares row counts and content fingerprints column by column, plus coordinate system, extent and geometry validity. Files are checked by size and SHA-256. |

`awsmig all job.yml` runs all four and writes a JSON manifest and an HTML report, kept locally
and copied to S3. Every action also goes to an append-only audit log.

## Why verification is the point

A migration is finished when someone can show the target matches the source, not when the
copy command exits. awsmig turns every row into a canonical string, hashes it, and sums the
hashes in a way that does not depend on row order. Equal fingerprints mean the same rows.
When they differ, the per-column fingerprints name the column that changed. Geometry is
compared after normalizing its binary form, so a GeoPackage, an Esri service and PostGIS
can be compared directly.

That check earned its keep while this was being built. Two sources both had a layer called
`leases`, and the second load replaced the first table in PostGIS. Row counts still matched.
The fingerprint did not, and named the `geom` column. Target tables are now named
`<source>__<item>`.

## A run on real-shaped data

The simulated environment in `tools/sim.py` was run against a 35-layer oil and gas
GeoPackage (PLSS sections, mineral and surface tracts, 3,312 leases, wells, seismic and
gathering layers, plus title and ownership tables). It migrated 54 items and 53,255 rows and
files from six sources: the GeoPackage, an on-premises PostGIS database, a SQLite stand-in for
a legacy land records system, a mock ArcGIS REST service with paging, tokens and attachments,
a file share, and an SFTP drop. Every item verified. Seventeen items were flagged for
personal-data review.

The simulated target is a GovCloud-shaped account (`aws-us-gov`, `us-gov-west-1`, FIPS
endpoints, KMS default encryption, versioning, public access blocked) provided by
[moto](https://github.com/getmoto/moto). No real AWS account was used.

## Try it

```bash
pip install -e ".[dev]"
python tools/sim.py run --data sample          # synthetic data, builds everything, runs `awsmig all`
pytest -q                                      # unit tests plus end to end against mocked S3 and KMS
```

`tools/sim.py` needs PostgreSQL 16 with PostGIS installed locally, or two database URLs in
`AWSMIG_SIM_SOURCE_DB` and `AWSMIG_SIM_TARGET_DB`.

For a real account, apply `infra/terraform` (landing bucket, KMS key, runner role, and
optionally RDS PostGIS, DMS, DataSync and Transfer Family), copy
[`examples/job.example.yml`](examples/job.example.yml), and run as the runner role.

## Sources

| Type | Reads | Notes |
|---|---|---|
| `gis_file` | GeoPackage, file geodatabase, shapefile, GeoJSON, CSV, FlatGeobuf, KML and anything GDAL opens | Streams Arrow batches; geometry stays WKB |
| `database` | PostgreSQL/PostGIS, SQL Server, Oracle, MySQL, SQLite via SQLAlchemy | Pages by primary key so reads are stable and resumable; geometry read as WKB (`ST_AsBinary`, `STAsBinary`, `SDO_UTIL.TO_WKBGEOMETRY`) |
| `files` | Local or mounted shares | One item per top-level folder; SHA-256 per file; unchanged files are skipped on rerun |
| `sftp` | SFTP servers (password or key, pinned host key) | Same as files |
| `esri` | ArcGIS Online, ArcGIS Enterprise, any FeatureServer or MapServer layer | Pages by object ID within `maxRecordCount`, retries with backoff, tokens from the environment, attachments copied to S3 |

## Layout

```
awsmig/                 the package and the `awsmig` command
  sources/              gis_file, database, files and sftp, esri
  targets/              S3 landing zone (GeoParquet, checksums, KMS) and RDS PostGIS
  plan.py               route rules and the facts behind them
  fingerprint.py        order-independent row and column fingerprints
  run.py                discover, plan, run, verify, state, audit log, manifest
infra/terraform/        landing zone, runner role, RDS PostGIS, DMS, DataSync, Transfer Family
tools/                  simulated environment, mock ArcGIS REST and SFTP servers, sample data
docs/                   routes, verification, data handling
```

See [docs/routes.md](docs/routes.md) for how each AWS service fits,
[docs/verification.md](docs/verification.md) for what each check proves, and
[docs/data-handling.md](docs/data-handling.md) for CUI and GovCloud practice.

## Limits

* Raster data, versioned enterprise geodatabase history, relationship classes and topology
  rules are not migrated; feature classes and tables are.
* Esri attachments are copied as files; the attachment table relationship is recorded in the
  key path, not rebuilt as an Esri attachment table.
* Fingerprints compare values as text after normalizing; a column type change that keeps the
  value (integer 5 to float 5.0) is treated as equal on purpose.
* The Terraform has been checked with `terraform fmt` and `terraform validate` in CI only.
