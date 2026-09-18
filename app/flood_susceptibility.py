"""
app/flood_susceptibility.py
Ordinal FLOOD susceptibility class per 1 km cell, kept entirely separate from the
slope/landslide class (app/susceptibility_utils.py).

Run from the repo root with the pinned venv:

    venv\\Scripts\\python.exe app/flood_susceptibility.py

Reads   data/processed/flood_terrain_features.parquet   (locally derived HAND; see docs/flood_layer_method.md)
Writes  data/processed/flood_susceptibility.parquet
        docs/flood_layer_sensitivity.md

Rule (as specified; not tuned):
    channel condition = upa_max > channel threshold (1 km2)  OR  chan_dist_m < 300 m
    High     if hand_min < 5 m  AND channel condition
    Moderate if hand_min < 10 m AND channel condition (and not High)
    Low      otherwise
    null     where HAND is unavailable (no DEM, or no valid HAND)

NOT USED: built-up fraction. It came from an Earth Engine export (ESA WorldCover) that
failed, so that term is omitted from the class. Urban density therefore plays no part.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.flood_terrain import CHANNEL_MIN_KM2  # noqa: E402  (1 km2; single source of truth)

ROOT = Path(__file__).resolve().parents[1]
IN_PARQUET = ROOT / "data" / "processed" / "flood_terrain_features.parquet"
OUT_PARQUET = ROOT / "data" / "processed" / "flood_susceptibility.parquet"
OUT_DOC = ROOT / "docs" / "flood_layer_sensitivity.md"

FLOOD_CLASSES = ("Low", "Moderate", "High")        # ordinal, lowest to highest
HAND_HIGH_M = 5.0
HAND_MODERATE_M = 10.0
CHAN_DIST_M = 300.0

# Sensitivity grid. HAND value = the High threshold; the Moderate threshold is 2x it
# (baseline 5 / 10), so 3 -> 6, 5 -> 10, 10 -> 20.
SENS_HAND_M = (3.0, 5.0, 10.0)
SENS_CHAN_M = (200.0, 300.0, 500.0)
FULL_CELL_QUANTILE = 0.95
MIN_COVERAGE_FRAC = 0.5      # same "well covered" rule as flood_terrain.py


def classify_flood(hand_min, upa_max, chan_dist_m, high_hand_m=HAND_HIGH_M,
                   moderate_hand_m=HAND_MODERATE_M, chan_limit_m=CHAN_DIST_M,
                   upa_threshold_km2=CHANNEL_MIN_KM2) -> pd.Series:
    """Ordinal flood class ('Low' < 'Moderate' < 'High'), <NA> where hand_min is unavailable.

    A missing upa_max or chan_dist_m counts as "condition not met", not as unknown: HAND is
    available for those cells, so they are classified on what is known (24 cells have a
    HAND but no channel distance because their centroid lies outside the DEM tile).
    """
    hand_min = pd.Series(hand_min).astype(float)
    channel_condition = (pd.Series(upa_max).astype(float) > upa_threshold_km2) | (
        pd.Series(chan_dist_m).astype(float) < chan_limit_m)
    high = (hand_min < high_hand_m) & channel_condition
    moderate = (hand_min < moderate_hand_m) & channel_condition & ~high
    out = pd.Series(np.where(high, "High", np.where(moderate, "Moderate", "Low")), index=hand_min.index,
                    dtype="object")
    out[hand_min.isna()] = None
    return pd.Series(pd.Categorical(out, categories=list(FLOOD_CLASSES), ordered=True), index=hand_min.index)


def build_table() -> pd.DataFrame:
    feat = pd.read_parquet(IN_PARQUET)
    assert len(feat) == 904 and feat["grid_id"].is_unique
    cls = classify_flood(feat["hand_min"], feat["upa_max"], feat["chan_dist_m"])
    full_px = feat.loc[feat["hand_min"].notna(), "n_valid_hand"].quantile(FULL_CELL_QUANTILE)
    out = pd.DataFrame({
        "grid_id": feat["grid_id"],
        "flood_class": cls.astype("object"),
        "flood_class_ord": cls.cat.codes.where(cls.notna()).add(1).astype("Int8"),   # 1 Low, 2 Moderate, 3 High
        "low_coverage": (feat["n_valid_hand"] < MIN_COVERAGE_FRAC * full_px) & feat["hand_min"].notna(),
        "chan_dist_missing": feat["hand_min"].notna() & feat["chan_dist_m"].isna(),
    })
    return out


def sensitivity_table() -> pd.DataFrame:
    feat = pd.read_parquet(IN_PARQUET)
    rows = []
    for h in SENS_HAND_M:
        for d in SENS_CHAN_M:
            cls = classify_flood(feat["hand_min"], feat["upa_max"], feat["chan_dist_m"],
                                 high_hand_m=h, moderate_hand_m=2 * h, chan_limit_m=d)
            counts = cls.value_counts()
            rows.append({"hand_high_m": h, "hand_moderate_m": 2 * h, "chan_dist_m": d,
                         "High": int(counts.get("High", 0)), "Moderate": int(counts.get("Moderate", 0)),
                         "Low": int(counts.get("Low", 0)), "null": int(cls.isna().sum()),
                         "baseline": (h == HAND_HIGH_M and d == CHAN_DIST_M)})
    return pd.DataFrame(rows)


def write_doc(table: pd.DataFrame, sens: pd.DataFrame) -> None:
    feat = pd.read_parquet(IN_PARQUET)
    ok = feat[feat["hand_min"].notna()]
    n = len(ok)
    base = sens[sens["baseline"]].iloc[0]
    q = ok["hand_min"].quantile([0.5, 0.75, 0.9, 0.95]).to_dict()
    qm = ok["hand_mean"].quantile([0.1, 0.25, 0.5, 0.75, 0.9]).to_dict()
    L = []
    L.append("# Flood susceptibility class: definition and sensitivity\n")
    L.append("Generated by `app/flood_susceptibility.py` from `data/processed/flood_terrain_features.parquet` "
             "(locally derived HAND, see `docs/flood_layer_method.md`). This is a terrain proxy, not an inundation model. "
             "It is kept entirely separate from the slope/landslide class.\n")
    L.append("**Built-up fraction is NOT used.** It was to come from an Earth Engine export (ESA WorldCover), which failed, "
             "so that term is omitted. Nothing in this class knows how urban a cell is.\n")
    L.append("## Rule (as specified, not tuned)\n")
    L.append(f"- Channel condition: `upa_max` > {CHANNEL_MIN_KM2:g} km2 (the channel threshold) **or** `chan_dist_m` < {CHAN_DIST_M:.0f} m.")
    L.append(f"- **High**: `hand_min` < {HAND_HIGH_M:g} m and the channel condition.")
    L.append(f"- **Moderate**: `hand_min` < {HAND_MODERATE_M:g} m and the channel condition (and not High).")
    L.append("- **Low**: otherwise. **null** where HAND is unavailable (no DEM, or no valid HAND).")
    L.append("- A missing `upa_max` or `chan_dist_m` counts as \"condition not met\". "
             f"{int(table['chan_dist_missing'].sum())} cells have a HAND but no channel distance (centroid outside the DEM tile) "
             f"and are classified on `upa_max` alone; {int(table['low_coverage'].sum())} cells are covered by less than half a "
             "cell's worth of valid pixels (`low_coverage` column).\n")
    L.append("## Result at the baseline thresholds (HAND 5 / 10 m, channel distance 300 m)\n")
    L.append(f"| Class | Cells |\n|---|---|\n| High | {base['High']} |\n| Moderate | {base['Moderate']} |\n| Low | {base['Low']} |\n"
             f"| null (HAND unavailable) | {base['null']} |\n| Total | {int(base[['High', 'Moderate', 'Low', 'null']].sum())} |\n")
    L.append("## Sensitivity\n")
    L.append("The HAND value is the High threshold; the Moderate threshold is twice it (as in the 5 / 10 baseline). "
             "The channel-distance value replaces the 300 m. The baseline row is in bold.\n")
    L.append("| High: HAND below (m) | Moderate: HAND below (m) | Channel distance below (m) | High | Moderate | Low | null |")
    L.append("|---|---|---|---|---|---|---|")
    for _, r in sens.iterrows():
        cells = [f"{r['hand_high_m']:g}", f"{r['hand_moderate_m']:g}", f"{r['chan_dist_m']:.0f}",
                 str(r["High"]), str(r["Moderate"]), str(r["Low"]), str(r["null"])]
        L.append("| " + " | ".join(f"**{c}**" if r["baseline"] else c for c in cells) + " |")
    L.append("")
    L.append("## How to read this: the class is not selective\n")
    L.append(f"- Of the {n} cells with a HAND, **{int((ok['hand_min'] < 5).sum())} ({100 * (ok['hand_min'] < 5).mean():.0f}%) have `hand_min` below 5 m** "
             f"and {int((ok['hand_min'] == 0).sum())} have `hand_min` of exactly 0. `hand_min` is the minimum over roughly 1,170 "
             "pixels per cell, and 17% of all DEM pixels have HAND = 0 (integer-metre DEM, see `docs/flood_layer_method.md`), "
             "so almost every cell contains some pixel that is low.")
    L.append(f"- Median `hand_min` is {q[0.5]:.1f} m, the 90th percentile {q[0.9]:.1f} m. By contrast `hand_mean` "
             f"(not used by this rule) has a 10th to 90th percentile range of {qm[0.1]:.1f} to {qm[0.9]:.1f} m.")
    L.append(f"- Consequently **High covers {100 * base['High'] / n:.0f}% of cells with a HAND, and Moderate is "
             f"{'empty' if base['Moderate'] == 0 else 'small'}** at the baseline; widening or narrowing the HAND and "
             "channel thresholds within the ranges above moves High only between the counts in the table.")
    L.append("- `upa_max` > 1 km2 is close to \"the cell contains a channel pixel\", since a channel pixel is by definition above that "
             "threshold, so the channel condition is met by most cells with a low `hand_min`.")
    L.append("- The rule was implemented as specified and **not adjusted** after seeing this. A class this broad cannot separate "
             "flood-prone cells from the rest of the valley floor; any test that asks whether affected localities are in \"High\" "
             "has a high base rate to beat. Options such as ranking on `hand_mean` instead are noted for a decision, not applied.\n")
    OUT_DOC.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> None:
    table = build_table()
    sens = sensitivity_table()
    table.to_parquet(OUT_PARQUET, index=False)
    write_doc(table, sens)
    print(table["flood_class"].value_counts(dropna=False).to_string())
    print("low_coverage:", int(table["low_coverage"].sum()), " chan_dist_missing:", int(table["chan_dist_missing"].sum()))
    print(sens.to_string(index=False))
    print("wrote", OUT_PARQUET.relative_to(ROOT), OUT_DOC.relative_to(ROOT))


if __name__ == "__main__":
    main()
