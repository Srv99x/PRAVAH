"""
app/priority_analysis.py
Evidence for the priority decision matrix (app/config.py: PRIORITY_MATRIX).

Run from the repo root with the pinned venv (scikit-learn 1.9.0):

    venv\\Scripts\\python.exe app/priority_analysis.py

Writes docs/priority_matrix.md and docs/priority_matrix.json.

Part B  Alert-cut selection.  Which minimum priority level enters the review
        queue?  Rule: maximise EPISODE RECALL subject to a MEDIAN review queue
        of <= 30 cells per alert day.  Chosen on TRAINING folds only:
          * same 5 episode-grouped folds as app/evaluate_trigger_cv.py
          * for each outer fold, the trigger model is re-trained on the training
            episodes; the training episodes are then scored OUT-OF-FOLD by an
            inner 4-fold split (a model never scores hours from episodes it was
            trained on), and the cut is picked from those scores;
          * the held-out fold is scored by the outer model and used only to report.
Part A  Replay comparison on the 2018-2025 cached trigger probabilities
        (data/processed/trigger_prob_daily.parquet): old rule vs the matrix.

Episode recall: an "episode" is a group (storm episode, or the quiet interval
holding a verified incident) that contains at least one positive label. It is
caught at cut c if at least one of its POSITIVE cells has priority >= c on the
day of the positive. This is cell-aware on purpose: "some cell somewhere was
flagged" would be met by almost any cut.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from sklearn.model_selection import StratifiedGroupKFold

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.train_trigger_model as T
from app.config import (
    PRIORITY_MATRIX, PRIORITY_NAMES, REVIEW_MIN_PRIORITY, SUSCEPTIBILITY_MULTIPLIERS,
    SUSCEPTIBILITY_ORDER, TRIGGER_TIER_LABELS, priority_level, priority_levels,
)
from app.evaluate_trigger_cv import rf

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CANDIDATE_CUTS = (2, 3, 4)
MAX_MEDIAN_QUEUE = 30
OLD_THRESHOLD = 0.50
INNER_SPLITS = 4
MONSOON = (5, 6, 7, 8, 9, 10)


# ── shared helpers ────────────────────────────────────────────────────

def queue_stats(flags: np.ndarray) -> dict:
    """flags: days x cells bool. Queue statistics over ALERT days (>= 1 flagged cell)."""
    queue = flags.sum(axis=1)
    alert = queue[queue > 0]
    return {
        "alert_days": int(len(alert)),
        "cells_ever_flagged": int(flags.any(axis=0).sum()),
        "flagged_cell_days": int(flags.sum()),
        "queue_median": float(np.median(alert)) if len(alert) else None,
        "queue_q1": float(np.percentile(alert, 25)) if len(alert) else None,
        "queue_q3": float(np.percentile(alert, 75)) if len(alert) else None,
        "queue_max": int(alert.max()) if len(alert) else 0,
    }


def load_cells() -> pd.DataFrame:
    """Every cell with a DEM-derived susceptibility class (the no-DEM cells are excluded)."""
    mapping = pd.read_parquet(T.DATA_DIR / "grid_weather_mapping.parquet",
                              columns=["grid_id", "weather_point_id"])
    susc = pd.read_parquet(T.DATA_DIR / "susceptibility_features.parquet")
    cells = mapping.merge(susc[["grid_id", "gsi_susceptibility_class"]], on="grid_id", how="left")
    n_total = len(cells)
    no_dem = json.loads((T.DATA_DIR / "missing_terrain_cells.json").read_text())
    assert cells["gsi_susceptibility_class"].isna().sum() == len(no_dem)
    cells = cells[cells["gsi_susceptibility_class"].notna()].reset_index(drop=True)
    cells.attrs["n_total"] = n_total
    return cells


# ── Part B: nested cut selection ──────────────────────────────────────

def build_training_frame():
    """Identical construction to app/evaluate_trigger_cv.py (same rows, same groups)."""
    grid = gpd.read_parquet(T.DATA_DIR / "kamrup_metro_grid_1km.parquet")
    weather = T.load_weather()
    mapping = T.load_mapping_with_terrain()
    pos = T.pass1_collect_positives(weather, mapping, grid)
    safe = T.compute_safe_cells(pos, grid)
    neg = T.pass2_sample_negatives(weather, mapping, grid, safe, len(pos) * T.NEG_TO_POS_RATIO)
    df = pd.concat([pos, neg], ignore_index=True).sort_values(["grid_id", "timestamp"]).reset_index(drop=True)
    starts, ends = T.build_storm_episodes(weather)
    df["episode_group"] = T.assign_groups(df["timestamp"], starts, ends)
    ref = json.loads((DOCS / "evaluation_cv.json").read_text())
    assert len(pos) == ref["n_positive"] and len(neg) == ref["n_negative"], "training set differs from evaluate_trigger_cv"
    return df, weather, starts, ends


def hourly_units(weather: pd.DataFrame, starts, ends) -> pd.DataFrame:
    """Monsoon hours with their episode group and calendar date."""
    hourly = weather[["weather_point_id", "timestamp"] + T.FEATURES].copy()
    stamps = pd.Series(np.sort(hourly["timestamp"].unique()))
    group_of = pd.Series(T.assign_groups(stamps, starts, ends).to_numpy(), index=stamps.to_numpy())
    hourly["group"] = hourly["timestamp"].map(group_of)
    hourly["date"] = hourly["timestamp"].dt.normalize()
    return hourly


def unit_peaks(model, hourly: pd.DataFrame, groups: set) -> pd.DataFrame:
    """Peak trigger per (weather point, date, group), scored by `model`, for the given groups only."""
    sub = hourly[hourly["group"].isin(groups)]
    prob = model.predict_proba(sub[T.FEATURES].astype("float32"))[:, 1]
    return (sub.assign(p=prob)
            .groupby(["weather_point_id", "date", "group"], as_index=False)["p"].max())


class Side:
    """One side (train or test) of an outer fold: peaks, day presence, positives lookups."""

    def __init__(self, units, dates, wps, cells, cell_wp_idx, positives, groups):
        peak = (units.groupby(["date", "weather_point_id"])["p"].max()
                .unstack("weather_point_id").reindex(index=dates, columns=wps))
        self.present = peak.notna().any(axis=1).to_numpy()
        self.trigger_by_cell = peak.fillna(0.0).to_numpy()[:, cell_wp_idx]
        self.cells = cells
        self.priority = priority_levels(cells["gsi_susceptibility_class"], self.trigger_by_cell)
        self.old_flags = (self.trigger_by_cell
                          * cells["gsi_susceptibility_class"].map(SUSCEPTIBILITY_MULTIPLIERS).to_numpy()
                          ) >= OLD_THRESHOLD
        self.positives = positives[positives["group"].isin(groups)]
        self.n_episodes = int(self.positives["group"].nunique())

    def caught(self, flags: np.ndarray) -> int:
        """Episodes with >= 1 positive (cell, day) flagged. flags: days x cells bool."""
        p = self.positives
        hit = np.zeros(len(p), dtype=bool)
        ok = p["cell_idx"].to_numpy() >= 0
        hit[ok] = flags[p["day_idx"].to_numpy()[ok], p["cell_idx"].to_numpy()[ok]]
        return int(p.assign(hit=hit).groupby("group")["hit"].any().sum())

    def evaluate(self, cut: int | None) -> dict:
        """Recall + queue stats for the matrix at `cut`, or for the OLD rule if cut is None."""
        flags = self.old_flags if cut is None else self.priority >= cut
        out = queue_stats(flags[self.present])
        out["episodes"] = self.n_episodes
        out["episodes_caught"] = self.caught(flags)
        out["recall"] = out["episodes_caught"] / self.n_episodes
        return out


def choose_cut(train_stats: dict) -> tuple[int, str]:
    """Max recall subject to median queue <= MAX_MEDIAN_QUEUE; ties -> the higher cut (smaller queue)."""
    feasible = [c for c in CANDIDATE_CUTS
                if train_stats[c]["queue_median"] is None or train_stats[c]["queue_median"] <= MAX_MEDIAN_QUEUE]
    if not feasible:
        return max(CANDIDATE_CUTS), "no cut met the queue limit; took the strictest"
    best = max(train_stats[c]["recall"] for c in feasible)
    tied = [c for c in feasible if train_stats[c]["recall"] == best]
    return max(tied), ("unique best recall" if len(tied) == 1 else "recall tie; took the higher cut")


def select_cut_nested() -> dict:
    df, weather, starts, ends = build_training_frame()
    hourly = hourly_units(weather, starts, ends)
    cells = load_cells()
    wps = sorted(hourly["weather_point_id"].unique())
    cell_wp_idx = cells["weather_point_id"].map({w: i for i, w in enumerate(wps)}).to_numpy()
    dates = pd.DatetimeIndex(sorted(hourly["date"].unique()))
    day_idx = {d: i for i, d in enumerate(dates)}

    pos = df[df["target_event"] == 1].copy()
    pos["group"] = pos["episode_group"]
    pos["day_idx"] = pos["timestamp"].dt.normalize().map(day_idx)
    pos["cell_idx"] = pos["grid_id"].map({g: i for i, g in enumerate(cells["grid_id"])}).fillna(-1).astype(int)

    X = df[T.FEATURES].astype("float32")
    y = df["target_event"].to_numpy()
    g = df["episode_group"]
    outer = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)
    folds = []
    for k, (tr, te) in enumerate(outer.split(X, y, g)):
        train_groups, test_groups = set(g.iloc[tr]), set(g.iloc[te])
        assert not train_groups & test_groups
        # training side: every training episode scored by an inner model that never saw it
        inner = StratifiedGroupKFold(n_splits=INNER_SPLITS, shuffle=True, random_state=42)
        parts = []
        for itr, ite in inner.split(X.iloc[tr], y[tr], g.iloc[tr]):
            model = rf().fit(X.iloc[tr].iloc[itr], y[tr][itr])
            parts.append(unit_peaks(model, hourly, set(g.iloc[tr].iloc[ite])))
        train_side = Side(pd.concat(parts), dates, wps, cells, cell_wp_idx, pos, train_groups)
        # held-out side: scored by the outer model, used only to report
        outer_model = rf().fit(X.iloc[tr], y[tr])
        test_side = Side(unit_peaks(outer_model, hourly, test_groups), dates, wps, cells,
                         cell_wp_idx, pos, test_groups)

        train_stats = {c: train_side.evaluate(c) for c in CANDIDATE_CUTS}
        cut, why = choose_cut(train_stats)
        folds.append({
            "fold": k, "chosen_cut": cut, "why": why,
            "train": {str(c): train_stats[c] for c in CANDIDATE_CUTS},
            "train_old_rule": train_side.evaluate(None),
            "test": {str(c): test_side.evaluate(c) for c in CANDIDATE_CUTS},
            "test_old_rule": test_side.evaluate(None),
        })
        print(f"fold {k}: chosen cut {cut} ({why}); "
              f"train recall/median " + ", ".join(
                  f"c{c}={train_stats[c]['recall']:.2f}/{train_stats[c]['queue_median']}" for c in CANDIDATE_CUTS),
              flush=True)

    votes = [f["chosen_cut"] for f in folds]
    counts = {c: votes.count(c) for c in CANDIDATE_CUTS}
    selected = max(c for c in CANDIDATE_CUTS if counts[c] == max(counts.values()))
    pooled = {}
    for key, label in (("matrix_at_selected", None), ("matrix_at_fold_choice", "choice"), ("old_rule", "old")):
        caught = total = 0
        for f in folds:
            s = (f["test_old_rule"] if key == "old_rule"
                 else f["test"][str(f["chosen_cut"] if label == "choice" else selected)])
            caught += s["episodes_caught"]
            total += s["episodes"]
        pooled[key] = {"episodes_caught": caught, "episodes": total, "recall": caught / total}
    positives_by_class = (pos.merge(cells[["grid_id", "gsi_susceptibility_class"]], on="grid_id", how="left")
                          ["gsi_susceptibility_class"].fillna("No DEM").value_counts().to_dict())
    return {"folds": folds, "votes": counts, "selected_cut": selected, "pooled_test": pooled,
            "positives_by_class": positives_by_class,
            "episodes_total": int(pos["group"].nunique()),
            "episodes_storm": int(pos.loc[pos["group"].str.contains("_S"), "group"].nunique())}


# ── Part A: replay comparison ─────────────────────────────────────────

def replay_comparison(selected_cut: int) -> dict:
    cells = load_cells()
    cache = pd.read_parquet(T.DATA_DIR / "trigger_prob_daily.parquet")
    cache["date"] = pd.to_datetime(cache["date"])
    daily = cache.pivot(index="date", columns="weather_point_id", values="trigger_prob")
    trigger = daily[cells["weather_point_id"]].to_numpy()
    classes = cells["gsi_susceptibility_class"].to_numpy()
    priority = priority_levels(classes, trigger)
    old = trigger * cells["gsi_susceptibility_class"].map(SUSCEPTIBILITY_MULTIPLIERS).to_numpy() >= OLD_THRESHOLD

    # the vectorised lookup must agree with the scalar one used by the app's text
    rng = np.random.default_rng(0)
    for _ in range(2000):
        d, c = rng.integers(len(daily)), rng.integers(len(cells))
        assert priority[d, c] == priority_level(classes[c], trigger[d, c])

    months = daily.index.month.to_numpy()
    years = len(set(daily.index.year))

    def summarise(flags):
        out = queue_stats(flags)
        alert = flags.any(axis=1)
        out["alert_days_by_month"] = {int(m): int(alert[months == m].sum()) for m in range(1, 13)}
        out["alert_days_outside_monsoon"] = int(alert[~np.isin(months, MONSOON)].sum())
        out["flagged_cell_days_by_class"] = {
            c: int(flags[:, classes == c].sum()) for c in SUSCEPTIBILITY_ORDER}
        return out

    structural = {}
    for cut in CANDIDATE_CUTS:
        reachable = {s for s in SUSCEPTIBILITY_ORDER if max(PRIORITY_MATRIX[s].values()) >= cut}
        structural[cut] = int(np.isin(classes, list(reachable)).sum())
    old_reachable = {s for s in SUSCEPTIBILITY_ORDER if SUSCEPTIBILITY_MULTIPLIERS[s] >= OLD_THRESHOLD}
    return {
        "days": int(len(daily)), "years": years, "cells_total": cells.attrs["n_total"],
        "cells_with_class": int(len(cells)),
        "cells_by_class": {s: int((classes == s).sum()) for s in SUSCEPTIBILITY_ORDER},
        "old_rule": {**summarise(old), "structurally_eligible": int(np.isin(classes, list(old_reachable)).sum())},
        "matrix": {str(cut): {**summarise(priority >= cut), "structurally_eligible": structural[cut]}
                   for cut in CANDIDATE_CUTS},
        "selected_cut": selected_cut,
    }


# ── report ────────────────────────────────────────────────────────────

def _iqr(s):
    return "n/a" if s["queue_median"] is None else f"{s['queue_median']:.0f} ({s['queue_q1']:.0f} to {s['queue_q3']:.0f})"


def write_report(cv: dict, replay: dict) -> str:
    sel = cv["selected_cut"]
    sel_name = PRIORITY_NAMES[sel]
    old, new = replay["old_rule"], replay["matrix"][str(sel)]
    L = []
    L.append("# Priority decision matrix: replay comparison and alert-cut selection\n")
    L.append("Generated by `app/priority_analysis.py`. Trigger model, labels and susceptibility classes are unchanged.\n")
    L.append("## 1. The rule\n")
    L.append("`priority = PRIORITY_MATRIX[susceptibility class][trigger tier]` (in `app/config.py`). "
             "Trigger tiers from the model score: T1 < 0.25, T2 0.25-0.50, T3 0.50-0.80, T4 >= 0.80.\n")
    L.append("| Susceptibility | T1 | T2 | T3 | T4 |\n|---|---|---|---|---|")
    for s in SUSCEPTIBILITY_ORDER:
        L.append(f"| {s} | " + " | ".join(f"{PRIORITY_MATRIX[s][t]} {PRIORITY_NAMES[PRIORITY_MATRIX[s][t]]}"
                                          for t in TRIGGER_TIER_LABELS) + " |")
    L.append(f"\nCells at priority **{sel} ({sel_name})** or above enter the review queue. "
             "The table entries are team-assigned judgements, not values from a published study. "
             "The whole T1 column is Routine on purpose: a cell above Routine under a weak trigger would sit on the "
             "list every dry day, and the alert-day count would mean nothing.\n")
    L.append("**Why the old rule failed.** `trigger x weight >= 0.50` with weights 0.20 / 0.45 / 0.70 / 0.90: "
             "0.45 x 1.0 < 0.50, so Low and Moderate cells could never be flagged, whatever the weather.\n")

    L.append("## 2. Replay 2018-2025 (%d days), old rule vs matrix\n" % replay["days"])
    L.append(f"Cells: {replay['cells_total']} in the grid, {replay['cells_with_class']} with a susceptibility class "
             f"({', '.join(f'{v} {k}' for k, v in replay['cells_by_class'].items())}); "
             f"{replay['cells_total'] - replay['cells_with_class']} have no DEM coverage and are never scored. "
             "Trigger probabilities are the cached production values, so this is a **workload** description, not an accuracy claim.\n")
    L.append("| | Old rule (trigger x weight >= 0.50) | " + " | ".join(
        f"Matrix, cut {c} ({PRIORITY_NAMES[c]}+)" + (" **selected**" if c == sel else "") for c in CANDIDATE_CUTS) + " |")
    L.append("|---|---|" + "---|" * len(CANDIDATE_CUTS))
    cols = [old] + [replay["matrix"][str(c)] for c in CANDIDATE_CUTS]
    rows = [
        ("Cells that can ever be flagged (by the rule)", lambda s: str(s["structurally_eligible"])),
        ("Cells actually flagged at least once in the replay", lambda s: str(s["cells_ever_flagged"])),
        ("Alert days (>= 1 flagged cell)", lambda s: f"{s['alert_days']} ({100 * s['alert_days'] / replay['days']:.0f}% of days)"),
        ("Flagged cells per alert day: median (IQR)", _iqr),
        ("Largest single-day queue", lambda s: str(s["queue_max"])),
        ("Flagged cell-days, all", lambda s: str(s["flagged_cell_days"])),
    ]
    for label, fn in rows:
        L.append(f"| {label} | " + " | ".join(fn(s) for s in cols) + " |")
    for cls in SUSCEPTIBILITY_ORDER:
        L.append(f"| Flagged cell-days in {cls} cells | " + " | ".join(str(s["flagged_cell_days_by_class"][cls]) for s in cols) + " |")

    L.append(f"\n### Alert days per month (total over {replay['years']} years; per-year average in brackets)\n")
    L.append("| Month | Old rule | " + " | ".join(f"Matrix cut {c}" for c in CANDIDATE_CUTS) + " |")
    L.append("|---|---|" + "---|" * len(CANDIDATE_CUTS))
    names = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    for m in range(1, 13):
        if m not in MONSOON and not any(s["alert_days_by_month"][m] for s in cols):
            continue
        tag = "" if m in MONSOON else " (outside monsoon)"
        L.append(f"| {names[m]}{tag} | " + " | ".join(
            f"{s['alert_days_by_month'][m]} ({s['alert_days_by_month'][m] / replay['years']:.1f})" for s in cols) + " |")
    L.append("")

    L.append("## 3. Alert-cut selection (training folds only)\n")
    L.append(f"**Rule:** among candidate cuts {list(CANDIDATE_CUTS)} (priority levels), pick the one with the highest "
             f"episode recall subject to a median review queue <= {MAX_MEDIAN_QUEUE} cells per alert day; on a recall tie take the higher "
             "cut (smaller queue); if no cut meets the limit take the strictest.\n")
    L.append("**Leak safety.** Same 5 episode-grouped folds as `app/evaluate_trigger_cv.py`. In each outer fold the trigger model is "
             f"re-trained on the training episodes only, the training episodes are scored out-of-fold by an inner {INNER_SPLITS}-fold split "
             "(no model scores hours from an episode it trained on), and the cut is chosen from those scores. The held-out fold is scored "
             "by the outer model and used only to report. The cached production model is not used here because it was trained on most "
             "of these episodes.\n")
    L.append(f"**Episode recall.** {cv['episodes_total']} episodes contain at least one positive label "
             f"({cv['episodes_storm']} storm episodes, {cv['episodes_total'] - cv['episodes_storm']} quiet-interval verified incidents). "
             "An episode is caught if at least one of its positive cells has priority >= cut on the day of the positive.\n")
    L.append("| Fold | Train recall / median queue at cut 2 | cut 3 | cut 4 | **Chosen** | Held-out recall at chosen cut | Held-out median queue (IQR) | Held-out recall, old rule |")
    L.append("|---|---|---|---|---|---|---|---|")
    for f in cv["folds"]:
        tr = [f["train"][str(c)] for c in CANDIDATE_CUTS]
        te = f["test"][str(f["chosen_cut"])]
        L.append(f"| {f['fold']} | " + " | ".join(
            f"{s['episodes_caught']}/{s['episodes']} / {s['queue_median'] if s['queue_median'] is None else format(s['queue_median'], '.0f')}" for s in tr)
                 + f" | **{f['chosen_cut']}** ({f['why']}) | {te['episodes_caught']}/{te['episodes']} | {_iqr(te)} | "
                 f"{f['test_old_rule']['episodes_caught']}/{f['test_old_rule']['episodes']} |")
    v = cv["votes"]
    L.append(f"\nVotes across folds: {', '.join(f'cut {c}: {n}' for c, n in v.items())}. **Selected cut: {sel} ({sel_name}).**\n")
    p = cv["pooled_test"]
    L.append(f"Pooled held-out episode recall (each episode is held out exactly once): matrix at cut {sel} = "
             f"{p['matrix_at_selected']['episodes_caught']}/{p['matrix_at_selected']['episodes']} "
             f"({100 * p['matrix_at_selected']['recall']:.0f}%); matrix at each fold's own choice = "
             f"{p['matrix_at_fold_choice']['episodes_caught']}/{p['matrix_at_fold_choice']['episodes']} "
             f"({100 * p['matrix_at_fold_choice']['recall']:.0f}%); old rule = "
             f"{p['old_rule']['episodes_caught']}/{p['old_rule']['episodes']} ({100 * p['old_rule']['recall']:.0f}%).\n")

    over = [f"fold {f['fold']} ({f['test'][str(f['chosen_cut'])]['queue_median']:.0f})" for f in cv["folds"]
            if f["test"][str(f["chosen_cut"])]["queue_median"] is not None
            and f["test"][str(f["chosen_cut"])]["queue_median"] > MAX_MEDIAN_QUEUE]
    gain = p["matrix_at_selected"]["episodes_caught"] - p["old_rule"]["episodes_caught"]
    L.append("Reading this honestly:\n")
    L.append(f"- The recall gain over the old rule is {gain} episodes out of {p['old_rule']['episodes']}. That is a small count "
             "with no confidence interval, so it supports \"no worse\" more than \"better\". "
             "It comes from High cells reaching review at a trigger of 0.50 instead of 0.71 (old rule: 0.70 x trigger >= 0.50), "
             "not from Moderate cells, which hold no positives.")
    L.append(f"- The queue limit is met on the training sides but not with much room: the replay median at cut {sel} is "
             f"{new['queue_median']:.0f} against a limit of {MAX_MEDIAN_QUEUE}, one fold's training median exceeded the limit "
             "and fell back to the next cut, and the held-out median was above the limit in: "
             + (", ".join(over) if over else "no fold") + ".")
    L.append("")

    L.append("## 4. Limits, stated plainly\n")
    pc = cv["positives_by_class"]
    L.append(f"- **The labels cannot vouch for Moderate cells.** Positives by susceptibility class: "
             f"{', '.join(f'{k} {v}' for k, v in pc.items())}. No positive lies in a Moderate cell (labels need slope >= 15 deg, or a hazard site "
             "that is floored to High), so episode recall is blind to whether Moderate cells belong in the queue. "
             "Their eligibility under the matrix rests on the design argument, not on measured recall.")
    L.append("- **Label circularity.** Threshold-derived positives come from the same ERA5 rainfall that drives the trigger features. "
             "Recall here measures agreement with the labels, not independent detection.")
    L.append("- **Held-out numbers were not used to choose the cut.** The replay queue sizes in section 2 use the production cache, "
             "which is in-sample for most episodes, so they describe workload and are not a forecast of it.")
    L.append("- **A calendar day that straddles two episode groups is split across train and test sides** so that every hour is scored "
             "by a model that never trained on its own episode; the peak hour of a storm day is always inside the storm group.")
    L.append("- **Table entries and tier boundaries are team-assigned.** Changing them changes every number above; re-run this script.")
    return "\n".join(L) + "\n"


def main():
    cv = select_cut_nested()
    replay = replay_comparison(cv["selected_cut"])
    (DOCS / "priority_matrix.json").write_text(json.dumps({"cut_selection": cv, "replay": replay}, indent=2, default=float))
    (DOCS / "priority_matrix.md").write_text(write_report(cv, replay), encoding="utf-8")
    print("selected cut:", cv["selected_cut"], "votes:", cv["votes"])
    print("wrote docs/priority_matrix.md and docs/priority_matrix.json")
    if cv["selected_cut"] != REVIEW_MIN_PRIORITY:
        print(f"!! app/config.py has REVIEW_MIN_PRIORITY = {REVIEW_MIN_PRIORITY}; the rule selected "
              f"{cv['selected_cut']}. Update the constant and re-run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
