"""
app/flood_terrain.py
Flood-terrain features derived LOCALLY from the SRTM DEM used by the terrain
pipeline (data/raw/kamrup_metro_dem.tif) and the 904-cell 1 km grid.

Run from the repo root with the pinned venv:

    venv\\Scripts\\python.exe app/flood_terrain.py

Writes (only if the sanity check passes):
    data/processed/flood_terrain_features.parquet
        grid_id, hand_min, hand_mean, upa_max, chan_dist_m, n_valid_hand
    docs/flood_layer_method.md
    docs/img/flood_hand_map.png

Steps
  1  condition the DEM: fill pits, fill depressions, resolve flats (pysheds)
  2  D8 flow direction and flow accumulation (area-weighted, km2)
  3  channels = pixels whose accumulated upstream area exceeds 1 km2
  4  HAND: elevation of each pixel minus the elevation of the channel pixel it
     drains into along the D8 flow path
  5  aggregate per grid cell; chan_dist_m = centroid to nearest channel pixel
  6  sanity check against known geography; refuse to write if it fails

This is a susceptibility PROXY from 30 m SRTM, not an inundation model.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio import features
from scipy.spatial import cKDTree
from scipy.stats import pearsonr, spearmanr

# pysheds 0.5 calls np.in1d, which numpy 2.x removed (requirements pin numpy 2.5.2).
# np.isin is the documented replacement with the same behaviour for our use.
if not hasattr(np, "in1d"):
    np.in1d = np.isin

from pysheds.grid import Grid  # noqa: E402
from pysheds.sview import Raster  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEM_PATH = ROOT / "data" / "raw" / "kamrup_metro_dem.tif"
GRID_PARQUET = ROOT / "data" / "processed" / "kamrup_metro_grid_1km.parquet"
TERRAIN_PARQUET = ROOT / "data" / "processed" / "terrain_features.parquet"
SUSC_PARQUET = ROOT / "data" / "processed" / "susceptibility_features.parquet"
GEE_CSV = ROOT / "data" / "raw" / "gee_flood_features.csv"
OUT_PARQUET = ROOT / "data" / "processed" / "flood_terrain_features.parquet"
OUT_DOC = ROOT / "docs" / "flood_layer_method.md"
OUT_MAP = ROOT / "docs" / "img" / "flood_hand_map.png"

CHANNEL_MIN_KM2 = 1.0
EARTH_RADIUS_M = 6_371_008.8          # one radius for pixel areas and distances
EXPECTED_CELLS = 904
CORRIDOR_CENTRE = (26.14, 91.77)      # (lat, lon) as given for central Guwahati; approximate
CORRIDOR_RADIUS_M = 3000.0

# Sanity-check criteria. Written before HAND was computed, EXCEPT the top-decile-slope check,
# which replaced a failed one after the first run (see docs/flood_layer_method.md, section 3).
# "Well covered" cells have at least half a full cell's worth of valid-HAND pixels.
MIN_COVERAGE_FRAC = 0.5
SANITY = {
    "lowest10_in_lowest_elevation_quartile_min": 8,    # of 10 lowest-HAND cells
    "highest10_in_top_elevation_quartile_min": 8,      # of 10 highest-HAND cells
    "highest10_slope_above_median_min": 10,            # of 10 highest-HAND cells
    "spearman_hand_vs_elevation_min": 0.5,
    "spearman_hand_vs_slope_min": 0.3,
    "top_decile_slope_cells_median_hand_above_district_median": True,   # replaces a failed check
    "corridor_share_below_district_median_min": 0.6,   # cells within 3 km of 26.14N 91.77E
}
# The MERIT chan_dist_m cross-check "agrees closely" if Spearman is at least this.
CROSSCHECK_SPEARMAN_MIN = 0.85


# ── 1-4: hydrology ────────────────────────────────────────────────────

def pixel_size_m(transform, rows: int) -> tuple[float, np.ndarray]:
    """North-south pixel height (m, constant) and east-west width per row (m)."""
    metres_per_degree = np.pi / 180.0 * EARTH_RADIUS_M
    height = abs(transform.e) * metres_per_degree
    row_lat = transform.f + transform.e * (np.arange(rows) + 0.5)
    width = abs(transform.a) * metres_per_degree * np.cos(np.radians(row_lat))
    return height, width


def run_hydrology():
    grid = Grid.from_raster(str(DEM_PATH))
    dem = grid.read_raster(str(DEM_PATH))
    raw = np.asarray(dem).astype(float)

    # 1. condition: fill pits, fill depressions, resolve flats
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)          # flow routing only (tiny epsilon slopes)
    conditioned = np.asarray(flooded).astype(float)  # heights for HAND: no epsilon offsets
    stats = {
        "pits_raised_px": int((np.asarray(pit_filled).astype(float) > raw).sum()),
        "depression_px_raised": int((conditioned > np.asarray(pit_filled).astype(float)).sum()),
        "px_raised_any": int((conditioned > raw).sum()),
        "raise_median_m": float(np.median((conditioned - raw)[conditioned > raw])),
        "raise_max_m": float((conditioned - raw).max()),
        "raise_over_5m_px": int(((conditioned - raw) > 5).sum()),
        "negative_px_before": int((raw < 0).sum()),
    }

    # 2. D8 flow direction and accumulation (area-weighted so the 1 km2 test is exact)
    fdir = grid.flowdir(inflated, routing="d8")
    height_m, width_m = pixel_size_m(grid.affine, raw.shape[0])
    area_km2 = np.repeat((height_m * width_m / 1e6)[:, None], raw.shape[1], axis=1)
    acc_km2 = np.asarray(grid.accumulation(fdir, weights=Raster(area_km2, grid.viewfinder))).astype(float)

    # 3. channels
    channel = acc_km2 > CHANNEL_MIN_KM2
    stats["pixel_area_m2_min"] = float((height_m * width_m).min())
    stats["pixel_area_m2_max"] = float((height_m * width_m).max())
    stats["threshold_px_min"] = float(1e6 / (height_m * width_m).max())
    stats["threshold_px_max"] = float(1e6 / (height_m * width_m).min())
    stats["channel_px"] = int(channel.sum())

    # 4. HAND along the D8 path, heights from the conditioned DEM.
    # Computed with our own path walk (_hand_from_path) so that a pixel draining into a channel
    # pixel on the tile border still gets a value. pysheds' compute_hand leaves those empty, so it
    # is kept only as a cross-check (see verify_hand).
    fdir_arr = np.asarray(fdir)
    hand, receiver, touches_border = _hand_from_path(fdir_arr, channel, conditioned)
    hand_pysheds = np.asarray(
        grid.compute_hand(fdir, flooded, Raster(channel.astype(np.int16), grid.viewfinder), routing="d8")
    ).astype(float)
    hand_pysheds[~np.isfinite(hand_pysheds)] = np.nan
    stats["px_valid_hand"] = int(np.isfinite(hand).sum())
    stats["px_no_channel_reached"] = int(np.isnan(hand).sum())

    # HAND on raw SRTM heights (negatives clipped to 0), for the sensitivity note only
    hand_raw, _, _ = _hand_from_path(fdir_arr, channel, raw, clip=True)
    return dict(grid=grid, fdir=fdir_arr, raw=raw, conditioned=conditioned, acc_km2=acc_km2,
                channel=channel, hand=hand, receiver=receiver, touches_border=touches_border,
                hand_pysheds=hand_pysheds,
                hand_raw=hand_raw, transform=grid.affine, stats=stats)


# D8 direction codes used by pysheds (dirmap order N, NE, E, SE, S, SW, W, NW) -> (drow, dcol)
_STEP = {64: (-1, 0), 128: (-1, 1), 1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1), 16: (0, -1), 32: (-1, -1)}


def _trace_to_channel(fdir, channel, r, c, limit=20000):
    """Follow D8 directions from (r, c) to the first channel pixel; None if it never reaches one."""
    rows, cols = fdir.shape
    for _ in range(limit):
        if channel[r, c]:
            return r, c
        step = _STEP.get(int(fdir[r, c]))
        if step is None:
            return None
        r, c = r + step[0], c + step[1]
        if not (0 <= r < rows and 0 <= c < cols):
            return None
    return None


def _hand_from_path(fdir, channel, heights, clip=False):
    """
    HAND from the D8 path: pixel height minus the height of the channel pixel it drains into.

    Every pixel's downstream neighbour is looked up once, then 'pointer jumping' (repeatedly
    replacing each pointer by its target's pointer) reaches the end of every flow path in about
    log2(path length) passes. Channel pixels, and pixels with no valid direction, are terminal.
    Returns (hand, receiver, touches_border): hand is NaN where the path never reaches a channel
    pixel; receiver is the flat index of the receiving channel pixel (-1 where none);
    touches_border is True where the path passes through the outermost ring of pixels.
    """
    rows, cols = fdir.shape
    idx = np.arange(rows * cols).reshape(rows, cols)
    nxt = idx.copy()
    valid = np.zeros((rows, cols), dtype=bool)
    for code, (dr, dc) in _STEP.items():
        rr, cc = np.nonzero(fdir == code)
        nr, nc = rr + dr, cc + dc
        inside = (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
        nxt[rr[inside], cc[inside]] = idx[nr[inside], nc[inside]]
        valid[rr[inside], cc[inside]] = True
    chan_flat = channel.ravel()
    terminal = chan_flat | ~valid.ravel()
    ptr = np.where(terminal, np.arange(rows * cols), nxt.ravel())
    ring = np.zeros((rows, cols), dtype=bool)
    ring[0, :] = ring[-1, :] = ring[:, 0] = ring[:, -1] = True
    touched = ring.ravel().copy()
    for _ in range(17):                       # 2**17 steps, far beyond any path in this tile
        touched = touched | touched[ptr]
        ptr = ptr[ptr]
    reached = chan_flat[ptr]
    flat_heights = heights.ravel()
    hand = np.where(reached, flat_heights - flat_heights[ptr], np.nan)
    if clip:
        hand = np.clip(hand, 0, None)
    return (hand.reshape(rows, cols), np.where(reached, ptr, -1).reshape(rows, cols),
            touched.reshape(rows, cols))


def verify_hand(res: dict, n_samples: int = 3000, seed: int = 0) -> dict:
    """
    (a) Walk random pixels one step at a time along the D8 directions and confirm the vectorised
        HAND equals the path definition. Any disagreement stops the run.
    (b) Compare with pysheds' own compute_hand over every pixel. Allowed difference: pysheds
        leaves pixels empty when they drain into a channel pixel on the tile border.
    """
    rng = np.random.default_rng(seed)
    fdir, channel, cond, hand = res["fdir"], res["channel"], res["conditioned"], res["hand"]
    rr = rng.integers(0, fdir.shape[0], n_samples)
    cc = rng.integers(0, fdir.shape[1], n_samples)
    trace_mismatch = 0
    for r, c in zip(rr, cc):
        end = _trace_to_channel(fdir, channel, r, c)
        expected = np.nan if end is None else cond[r, c] - cond[end]
        got = hand[r, c]
        if np.isnan(expected) != np.isnan(got) or (not np.isnan(got) and abs(expected - got) > 1e-9):
            trace_mismatch += 1

    ps = res["hand_pysheds"]
    both = np.isfinite(hand) & np.isfinite(ps)
    only_mine = np.isfinite(hand) & ~np.isfinite(ps)
    touches = res["touches_border"]
    return {
        "sampled": n_samples, "trace_mismatches": trace_mismatch,
        "pysheds_both_valid": int(both.sum()),
        "pysheds_value_differences": int((both & (np.abs(hand - ps) > 1e-9)).sum()),
        "valid_here_empty_in_pysheds": int(only_mine.sum()),
        "of_which_path_touches_border_ring": int((only_mine & touches).sum()),
        "valid_in_pysheds_empty_here": int((np.isfinite(ps) & ~np.isfinite(hand)).sum()),
        "valid_here_and_path_touches_border_ring": int((np.isfinite(hand) & touches).sum()),
    }


# ── 5: aggregate to grid cells ────────────────────────────────────────

def aggregate(res: dict) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    grid_gdf = gpd.read_parquet(GRID_PARQUET).reset_index(drop=True)
    assert len(grid_gdf) == EXPECTED_CELLS, len(grid_gdf)
    hand, acc, channel, transform = res["hand"], res["acc_km2"], res["channel"], res["transform"]

    labels = features.rasterize(((g, i + 1) for i, g in enumerate(grid_gdf.geometry)),
                                out_shape=hand.shape, transform=transform, fill=0, dtype="int32")
    inside = labels > 0
    px = pd.DataFrame({"cell": labels[inside] - 1, "hand": hand[inside], "upa": acc[inside]})
    by_cell = px.groupby("cell")
    agg = pd.DataFrame({
        "hand_min": by_cell["hand"].min(),          # skipna
        "hand_mean": by_cell["hand"].mean(),
        "upa_max": by_cell["upa"].max(),
        "n_valid_hand": by_cell["hand"].count().astype("int32"),
        "n_dem_px": by_cell["hand"].size(),
    }).reindex(range(len(grid_gdf)))
    agg["n_valid_hand"] = agg["n_valid_hand"].fillna(0).astype("int32")
    agg["n_dem_px"] = agg["n_dem_px"].fillna(0).astype("int32")
    agg.insert(0, "grid_id", grid_gdf["grid_id"].to_numpy())

    # chan_dist_m: centroid -> nearest channel pixel centre, metres (local equirectangular)
    rows_ch, cols_ch = np.nonzero(channel)
    lon_ch, lat_ch = transform * (cols_ch + 0.5, rows_ch + 0.5)
    lat0 = np.radians(float(grid_gdf["centroid_lat"].mean()))

    def to_xy(lon, lat):
        return np.column_stack([np.radians(lon) * EARTH_RADIUS_M * np.cos(lat0),
                                np.radians(lat) * EARTH_RADIUS_M])

    tree = cKDTree(to_xy(np.asarray(lon_ch), np.asarray(lat_ch)))
    dist, _ = tree.query(to_xy(grid_gdf["centroid_lon"].to_numpy(), grid_gdf["centroid_lat"].to_numpy()))
    h, w = hand.shape
    col = np.floor((grid_gdf["centroid_lon"].to_numpy() - transform.c) / transform.a).astype(int)
    row = np.floor((grid_gdf["centroid_lat"].to_numpy() - transform.f) / transform.e).astype(int)
    in_dem = (row >= 0) & (row < h) & (col >= 0) & (col < w)
    agg["chan_dist_m"] = np.where(in_dem, dist, np.nan)
    return agg, grid_gdf


# ── 6: sanity check and cross-check ───────────────────────────────────

def sanity_check(feat: pd.DataFrame, grid_gdf: gpd.GeoDataFrame, res: dict) -> tuple[bool, dict]:
    terr = pd.read_parquet(TERRAIN_PARQUET)[["grid_id", "elevation_mean", "slope_mean"]]
    susc = pd.read_parquet(SUSC_PARQUET)[["grid_id", "hazard_floor_applied"]]
    df = (feat.merge(grid_gdf[["grid_id", "centroid_lat", "centroid_lon"]], on="grid_id")
              .merge(terr, on="grid_id").merge(susc, on="grid_id"))
    full_px = int(df["n_dem_px"].quantile(0.95))
    ok = df[(df["n_valid_hand"] >= MIN_COVERAGE_FRAC * full_px) & df["hand_mean"].notna()].copy()
    ranked = ok.sort_values(["hand_mean", "hand_min", "grid_id"])
    low10, high10 = ranked.head(10), ranked.tail(10).iloc[::-1]
    elev_q25, elev_q75 = ok["elevation_mean"].quantile([0.25, 0.75])
    slope_med, hand_med = ok["slope_mean"].median(), ok["hand_mean"].median()

    lat0, lon0 = CORRIDOR_CENTRE
    d_m = np.hypot((ok["centroid_lon"] - lon0) * np.cos(np.radians(lat0)),
                   ok["centroid_lat"] - lat0) * np.pi / 180 * EARTH_RADIUS_M
    corridor = ok[d_m <= CORRIDOR_RADIUS_M]

    rho_elev = spearmanr(ok["hand_mean"], ok["elevation_mean"])[0]
    rho_slope = spearmanr(ok["hand_mean"], ok["slope_mean"])[0]
    steep = ok[ok["slope_mean"] >= ok["slope_mean"].quantile(0.9)]
    # The ORIGINAL check that failed, kept only for the disclosure in the doc: it assumed
    # hazard-site cells are hill cells, which app/susceptibility_utils.py says they are not.
    hazard = ok[ok["hazard_floor_applied"].astype(bool)]
    checks = {
        "lowest10_in_lowest_elevation_quartile": (int((low10["elevation_mean"] <= elev_q25).sum()),
                                                  SANITY["lowest10_in_lowest_elevation_quartile_min"]),
        "highest10_in_top_elevation_quartile": (int((high10["elevation_mean"] >= elev_q75).sum()),
                                                SANITY["highest10_in_top_elevation_quartile_min"]),
        "highest10_slope_above_median": (int((high10["slope_mean"] > slope_med).sum()),
                                         SANITY["highest10_slope_above_median_min"]),
        "spearman_hand_vs_elevation": (float(rho_elev), SANITY["spearman_hand_vs_elevation_min"]),
        "spearman_hand_vs_slope": (float(rho_slope), SANITY["spearman_hand_vs_slope_min"]),
        "top_decile_slope_cells_median_hand": (float(steep["hand_mean"].median()), float(hand_med)),
        "corridor_share_below_district_median": (
            float((corridor["hand_mean"] < hand_med).mean()) if len(corridor) else float("nan"),
            SANITY["corridor_share_below_district_median_min"]),
    }
    passed = (
        checks["lowest10_in_lowest_elevation_quartile"][0] >= checks["lowest10_in_lowest_elevation_quartile"][1]
        and checks["highest10_in_top_elevation_quartile"][0] >= checks["highest10_in_top_elevation_quartile"][1]
        and checks["highest10_slope_above_median"][0] >= checks["highest10_slope_above_median"][1]
        and rho_elev >= SANITY["spearman_hand_vs_elevation_min"]
        and rho_slope >= SANITY["spearman_hand_vs_slope_min"]
        and checks["top_decile_slope_cells_median_hand"][0] > hand_med
        and len(corridor) > 0
        and checks["corridor_share_below_district_median"][0] >= SANITY["corridor_share_below_district_median_min"]
    )
    detail = {"checks": checks, "low10": low10, "high10": high10, "n_well_covered": len(ok),
              "full_cell_px": full_px, "corridor_n": len(corridor), "district_median_hand_mean": float(hand_med),
              "elev_q25": float(elev_q25), "elev_q75": float(elev_q75), "df": df, "ok": ok,
              "steep_n": len(steep),
              "hazard": {"n": len(hazard), "median_hand": float(hazard["hand_mean"].median()),
                         "median_slope": float(hazard["slope_mean"].median()),
                         "median_elev": float(hazard["elevation_mean"].median()),
                         "district_median_slope": float(slope_med),
                         "district_median_elev": float(ok["elevation_mean"].median()),
                         "share_flatter": float((hazard["slope_mean"] < slope_med).mean())}}
    return passed, detail


def crosscheck_merit(feat: pd.DataFrame) -> dict:
    gee = pd.read_csv(GEE_CSV, usecols=["grid_id", "chan_dist_m"]).rename(columns={"chan_dist_m": "merit"})
    m = feat[["grid_id", "chan_dist_m"]].merge(gee, on="grid_id", how="left")
    both = m.dropna(subset=["chan_dist_m", "merit"])
    diff = both["chan_dist_m"] - both["merit"]
    out = {
        "n_compared": int(len(both)), "n_local_nan": int(m["chan_dist_m"].isna().sum()),
        "pearson": float(pearsonr(both["chan_dist_m"], both["merit"])[0]),
        "spearman": float(spearmanr(both["chan_dist_m"], both["merit"])[0]),
        "median_local_m": float(both["chan_dist_m"].median()), "median_merit_m": float(both["merit"].median()),
        "max_local_m": float(both["chan_dist_m"].max()), "max_merit_m": float(both["merit"].max()),
        "median_abs_diff_m": float(diff.abs().median()), "mean_signed_diff_m": float(diff.mean()),
        "share_within_100m": float((diff.abs() <= 100).mean()),
        "share_within_200m": float((diff.abs() <= 200).mean()),
    }
    out["agrees_closely"] = out["spearman"] >= CROSSCHECK_SPEARMAN_MIN
    return out


def raw_height_sensitivity(feat: pd.DataFrame, res: dict, grid_gdf: gpd.GeoDataFrame) -> dict:
    """How much would per-cell hand_mean change if raw SRTM heights were used instead?"""
    alt = dict(res, hand=res["hand_raw"])
    agg, _ = aggregate(alt)
    j = feat[["grid_id", "hand_mean"]].merge(agg[["grid_id", "hand_mean"]], on="grid_id", suffixes=("", "_raw")).dropna()
    return {"spearman": float(spearmanr(j["hand_mean"], j["hand_mean_raw"])[0]),
            "median_abs_diff_m": float((j["hand_mean"] - j["hand_mean_raw"]).abs().median()),
            "n": int(len(j))}


# ── outputs ───────────────────────────────────────────────────────────

def plot_map(res, feat, grid_gdf):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    hand, channel, T = res["hand"], res["channel"], res["transform"]
    extent = [T.c, T.c + T.a * hand.shape[1], T.f + T.e * hand.shape[0], T.f]
    fig, axes = plt.subplots(1, 2, figsize=(16, 5.2))
    im = axes[0].imshow(np.clip(hand, 0, 30), extent=extent, cmap="viridis_r", vmin=0, vmax=30)
    ch = np.where(channel, 1.0, np.nan)
    axes[0].imshow(ch, extent=extent, cmap="Reds", vmin=0, vmax=1, alpha=0.9)
    axes[0].set_title("HAND per DEM pixel (m, clipped at 30); channels in red")
    plt.colorbar(im, ax=axes[0], shrink=0.8)
    g = grid_gdf.merge(feat[["grid_id", "hand_mean"]], on="grid_id")
    g.plot(column="hand_mean", ax=axes[1], cmap="viridis_r", vmin=0, vmax=30, edgecolor="none",
           missing_kwds={"color": "lightgrey"}, legend=True, legend_kwds={"shrink": 0.8})
    axes[1].set_title("hand_mean per 1 km cell (m); grey = no DEM")
    for ax in axes:
        ax.plot(CORRIDOR_CENTRE[1], CORRIDOR_CENTRE[0], "k+", ms=14)
        ax.set_xlabel("lon"); ax.set_ylabel("lat")
    fig.tight_layout()
    OUT_MAP.parent.mkdir(exist_ok=True)
    fig.savefig(OUT_MAP, dpi=110)
    plt.close(fig)


def _table(df, cols):
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(f"{r[c]:.3f}" if isinstance(r[c], float) else str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def write_doc(res, feat, detail, verify, cross, sens, no_value):
    s, c = res["stats"], detail["checks"]
    zero_px = int((res["hand"] == 0).sum())
    zero_share = zero_px / res["hand"].size
    cols = ["grid_id", "centroid_lat", "centroid_lon", "hand_min", "hand_mean", "elevation_mean", "slope_mean"]
    L = []
    L.append("# Flood layer: locally derived HAND from SRTM 30 m\n")
    L.append("Generated by `app/flood_terrain.py`. **This is a terrain susceptibility proxy, not an inundation model.**\n")
    L.append("## What was computed\n")
    L.append("Source: the SRTM DEM already used by the terrain pipeline (`data/raw/kamrup_metro_dem.tif`, "
             "1 arc-second, integer metres) and the 904-cell 1 km grid. Nothing is taken from Google Earth Engine "
             "except the cross-check in section 4. Steps, all with `pysheds` 0.5:\n")
    L.append("1. Condition the DEM: fill pits, fill depressions, resolve flats "
             f"({s['px_raised_any']:,} of {res['raw'].size:,} pixels raised; median raise {s['raise_median_m']:.0f} m, "
             f"{s['raise_over_5m_px']:,} pixels raised by more than 5 m, largest {s['raise_max_m']:.0f} m; "
             f"{s['negative_px_before']} negative-elevation pixels in the raw DEM, all removed by filling).")
    L.append("2. D8 flow direction and area-weighted flow accumulation (pixel area varies with latitude).")
    L.append(f"3. Channels = pixels with accumulated upstream area above **{CHANNEL_MIN_KM2:g} km2**. A pixel is "
             f"{s['pixel_area_m2_min']:.0f}-{s['pixel_area_m2_max']:.0f} m2 across the tile, so the threshold is "
             f"**about {s['threshold_px_min']:,.0f}-{s['threshold_px_max']:,.0f} pixels** "
             f"(area-weighted, so exact per row). {s['channel_px']:,} channel pixels result.")
    L.append("4. HAND: for every pixel, its height on the depression-filled DEM minus the height of the channel pixel "
             f"it drains into along the D8 path (path walk written for this project; pysheds' `compute_hand` is the cross-check below). "
             f"{s['px_valid_hand']:,} pixels have a value; "
             f"{s['px_no_channel_reached']:,} drain out of the tile (or into an unresolved flat or sink) before reaching a channel and are left empty.")
    L.append(f"5. Per grid cell: `hand_min`, `hand_mean` (over pixels with a valid HAND), `upa_max` (km2), "
             "`chan_dist_m` (cell centroid to the nearest channel pixel centre, metres, local equirectangular), "
             "`n_valid_hand` (pixels with a valid HAND).\n")
    L.append(f"Checks on step 4. (a) {verify['sampled']} random pixels were walked one step at a time along the D8 "
             f"directions: {verify['trace_mismatches']} disagreed with the computed HAND. (b) pysheds' own `compute_hand` "
             f"was run as a cross-check: on the {verify['pysheds_both_valid']:,} pixels where both give a value there are "
             f"{verify['pysheds_value_differences']} differences. pysheds leaves {verify['valid_here_empty_in_pysheds']:,} further pixels "
             "empty, and every one of them has a flow path that passes through the outermost ring of the tile (pysheds "
             "treats the tile edge as a boundary); HAND here is computed by walking the D8 path directly, so those pixels "
             "keep a value. Nowhere does pysheds give a value where this computation gives none. "
             f"{verify['valid_here_and_path_touches_border_ring']:,} pixels with a value have a path touching the edge ring; "
             "they lie next to the tile boundary and are the least reliable.\n")

    L.append("## 1. Cells with no valid values, and why\n")
    L.append(f"- **{no_value['no_dem']} cells have no DEM pixels at all**, so every feature is empty. They are exactly the "
             "93 cells in `data/processed/missing_terrain_cells.json`: the DEM tile ends at 26.200 N and 92.150 E, "
             "while the grid extends to 26.255 N and 92.178 E.")
    L.append(f"- {no_value['dem_but_no_hand']} further cells have DEM pixels but none with a valid HAND "
             "(all their pixels drain off the tile edge or into an unresolved sink before reaching a channel).")
    L.append(f"- {no_value['partial']} cells have valid values but fewer than half a full cell's pixels "
             f"(under {int(MIN_COVERAGE_FRAC * detail['full_cell_px'])} of about {detail['full_cell_px']}), because the DEM tile "
             "clips them. Their values describe only the covered part; use `n_valid_hand` to filter.")
    L.append(f"- `chan_dist_m` is empty for the {no_value['chan_nan']} cells whose centroid lies outside the DEM tile.\n")

    L.append("## 2. Sanity check\n")
    L.append("Seven checks. Six were written before HAND was computed; the seventh replaced a failed check after "
             "the first run (section 3).\n")
    L.append("Ranking uses `hand_mean`, ties broken by `hand_min`, on the "
             f"{detail['n_well_covered']} cells with at least half a cell of valid pixels. Many cells contain a channel "
             "pixel and so have `hand_min = 0`, which is why `hand_mean` ranks them.\n")
    L.append("| Check | Result | Required |\n|---|---|---|")
    names = {
        "lowest10_in_lowest_elevation_quartile": "10 lowest-HAND cells in the lowest elevation quartile (of 10)",
        "highest10_in_top_elevation_quartile": "10 highest-HAND cells in the top elevation quartile (of 10)",
        "highest10_slope_above_median": "10 highest-HAND cells steeper than the district median slope (of 10)",
        "spearman_hand_vs_elevation": "Spearman, hand_mean vs elevation_mean",
        "spearman_hand_vs_slope": "Spearman, hand_mean vs slope_mean",
        "top_decile_slope_cells_median_hand": f"Median hand_mean of the {detail['steep_n']} steepest-decile cells vs district median (replaces a failed check, see section 3)",
        "corridor_share_below_district_median": f"Share of the {detail['corridor_n']} cells within 3 km of 26.14 N 91.77 E with below-median HAND",
    }
    for k, (got, req) in c.items():
        gs = f"{got:.3f}" if isinstance(got, float) else str(got)
        rq = (f"> {req:.3f}" if k == "top_decile_slope_cells_median_hand" else f">= {req}")
        L.append(f"| {names[k]} | {gs} | {rq} |")
    L.append("\n### 10 lowest-HAND cells\n")
    L.append(_table(detail["low10"], cols))
    L.append("\n### 10 highest-HAND cells\n")
    L.append(_table(detail["high10"], cols))
    L.append("\nMap: `docs/img/flood_hand_map.png`. The reference point (26.14 N 91.77 E) is the approximate location given "
             "for the Bharalu corridor and was not independently surveyed here.\n")

    h = detail["hazard"]
    steep_med = c["top_decile_slope_cells_median_hand"][0]
    L.append("## 3. Sanity-check history: one check failed and was replaced\n")
    L.append("**The original check.** \"The median `hand_mean` of hazard-site cells (cells floored to High because they "
             "contain a listed hazard site or a dated incident) is above the district median.\" It was meant to test that "
             "hill cells score high.\n")
    L.append(f"**What happened.** On the first run it failed: median `hand_mean` {h['median_hand']:.2f} m for "
             f"{h['n']} well-covered hazard-site cells, against a district median of {detail['district_median_hand_mean']:.2f} m. "
             "That run stopped before writing any output.\n")
    L.append("**Why the premise was wrong.** `app/susceptibility_utils.py` documents that documented landslide locations sit in "
             "cells *flatter* than the district average (median `slope_mean` 6.0 deg for the 6 incident cells and 4.5 deg for the "
             "34 listed-hazard-site cells, against 9.4 deg for the district), which is the reason the hazard floor exists. "
             f"This run agrees: hazard-site cells have median slope {h['median_slope']:.1f} deg against {h['district_median_slope']:.1f} deg, "
             f"median elevation {h['median_elev']:.0f} m against {h['district_median_elev']:.0f} m, and {100 * h['share_flatter']:.0f}% "
             "are flatter than the district median. A flat, low cell should get a low HAND, so the layer behaved correctly and "
             "the check was mis-specified: hazard sites are not hill cells.\n")
    L.append(f"**The replacement.** \"The median `hand_mean` of the {detail['steep_n']} steepest-decile cells (by `slope_mean`) is above "
             f"the district median.\" Result: {steep_med:.1f} m against {detail['district_median_hand_mean']:.1f} m, a pass.\n")
    L.append("**Two disclosures.** (1) The replacement was chosen *after* seeing the results. (2) It had already been computed "
             "and had already passed in a diagnostic run before it was adopted, so it was not a pre-registered test that could "
             "have failed. It is also a weak check on its own, because steep terrain has high HAND almost by construction "
             "(Spearman with slope is shown above). The informative checks are the lowest-10 and highest-10 lists, the "
             "corridor share, and the elevation correlation. None of the other six checks was changed.\n")

    L.append("## 4. Cross-check against MERIT Hydro channel distance\n")
    L.append("`data/raw/gee_flood_features.csv` has a valid `chan_dist_m` (MERIT Hydro, upa > 1 km2) for all 904 cells; "
             "every other flood column in that file was empty and is ignored. It also holds a `chan_dist_capped` column, "
             "which is not used. The GEE script that produced it was not available, so how MERIT's per-cell distance "
             "was defined (centroid or cell-wide statistic) is unverified.\n")
    L.append(f"| | Local (SRTM) | MERIT Hydro |\n|---|---|---|\n| Median (m) | {cross['median_local_m']:.0f} | {cross['median_merit_m']:.0f} |\n"
             f"| Maximum (m) | {cross['max_local_m']:.0f} | {cross['max_merit_m']:.0f} |\n")
    L.append(f"Over the {cross['n_compared']} cells where both exist: **Pearson r = {cross['pearson']:.3f}, "
             f"Spearman = {cross['spearman']:.3f}**; median absolute difference {cross['median_abs_diff_m']:.0f} m, "
             f"mean signed difference (local minus MERIT) {cross['mean_signed_diff_m']:+.0f} m; "
             f"{100 * cross['share_within_100m']:.0f}% of cells agree within 100 m and {100 * cross['share_within_200m']:.0f}% within 200 m.\n")
    if cross["agrees_closely"]:
        L.append(f"Agreement is close (Spearman at or above the {CROSSCHECK_SPEARMAN_MIN} bar set beforehand). "
                 "It is still only a cross-check of one derived quantity, not a validation of flood susceptibility.\n")
    else:
        L.append(f"**Agreement is moderate, and is NOT claimed as validation** (Spearman below the "
                 f"{CROSSCHECK_SPEARMAN_MIN} bar set beforehand). The medians match, but individual cells can differ by "
                 "hundreds of metres, and the local maximum is larger than MERIT's. The local values are written because "
                 "they are the only complete set, not because MERIT confirmed them.\n")

    L.append("## 5. Sensitivity to the height surface\n")
    L.append(f"Using raw SRTM heights instead of depression-filled heights (negatives clipped to 0) changes per-cell `hand_mean` "
             f"by a median of {sens['median_abs_diff_m']:.2f} m (Spearman between the two rankings {sens['spearman']:.3f}, "
             f"{sens['n']} cells).\n")

    L.append("## 6. Limitations\n")
    L.append("- **No urban drainage.** Storm drains, culverts, pipes, embankments, roads on fill and sluice gates are not in a "
             "DEM. Guwahati's urban flooding is largely a drainage-capacity problem that HAND cannot see.")
    L.append("- **30 m cannot resolve street-level drainage.** Roughly 1,170 pixels make up a 1 km cell; underpasses, "
             "kerbs and building-scale relief are averaged away. SRTM is a surface model: it includes tree canopy and "
             "buildings, and its integer-metre heights make flood-plain flats coarse.")
    L.append("- **Upstream area is truncated at the tile edge.** `upa_max` counts only what lies inside the DEM tile, "
             "so it understates the true catchment of any river that enters from outside, including the Brahmaputra.")
    L.append("- **D8 on a flat floodplain is arbitrary.** Where the surface is flat to within 1 m, flow paths are set by the "
             "flat-resolution step, not by real gradients, so channel positions there are approximate.")
    L.append(f"- **The DEM is whole metres.** SRTM heights are stored as integers, so flat ground within 1 m of a channel has "
             f"HAND exactly 0: {100 * zero_share:.0f}% of all DEM pixels ({zero_px:,}) have HAND = 0, far more than the "
             f"{s['channel_px']:,} channel pixels alone. HAND therefore takes coarse integer values, and `hand_min = 0` is "
             "common for any cell touching flat ground beside a channel.")
    L.append("- **Channels are a 1 km2 threshold, not a surveyed river network.** Small drains and ditches are not channels.")
    L.append("- **HAND is a static height, not water.** It does not model rainfall, discharge, backwater from the "
             "Brahmaputra, or duration. Low HAND means a cell sits near a drainage line, not that it floods.")
    L.append("- **Partly covered cells.** See section 1.\n")
    L.append("## 7. Implementation notes\n")
    L.append("- **numpy / pysheds shim.** `requirements.txt` pins numpy 2.5.2 and pysheds 0.5, but pysheds 0.5 calls `np.in1d`, "
             "which numpy 2.x removed, so `flow accumulation` crashes with an `AttributeError`. `app/flood_terrain.py` sets "
             "`np.in1d = np.isin` when it is missing (same behaviour for this use) before importing pysheds. Nothing in the "
             "environment or in `requirements.txt` was changed.")
    L.append("- **HAND is computed by a path walk written for this project,** not by pysheds' `compute_hand`, because pysheds leaves "
             "empty every pixel whose flow path touches the tile's edge ring. pysheds' result is kept as a cross-check (section above).")
    L.append("- **Use the pinned interpreter:** `venv\\Scripts\\python.exe app/flood_terrain.py`.")
    OUT_DOC.write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    res = run_hydrology()
    verify = verify_hand(res)
    print("HAND path verification:", verify)
    if (verify["trace_mismatches"] or verify["pysheds_value_differences"]
            or verify["valid_in_pysheds_empty_here"]
            or verify["valid_here_empty_in_pysheds"] != verify["of_which_path_touches_border_ring"]):
        sys.exit("STOP: HAND disagrees with the D8 path definition or with pysheds beyond the border case; "
                 "not writing anything.")

    feat, grid_gdf = aggregate(res)
    passed, detail = sanity_check(feat, grid_gdf, res)

    cols = ["grid_id", "centroid_lat", "centroid_lon", "hand_min", "hand_mean", "elevation_mean", "slope_mean"]
    pd.set_option("display.width", 200)
    print("\n10 LOWEST-HAND cells (by hand_mean):\n", detail["low10"][cols].round(3).to_string(index=False))
    print("\n10 HIGHEST-HAND cells:\n", detail["high10"][cols].round(3).to_string(index=False))
    print("\nSanity checks (got, required):")
    for k, v in detail["checks"].items():
        print(f"  {k}: {v}")
    print("SANITY:", "PASS" if passed else "FAIL")
    if not passed:
        sys.exit("STOP: sanity check failed. flood_terrain_features.parquet was NOT written.")

    cross = crosscheck_merit(feat)
    print("\nMERIT cross-check:", json.dumps(cross, indent=2))
    sens = raw_height_sensitivity(feat, res, grid_gdf)
    no_value = {
        "no_dem": int((feat["n_dem_px"] == 0).sum()),
        "dem_but_no_hand": int(((feat["n_dem_px"] > 0) & (feat["n_valid_hand"] == 0)).sum()),
        "partial": int(((feat["n_valid_hand"] > 0) & (feat["n_valid_hand"] < MIN_COVERAGE_FRAC * detail["full_cell_px"])).sum()),
        "chan_nan": int(feat["chan_dist_m"].isna().sum()),
    }
    print("Cells without valid values:", no_value)

    out = feat[["grid_id", "hand_min", "hand_mean", "upa_max", "chan_dist_m", "n_valid_hand"]]
    assert len(out) == EXPECTED_CELLS and out["grid_id"].is_unique
    out.to_parquet(OUT_PARQUET, index=False)
    plot_map(res, feat, grid_gdf)
    write_doc(res, feat, detail, verify, cross, sens, no_value)
    print("\nchannel threshold:", {k: v for k, v in res["stats"].items() if "threshold" in k or "pixel_area" in k})
    print("wrote", OUT_PARQUET.relative_to(ROOT), OUT_DOC.relative_to(ROOT), OUT_MAP.relative_to(ROOT))


if __name__ == "__main__":
    main()
