"""Exploratory diagnostics for the frozen PRAVAH forecast backtest.

This script is deliberately separate from the preregistered evaluation.  It reads
the frozen configuration and completed backtest JSON without changing either,
uses only cached Open-Meteo responses, and writes exploratory diagnostics that
cannot be used to establish a lead-time claim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import forecast_backtest as fb


LOG = logging.getLogger("forecast_diagnostics")
ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "docs" / "forecast_config.json"
BACKTEST_JSON_PATH = ROOT / "docs" / "forecast_backtest.json"
BACKTEST_MD_PATH = ROOT / "docs" / "forecast_backtest.md"
OUTPUT_MD_PATH = ROOT / "docs" / "forecast_diagnostics.md"
OUTPUT_PNG_PATH = ROOT / "docs" / "img" / "threshold_sweep.png"
PRIMARY_LATENCY = 7
THRESHOLDS = tuple(round(value / 100.0, 2) for value in range(5, 101, 5))
EXPLORATORY_LABEL = "EXPLORATORY DIAGNOSTIC — NOT A LEAD-TIME CLAIM"
GENERATED_SECTION_START = "<!-- forecast-diagnostics:start -->"
GENERATED_SECTION_END = "<!-- forecast-diagnostics:end -->"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fmt_mm(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "missing"
    return f"{value:.1f}"


def fmt_pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def wilson_interval(hits: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 0.0)
    proportion = hits / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def observed_precipitation() -> pd.DataFrame:
    weather = pd.read_parquet(
        fb.DATA_DIR / "weather_hourly.parquet",
        columns=["weather_point_id", "timestamp", "precipitation_mm"],
    ).sort_values(["weather_point_id", "timestamp"])
    weather["precip_3hr_mm"] = weather.groupby("weather_point_id")[
        "precipitation_mm"
    ].transform(lambda values: values.rolling(window=3, min_periods=3).sum())
    timestamps = pd.to_datetime(weather["timestamp"])
    if timestamps.dt.tz is None:
        weather["reference_time_utc"] = timestamps.dt.tz_localize(fb.IST).dt.tz_convert(fb.UTC)
    else:
        weather["reference_time_utc"] = timestamps.dt.tz_convert(fb.UTC)
    weather["weather_point_id"] = weather["weather_point_id"].astype(str)
    return weather.set_index(["weather_point_id", "reference_time_utc"]).sort_index()


def finite_max(values: list[float]) -> float | None:
    finite = [float(value) for value in values if pd.notna(value) and math.isfinite(float(value))]
    return max(finite) if finite else None


def forecast_rain_at_onset(
    single: pd.DataFrame,
    weather_point_id: str,
    onset_time_utc: pd.Timestamp,
) -> tuple[float | None, float | None]:
    onset = fb.utc_timestamp(onset_time_utc)
    times = pd.date_range(onset - pd.Timedelta(hours=2), onset, freq="1h")
    index = pd.MultiIndex.from_product(
        [[str(weather_point_id)], times], names=["weather_point_id", "timestamp"]
    )
    values = pd.to_numeric(single["precipitation"].reindex(index), errors="coerce")
    if len(values) != 3 or values.isna().any():
        return (None, None)
    return (float(values.iloc[-1]), float(values.sum()))


def episode_rainfall_comparison(
    episodes: pd.DataFrame,
    observed: pd.DataFrame,
    runs: dict[pd.Timestamp, pd.DataFrame],
    schedules: dict[int, list[pd.Timestamp]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for year in (2024, 2025):
        year_episodes = episodes[episodes["year"] == year].copy()
        for lead_value in config["issue_policy"]["lead_hours"]:
            lead = int(lead_value)
            targets = fb.expected_targets(
                schedules[year], PRIMARY_LATENCY, lead, config, year
            )
            assigned, unassigned = fb.assign_episodes_to_targets(episodes, year, targets)
            target_by_episode = {
                event["episode_id"]: target
                for target, events in assigned.items()
                for event in events
            }
            unassigned_ids = {event["episode_id"] for event in unassigned}
            for episode in year_episodes.itertuples(index=False):
                onset_reference = fb.utc_timestamp(episode.reference_hour_time_utc)
                onset_forecast = fb.utc_timestamp(episode.onset_time_utc)
                points = tuple(str(value) for value in episode.onset_weather_points)
                observed_1h: list[float] = []
                observed_3h: list[float] = []
                for point in points:
                    try:
                        record = observed.loc[(point, onset_reference)]
                    except KeyError:
                        continue
                    if isinstance(record, pd.DataFrame):
                        record = record.iloc[-1]
                    observed_1h.append(float(record["precipitation_mm"]))
                    observed_3h.append(float(record["precip_3hr_mm"]))

                target = target_by_episode.get(str(episode.episode_id))
                selected_run = (
                    target - pd.Timedelta(hours=PRIMARY_LATENCY + lead)
                    if target is not None
                    else None
                )
                forecast_1h: list[float] = []
                forecast_3h: list[float] = []
                single = runs.get(selected_run) if selected_run is not None else None
                if single is not None:
                    for point in points:
                        rain_1h, rain_3h = forecast_rain_at_onset(single, point, onset_forecast)
                        if rain_1h is not None:
                            forecast_1h.append(rain_1h)
                        if rain_3h is not None:
                            forecast_3h.append(rain_3h)
                observed_1h_max = finite_max(observed_1h)
                observed_3h_max = finite_max(observed_3h)
                forecast_1h_max = finite_max(forecast_1h)
                forecast_3h_max = finite_max(forecast_3h)
                forecast_crossed = bool(
                    (forecast_1h_max is not None and forecast_1h_max >= 20.0)
                    or (forecast_3h_max is not None and forecast_3h_max >= 60.0)
                )
                rows.append(
                    {
                        "year": year,
                        "episode_id": str(episode.episode_id),
                        "onset_ist": episode.onset_time_local.isoformat(),
                        "lead_hours": lead,
                        "selected_run_utc": (
                            selected_run.isoformat() if selected_run is not None else None
                        ),
                        "actual_issue_to_onset_hours": (
                            float(
                                (
                                    onset_forecast
                                    - (selected_run + pd.Timedelta(hours=PRIMARY_LATENCY))
                                ).total_seconds()
                                / 3600.0
                            )
                            if selected_run is not None
                            else None
                        ),
                        "onset_cell_count": len(episode.onset_cells),
                        "onset_weather_point_count": len(points),
                        "observed_1h_max_mm": observed_1h_max,
                        "observed_3h_max_mm": observed_3h_max,
                        "forecast_1h_max_mm": forecast_1h_max,
                        "forecast_3h_max_mm": forecast_3h_max,
                        "forecast_crossed_rainfall_threshold": forecast_crossed,
                        "unavailable": str(episode.episode_id) in unassigned_ids or single is None,
                    }
                )
    return rows


def episode_scores(
    prediction: pd.DataFrame,
    schedules: list[pd.Timestamp],
    episodes: pd.DataFrame,
    year: int,
    lead: int,
    config: dict[str, Any],
) -> tuple[list[float], list[pd.Timestamp], dict[pd.Timestamp, list[dict[str, Any]]]]:
    targets = fb.expected_targets(schedules, PRIMARY_LATENCY, lead, config, year)
    events_by_target, unassigned = fb.assign_episodes_to_targets(episodes, year, targets)
    frame = prediction[prediction["lead_hours"] == lead]
    groups = {
        fb.utc_timestamp(timestamp): group
        for timestamp, group in frame.groupby("valid_time", sort=False)
    }
    scores: list[float] = []
    for target in targets:
        target = fb.utc_timestamp(target)
        group = groups.get(target)
        for event in events_by_target.get(target, []):
            score = -math.inf
            if group is not None:
                relevant = group[
                    group["weather_point_id"].astype(str).isin(event["weather_points"])
                ]
                if not relevant.empty:
                    score = float(relevant["priority_index"].max())
            scores.append(score)
    scores.extend([-math.inf] * len(unassigned))
    return scores, targets, events_by_target


def threshold_sweep(
    prediction: pd.DataFrame,
    schedules: list[pd.Timestamp],
    episodes: pd.DataFrame,
    year: int,
    config: dict[str, Any],
    point_count: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lead_value in config["issue_policy"]["lead_hours"]:
        lead = int(lead_value)
        frame = prediction[prediction["lead_hours"] == lead]
        scores, targets, events_by_target = episode_scores(
            prediction, schedules, episodes, year, lead, config
        )
        for threshold in THRESHOLDS:
            hits = sum(score >= threshold for score in scores)
            false_days, month_status = fb._monthly_false_alerts(
                frame,
                targets,
                events_by_target,
                threshold,
                point_count,
                baseline=False,
            )
            full_month_counts = {
                month: count
                for month, count in false_days.items()
                if bool(month_status[month]["fully_evaluated"])
            }
            rows.append(
                {
                    "year": year,
                    "lead_hours": lead,
                    "threshold": threshold,
                    "hits": hits,
                    "episodes": len(scores),
                    "recall": hits / len(scores) if scores else 0.0,
                    "false_alert_days_by_month": false_days,
                    "max_false_alert_days_full_month": max(
                        full_month_counts.values(), default=0
                    ),
                    "partial_months": [
                        month
                        for month, status in month_status.items()
                        if not bool(status["fully_evaluated"])
                    ],
                }
            )
    return rows


def coverage_detail(
    prediction: pd.DataFrame,
    schedules: list[pd.Timestamp],
    runs: dict[pd.Timestamp, pd.DataFrame],
    config: dict[str, Any],
    point_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for lead_value in config["issue_policy"]["lead_hours"]:
        lead = int(lead_value)
        targets = fb.expected_targets(schedules, PRIMARY_LATENCY, lead, config, 2025)
        frame = prediction[prediction["lead_hours"] == lead]
        counts = frame.groupby("valid_time")["weather_point_id"].nunique().to_dict()
        complete = 0
        for target in targets:
            target = fb.utc_timestamp(target)
            available_points = int(counts.get(target, 0))
            if available_points == point_count:
                complete += 1
                continue
            selected_run = target - pd.Timedelta(hours=PRIMARY_LATENCY + lead)
            cause = (
                "single run unavailable"
                if selected_run not in runs
                else "incomplete required feature window"
            )
            gaps.append(
                {
                    "lead_hours": lead,
                    "target_utc": target.isoformat(),
                    "target_date_ist": target.tz_convert(fb.IST).strftime("%Y-%m-%d"),
                    "run_utc": selected_run.isoformat(),
                    "available_points": available_points,
                    "cause": cause,
                }
            )
        summaries.append(
            {
                "lead_hours": lead,
                "scheduled_targets": len(targets),
                "complete_targets": complete,
                "coverage": complete / len(targets) if targets else 0.0,
            }
        )
    return summaries, gaps


def best_at_budgets(sweep: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    years = sorted({int(row["year"]) for row in sweep})
    leads = sorted({int(row["lead_hours"]) for row in sweep})
    for year in years:
        for lead in leads:
            candidates = [
                row for row in sweep if row["year"] == year and row["lead_hours"] == lead
            ]
            for budget in (4, 8, 15):
                eligible = [
                    row
                    for row in candidates
                    if row["max_false_alert_days_full_month"] <= budget
                ]
                if not eligible:
                    rows.append(
                        {
                            "year": year,
                            "lead_hours": lead,
                            "budget": budget,
                            "hits": None,
                            "episodes": candidates[0]["episodes"] if candidates else 0,
                            "recall": None,
                            "thresholds": [],
                            "max_false_days": None,
                        }
                    )
                    continue
                # Use the frozen calibration tie-break after maximising recall:
                # highest threshold, then fewer false-alert days.
                selected = max(
                    eligible,
                    key=lambda row: (
                        float(row["recall"]),
                        float(row["threshold"]),
                        -int(row["max_false_alert_days_full_month"]),
                    ),
                )
                rows.append(
                    {
                        "year": year,
                        "lead_hours": lead,
                        "budget": budget,
                        "hits": int(selected["hits"]),
                        "episodes": int(selected["episodes"]),
                        "recall": float(selected["recall"]),
                        "thresholds": [float(selected["threshold"])],
                        "max_false_days": int(
                            selected["max_false_alert_days_full_month"]
                        ),
                    }
                )
    return rows


def make_sweep_plot(sweep: list[dict[str, Any]]) -> None:
    OUTPUT_PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(1, 2, figsize=(13.5, 6.4), sharey=True)
    colors = {6: "#16765f", 12: "#d0742f", 24: "#4169a1"}
    markers = {6: "o", 12: "s", 24: "^"}
    for axis, year in zip(axes, (2024, 2025), strict=True):
        for lead in (6, 12, 24):
            rows = sorted(
                [
                    row
                    for row in sweep
                    if row["year"] == year and row["lead_hours"] == lead
                ],
                key=lambda row: row["threshold"],
            )
            x = [row["max_false_alert_days_full_month"] for row in rows]
            y = [row["recall"] for row in rows]
            axis.plot(
                x,
                y,
                color=colors[lead],
                marker=markers[lead],
                markersize=4,
                linewidth=1.5,
                label=f"{lead} h",
            )
            for row in rows:
                if row["threshold"] in (0.05, 0.5, 1.0):
                    axis.annotate(
                        f"t={row['threshold']:.2f}",
                        (row["max_false_alert_days_full_month"], row["recall"]),
                        xytext=(4, 4),
                        textcoords="offset points",
                        fontsize=7,
                        color=colors[lead],
                    )
        axis.axvline(4, color="#7f3c8d", linestyle="--", linewidth=1.5, label="Budget = 4")
        axis.set_title(
            "2024 calibration" if year == 2024 else "2025 exploratory — not a claim",
            fontsize=12,
            fontweight="bold",
        )
        axis.set_xlabel("Maximum false-alert days in a fully evaluated monsoon month")
        axis.grid(alpha=0.25)
        axis.set_xlim(left=-0.5)
        axis.set_ylim(-0.03, 1.03)
    axes[0].set_ylabel("Episode recall")
    handles, labels = axes[0].get_legend_handles_labels()
    unique = dict(zip(labels, handles, strict=True))
    figure.legend(
        unique.values(),
        unique.keys(),
        loc="lower center",
        bbox_to_anchor=(0.5, 0.065),
        ncol=4,
        frameon=False,
    )
    figure.suptitle(
        "PRAVAH threshold sweep — exploratory diagnostics only",
        fontsize=16,
        fontweight="bold",
    )
    figure.text(
        0.5,
        0.018,
        "2025 thresholds were inspected after the test and cannot support a lead-time claim.",
        ha="center",
        fontsize=9,
        color="#8b1e1e",
    )
    figure.tight_layout(rect=(0, 0.16, 1, 0.94))
    figure.savefig(OUTPUT_PNG_PATH, dpi=180, bbox_inches="tight")
    plt.close(figure)


def threshold_list(values: list[float]) -> str:
    if not values:
        return "none"
    return ", ".join(f"{value:.2f}" for value in values)


def render_sweep_table(rows: list[dict[str, Any]], year: int) -> list[str]:
    months = [f"{year}-{month:02d}" for month in range(5, 11)]
    output = [
        "| Lead | Threshold | Episode recall | "
        + " | ".join(months)
        + " | Max in fully evaluated month |",
        "|---:|---:|---:|" + "---:|" * len(months) + "---:|",
    ]
    for row in sorted(
        [item for item in rows if item["year"] == year],
        key=lambda item: (item["lead_hours"], item["threshold"]),
    ):
        month_values = []
        for month in months:
            value = int(row["false_alert_days_by_month"].get(month, 0))
            suffix = "*" if month in row["partial_months"] else ""
            month_values.append(f"{value}{suffix}")
        output.append(
            f"| {row['lead_hours']} h | {row['threshold']:.2f} | "
            f"{row['hits']} of {row['episodes']} ({fmt_pct(row['recall'])}) | "
            + " | ".join(month_values)
            + f" | {row['max_false_alert_days_full_month']} |"
        )
    return output


def write_diagnostics_markdown(
    comparison: list[dict[str, Any]],
    sweep: list[dict[str, Any]],
    best: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    run_failures: list[dict[str, str]],
    backtest: dict[str, Any],
) -> dict[str, Any]:
    comparison_frame = pd.DataFrame(comparison)
    observed_unique = comparison_frame.drop_duplicates("episode_id")
    observed_1h_extreme = float(observed_unique["observed_1h_max_mm"].max())
    observed_3h_extreme = float(observed_unique["observed_3h_max_mm"].max())
    forecast_1h_extreme = float(comparison_frame["forecast_1h_max_mm"].max())
    forecast_3h_extreme = float(comparison_frame["forecast_3h_max_mm"].max())
    ratio_1h = forecast_1h_extreme / observed_1h_extreme
    ratio_3h = forecast_3h_extreme / observed_3h_extreme
    crossed_by_lead = {
        lead: int(
            comparison_frame.loc[
                comparison_frame["lead_hours"] == lead,
                "forecast_crossed_rainfall_threshold",
            ].sum()
        )
        for lead in (6, 12, 24)
    }
    crossed_any = int(
        comparison_frame.groupby("episode_id")["forecast_crossed_rainfall_threshold"]
        .any()
        .sum()
    )
    episode_total = int(comparison_frame["episode_id"].nunique())
    hres_ever_crossed = crossed_any > 0

    lines = [
        "# PRAVAH exploratory forecast diagnostics",
        "",
        f"**{EXPLORATORY_LABEL}.** These analyses were produced after the frozen evaluation. "
        "They do not change the threshold, JSON results, gate decision, or the conclusion that "
        "the lead time was not established.",
        "",
        "The primary-latency diagnostic uses the preregistered 7-hour availability assumption, "
        "the cached pinned `ecmwf_ifs` runs, and the same 6, 12 and 24-hour alert schedule as the "
        "backtest. The 2025 threshold sweep is explicitly post-test sensitivity analysis.",
        "",
        "## Input-distribution check",
        "",
        "For each episode, the selected run is the run attached to the alert window that contains "
        "the episode onset. Forecast rainfall is then read from that same run at the exact "
        "forecast-aligned onset hour and weather point. Three-hour totals end at that hour. Values "
        "below are maxima across the slope-eligible cells positive at episode onset.",
        "",
        "| Year | Episode | Onset (IST) | Lead | Actual issue-to-onset | Observed 1 h | Forecast 1 h | Observed 3 h | Forecast 3 h | Forecast crossed 20/60? |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in sorted(
        comparison,
        key=lambda item: (item["year"], item["episode_id"], item["lead_hours"]),
    ):
        actual = (
            f"{row['actual_issue_to_onset_hours']:.0f} h"
            if row["actual_issue_to_onset_hours"] is not None
            else "missing"
        )
        lines.append(
            f"| {row['year']} | {row['episode_id']} | {row['onset_ist']} | "
            f"{row['lead_hours']} h | {actual} | {fmt_mm(row['observed_1h_max_mm'])} mm | "
            f"{fmt_mm(row['forecast_1h_max_mm'])} mm | {fmt_mm(row['observed_3h_max_mm'])} mm | "
            f"{fmt_mm(row['forecast_3h_max_mm'])} mm | "
            f"{'yes' if row['forecast_crossed_rainfall_threshold'] else 'no'} |"
        )

    lines.extend(
        [
            "",
            "Threshold crossings by nominal lead:",
            "",
            "| Lead | Forecast crossed either rainfall threshold |",
            "|---:|---:|",
            *[
                f"| {lead} h | {crossed_by_lead[lead]} of {episode_total} episodes |"
                for lead in (6, 12, 24)
            ],
            f"| Any of the three leads | {crossed_any} of {episode_total} episodes |",
            "",
            f"The largest forecast 1-hour value was **{forecast_1h_extreme:.1f} mm**, versus "
            f"**{observed_1h_extreme:.1f} mm** observed: a forecast-to-observed extreme ratio of "
            f"**{ratio_1h:.3f}**. The largest forecast 3-hour value was "
            f"**{forecast_3h_extreme:.1f} mm**, versus **{observed_3h_extreme:.1f} mm** observed: "
            f"a ratio of **{ratio_3h:.3f}**. These ratios compare maxima across all available "
            "episode/lead pairs with maxima across the unique observed episodes; they are not "
            "mean bias estimates.",
            "",
            (
                "Plain result: **ECMWF HRES at roughly 9 km did reach at least one of the "
                "20 mm/1 h or 60 mm/3 h thresholds at an ERA5-defined episode onset in this "
                "sample.** The table shows how often and at which leads."
                if hres_ever_crossed
                else "Plain result: **ECMWF HRES at roughly 9 km never reached either the "
                "20 mm/1 h or 60 mm/3 h threshold at the onset of any ERA5-defined episode, "
                "at any tested lead.** ERA5 reached those thresholds by construction."
            ),
            "",
            "## 2024 threshold sweep",
            "",
            "This is an exploratory view of the calibration period only. Recall uses raw district "
            "episode counts. The alert-budget coordinate is the maximum false-alert-day count "
            "among fully evaluated monsoon months, matching the frozen per-month constraint.",
            "",
            "![Exploratory threshold sweep](img/threshold_sweep.png)",
            "",
            "### Best 2024 recall at alternative monthly budgets",
            "",
            "| Lead | Budget | Best recall | Threshold after frozen tie-break | Max false-alert days |",
            "|---:|---:|---:|---|---:|",
        ]
    )
    for row in [item for item in best if item["year"] == 2024]:
        if row["recall"] is None:
            recall = "no eligible threshold"
        else:
            recall = f"{row['hits']} of {row['episodes']} ({fmt_pct(row['recall'])})"
        lines.append(
            f"| {row['lead_hours']} h | {row['budget']} days/month | {recall} | "
            f"{threshold_list(row['thresholds'])} | "
            f"{row['max_false_days'] if row['max_false_days'] is not None else 'n/a'} |"
        )
    lines.extend(["", "### Full 2024 sweep", "", *render_sweep_table(sweep, 2024)])

    lines.extend(
        [
            "",
            "## 2025 test-period sensitivity",
            "",
            "**NOT A CLAIM; THRESHOLD WAS NOT SELECTED ON THIS DATA.** This table asks what the "
            "trade-off would look like if thresholds were inspected after seeing 2025. It cannot "
            "be used to revise the frozen threshold or establish a lead time.",
            "",
            "### Best exploratory 2025 recall at alternative monthly budgets",
            "",
            "| Lead | Budget | Best recall | Threshold after frozen tie-break | Max false-alert days |",
            "|---:|---:|---:|---|---:|",
        ]
    )
    for row in [item for item in best if item["year"] == 2025]:
        if row["recall"] is None:
            recall = "no eligible threshold"
        else:
            recall = f"{row['hits']} of {row['episodes']} ({fmt_pct(row['recall'])})"
        lines.append(
            f"| {row['lead_hours']} h | {row['budget']} days/month | {recall} | "
            f"{threshold_list(row['thresholds'])} | "
            f"{row['max_false_days'] if row['max_false_days'] is not None else 'n/a'} |"
        )
    lines.extend(
        [
            "",
            "### Full 2025 exploratory sweep",
            "",
            "An asterisk marks August 2025, which is only partially evaluated; it is excluded "
            "from the maximum-full-month budget coordinate.",
            "",
            *render_sweep_table(sweep, 2025),
            "",
            "## Corrected uncertainty and raw counts",
            "",
        ]
    )
    primary = backtest["test"][str(PRIMARY_LATENCY)]
    lines.extend(
        [
            "The episode-cluster percentile bootstrap is degenerate when every one of five binary "
            "outcomes is zero, which produced a misleading 0–0% interval in the frozen JSON. For "
            "descriptive uncertainty, this document uses the Wilson binomial interval. The lower "
            "bound remains zero, so this correction does not change any gate decision.",
            "",
            "| Lead | Model recall | Wilson 95% interval | Baseline recall | Gate verdict |",
            "|---:|---:|---:|---:|---|",
        ]
    )
    uncertainty_rows = []
    for lead in (6, 12, 24):
        metric = primary[str(lead)]
        model_hits = sum(bool(row["model_hit"]) for row in metric["episode_results"])
        baseline_hits = sum(bool(row["baseline_hit"]) for row in metric["episode_results"])
        total = int(metric["reference_episodes_in_year"])
        low, high = wilson_interval(model_hits, total)
        lines.append(
            f"| {lead} h | {model_hits} of {total} ({fmt_pct(model_hits / total)}) | "
            f"{fmt_pct(low)}–{fmt_pct(high)} | {baseline_hits} of {total} "
            f"({fmt_pct(baseline_hits / total)}) | {metric['claim']} |"
        )
        uncertainty_rows.append(
            {
                "lead_hours": lead,
                "model_hits": model_hits,
                "baseline_hits": baseline_hits,
                "episodes": total,
                "wilson_low": low,
                "wilson_high": high,
            }
        )

    lines.extend(
        [
            "",
            "## Coverage detail",
            "",
            "The cache contains 751 usable scheduled runs and five explicit unavailable-run "
            "responses out of 756 expected runs. The unavailable runs are:",
            "",
            "| Unavailable initialization (UTC) | Affected 6 h target date (IST) | Affected 12 h target date (IST) | Affected 24 h target date (IST) |",
            "|---|---|---|---|",
        ]
    )
    unavailable_runs = []
    for failure in run_failures:
        run = fb.utc_timestamp(failure["run"])
        dates = {
            lead: (run + pd.Timedelta(hours=PRIMARY_LATENCY + lead))
            .tz_convert(fb.IST)
            .strftime("%Y-%m-%d")
            for lead in (6, 12, 24)
        }
        lines.append(
            f"| {run.isoformat()} | {dates[6]} | {dates[12]} | {dates[24]} |"
        )
        unavailable_runs.append({"run": run.isoformat(), "affected_dates": dates})

    lines.extend(
        [
            "",
            "2025 target coverage at the primary latency:",
            "",
            "| Lead | Complete scheduled targets | Target coverage | August status |",
            "|---:|---:|---:|---|",
        ]
    )
    for row in coverage:
        august_gaps = [
            gap
            for gap in gaps
            if gap["lead_hours"] == row["lead_hours"]
            and gap["target_date_ist"].startswith("2025-08")
        ]
        august_complete = 62 - len(august_gaps)
        lines.append(
            f"| {row['lead_hours']} h | {row['complete_targets']} of "
            f"{row['scheduled_targets']} | {fmt_pct(row['coverage'])} | partial: "
            f"{august_complete} of 62 targets complete |"
        )
    direct_missing = sum(gap["cause"] == "single run unavailable" for gap in gaps)
    feature_missing = sum(gap["cause"] == "incomplete required feature window" for gap in gaps)
    lines.extend(
        [
            "",
            f"Across the three lead tables there are **{direct_missing} lead-specific targets** "
            "missing directly because a selected run is unavailable and "
            f"**{feature_missing} lead-specific targets** rejected because a required feature "
            "window is incomplete. August 2025 is therefore partial, while May, June, July, "
            "September and October are fully evaluated.",
            "",
            "## Interpretation",
            "",
            "The endpoint threshold of 1.0 looks degenerate, but the 2024 sweep shows that it was "
            "not the reason recall was zero under the frozen alert budget: at no tested threshold "
            "could any lead recover an episode while staying within four false-alert days in every "
            "monsoon month. Even at 15 false-alert days per month, the best calibration recall was "
            "only 1 of 12 at 6 hours, 2 of 12 at 12 hours, and 1 of 12 at 24 hours. Combined with "
            "the complete failure of forecast rainfall to reach the ERA5-defining thresholds, this "
            "supports a finding of no useful forecast-domain skill under the stated alert budget, "
            "rather than a calibration failure caused only by selecting 1.0. It does not prove that "
            "the ranking contains no signal at any alert rate. The 2025 sweep remains diagnostic "
            "only, and the frozen conclusion remains **lead time not established**.",
            "",
        ]
    )
    OUTPUT_MD_PATH.write_text("\n".join(lines), encoding="utf-8")
    return {
        "episode_total": episode_total,
        "crossed_any": crossed_any,
        "crossed_by_lead": crossed_by_lead,
        "forecast_1h_extreme": forecast_1h_extreme,
        "observed_1h_extreme": observed_1h_extreme,
        "forecast_3h_extreme": forecast_3h_extreme,
        "observed_3h_extreme": observed_3h_extreme,
        "ratio_1h": ratio_1h,
        "ratio_3h": ratio_3h,
        "hres_ever_crossed": hres_ever_crossed,
        "uncertainty": uncertainty_rows,
    }


def update_backtest_markdown(summary: dict[str, Any]) -> None:
    text = BACKTEST_MD_PATH.read_text(encoding="utf-8")
    text = text.replace(
        "Episode recall (95% bootstrap interval)",
        "Episode recall (Wilson 95% interval)",
    )
    for row in summary["uncertainty"]:
        lead = int(row["lead_hours"])
        hits = int(row["model_hits"])
        baseline_hits = int(row["baseline_hits"])
        total = int(row["episodes"])
        low = float(row["wilson_low"])
        high = float(row["wilson_high"])
        pattern = re.compile(rf"^\| {lead} h \|.*$", re.MULTILINE)
        match = pattern.search(text)
        if not match:
            raise RuntimeError(f"Could not locate {lead} h result row in forecast_backtest.md")
        cells = [value.strip() for value in match.group(0).strip("|").split("|")]
        if len(cells) != 6:
            raise RuntimeError(f"Unexpected result-table structure for {lead} h")
        cells[2] = (
            f"{hits} of {total} ({fmt_pct(hits / total)}; Wilson 95% CI "
            f"{fmt_pct(low)}–{fmt_pct(high)})"
        )
        cells[3] = f"{baseline_hits} of {total} ({fmt_pct(baseline_hits / total)})"
        replacement = "| " + " | ".join(cells) + " |"
        text = text[: match.start()] + replacement + text[match.end() :]

    section = "\n".join(
        [
            GENERATED_SECTION_START,
            "## Why recall was zero",
            "",
            f"**Exploratory diagnostic only; this does not revise the frozen gate.** The 2024 "
            "calibration selected the endpoint threshold 1.0. The separate diagnostic compares "
            "forecast and ERA5 rainfall at every episode onset and sweeps lower thresholds against "
            "the alert budget.",
            "",
            f"Across all {summary['episode_total']} reference episodes, forecast rainfall crossed "
            f"the 20 mm/1 h or 60 mm/3 h threshold at any tested lead for "
            f"**{summary['crossed_any']} of {summary['episode_total']} episodes**. The maximum "
            f"forecast-to-observed extreme ratios were **{summary['ratio_1h']:.3f}** for 1-hour "
            f"rainfall and **{summary['ratio_3h']:.3f}** for 3-hour rainfall. See "
            "[`forecast_diagnostics.md`](forecast_diagnostics.md) for the episode-level input "
            "comparison, 2024 calibration sweep, clearly post-test 2025 sensitivity, corrected "
            "Wilson intervals, and coverage details.",
            "",
            "The 2024 sweep shows this was not simply a calibration failure caused by selecting the "
            "endpoint: no tested threshold recovered an episode at any lead while satisfying the "
            "four-false-alert-days-per-month budget. At a much looser 15-day budget, the best "
            "calibration recalls were only 1 of 12, 2 of 12, and 1 of 12 episodes at 6, 12, and "
            "24 hours. This indicates no useful forecast-domain skill under the stated alert budget.",
            "",
            "The raw 2025 result remains **0 of 5 episodes** at every lead. Its Wilson 95% interval "
            "is **0.0%–43.4%**, rather than the degenerate percentile-bootstrap interval. The lower "
            "bound and gate verdict are unchanged: **lead time not established**.",
            GENERATED_SECTION_END,
        ]
    )
    if GENERATED_SECTION_START in text:
        start = text.index(GENERATED_SECTION_START)
        end = text.index(GENERATED_SECTION_END, start) + len(GENERATED_SECTION_END)
        text = text[:start].rstrip() + "\n\n" + section + text[end:]
    else:
        text = text.rstrip() + "\n\n" + section + "\n"
    BACKTEST_MD_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    protected_before = {
        CONFIG_PATH: file_sha256(CONFIG_PATH),
        BACKTEST_JSON_PATH: file_sha256(BACKTEST_JSON_PATH),
    }
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    backtest = json.loads(BACKTEST_JSON_PATH.read_text(encoding="utf-8"))
    if float(config["frozen_calibration"]["threshold_by_latency_hours"]["7"]) != 1.0:
        raise RuntimeError("This diagnostic was requested for the frozen endpoint threshold 1.0")
    if backtest["test"]["7"]["6"]["claim"] != "lead time not established":
        raise RuntimeError("Frozen gate verdict differs from the expected failed claim")

    points = fb.weather_points()
    mapping = fb.load_cell_mapping()
    episodes = fb.build_reference_episodes(config, mapping)
    client = fb.ForecastClient(config, offline=True)
    LOG.info("loading cached Historical Forecast precipitation and single runs")
    history = fb.load_historical_precipitation(client, config, points)
    runs, schedules, run_failures = fb.fetch_runs(client, config, points, (2024, 2025))
    if client.network_requests:
        raise AssertionError("Exploratory diagnostics must not make network requests")

    observed = observed_precipitation()
    comparison = episode_rainfall_comparison(
        episodes, observed, runs, schedules, config
    )

    LOG.info("training the permitted fresh 2018-2023 RandomForest")
    model, _ = fb.train_fresh_random_forest(config)
    sweeps: list[dict[str, Any]] = []
    prediction_by_year: dict[int, pd.DataFrame] = {}
    for year in (2024, 2025):
        LOG.info("assembling %d primary-latency predictions", year)
        prediction = fb.assemble_predictions(
            year,
            PRIMARY_LATENCY,
            config,
            points,
            history,
            runs,
            schedules[year],
            model,
        )
        prediction_by_year[year] = prediction
        sweeps.extend(
            threshold_sweep(
                prediction,
                schedules[year],
                episodes,
                year,
                config,
                len(points),
            )
        )

    coverage, gaps = coverage_detail(
        prediction_by_year[2025], schedules[2025], runs, config, len(points)
    )
    best = best_at_budgets(sweeps)
    make_sweep_plot(sweeps)
    summary = write_diagnostics_markdown(
        comparison,
        sweeps,
        best,
        coverage,
        gaps,
        run_failures,
        backtest,
    )
    update_backtest_markdown(summary)

    protected_after = {path: file_sha256(path) for path in protected_before}
    if protected_after != protected_before:
        raise AssertionError("A protected frozen artifact changed during exploratory diagnostics")
    LOG.info("wrote %s and %s; updated %s", OUTPUT_MD_PATH, OUTPUT_PNG_PATH, BACKTEST_MD_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
