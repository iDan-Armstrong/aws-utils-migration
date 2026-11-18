import shapely
from shapely.geometry import Polygon, box

from awsmig.config import AwsSettings, ConfigError
from awsmig.fingerprint import Fingerprint, canon
from awsmig.model import Item
from awsmig.plan import route
from awsmig.sensitive import scan
from awsmig.sources.esri import esri_geometry


def test_fingerprint_ignores_row_order_and_wkb_flavor():
    g = box(0, 0, 1, 1)
    iso = shapely.to_wkb(g)
    ewkb = shapely.to_wkb(shapely.set_srid(g, 32614), include_srid=True, byte_order=0)   # big-endian EWKB, as some servers send
    a = Fingerprint(["id", "geom"], "geom")
    b = Fingerprint(["id", "geom"], "geom")
    a.add_rows([{"id": 1, "geom": iso}, {"id": 2, "geom": None}])
    b.add_rows([{"id": 2, "geom": None}, {"id": 1, "geom": ewkb}])
    assert a.to_dict()["fingerprint"] == b.to_dict()["fingerprint"]


def test_fingerprint_points_at_the_changed_column():
    a, b = Fingerprint(["id", "name", "acres"]), Fingerprint(["id", "name", "acres"])
    a.add_rows([{"id": 1, "name": "Ada", "acres": 34.91}])
    b.add_rows([{"id": 1, "name": "Ada", "acres": 34.9}])
    da, db = a.to_dict(), b.to_dict()
    assert da["fingerprint"] != db["fingerprint"]
    assert [c for c in da["columns"] if da["columns"][c] != db["columns"][c]] == ["acres"]


def test_canon_treats_equal_numbers_alike():
    assert canon(5) == canon(5.0)
    assert canon(0.1) != canon(0.10000001)
    assert canon(None) != canon("")


def test_esri_rings_clockwise_is_exterior():
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]            # clockwise
    hole = [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]                 # counterclockwise
    second = [[20, 0], [20, 5], [25, 5], [25, 0], [20, 0]]          # clockwise: a second polygon
    g = esri_geometry({"rings": [outer, hole, second]})
    assert g.geom_type == "MultiPolygon"
    assert len(g.geoms[0].interiors) == 1 and g.area == 100 - 4 + 25


def test_esri_point_line():
    assert esri_geometry({"x": 1, "y": 2}).wkt == "POINT (1 2)"
    assert esri_geometry({"paths": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]}).geom_type == "MultiLineString"
    assert esri_geometry(None) is None


def test_sensitive_scan_names_and_values():
    found = scan(["owner_email", "tax_id", "acres"], {"tax_id": ["912-77-1234"] * 10, "acres": [1, 2]})
    assert any("owner_email" in f for f in found)
    assert any("tax_id" in f and "Social Security" in f for f in found)
    assert not any(f.startswith("acres") for f in found)


def test_govcloud_region_check():
    AwsSettings(partition="aws-us-gov", region="us-gov-west-1").check()
    for bad in (AwsSettings(partition="aws-us-gov", region="us-east-1"), AwsSettings(partition="aws", region="us-gov-east-1")):
        try:
            bad.check()
            raise AssertionError("expected ConfigError")
        except ConfigError:
            pass


class _Job:
    def __init__(self, mbps=100):
        self.network_mbps, self.network_utilization = mbps, 0.7
        self.aws = AwsSettings(partition="aws-us-gov", region="us-gov-west-1")
        self.marking = "CUI"


def test_plan_routes():
    j = _Job()
    big_share = Item("share", "scans", "files", rows=10, bytes=60 * 1024 ** 4)
    r = route(big_share, {"type": "files"}, j)
    assert "Data Transfer Terminal" in r.method and not r.executable
    assert any("no longer offered to new customers" in x for x in r.reasons)
    mid_share = Item("share", "imagery", "files", rows=50_000, bytes=2 * 1024 ** 4)
    assert route(mid_share, {"type": "files"}, _Job(mbps=10_000)).aws_service == "AWS DataSync"
    small = Item("share", "docs", "files", rows=40, bytes=10 * 1024 ** 2)
    assert route(small, {"type": "files"}, j).executable
    big_db = Item("db", "parcels", "table", rows=10_000_000, bytes=120 * 1024 ** 3, geometry="geom")
    r = route(big_db, {"type": "database"}, j, engine="mssql")
    assert r.aws_service == "AWS Database Migration Service" and any("Schema Conversion" in x for x in r.reasons)
    assert any("LOB" in x for x in r.reasons)
    small_db = Item("db", "owners", "table", rows=100, bytes=1024 ** 2, flags=["owner: person"])
    r = route(small_db, {"type": "database"}, j, engine="postgresql")
    assert r.executable and any("marking" in x for x in r.reasons)


def test_invalid_geometry_is_counted_not_hidden():
    bow = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    f = Fingerprint(["geom"], "geom")
    f.add_rows([{"geom": shapely.to_wkb(bow)}])
    assert f.to_dict()["invalid_geometries"] == 1


def test_aspatial_geopackage_table_has_no_geometry(tmp_path):
    import geopandas as gpd
    import pandas as pd
    import pyogrio

    from awsmig.sources.gisfile import GisFileSource
    p = tmp_path / "mixed.gpkg"
    gpd.GeoDataFrame({"a": [1]}, geometry=[box(0, 0, 1, 1)], crs=32614).to_file(p, layer="tracts")
    pyogrio.write_dataframe(pd.DataFrame({"owner": ["Ada"], "share": [0.25]}), p, layer="owners")
    items = {i.name: i for i in GisFileSource({"name": "g", "path": str(p)}).discover()}
    assert items["tracts"].geometry == "geom" and items["tracts"].srid == 32614
    assert items["owners"].geometry is None and "geom" not in [c.name for c in items["owners"].columns]
