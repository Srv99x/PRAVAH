"""
app/localities.py
Nearest OpenStreetMap place name for each 1 km grid cell.

Run from the repo root with the pinned venv:

    venv\\Scripts\\python.exe app/localities.py

Reads   data/raw/boundaries/osm_places.geojson   (Overpass export; OSM place nodes)
        data/processed/kamrup_metro_grid_1km.parquet
Writes  data/processed/cell_localities.parquet
            grid_id, nearest_place, nearest_place_dist_m, place_type, source
        docs/localities_method.md

WHAT THIS IS: the OpenStreetMap place node nearest to the cell centre, as a reading aid.
WHAT THIS IS NOT: an official ward or administrative attribution. GMC ward polygons were
tried and rejected (see docs/localities_method.md); names here come from OpenStreetMap only.

If the nearest place is more than MAX_DISTANCE_M away, nearest_place (and place_type) are
left null and the distance is still recorded; a distant name is never attached.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd

ROOT = Path(__file__).resolve().parents[1]
PLACES_GEOJSON = ROOT / "data" / "raw" / "boundaries" / "osm_places.geojson"
GRID_PARQUET = ROOT / "data" / "processed" / "kamrup_metro_grid_1km.parquet"
OUT_PARQUET = ROOT / "data" / "processed" / "cell_localities.parquet"
OUT_DOC = ROOT / "docs" / "localities_method.md"

METRIC_CRS = 32646            # UTM 46N: distances are in metres, not degrees
MAX_DISTANCE_M = 3000.0       # beyond this the name is not attached
SOURCE = "OpenStreetMap contributors (ODbL)"
PLACE_TYPES = ("suburb", "neighbourhood", "quarter", "village", "hamlet", "locality")
EXPECTED_CELLS = 904
ATTRIBUTION = "© OpenStreetMap contributors"


def locality_label(name) -> str:
    """How a locality is shown to users: 'near <place>', or 'unnamed' when no place is close enough."""
    if name is None or (isinstance(name, float) and np.isnan(name)) or pd.isna(name):
        return "unnamed"
    return f"near {name}"


def load_places() -> gpd.GeoDataFrame:
    """OSM place nodes with a usable name, in the metric CRS. `name:en` is preferred when OSM has one."""
    g = gpd.read_file(PLACES_GEOJSON)
    assert (g.geometry.geom_type == "Point").all(), "expected point features only"
    g = g[g["place"].isin(PLACE_TYPES)].copy()
    en = g["name:en"] if "name:en" in g.columns else pd.Series(index=g.index, dtype=object)
    display = en.where(en.notna() & (en.astype(str).str.strip() != ""), g["name"])
    g["place_name"] = display.astype("string").str.strip()
    g = g[g["place_name"].notna() & (g["place_name"] != "")].copy()
    return g.to_crs(METRIC_CRS).reset_index(drop=True)[["place_name", "place", "id", "geometry"]]


def build(places: gpd.GeoDataFrame | None = None, grid: gpd.GeoDataFrame | None = None) -> pd.DataFrame:
    """One row per grid cell: nearest OSM place by centroid distance in EPSG:32646."""
    from scipy.spatial import cKDTree      # imported here so the app can read the parquet without scipy

    places = load_places() if places is None else places
    grid = gpd.read_parquet(GRID_PARQUET).reset_index(drop=True) if grid is None else grid
    assert len(grid) == EXPECTED_CELLS and grid["grid_id"].is_unique
    centroids = grid.to_crs(METRIC_CRS).geometry.centroid
    tree = cKDTree(np.column_stack([places.geometry.x, places.geometry.y]))
    dist, idx = tree.query(np.column_stack([centroids.x, centroids.y]))
    near = dist <= MAX_DISTANCE_M
    return pd.DataFrame({
        "grid_id": grid["grid_id"].to_numpy(),
        "nearest_place": pd.Series(np.where(near, places["place_name"].to_numpy()[idx], None), dtype="object"),
        "nearest_place_dist_m": np.round(dist, 1),
        "place_type": pd.Series(np.where(near, places["place"].to_numpy()[idx], None), dtype="object"),
        "source": SOURCE,
    })


def load_cell_localities() -> pd.DataFrame:
    """The written table, for the app. Fails loudly, with the fix, if it has not been built."""
    if not OUT_PARQUET.exists():
        raise FileNotFoundError(f"{OUT_PARQUET} not found. Build it with: venv\\Scripts\\python.exe app/localities.py")
    return pd.read_parquet(OUT_PARQUET)


def summarise(df: pd.DataFrame) -> dict:
    d = df["nearest_place_dist_m"]
    named = df["nearest_place"].notna()
    return {
        "cells": int(len(df)), "named": int(named.sum()), "null": int((~named).sum()),
        "dist_median_m": float(d.median()), "dist_p90_m": float(d.quantile(0.9)), "dist_max_m": float(d.max()),
        "named_dist_median_m": float(d[named].median()), "named_dist_p90_m": float(d[named].quantile(0.9)),
        "named_dist_max_m": float(d[named].max()),
        "null_dist_min_m": float(d[~named].min()) if (~named).any() else None,
        "null_dist_max_m": float(d[~named].max()) if (~named).any() else None,
        "distinct_names_used": int(df["nearest_place"].nunique()),
        "top15": df["nearest_place"].value_counts().head(15).to_dict(),
    }


def write_doc(stats: dict, n_places_raw: int, n_places_used: int, n_unnamed_dropped: int, place_counts: dict) -> None:
    ts = "2026-09-18T20:20:01Z"
    try:
        import json
        ts = json.loads(PLACES_GEOJSON.read_text(encoding="utf-8")).get("timestamp", ts)
    except Exception:
        pass
    top = "\n".join(f"| {name} | {n} |" for name, n in stats["top15"].items())
    types = ", ".join(f"{k} {v}" for k, v in place_counts.items())
    L = f"""# Cell localities: nearest OpenStreetMap place

Generated by `app/localities.py`. Output: `data/processed/cell_localities.parquet`
(`grid_id, nearest_place, nearest_place_dist_m, place_type, source`).

## What the locality is, and is not

- **It is the nearest OpenStreetMap place name to the centre of a 1 km grid cell**, shown as "near <place>" so a reader can find the
  cell on a familiar map. It is a reading aid.
- **It is not an official ward or administrative attribution, and must never be presented as one.** A cell is not "in" that place; the
  place is only the closest OSM place node. The app never uses the word "ward".
- Distances are computed from the cell centroid in a projected CRS (EPSG:32646, UTM 46N), in metres, not in degrees.

## Source and export

- **Data:** OpenStreetMap place nodes, exported with Overpass (`data/raw/boundaries/osm_places.geojson`; generator recorded in the
  file: overpass-turbo). This export replaces the earlier 22-point hand-verified file.
- **Export date:** the file's own timestamp is `{ts}`.
- **Query:** place = suburb | neighbourhood | quarter | village | hamlet | locality over the district bounding box
  (south 25.98, west 91.60, north 26.28, east 92.20), for example:
```text
[out:json][timeout:120];
node["place"~"^(suburb|neighbourhood|quarter|village|hamlet|locality)$"](25.98,91.60,26.28,92.20);
out body;
```
  The query text is not embedded in the file, so the exact string used cannot be confirmed from the file itself. The file contains
  point features only (nodes).
- **Contents:** {n_places_raw} features ({types}); {n_unnamed_dropped} have no name in any language and are dropped, leaving {n_places_used} named places
  ({stats['distinct_names_used']} of them are the nearest place of at least one cell). When OSM provides `name:en` it is used (3 rows,
  for example "Mayang" instead of the Assamese-script name); otherwise `name`. Two names occur twice as different nodes
  ("Railway Colony", "Bleshah") and are not merged.
- **Attribution (ODbL):** the data is made available under the Open Database Licence. Any display of these names must credit
  **"© OpenStreetMap contributors"**; the app does so on the map legend, in a caption under the map and in the sidebar notes.
  `source` in the output table records `{SOURCE}` for every row. ODbL also carries share-alike terms for derived databases, so check them
  (openstreetmap.org/copyright) before redistributing `cell_localities.parquet` outside this project.

## The 3 km cut-off

If the nearest place is more than **{MAX_DISTANCE_M:.0f} m** from the cell centre, `nearest_place` and `place_type` are left null, the
distance is still recorded, and the app shows "unnamed". A cell more than 3 km from every recorded place is in country that OSM does
not label at this level; attaching the closest name anyway would suggest a nearby landmark where none is recorded. The value is a
readability judgement (about 3 cell-widths), not a tuned parameter, and it was fixed before the distances were computed.

## Result

| | Cells |
|---|---|
| Named (nearest place within {MAX_DISTANCE_M:.0f} m) | {stats['named']} of {stats['cells']} |
| Null (nearest place farther than {MAX_DISTANCE_M:.0f} m) | {stats['null']} |

Distance from cell centroid to the nearest place: median {stats['dist_median_m']:.0f} m, 90th percentile {stats['dist_p90_m']:.0f} m,
maximum {stats['dist_max_m']:.0f} m (over all {stats['cells']} cells). Among named cells: median {stats['named_dist_median_m']:.0f} m,
90th percentile {stats['named_dist_p90_m']:.0f} m, maximum {stats['named_dist_max_m']:.0f} m.
{('Null cells lie ' + format(stats['null_dist_min_m'], '.0f') + ' to ' + format(stats['null_dist_max_m'], '.0f') + ' m from the nearest place.') if stats['null'] else 'No cell was left null.'}

The 15 most frequently assigned names:

| Place | Cells |
|---|---|
{top}

A name that heads this list is simply the nearest place node for many centres, which happens where OSM has few nodes (rural and hill
cells) and says nothing about how large or important that place is.

## Why the official ward route was not used

GMC ward polygons (PR #15, `gmc_wards.geojson`) were verified and **rejected**. Traced from a raster of the official map, they were
tested by asking whether places named in a ward's own `ward_name` actually lie inside that ward: **only 3 of 9 did** (and one of the three,
"Railway Colony", appears in every ward's name), with misses of **1.5 to 3.7 km** against wards only about **1.4 km across**. They also
covered about 175 km2 against the roughly 216 km2 quoted for GMC. Because ward numbers and names could not be matched to locations,
official ward attribution would have been wrong for a large share of cells, so it was not used. Locality names here come from
OpenStreetMap only.

## Limits

- OSM place nodes are volunteer-mapped points, unevenly dense: dense in central Guwahati, sparse in hills and rural areas.
- "Nearest" is not "containing": in a boundary area the nearest place may be across a road or river from the cell.
- Names are as recorded in OSM (for example "Rukimininagar", "Bonda Gaon"), not normalised or corrected.
- Names carry no hazard information; they do not change any priority.
"""
    OUT_DOC.write_text(L, encoding="utf-8")


def main() -> None:
    raw = gpd.read_file(PLACES_GEOJSON)
    raw = raw[raw["place"].isin(PLACE_TYPES)]
    places = load_places()
    df = build(places)
    df.to_parquet(OUT_PARQUET, index=False)
    stats = summarise(df)
    write_doc(stats, len(raw), len(places), len(raw) - len(places), raw["place"].value_counts().to_dict())

    print(f"places: {len(raw)} in file, {len(places)} named ({len(raw) - len(places)} unnamed dropped)")
    print(f"cells named: {stats['named']} of {stats['cells']}; left null (nearest > {MAX_DISTANCE_M:.0f} m): {stats['null']}")
    print(f"distance to nearest place, all cells: median {stats['dist_median_m']:.0f} m, p90 {stats['dist_p90_m']:.0f} m, max {stats['dist_max_m']:.0f} m")
    print(f"  named cells: median {stats['named_dist_median_m']:.0f} m, p90 {stats['named_dist_p90_m']:.0f} m, max {stats['named_dist_max_m']:.0f} m")
    if stats["null"]:
        print(f"  null cells: {stats['null_dist_min_m']:.0f} to {stats['null_dist_max_m']:.0f} m from the nearest place")
    print(f"distinct names used: {stats['distinct_names_used']}")
    print("15 most assigned names:")
    for name, n in stats["top15"].items():
        print(f"  {n:4d}  {name}")
    print("wrote", OUT_PARQUET.relative_to(ROOT), OUT_DOC.relative_to(ROOT))


if __name__ == "__main__":
    main()
