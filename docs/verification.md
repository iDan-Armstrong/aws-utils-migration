# What each check proves

| Check | Compares | Catches |
|---|---|---|
| Source row count matches discovery | Rows now vs rows at discovery | The source changed during the migration window |
| S3 rows, S3 content fingerprint | Source vs every Parquet part, re-read from S3 | Lost or duplicated batches, truncation, any changed value, tampering after upload |
| PostGIS rows, PostGIS content fingerprint | Source vs the loaded table | Type coercion that changed values, encoding problems, a table replaced by another load |
| PostGIS coordinate system | SRID in `geometry_columns` vs the source | A layer loaded with the wrong or missing SRID |
| Geometry extent | Bounding box of all geometries | Coordinates shifted or reprojected by accident |
| Invalid geometries carried over unchanged | Count of invalid geometries on both sides | Silent repair or loss of bad shapes; they are reported, not hidden |
| Every file present, sizes match | S3 object list vs source listing | Missing files |
| SHA-256 matches the source | Hash stored at upload vs the source hashed again | Files that changed after upload |
| S3 object re-read and re-hashed (`--deep`) | The object downloaded and hashed again | Corruption after upload |

## How the fingerprint works

1. Each value becomes canonical text: numbers that are equal print the same way (5 and 5.0),
   dates use ISO 8601, timestamps are converted to UTC, NULL has its own marker.
2. Geometry is parsed and written back as little-endian WKB without an SRID, so ISO WKB from a
   GeoPackage, EWKB from PostGIS and Esri JSON converted to WKB compare equal when the
   coordinates are equal.
3. The row's values are joined in column-name order and hashed (BLAKE2b, 64 bits).
4. Row hashes are summed modulo 2^64. Addition does not care about order, so the source and
   target can return rows in any order. Each column keeps its own sum as well, which is how a
   mismatch is traced to a column.

Two different sets of rows could in theory sum to the same 64-bit value. With rows hashed by
BLAKE2b the chance is about 1 in 2^64 per comparison, far below the chance of a disk error.

## Tested failure cases

`tests/test_end_to_end.py` changes one royalty value in one Parquet part after upload and
checks that `verify` reports a mismatch naming the `royalty` column. It also drops the network
on the third batch and checks that the next run resumes from the third batch and verifies.
