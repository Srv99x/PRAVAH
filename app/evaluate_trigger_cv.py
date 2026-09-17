"""
app/evaluate_trigger_cv.py
Reproducible evaluation for the PRAVAH trigger model (source of every metric on the SIH slides).

Run from the repo root:   python app/evaluate_trigger_cv.py
Writes:                   docs/evaluation_cv.json

What it reports (all on the SAME leak-safe training set built by train_trigger_model.py):
  1. 5-fold StratifiedGroupKFold (storm-episode groups) for RandomForest and XGBoost:
     PR-AUC, macro-F1, precision, recall at 0.5, mean +/- sample SD.
  2. Forward-in-time tests: train on years < Y, test on year Y (Y = 2024, 2025).
It never evaluates models/random_forest_trigger_model.pkl: that pickle was trained on a
different split and scores ~0.92 PR-AUC on held-out fold 0, which is leakage, not skill.
"""
import json, platform, subprocess, sys
from pathlib import Path
import numpy as np, pandas as pd, geopandas as gpd
import sklearn, xgboost
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.train_trigger_model as T
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score
from sklearn.model_selection import StratifiedGroupKFold
from xgboost import XGBClassifier


def metrics(p, y):
    pr = (p >= 0.5).astype(int)
    return {"pr_auc": average_precision_score(y, p), "f1_macro": f1_score(y, pr, average="macro"),
            "precision": precision_score(y, pr, zero_division=0), "recall": recall_score(y, pr),
            "positive_rate": float(y.mean()), "n": int(len(y))}


def rf():
    return RandomForestClassifier(n_estimators=100, class_weight="balanced", random_state=42, n_jobs=-1)


def xgb(spw):
    return XGBClassifier(n_estimators=150, max_depth=6, learning_rate=0.05, scale_pos_weight=spw,
                         eval_metric="aucpr", random_state=42, n_jobs=-1)


def main():
    grid = gpd.read_parquet(T.DATA_DIR / "kamrup_metro_grid_1km.parquet")
    w = T.load_weather(); mp = T.load_mapping_with_terrain()
    pos = T.pass1_collect_positives(w, mp, grid)
    safe = T.compute_safe_cells(pos, grid)
    neg = T.pass2_sample_negatives(w, mp, grid, safe, len(pos) * T.NEG_TO_POS_RATIO)
    df = pd.concat([pos, neg], ignore_index=True).sort_values(["grid_id", "timestamp"]).reset_index(drop=True)
    s, e = T.build_storm_episodes(w)
    df["episode_group"] = T.assign_groups(df["timestamp"], s, e)
    X = df[T.FEATURES].astype("float32"); y = df["target_event"].to_numpy(); g = df["episode_group"]

    out = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
           "python": platform.python_version(), "sklearn": sklearn.__version__, "xgboost": xgboost.__version__,
           "n_positive": int(len(pos)), "n_negative": int(len(neg)),
           "label_sources": pos["label_source"].value_counts().to_dict(),
           "storm_episodes": int(len(s)), "groups": int(g.nunique()), "folds": []}

    sgkf = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)
    for k, (tr, te) in enumerate(sgkf.split(X, y, g)):
        assert not set(g.iloc[tr]) & set(g.iloc[te]), "episode leakage"
        spw = (y[tr] == 0).sum() / (y[tr] == 1).sum()
        a = rf().fit(X.iloc[tr], y[tr]); b = xgb(spw).fit(X.iloc[tr], y[tr])
        out["folds"].append({"fold": k, "random_forest": metrics(a.predict_proba(X.iloc[te])[:, 1], y[te]),
                             "xgboost": metrics(b.predict_proba(X.iloc[te])[:, 1], y[te])})
        print("fold", k, "done", flush=True)

    out["summary"] = {}
    for m in ("random_forest", "xgboost"):
        out["summary"][m] = {k: {"mean": float(np.mean([f[m][k] for f in out["folds"]])),
                                 "sd": float(np.std([f[m][k] for f in out["folds"]], ddof=1))}
                             for k in ("pr_auc", "f1_macro", "precision", "recall")}

    yr = df["timestamp"].dt.year
    for Y in (2024, 2025):
        tr = (yr < Y).to_numpy(); te = (yr == Y).to_numpy()
        spw = (y[tr] == 0).sum() / (y[tr] == 1).sum()
        out[f"temporal_{Y}"] = {
            "random_forest": metrics(rf().fit(X[tr], y[tr]).predict_proba(X[te])[:, 1], y[te]),
            "xgboost": metrics(xgb(spw).fit(X[tr], y[tr]).predict_proba(X[te])[:, 1], y[te])}

    path = T.DOCS_DIR / "evaluation_cv.json"
    path.write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps(out["summary"], indent=2)); print("wrote", path)


if __name__ == "__main__":
    main()
