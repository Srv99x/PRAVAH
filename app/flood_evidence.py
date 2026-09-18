"""
app/flood_evidence.py
Does PRAVAH prioritise VERIFIED urban-flood localities?  A retrospective test, nothing tuned.

Run from the repo root with the pinned venv:

    venv\\Scripts\\python.exe app/flood_evidence.py

Reads   data/raw/urban_flood_events.csv          (ledger; locality centroids = weak labels)
        data/processed/trigger_prob_daily.parquet (the existing replay trigger cache)
        data/processed/susceptibility_features.parquet (landslide/slope class, unchanged)
        data/processed/flood_susceptibility.parquet (flood class, app/flood_susceptibility.py)
Writes  docs/flood_evidence.md, docs/flood_evidence.json

For each event date and each affected cell (the grid cell containing a geocoded locality):
  WITHOUT the flood layer: priority = PRIORITY_MATRIX[landslide class][trigger tier]   (what main does today)
  WITH the flood layer:    priority = max(that, PRIORITY_MATRIX[flood class][trigger tier])
The matrix, tier boundaries and review level (REVIEW_MIN_PRIORITY) are those in app/config.py, unchanged;
the review level was chosen for the landslide-only system and is NOT re-chosen here.
The flood class uses the matrix rows Low / Moderate / High by name; no cell is "Very High" for flood.

None of these events was used to fit or select anything in the system.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.stats import hypergeom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import PRIORITY_NAMES, REVIEW_MIN_PRIORITY, priority_levels, trigger_tier  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
LEDGER = ROOT / "data" / "raw" / "urban_flood_events.csv"
OUT_MD = ROOT / "docs" / "flood_evidence.md"
OUT_JSON = ROOT / "docs" / "flood_evidence.json"
LEVEL_LABEL = {0: "No data", **{k: f"{k} {v}" for k, v in PRIORITY_NAMES.items()}}


def load():
    grid = gpd.read_parquet(PROC / "kamrup_metro_grid_1km.parquet").reset_index(drop=True)[["grid_id", "geometry"]]
    cells = (grid.merge(pd.read_parquet(PROC / "grid_weather_mapping.parquet")[["grid_id", "weather_point_id"]], on="grid_id")
                 .merge(pd.read_parquet(PROC / "susceptibility_features.parquet")[["grid_id", "gsi_susceptibility_class"]], on="grid_id")
                 .merge(pd.read_parquet(PROC / "flood_susceptibility.parquet")[["grid_id", "flood_class", "low_coverage"]], on="grid_id"))
    assert len(cells) == 904 and cells["grid_id"].is_unique
    trig = pd.read_parquet(PROC / "trigger_prob_daily.parquet")
    trig["date"] = pd.to_datetime(trig["date"])
    ledger = pd.read_csv(LEDGER, comment="#", parse_dates=["date"])
    return cells, trig, ledger


def attach_cells(ledger: pd.DataFrame, cells: gpd.GeoDataFrame) -> pd.DataFrame:
    """Grid cell containing each geocoded locality (blank coordinates stay blank)."""
    ok = ledger.dropna(subset=["lat", "lon"]).copy()
    pts = gpd.GeoDataFrame(ok, geometry=gpd.points_from_xy(ok["lon"], ok["lat"]), crs=4326)
    joined = gpd.sjoin(pts, cells[["grid_id", "geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]          # a point on a shared edge: first cell
    ledger = ledger.copy()
    ledger["grid_id"] = joined["grid_id"].reindex(ledger.index)
    return ledger


def day_priorities(cells: pd.DataFrame, trig: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    d = trig[trig["date"] == day]
    if d.empty:
        raise ValueError(f"no trigger cache rows for {day.date()}")
    t = cells["weather_point_id"].map(d.set_index("weather_point_id")["trigger_prob"]).to_numpy()
    out = pd.DataFrame({"grid_id": cells["grid_id"].to_numpy(), "trigger": t})
    out["tier"] = [trigger_tier(x) if not np.isnan(x) else "" for x in t]
    out["p_without"] = priority_levels(cells["gsi_susceptibility_class"], t)
    out["p_flood_only"] = priority_levels(cells["flood_class"], t)
    out["p_with"] = np.maximum(out["p_without"], out["p_flood_only"])
    return out


def rank_range(values: np.ndarray, level: int) -> tuple[int, int]:
    """Tie-honest rank among all cells: best case = 1 + cells strictly higher; worst case = cells at least as high."""
    return int(1 + (values > level).sum()), int((values >= level).sum())


def summarise(rows: pd.DataFrame, day_df: pd.DataFrame) -> dict:
    """Counts for one event over unique affected cells (rows: one per unique cell)."""
    n = len(rows)
    scored_mask = day_df["p_without"] > 0            # cells that have a susceptibility class at all
    m_scored = int(scored_mask.sum())
    out = {"n_cells": n, "n_no_data": int((rows["p_with"] == 0).sum()), "flood_high": int((rows["flood_class"] == "High").sum()),
           "flood_high_low_coverage": int(((rows["flood_class"] == "High") & rows["low_coverage"]).sum())}
    for tag in ("without", "with"):
        col = f"p_{tag}"
        review = int((rows[col] >= REVIEW_MIN_PRIORITY).sum())
        K = int((day_df[col][scored_mask] >= REVIEW_MIN_PRIORITY).sum())
        n_scored = int((rows[col] > 0).sum())
        chance = float(hypergeom.sf(review - 1, m_scored, K, n_scored)) if n_scored and review else (1.0 if n_scored else float("nan"))
        out[tag] = {"review": review, "levels": {LEVEL_LABEL[l]: int((rows[col] == l).sum()) for l in range(0, 5)},
                    "district_review_cells": K, "district_review_share": K / m_scored, "chance_at_least": chance,
                    "expected_by_chance": n_scored * K / m_scored,
                    "rose_with_flood_layer": int((rows["p_with"] > rows["p_without"]).sum()),
                    "crossed_into_review": int(((rows["p_with"] >= REVIEW_MIN_PRIORITY) & (rows["p_without"] < REVIEW_MIN_PRIORITY)).sum())}
    out["scored_cells_district"] = m_scored
    out["flood_high_share_district"] = float((day_df["flood_class"] == "High").mean()) if "flood_class" in day_df else None
    return out


def run() -> dict:
    cells, trig, ledger = load()
    ledger = attach_cells(ledger, cells)
    cls = cells.set_index("grid_id")[["gsi_susceptibility_class", "flood_class", "low_coverage"]]
    results, per_cell_rows = {}, []
    for day, ev in ledger.groupby("date"):
        d = day_priorities(cells, trig, day)
        d = d.merge(cells[["grid_id", "flood_class"]], on="grid_id")
        rain = ev["rainfall_mm"].iloc[0]
        loc_total, loc_resolved = len(ev), int(ev["lat"].notna().sum())
        in_grid = ev.dropna(subset=["grid_id"])
        outside_grid = int(ev["lat"].notna().sum() - len(in_grid))
        d_idx = d.set_index("grid_id")
        cell_rows = []
        for gid, grp in in_grid.groupby("grid_id"):
            r = d_idx.loc[gid]
            c = cls.loc[gid]
            cell_rows.append({
                "grid_id": gid, "localities": "; ".join(grp["locality"]), "types": "; ".join(sorted(set(grp["locality_type"]))),
                "quality": "; ".join(sorted(set(grp["geocode_quality"]))),
                "road_only": bool((grp["locality_type"] == "road_corridor").all()),
                "landslide_class": c["gsi_susceptibility_class"] if isinstance(c["gsi_susceptibility_class"], str) else "none (no DEM)",
                "flood_class": c["flood_class"] if isinstance(c["flood_class"], str) else "none",
                "low_coverage": bool(c["low_coverage"]),
                "trigger": float(r["trigger"]), "tier": r["tier"],
                "p_without": int(r["p_without"]), "p_with": int(r["p_with"]),
                "rank_without": rank_range(d["p_without"].to_numpy(), int(r["p_without"])),
                "rank_with": rank_range(d["p_with"].to_numpy(), int(r["p_with"])),
            })
        rows = pd.DataFrame(cell_rows)
        rows_full = rows.copy()
        neigh = rows[~rows["road_only"]]
        results[str(day.date())] = {
            "rainfall_mm": None if pd.isna(rain) else float(rain), "grade": ev["evidence_grade"].iloc[0],
            "localities_in_ledger": loc_total, "localities_geocoded": loc_resolved, "geocoded_outside_grid": outside_grid,
            "district_trigger_max": float(d["trigger"].max()),
            "all_resolved": summarise(rows_full, d), "neighbourhood_points_only": summarise(neigh, d),
            "cells": rows_full.to_dict(orient="records"),
        }
        for rec in cell_rows:
            per_cell_rows.append({"date": str(day.date()), **rec})
    return {"events": results, "review_min_priority": REVIEW_MIN_PRIORITY, "replay_queue": replay_queue_effect(cells, trig),
            "flood_class_counts": cells["flood_class"].value_counts(dropna=False).to_dict()}


def replay_queue_effect(cells: pd.DataFrame, trig: pd.DataFrame) -> dict:
    """Review-queue size over every replay day (2018-2025), flood layer ignored vs with it."""
    wide = trig.pivot(index="date", columns="weather_point_id", values="trigger_prob")
    t = wide[cells["weather_point_id"]].to_numpy()
    ls = priority_levels(cells["gsi_susceptibility_class"], t)
    fl = priority_levels(cells["flood_class"], t)
    out = {"days": int(len(wide))}
    for tag, pr in (("without", ls), ("with", np.maximum(ls, fl))):
        flags = pr >= REVIEW_MIN_PRIORITY
        q = flags.sum(axis=1)
        a = q[q > 0]
        out[tag] = {"alert_days": int(len(a)), "cells_ever_flagged": int(flags.any(axis=0).sum()),
                    "median": float(np.median(a)), "q1": float(np.percentile(a, 25)), "q3": float(np.percentile(a, 75)),
                    "max": int(a.max()), "cell_days": int(flags.sum())}
    return out


def pooled(res: dict, view: str) -> dict:
    tot = {"n": 0, "review_with": 0, "review_without": 0, "flood_high": 0, "no_data": 0,
           "expected_with": 0.0, "expected_without": 0.0, "rose": 0, "crossed": 0}
    for ev in res["events"].values():
        s = ev[view]
        tot["n"] += s["n_cells"]; tot["review_with"] += s["with"]["review"]; tot["review_without"] += s["without"]["review"]
        tot["flood_high"] += s["flood_high"]; tot["no_data"] += s["n_no_data"]
        tot["expected_with"] += s["with"]["expected_by_chance"]; tot["expected_without"] += s["without"]["expected_by_chance"]
        tot["rose"] += s["with"]["rose_with_flood_layer"]; tot["crossed"] += s["with"]["crossed_into_review"]
    return tot


def reading_section(res: dict, p: dict, pn: dict) -> str:
    ev, rq = res["events"], res["replay_queue"]
    aug = ev["2024-08-05"]["all_resolved"]
    zero = [d for d in sorted(ev) if ev[d]["all_resolved"]["with"]["review"] == 0]
    quiet = [d for d in sorted(ev) if ev[d]["district_trigger_max"] < 0.5]
    lift_wo = p["review_without"] / p["expected_without"] if p["expected_without"] else float("nan")
    lift_w = p["review_with"] / p["expected_with"] if p["expected_with"] else float("nan")
    fc = res["flood_class_counts"]
    high_share = fc.get("High", 0) / (fc.get("High", 0) + fc.get("Low", 0) + fc.get("Moderate", 0))
    tiers = pd.DataFrame(ev["2024-08-05"]["cells"])
    parts = []
    for tier, grp in tiers.groupby("tier"):
        parts.append(f"{len(grp)} cells at {tier} ({grp['trigger'].min():.2f} to {grp['trigger'].max():.2f})")
    aug_tiers = "; ".join(parts)
    L = ["## Reading the result plainly\n"]
    L.append(f"- **Pooled over the 7 events**, {p['review_without']} of {p['n']} affected cells reached review priority with the flood layer ignored "
             f"({p['expected_without']:.1f} expected if the same number of cells were picked at random each day; observed/expected = {lift_wo:.1f}), and "
             f"{p['review_with']} of {p['n']} with the flood layer ({p['expected_with']:.1f} expected; observed/expected = {lift_w:.1f}).")
    verdict = ("**On this evidence PRAVAH does not show that it prioritises the verified flood localities better than chance.**"
               if max(lift_wo, lift_w) < 1.5 else
               "Observed counts are at least 1.5 times what chance gives, which is suggestive but rests on 38 weak labels.")
    L.append(f"- {verdict} The flood layer raises the count because it puts more cells into the queue, not because it picks the affected cells out of it.")
    L.append(f"- **5 Aug 2024 (78.4 mm, the strongest single event):** {aug['without']['review']} of {aug['n_cells']} affected cells reached review with the flood layer ignored and "
             f"{aug['with']['review']} of {aug['n_cells']} with it (chance {aug['without']['chance_at_least']:.2f} and {aug['with']['chance_at_least']:.2f}). "
             f"{aug['with']['levels']['1 Routine']} of {aug['n_cells']} stayed at Routine with the flood layer against {aug['without']['levels']['1 Routine']} without; "
             f"but {aug['with']['levels']['2 Watch']} sit at Watch, below review. The affected cells' triggers were: {aug_tiers}, although the source reports the city's "
             "highest daily rain of the season up to that point. **This event is not a success story and should not be presented as one.**")
    same = "On all of them" if zero == quiet else f"On {', '.join(quiet)}"
    L.append(f"- **Zero affected cells reached review** on {len(zero)} of 7 events ({', '.join(zero)}). {same} the model's highest trigger anywhere in the district "
             f"was below 0.5, so no layer stacked on top of it could flag anything. That is consistent with ERA5-Land at about 9 km not resolving short, local downpours "
             "(an inference; not tested here). The inventory singles out 14 Jun and 22 Jul 2025 as low-total, high-impact cases for testing drainage sensitivity; HAND contains no drains.")
    L.append(f"- **The flood class is close to uninformative here.** {100 * high_share:.0f}% of the cells with a HAND are flood-High, and {p['flood_high']} of {p['n']} "
             f"({100 * p['flood_high'] / p['n']:.0f}%) affected cells are High, about the base rate. The specified rule cannot separate flooded localities from the rest of the valley floor.")
    L.append(f"- **The flood layer changed the priority of {p['rose']} of {p['n']} affected cells and moved {p['crossed']} of them into review**, "
             "but it did so for many other cells too (next section).")
    L.append("- **Not tested here:** whether the model scores flood-affected cells higher than unaffected cells on the same day at finer resolution, "
             "anything about depth or duration, and any event before 2024 (the ledger has none).\n")
    L.append("### Side effect on the review queue, replay 2018-2025\n")
    a, b = rq["without"], rq["with"]
    L.append("The review level was chosen for the landslide-only system (median queue of at most 30 cells per alert day). Adding the flood layer changes the workload:\n")
    L.append("| | Flood layer ignored | With flood layer |\n|---|---|---|")
    L.append(f"| Alert days (of {rq['days']}) | {a['alert_days']} | {b['alert_days']} |")
    L.append(f"| Flagged cells per alert day, median (IQR) | {a['median']:.0f} ({a['q1']:.0f} to {a['q3']:.0f}) | {b['median']:.0f} ({b['q1']:.0f} to {b['q3']:.0f}) |")
    L.append(f"| Largest single-day queue | {a['max']} | {b['max']} |")
    L.append(f"| Cells flagged at least once | {a['cells_ever_flagged']} | {b['cells_ever_flagged']} |\n")
    limit_note = ("above" if b["median"] > 30 else "within")
    L.append(f"With the flood layer the median queue is {b['median']:.0f} cells, {limit_note} the 30-cell limit the review level was selected under "
             f"(`docs/priority_matrix.md`); the review level has not been re-selected for the combined layer.\n")
    return "\n".join(L) + "\n"


def _fmt_rank(r):
    return f"{r[0]}" if r[0] == r[1] else f"{r[0]}-{r[1]}"


def write_report(res: dict) -> None:
    ev = res["events"]
    dates = sorted(ev)
    fc = res["flood_class_counts"]
    L = []
    L.append("# Flood evidence: does PRAVAH prioritise verified urban-flood localities?\n")
    L.append("Generated by `app/flood_evidence.py`. Nothing was tuned to improve any number below.\n")
    L.append("## What this is, and what it is not\n")
    L.append("- **The labels are weak.** Each affected place is a **locality centroid**: one point from OpenStreetMap for a named "
             "neighbourhood, road or bus stop, assigned to the 1 km grid cell that contains it. They are **not surveyed flood polygons**. "
             "A 1 km cell around a point may or may not have flooded, and a flooded street can lie in the next cell.")
    L.append("- **None of these events was used to fit or select anything** in the trigger model, the labels, the susceptibility "
             "classes, the priority matrix, the review level or the flood-class thresholds. The flood-event ledger did not exist when those were fixed.")
    L.append("- **The ledger is a source-verified inventory, not a census** (`docs/guwahati_flash_flood_events_2018_2025.md`). "
             "The 14 Jun 2022 Boragaon row is excluded from the flood ledger (its source grades it C for flood validation, B for landslide). "
             "Years 2018-2021 and 2023 have no retained row, so this is 7 events in 2024-2025 only.")
    L.append("- **Not every named place could be located.** "
             f"{sum(e['localities_geocoded'] for e in ev.values())} of {sum(e['localities_in_ledger'] for e in ev.values())} "
             "event-locality rows have coordinates; the rest have no OpenStreetMap feature with exactly that name and were left blank, not guessed "
             "(`data/raw/urban_flood_events.csv`, `geocode_quality`). Unresolved localities are absent from every count below, "
             "which biases the test towards the places OpenStreetMap happens to name.\n")

    L.append("## How priority is computed here\n")
    L.append("Same pipeline as the app on `main`. The daily-peak trigger comes from `trigger_prob_daily.parquet` (RandomForest on ERA5-Land, "
             "trained on landslide labels, about 9 km resolution, 19 weather points). Priority is the 4x4 decision matrix in `app/config.py`.\n")
    L.append("- **Flood layer ignored (iv):** priority = matrix[landslide class][trigger tier]. This is exactly what `main` produces today.")
    L.append("- **With the flood layer:** priority = the higher of that and matrix[flood class][trigger tier], using the same matrix "
             "and the rows Low / Moderate / High by name. The combination rule (maximum of the two) is my choice; it was not tuned.")
    L.append(f"- **Review priority** means level {REVIEW_MIN_PRIORITY} ({PRIORITY_NAMES[REVIEW_MIN_PRIORITY]}) or above, the level selected for the landslide-only system. It was not re-selected for the flood layer.")
    L.append("- **Rank** is among all 904 cells that day, tie-honest: `a-b` means at best `a`th and at worst `b`th, because cells at the same priority are not ordered. "
             "Cells with no DEM rank last.\n")
    L.append(f"**The flood class is not selective.** With the specified rule it gives High to {fc.get('High', 0)} cells, Moderate to {fc.get('Moderate', 0)}, "
             f"Low to {fc.get('Low', 0)} and null to {sum(v for k, v in fc.items() if not isinstance(k, str))} (see `docs/flood_layer_sensitivity.md`). "
             "Being in flood-High is therefore close to the base rate for any valley-floor cell, and the last columns below show what chance would give.\n")

    L.append("## Headline: affected cells that reached review priority\n")
    L.append("\"Affected cells\" are the unique grid cells containing at least one geocoded locality for that event. "
             "\"Chance\" is the probability of getting at least that many review cells if the same number of scored cells were picked at random that day "
             "(hypergeometric); small means better than chance. Events are not independent and the labels are weak, so treat it as a guide, not a significance test.\n")
    L.append("| Event | Rain (mm) | Grade | Localities geocoded | Affected cells | (i) In flood-High | Review, flood layer ignored | Review, with flood layer | District review cells (ignored / with) | Chance (ignored / with) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for d in dates:
        e, s = ev[d], ev[d]["all_resolved"]
        rain = "not reported" if e["rainfall_mm"] is None else f"{e['rainfall_mm']:g}"
        L.append(f"| {d} | {rain} | {e['grade']} | {e['localities_geocoded']} of {e['localities_in_ledger']} | {s['n_cells']} | "
                 f"{s['flood_high']} of {s['n_cells']} | **{s['without']['review']} of {s['n_cells']}** | **{s['with']['review']} of {s['n_cells']}** | "
                 f"{s['without']['district_review_cells']} / {s['with']['district_review_cells']} of {s['scored_cells_district']} | "
                 f"{s['without']['chance_at_least']:.2f} / {s['with']['chance_at_least']:.2f} |")
    p = pooled(res, "all_resolved")
    L.append(f"| **All 7 events pooled** | | | | {p['n']} | {p['flood_high']} of {p['n']} | **{p['review_without']} of {p['n']}** | **{p['review_with']} of {p['n']}** | | |")
    L.append(f"| Expected by chance (same number of cells drawn at random each day) | | | | | | {p['expected_without']:.1f} of {p['n']} | {p['expected_with']:.1f} of {p['n']} | | |")
    pn = pooled(res, "neighbourhood_points_only")
    L.append(f"\nNeighbourhood points only (road corridors excluded; declared before results): pooled {pn['review_without']} of {pn['n']} reached review with the flood layer ignored, "
             f"{pn['review_with']} of {pn['n']} with it; {pn['flood_high']} of {pn['n']} are in flood-High. Per-event figures for this view are in `docs/flood_evidence.json`.\n")

    L.append(reading_section(res, p, pn))
    L.append("## (ii) Priority received by the affected cells\n")
    L.append("| Event | View | No data | 1 Routine | 2 Watch | 3 Elevated | 4 Critical |")
    L.append("|---|---|---|---|---|---|---|")
    for d in dates:
        s = ev[d]["all_resolved"]
        for tag, label in (("without", "flood layer ignored"), ("with", "with flood layer")):
            lv = s[tag]["levels"]
            L.append(f"| {d} | {label} | {lv['No data']} | {lv['1 Routine']} | {lv['2 Watch']} | {lv['3 Elevated']} | {lv['4 Critical']} |")
    L.append("")

    for d in dates:
        e = ev[d]
        L.append(f"## {d}" + (" (78.4 mm, the strongest event)" if d == "2024-08-05" else "") + "\n")
        rain = "not reported" if e["rainfall_mm"] is None else f"{e['rainfall_mm']:g} mm"
        L.append(f"Rainfall reported: {rain}. Evidence grade {e['grade']}. Highest model trigger anywhere in the district that day: {e['district_trigger_max']:.2f}. "
                 f"{e['localities_geocoded']} of {e['localities_in_ledger']} localities geocoded"
                 + (f"; {e['geocoded_outside_grid']} fall outside the 904-cell grid" if e["geocoded_outside_grid"] else "") + ".\n")
        L.append("| Cell | Localities | Landslide class | Flood class | Trigger | Priority, ignored | Rank, ignored | Priority, with flood | Rank, with flood |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for c in e["cells"]:
            trig = f"{c['tier']} {c['trigger']:.2f}" if c["tier"] else "n/a"
            lowc = " (partial DEM)" if c["low_coverage"] else ""
            L.append(f"| {c['grid_id']} | {c['localities']} | {c['landslide_class']} | {c['flood_class']}{lowc} | {trig} | "
                     f"{LEVEL_LABEL[c['p_without']]} | {_fmt_rank(c['rank_without'])} | {LEVEL_LABEL[c['p_with']]} | {_fmt_rank(c['rank_with'])} |")
        L.append("")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> None:
    res = run()
    OUT_JSON.write_text(json.dumps(res, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o)), encoding="utf-8")
    write_report(res)
    for d, e in sorted(res["events"].items()):
        s = e["all_resolved"]
        print(f"{d}: cells {s['n_cells']:2d} (no data {s['n_no_data']}), flood-High {s['flood_high']:2d}, review ignored {s['without']['review']:2d} "
              f"/ with {s['with']['review']:2d}; district review {s['without']['district_review_cells']}/{s['with']['district_review_cells']} of {s['scored_cells_district']}; "
              f"max trigger {e['district_trigger_max']:.2f}")
    print("pooled all:", pooled(res, "all_resolved"), " neighbourhood-only:", pooled(res, "neighbourhood_points_only"))
    print("wrote", OUT_MD.relative_to(ROOT), OUT_JSON.relative_to(ROOT))


if __name__ == "__main__":
    main()
