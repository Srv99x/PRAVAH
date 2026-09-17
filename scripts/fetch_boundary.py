"""
scripts/fetch_boundary.py
──────────────────────────
Download the Kamrup Metropolitan district boundary from GADM.

`data/raw/boundaries/kamrup_metropolitan.geojson` is deliberately NOT
committed to this repository: it derives from GADM (https://gadm.org),
whose data is free to use for academic and non-commercial purposes but may
not be redistributed (https://gadm.org/license.html). Every teammate must
fetch their own copy with this script — see README.md, "Getting the
district boundary".

Source: GADM v4.1, India, admin level 2, feature GID_2 = "IND.4.15_1".
(In the raw GADM attribute table NAME_2 is "KamrupMetropolitan" — no
space between the two words; this script writes the output with
NAME_2 = "Kamrup Metropolitan" for readability elsewhere in the project.
Grid generation itself only uses the geometry, not this column.)

Run from the repository root:

    python scripts/fetch_boundary.py

Writes:
    data/raw/boundaries/kamrup_metropolitan.geojson
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "raw" / "boundaries" / "kamrup_metropolitan.geojson"

GADM_LEVEL2_ZIP_URL = (
    "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_IND_2.json.zip"
)
GADM_JSON_FILENAME = "gadm41_IND_2.json"

# GADM's raw NAME_2 for this district has no space between the words.
TARGET_NAME_2_NORMALIZED = "kamrupmetropolitan"
TARGET_GID_2 = "IND.4.15_1"  # cross-check, in case GADM ever renames it
DOWNLOAD_TIMEOUT_S = 120


def download_gadm_level2_india() -> gpd.GeoDataFrame:
    print(f"Downloading {GADM_LEVEL2_ZIP_URL} ...")
    try:
        resp = requests.get(GADM_LEVEL2_ZIP_URL, timeout=DOWNLOAD_TIMEOUT_S)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            "Could not download the GADM v4.1 India admin-level-2 boundary "
            f"file from {GADM_LEVEL2_ZIP_URL}.\n"
            f"Underlying error: {exc}\n\n"
            "Check your internet connection and try again. If GADM's "
            "server is down or has moved, check "
            "https://gadm.org/download_country.html for the current India "
            "level-2 GeoJSON link and update GADM_LEVEL2_ZIP_URL in this "
            "script."
        ) from exc

    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            with zf.open(GADM_JSON_FILENAME) as fh:
                gdf = gpd.read_file(io.BytesIO(fh.read()))
    except (zipfile.BadZipFile, KeyError) as exc:
        raise RuntimeError(
            f"Downloaded file from {GADM_LEVEL2_ZIP_URL} was not the "
            f"expected zip containing {GADM_JSON_FILENAME}. GADM may have "
            "changed their file layout — check "
            "https://gadm.org/download_country.html and update this "
            "script."
        ) from exc

    return gdf


def find_kamrup_metropolitan(india_level2: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    normalized = india_level2["NAME_2"].str.replace(" ", "", regex=False).str.lower()
    match = india_level2[normalized == TARGET_NAME_2_NORMALIZED]

    if match.empty:
        # Fall back to the stable GID in case GADM ever renames the district.
        match = india_level2[india_level2["GID_2"] == TARGET_GID_2]

    if match.empty:
        raise RuntimeError(
            "Could not find Kamrup Metropolitan in the downloaded GADM "
            f"data (looked for NAME_2 normalizing to "
            f"{TARGET_NAME_2_NORMALIZED!r} or GID_2 == {TARGET_GID_2!r} "
            f"among {len(india_level2)} India districts). GADM may have "
            "renamed or restructured this district — inspect the "
            "downloaded data's NAME_2 column by hand and update "
            "TARGET_NAME_2_NORMALIZED / TARGET_GID_2 in this script."
        )

    if len(match) > 1:
        raise RuntimeError(
            f"Expected exactly one Kamrup Metropolitan feature, found "
            f"{len(match)}. Refusing to guess which one is correct — "
            "inspect the downloaded data by hand."
        )

    return match


def main() -> None:
    india_level2 = download_gadm_level2_india()
    print(f"Loaded {len(india_level2)} India admin-level-2 (district) features.")

    boundary = find_kamrup_metropolitan(india_level2).copy()
    boundary["NAME_2"] = "Kamrup Metropolitan"  # raw GADM value has no space

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    boundary.to_file(OUT_PATH, driver="GeoJSON")

    minx, miny, maxx, maxy = boundary.total_bounds
    print(f"Wrote {OUT_PATH.relative_to(ROOT)}")
    print(f"  GID_2:   {boundary['GID_2'].iloc[0]}")
    print(f"  Bounds:  [{minx:.4f}, {miny:.4f}, {maxx:.4f}, {maxy:.4f}]")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
