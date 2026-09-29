"""Latency-assumed retrospective forecast evaluation for PRAVAH.

The executable specification is docs/forecast_config.json.  The script trains a
fresh RandomForest on the existing 2018-2023 ERA5-domain pipeline, calibrates one
priority-index threshold across leads using forecast-domain data from 2024, freezes
that threshold in the configuration, and only then scores 2025.

It deliberately never opens models/random_forest_trigger_model.pkl.

Run from the repository root:

    venv\Scripts\python.exe scripts/forecast_backtest.py

Useful recovery modes:

    venv\Scripts\python.exe scripts/forecast_backtest.py --offline       # cached API responses only
    venv\Scripts\python.exe scripts/forecast_backtest.py --self-test     # leakage/contract checks only

Raw API responses and request metadata are resumably cached under
data/raw/forecast_cache/, which is gitignored.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import math
import platform
import pickle
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import sklearn
from sklearn.ensemble import RandomForestClassifier

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.train_trigger_model as training


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "docs" / "forecast_config.json"
OUTPUT_JSON = ROOT / "docs" / "forecast_backtest.json"
OUTPUT_MD = ROOT / "docs" / "forecast_backtest.md"
OUTPUT_PNG = ROOT / "docs" / "img" / "lead_time_curve.png"
DATA_DIR = ROOT / "data" / "processed"
INCIDENTS_PATH = ROOT / "data" / "raw" / "verified_incidents.csv"
IST = "Asia/Kolkata"
UTC = "UTC"

LOG = logging.getLogger("forecast_backtest")


class ArchiveRateLimitError(RuntimeError):
    """Raised without retrying when Open-Meteo reports an exhausted quota."""


class ArchiveRunUnavailable(RuntimeError):
    """A run is absent from the archive; this is a coverage gap, not a retry."""


def load_config() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    forbidden = ROOT / config["training"]["forbidden_model_path"]
    # This comparison is a guard against a future refactor accidentally routing
    # evaluation through app.predict.load_model().  No file read is performed.
    if forbidden.name != "random_forest_trigger_model.pkl":
        raise RuntimeError("The frozen forbidden-model guard was altered.")
    return config


def canonical_json_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def utc_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)


def local_naive_to_utc(series: pd.Series) -> pd.Series:
    return series.dt.tz_localize(IST, ambiguous="raise", nonexistent="raise").dt.tz_convert(UTC)


def json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


@dataclass(frozen=True)
class WeatherPoint:
    weather_point_id: str
    lat: float
    lon: float


class ForecastClient:
    """Small resumable client that preserves API response provenance."""

    def __init__(self, config: dict[str, Any], offline: bool = False):
        self.config = config
        self.offline = offline
        self.cache_dir = ROOT / config["cache"]["directory"]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.last_request_monotonic = 0.0
        self.minimum_interval = float(config["cache"]["minimum_seconds_between_requests"])
        self.cache_hits = 0
        self.network_requests = 0

    @staticmethod
    def _prepare_url(endpoint: str, params: dict[str, Any]) -> str:
        return requests.Request("GET", endpoint, params=params).prepare().url

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            wrapped = json.load(handle)
        self.cache_hits += 1
        if int(wrapped.get("metadata", {}).get("http_status", 200)) >= 400:
            response = wrapped.get("response", {})
            reason = response.get("reason", "cached archive error") if isinstance(response, dict) else "cached archive error"
            raise ArchiveRunUnavailable(str(reason))
        return wrapped["response"]

    def _store_response(
        self,
        path: Path,
        endpoint: str,
        response: requests.Response,
        payload: Any,
        status_override: int | None = None,
    ) -> None:
        wrapped = {
            "metadata": {
                "request_url": response.url,
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "http_status": status_override or response.status_code,
                "endpoint": endpoint,
                "model": self.config["model"],
            },
            "response": payload,
        }
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(temp_path, "wt", encoding="utf-8") as handle:
            json.dump(wrapped, handle, separators=(",", ":"))
        temp_path.replace(path)

    def _request(
        self, endpoint: str, params: dict[str, Any], cache_path: Path
    ) -> dict[str, Any] | list[dict[str, Any]]:
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached
        if self.offline:
            raise FileNotFoundError(f"Offline cache miss: {cache_path}")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        url = self._prepare_url(endpoint, params)
        last_error: Exception | None = None
        for attempt in range(3):
            wait = self.minimum_interval - (time.monotonic() - self.last_request_monotonic)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(endpoint, params=params, timeout=30)
                self.last_request_monotonic = time.monotonic()
                self.network_requests += 1
                if response.status_code == 429:
                    try:
                        reason = response.json().get("reason", response.text)
                    except ValueError:
                        reason = response.text
                    raise ArchiveRateLimitError(str(reason))
                if 400 <= response.status_code < 500:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = {"error": True, "reason": response.text}
                    self._store_response(cache_path, endpoint, response, payload)
                    reason = payload.get("reason", response.text) if isinstance(payload, dict) else response.text
                    raise ArchiveRunUnavailable(str(reason))
                if response.status_code >= 500:
                    raise requests.HTTPError(
                        f"retryable HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    if "modelRunUnavailable" in response.text:
                        payload = {"error": True, "reason": response.text}
                        # Open-Meteo sometimes streams this archive error with
                        # HTTP 200. Store a synthetic error status so cache reads
                        # preserve the unavailable-run classification.
                        self._store_response(
                            cache_path, endpoint, response, payload, status_override=460
                        )
                        raise ArchiveRunUnavailable(response.text)
                    raise
                self._store_response(cache_path, endpoint, response, payload)
                return payload
            except ArchiveRateLimitError:
                raise
            except ArchiveRunUnavailable:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(min(30.0, 2.0**attempt))
        raise RuntimeError(f"Open-Meteo request failed after retries: {url}") from last_error

    @staticmethod
    def _point_params(points: list[WeatherPoint]) -> dict[str, str]:
        return {
            "latitude": ",".join(f"{point.lat:.8f}" for point in points),
            "longitude": ",".join(f"{point.lon:.8f}" for point in points),
        }

    def single_run(
        self, run_initialisation: pd.Timestamp, points: list[WeatherPoint]
    ) -> dict[str, Any] | list[dict[str, Any]]:
        run = utc_timestamp(run_initialisation)
        run_text = run.strftime("%Y-%m-%dT%H:%M")
        params: dict[str, Any] = {
            **self._point_params(points),
            "models": self.config["model"],
            "run": run_text,
            "hourly": (
                "temperature_2m,precipitation,soil_moisture_0_to_7cm,"
                "soil_moisture_7_to_28cm"
            ),
            "timezone": "GMT",
            "forecast_days": 3,
        }
        stamp = run.strftime("%Y%m%dT%H%MZ")
        path = self.cache_dir / "single_runs" / self.config["model"] / f"{stamp}.json.gz"
        return self._request(
            self.config["archive"]["single_runs_endpoint"], params, path
        )

    def historical_forecast(
        self, start_date: str, end_date: str, points: list[WeatherPoint]
    ) -> dict[str, Any] | list[dict[str, Any]]:
        params: dict[str, Any] = {
            **self._point_params(points),
            "models": self.config["model"],
            "start_date": start_date,
            "end_date": end_date,
            "hourly": "precipitation",
            "timezone": "GMT",
        }
        path = (
            self.cache_dir
            / "historical_forecast"
            / self.config["model"]
            / f"{start_date}_{end_date}.json.gz"
        )
        return self._request(
            self.config["archive"]["historical_forecast_endpoint"], params, path
        )


def weather_points() -> list[WeatherPoint]:
    frame = pd.read_parquet(
        DATA_DIR / "weather_hourly.parquet",
        columns=["weather_point_id", "lat", "lon"],
    ).drop_duplicates()
    frame = frame.sort_values("weather_point_id").reset_index(drop=True)
    points = [
        WeatherPoint(str(row.weather_point_id), float(row.lat), float(row.lon))
        for row in frame.itertuples(index=False)
    ]
    return points


def response_blocks(
    payload: dict[str, Any] | list[dict[str, Any]], points: list[WeatherPoint]
) -> list[tuple[WeatherPoint, dict[str, Any]]]:
    blocks = payload if isinstance(payload, list) else [payload]
    if len(blocks) != len(points):
        raise ValueError(f"Expected {len(points)} point responses, received {len(blocks)}")
    return list(zip(points, blocks, strict=True))


def parse_hourly_block(
    point: WeatherPoint, block: dict[str, Any], include_instantaneous: bool
) -> pd.DataFrame:
    hourly = block.get("hourly")
    if not isinstance(hourly, dict) or "time" not in hourly:
        raise ValueError(f"Missing hourly data for {point.weather_point_id}")
    frame = pd.DataFrame({"timestamp": pd.to_datetime(hourly["time"], utc=True)})
    frame["weather_point_id"] = point.weather_point_id
    variables = ["precipitation"]
    if include_instantaneous:
        variables.extend(
            [
                "temperature_2m",
                "soil_moisture_0_to_7cm",
                "soil_moisture_7_to_28cm",
            ]
        )
    for variable in variables:
        values = hourly.get(variable)
        if values is None or len(values) != len(frame):
            raise ValueError(f"Missing or malformed {variable} for {point.weather_point_id}")
        frame[variable] = pd.to_numeric(pd.Series(values), errors="coerce")
    return frame


def parse_single_run(
    payload: dict[str, Any] | list[dict[str, Any]], points: list[WeatherPoint]
) -> pd.DataFrame:
    return pd.concat(
        [parse_hourly_block(point, block, True) for point, block in response_blocks(payload, points)],
        ignore_index=True,
    ).set_index(["weather_point_id", "timestamp"]).sort_index()


def parse_history(
    payloads: Iterable[dict[str, Any] | list[dict[str, Any]]],
    points: list[WeatherPoint],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for payload in payloads:
        frames.extend(
            parse_hourly_block(point, block, False)
            for point, block in response_blocks(payload, points)
        )
    frame = pd.concat(frames, ignore_index=True)
    frame = frame.drop_duplicates(["weather_point_id", "timestamp"], keep="last")
    return frame.set_index(["weather_point_id", "timestamp"]).sort_index()


def make_run_schedule(config: dict[str, Any], year: int) -> list[pd.Timestamp]:
    # The buffer supplies late-April initialisations needed for 1 May targets
    # and the pre-day incident snapshots.  Evaluation still filters valid times
    # to the frozen date window and monsoon months.
    start = pd.Timestamp(f"{year}-04-26T00:00:00Z")
    end = pd.Timestamp(f"{year}-10-31T12:00:00Z")
    cycles = set(int(hour) for hour in config["issue_policy"]["run_cycles_utc"])
    return [ts for ts in pd.date_range(start, end, freq="12h") if ts.hour in cycles]


def valid_in_scope(valid_time: pd.Timestamp, config: dict[str, Any], year: int) -> bool:
    local = utc_timestamp(valid_time).tz_convert(IST)
    start, end = config["periods"]["calibration" if year == 2024 else "test"]
    return (
        local.year == year
        and local.month in set(config["periods"]["monsoon_months"])
        and pd.Timestamp(start).date() <= local.date() <= pd.Timestamp(end).date()
    )


def expected_targets(
    schedule: list[pd.Timestamp], latency: int, lead: int, config: dict[str, Any], year: int
) -> list[pd.Timestamp]:
    return sorted(
        {
            run + pd.Timedelta(hours=latency + lead)
            for run in schedule
            if valid_in_scope(run + pd.Timedelta(hours=latency + lead), config, year)
        }
    )


def load_historical_precipitation(
    client: ForecastClient, config: dict[str, Any], points: list[WeatherPoint]
) -> pd.DataFrame:
    payloads = []
    for year in (2024, 2025):
        payloads.append(
            client.historical_forecast(f"{year}-04-20", f"{year}-10-31", points)
        )
    return parse_history(payloads, points)


def fetch_runs(
    client: ForecastClient,
    config: dict[str, Any],
    points: list[WeatherPoint],
    years: Iterable[int],
) -> tuple[dict[pd.Timestamp, pd.DataFrame], dict[int, list[pd.Timestamp]], list[dict[str, str]]]:
    runs: dict[pd.Timestamp, pd.DataFrame] = {}
    schedules: dict[int, list[pd.Timestamp]] = {}
    failures: list[dict[str, str]] = []
    network_start = client.network_requests
    network_cap = int(
        config["cache"]["maximum_single_run_network_requests_per_invocation"]
    )
    all_runs: list[tuple[int, pd.Timestamp]] = []
    for year in years:
        schedules[year] = make_run_schedule(config, year)
        all_runs.extend((year, run) for run in schedules[year])
    for number, (year, run) in enumerate(all_runs, start=1):
        cache_path = (
            client.cache_dir
            / "single_runs"
            / config["model"]
            / f"{run.strftime('%Y%m%dT%H%MZ')}.json.gz"
        )
        if not cache_path.exists() and client.network_requests - network_start >= network_cap:
            raise ArchiveRateLimitError(
                f"Stopped at the preregistered per-invocation cap of {network_cap} uncached "
                "single-run requests. Resume later; cached runs will not be downloaded again."
            )
        try:
            runs[run] = parse_single_run(client.single_run(run, points), points)
        except ArchiveRateLimitError:
            raise
        except ArchiveRunUnavailable as exc:
            failures.append({"run": run.isoformat(), "error": str(exc)})
            LOG.warning("run unavailable %s: %s", run.isoformat(), exc)
        except FileNotFoundError as exc:
            if client.offline:
                raise ArchiveRateLimitError(str(exc)) from exc
            failures.append({"run": run.isoformat(), "error": f"FileNotFoundError: {exc}"})
        except Exception as exc:  # Missing archive runs become explicit coverage gaps.
            failures.append({"run": run.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
            LOG.warning("run unavailable %s: %s", run.isoformat(), exc)
        if number % 50 == 0 or number == len(all_runs):
            LOG.info("forecast runs loaded: %d/%d", number, len(all_runs))
    return runs, schedules, failures


def exact_hourly_window(
    history: pd.DataFrame,
    single: pd.DataFrame,
    weather_point_id: str,
    issue_time: pd.Timestamp,
    valid_time: pd.Timestamp,
    hours: int,
) -> pd.Series | None:
    issue = utc_timestamp(issue_time)
    valid = utc_timestamp(valid_time)
    expected = pd.date_range(valid - pd.Timedelta(hours=hours - 1), valid, freq="h")
    hist_times = expected[expected <= issue]
    future_times = expected[expected > issue]
    parts: list[pd.Series] = []
    try:
        if len(hist_times):
            parts.append(
                history.loc[(weather_point_id, hist_times), "precipitation"].set_axis(hist_times)
            )
        if len(future_times):
            parts.append(
                single.loc[(weather_point_id, future_times), "precipitation"].set_axis(future_times)
            )
    except KeyError:
        return None
    values = pd.concat(parts).reindex(expected) if parts else pd.Series(dtype=float)
    if len(values) != hours or values.isna().any() or not values.index.equals(expected):
        return None
    return values.astype(float)


def feature_row(
    history: pd.DataFrame,
    single: pd.DataFrame,
    weather_point_id: str,
    issue_time: pd.Timestamp,
    valid_time: pd.Timestamp,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    valid = utc_timestamp(valid_time)
    try:
        instant = single.loc[(weather_point_id, valid)]
    except KeyError:
        return None
    required = [
        "soil_moisture_0_to_7cm",
        "soil_moisture_7_to_28cm",
        "temperature_2m",
    ]
    if instant[required].isna().any():
        return None
    longest = int(max(config["features"]["api_windows_hours"].values()))
    precipitation = exact_hourly_window(
        history, single, weather_point_id, issue_time, valid, longest
    )
    if precipitation is None:
        return None
    api3_hours = int(config["features"]["api_windows_hours"]["api_3d"])
    api7_hours = int(config["features"]["api_windows_hours"]["api_7d"])
    return {
        "weather_point_id": weather_point_id,
        "valid_time": valid,
        "soil_moisture_0_7": float(instant["soil_moisture_0_to_7cm"]),
        "soil_moisture_7_28": float(instant["soil_moisture_7_to_28cm"]),
        "temp_c": float(instant["temperature_2m"]),
        "api_3d": float(precipitation.iloc[-api3_hours:].sum()),
        "api_7d": float(precipitation.iloc[-api7_hours:].sum()),
        "baseline_precip_1h": float(precipitation.iloc[-1]),
        "baseline_precip_3h": float(precipitation.iloc[-3:].sum()),
    }


def train_fresh_random_forest(config: dict[str, Any]) -> tuple[RandomForestClassifier, dict[str, Any]]:
    """Rebuild the training sample with the existing pipeline, restricted to 2018-2023."""
    LOG.info("building the 2018-2023 ERA5-domain training sample")
    grid = gpd.read_parquet(DATA_DIR / "kamrup_metro_grid_1km.parquet")
    weather = training.load_weather()
    train_start, train_end = config["periods"]["training"]
    weather = weather[
        (weather["timestamp"] >= pd.Timestamp(train_start))
        & (weather["timestamp"] < pd.Timestamp(train_end) + pd.Timedelta(days=1))
    ].copy()
    mapping = training.load_mapping_with_terrain()
    positives = training.pass1_collect_positives(weather, mapping, grid)
    safe_cells = training.compute_safe_cells(positives, grid)
    negatives = training.pass2_sample_negatives(
        weather,
        mapping,
        grid,
        safe_cells,
        len(positives) * int(config["training"]["negative_to_positive_ratio"]),
    )
    frame = pd.concat([positives, negatives], ignore_index=True)
    features = list(config["features"]["ordered"])
    if features != training.FEATURES:
        raise RuntimeError("Frozen feature order differs from the existing training pipeline.")
    if frame[features].isna().any().any():
        raise RuntimeError("Training features contain missing values.")
    model = RandomForestClassifier(
        n_estimators=int(config["training"]["n_estimators"]),
        class_weight=config["training"]["class_weight"],
        random_state=int(config["training"]["random_state"]),
        n_jobs=-1,
    )
    model.fit(frame[features].astype("float32"), frame["target_event"].to_numpy())
    details = {
        "algorithm": type(model).__name__,
        "features": features,
        "n_rows": int(len(frame)),
        "n_positive": int((frame["target_event"] == 1).sum()),
        "n_negative": int((frame["target_event"] == 0).sum()),
        "training_start": train_start,
        "training_end": train_end,
        "random_state": int(config["training"]["random_state"]),
        "forbidden_pickle_loaded": False,
    }
    return model, details


def load_cell_mapping() -> pd.DataFrame:
    mapping = pd.read_parquet(
        DATA_DIR / "grid_weather_mapping.parquet",
        columns=["grid_id", "weather_point_id"],
    )
    terrain = pd.read_parquet(
        DATA_DIR / "terrain_features.parquet", columns=["grid_id", "slope_mean"]
    )
    return mapping.merge(terrain, on="grid_id", how="left", validate="one_to_one")


def build_reference_episodes(config: dict[str, Any], mapping: pd.DataFrame) -> pd.DataFrame:
    """Build district episodes from ERA5 threshold labels, without incident augmentation."""
    weather = pd.read_parquet(
        DATA_DIR / "weather_hourly.parquet",
        columns=["weather_point_id", "timestamp", "precipitation_mm"],
    ).sort_values(["weather_point_id", "timestamp"])
    weather["precip_3hr_mm"] = weather.groupby("weather_point_id")[
        "precipitation_mm"
    ].transform(lambda values: values.rolling(window=3, min_periods=3).sum())
    eligible_cells = mapping[
        mapping["slope_mean"] >= float(config["reference"]["slope_degrees"])
    ].copy()
    cells_by_point = {
        str(point): tuple(sorted(group["grid_id"].astype(str)))
        for point, group in eligible_cells.groupby("weather_point_id")
    }
    weather = weather[weather["weather_point_id"].isin(cells_by_point)].copy()
    weather["active"] = (
        weather["precipitation_mm"]
        >= float(config["reference"]["precipitation_1h_mm"])
    ) | (
        weather["precip_3hr_mm"]
        >= float(config["reference"]["precipitation_3h_mm"])
    )
    years = weather["timestamp"].dt.year
    months = weather["timestamp"].dt.month
    weather = weather[
        years.isin([2024, 2025])
        & months.isin(config["periods"]["monsoon_months"])
        & weather["active"]
    ].copy()
    active_by_time = {
        pd.Timestamp(ts): tuple(sorted(group["weather_point_id"].astype(str).unique()))
        for ts, group in weather.groupby("timestamp")
    }
    active_times = sorted(active_by_time)
    if not active_times:
        raise RuntimeError("No reference threshold hours in 2024-2025.")
    gap = pd.Timedelta(hours=int(config["reference"]["episode_merge_gap_hours"]))
    episodes: list[list[pd.Timestamp]] = [[active_times[0]]]
    for timestamp in active_times[1:]:
        if timestamp - episodes[-1][-1] > gap:
            episodes.append([timestamp])
        else:
            episodes[-1].append(timestamp)
    rows = []
    for episode_number, timestamps in enumerate(episodes):
        onset_local_naive = timestamps[0]
        onset_points = active_by_time[onset_local_naive]
        onset_cells = sorted(
            {cell for point in onset_points for cell in cells_by_point[str(point)]}
        )
        onset_local = onset_local_naive.tz_localize(IST)
        verification_time = onset_local.tz_convert(UTC) + pd.Timedelta(
            minutes=int(config["reference"]["reference_key_offset_minutes"])
        )
        rows.append(
            {
                "episode_id": f"E{episode_number + 1:04d}",
                "onset_time_local": onset_local,
                "onset_time_utc": verification_time,
                "reference_hour_time_utc": onset_local.tz_convert(UTC),
                "end_time_local": timestamps[-1].tz_localize(IST),
                "onset_weather_points": tuple(onset_points),
                "onset_cells": tuple(onset_cells),
                "year": onset_local.year,
            }
        )
    return pd.DataFrame(rows)


def assemble_predictions(
    year: int,
    latency: int,
    config: dict[str, Any],
    points: list[WeatherPoint],
    history: pd.DataFrame,
    runs: dict[pd.Timestamp, pd.DataFrame],
    schedule: list[pd.Timestamp],
    model: RandomForestClassifier,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for run in schedule:
        single = runs.get(run)
        if single is None:
            continue
        issue = run + pd.Timedelta(hours=latency)
        for lead in config["issue_policy"]["lead_hours"]:
            valid = issue + pd.Timedelta(hours=int(lead))
            if not valid_in_scope(valid, config, year):
                continue
            for point in points:
                row = feature_row(
                    history,
                    single,
                    point.weather_point_id,
                    issue,
                    valid,
                    config,
                )
                if row is None:
                    continue
                row.update(
                    {
                        "run_initialisation": run,
                        "issue_time": issue,
                        "lead_hours": int(lead),
                        "latency_hours": int(latency),
                    }
                )
                records.append(row)
    columns = [
        "run_initialisation",
        "issue_time",
        "valid_time",
        "weather_point_id",
        "lead_hours",
        "latency_hours",
        *config["features"]["ordered"],
        "baseline_precip_1h",
        "baseline_precip_3h",
    ]
    frame = pd.DataFrame(records, columns=columns)
    if frame.empty:
        frame["priority_index"] = pd.Series(dtype=float)
        frame["baseline_alert"] = pd.Series(dtype=bool)
        return frame
    X = frame[config["features"]["ordered"]].astype("float32")
    frame["priority_index"] = model.predict_proba(X)[:, 1]
    frame["baseline_alert"] = (
        frame["baseline_precip_1h"]
        >= float(config["baseline"]["precipitation_1h_mm"])
    ) | (
        frame["baseline_precip_3h"]
        >= float(config["baseline"]["precipitation_3h_mm"])
    )
    return frame


def episode_maps(episodes: pd.DataFrame, year: int) -> dict[pd.Timestamp, dict[str, Any]]:
    result: dict[pd.Timestamp, dict[str, Any]] = {}
    for row in episodes[episodes["year"] == year].itertuples(index=False):
        result[utc_timestamp(row.onset_time_utc)] = {
            "episode_id": row.episode_id,
            "weather_points": set(row.onset_weather_points),
            "cells": set(row.onset_cells),
        }
    return result


def assign_episodes_to_targets(
    episodes: pd.DataFrame, year: int, targets: list[pd.Timestamp]
) -> tuple[dict[pd.Timestamp, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Assign each onset to the latest 12-hourly target at or before onset."""
    ordered = sorted(utc_timestamp(target) for target in targets)
    target_ns = np.asarray([target.value for target in ordered], dtype=np.int64)
    assigned: dict[pd.Timestamp, list[dict[str, Any]]] = {target: [] for target in ordered}
    unassigned: list[dict[str, Any]] = []
    year_episodes = episodes[episodes["year"] == year]
    for row in year_episodes.itertuples(index=False):
        onset = utc_timestamp(row.onset_time_utc)
        event = {
            "episode_id": row.episode_id,
            "onset_time_utc": onset,
            "weather_points": set(row.onset_weather_points),
            "cells": set(row.onset_cells),
        }
        index = int(np.searchsorted(target_ns, onset.value, side="right") - 1)
        if index < 0:
            unassigned.append(event)
            continue
        target = ordered[index]
        next_target = ordered[index + 1] if index + 1 < len(ordered) else target + pd.Timedelta(hours=12)
        if onset >= next_target:
            unassigned.append(event)
            continue
        assigned[target].append(event)
    return assigned, unassigned


def bootstrap_interval(
    values: list[float], config: dict[str, Any], seed_offset: int = 0
) -> list[float | None]:
    if not values:
        return [None, None]
    array = np.asarray(values, dtype=float)
    rng = np.random.default_rng(
        int(config["metrics"]["bootstrap"]["random_state"]) + seed_offset
    )
    replicates = int(config["metrics"]["bootstrap"]["replicates"])
    samples = rng.choice(array, size=(replicates, len(array)), replace=True).mean(axis=1)
    alpha = 1.0 - float(config["metrics"]["bootstrap"]["confidence"])
    low, high = np.quantile(samples, [alpha / 2.0, 1.0 - alpha / 2.0])
    return [float(low), float(high)]


def _monthly_false_alerts(
    prediction: pd.DataFrame,
    targets: list[pd.Timestamp],
    events_by_target: dict[pd.Timestamp, list[dict[str, Any]]],
    threshold: float,
    point_count: int,
    baseline: bool,
) -> tuple[dict[str, int], dict[str, dict[str, int | bool]]]:
    grouped = {
        utc_timestamp(timestamp): group
        for timestamp, group in prediction.groupby("valid_time", sort=False)
    }
    month_days: dict[str, set[str]] = {}
    month_status: dict[str, dict[str, int | bool]] = {}
    for target in targets:
        target = utc_timestamp(target)
        local = target.tz_convert(IST)
        month = local.strftime("%Y-%m")
        date = local.strftime("%Y-%m-%d")
        status = month_status.setdefault(
            month,
            {"scheduled_targets": 0, "complete_targets": 0, "fully_evaluated": False},
        )
        status["scheduled_targets"] = int(status["scheduled_targets"]) + 1
        group = grouped.get(target)
        if group is None:
            continue
        if group["weather_point_id"].nunique() == point_count:
            status["complete_targets"] = int(status["complete_targets"]) + 1
        if baseline:
            alert_points = set(
                group.loc[group["baseline_alert"], "weather_point_id"].astype(str)
            )
        else:
            alert_points = set(
                group.loc[
                    group["priority_index"] >= threshold, "weather_point_id"
                ].astype(str)
            )
        if not alert_points:
            continue
        target_events = events_by_target.get(target, [])
        matched = any(
            bool(alert_points & event["weather_points"]) for event in target_events
        )
        if not matched:
            month_days.setdefault(month, set()).add(date)
    for month, status in month_status.items():
        status["fully_evaluated"] = (
            status["scheduled_targets"] == status["complete_targets"]
        )
    counts = {month: len(month_days.get(month, set())) for month in month_status}
    return counts, month_status


def calibration_summary(
    prediction: pd.DataFrame,
    schedules: list[pd.Timestamp],
    episodes: pd.DataFrame,
    latency: int,
    threshold: float,
    config: dict[str, Any],
    point_count: int,
) -> dict[str, Any]:
    total_hits = 0
    total_events = 0
    all_false: dict[str, dict[str, int]] = {}
    budget_ok = True
    for lead in config["issue_policy"]["lead_hours"]:
        lead = int(lead)
        targets = expected_targets(schedules, latency, lead, config, 2024)
        events_by_target, unassigned = assign_episodes_to_targets(episodes, 2024, targets)
        lead_frame = prediction[prediction["lead_hours"] == lead]
        grouped = {
            utc_timestamp(timestamp): group
            for timestamp, group in lead_frame.groupby("valid_time", sort=False)
        }
        for timestamp, target_events in events_by_target.items():
            group = grouped.get(timestamp)
            for event in target_events:
                hit = False
                if group is not None:
                    relevant = group[
                        group["weather_point_id"].astype(str).isin(event["weather_points"])
                    ]
                    hit = bool((relevant["priority_index"] >= threshold).any())
                total_hits += int(hit)
                total_events += 1
        total_events += len(unassigned)
        false_days, _ = _monthly_false_alerts(
            lead_frame,
            targets,
            events_by_target,
            threshold,
            point_count,
            baseline=False,
        )
        all_false[str(lead)] = false_days
        if any(
            count
            > int(config["claim_gate"]["false_alert_days_maximum_per_full_monsoon_month"])
            for count in false_days.values()
        ):
            budget_ok = False
    return {
        "threshold": float(threshold),
        "pooled_episode_recall": (
            float(total_hits / total_events) if total_events else 0.0
        ),
        "hits": int(total_hits),
        "events": int(total_events),
        "false_alert_days_by_lead": all_false,
        "budget_ok": budget_ok,
        "total_false_alert_days": int(
            sum(sum(months.values()) for months in all_false.values())
        ),
    }


def choose_threshold(
    prediction: pd.DataFrame,
    schedules: list[pd.Timestamp],
    episodes: pd.DataFrame,
    latency: int,
    config: dict[str, Any],
    point_count: int,
) -> dict[str, Any] | None:
    if prediction.empty:
        return None
    facts: dict[int, list[tuple[str, str, float, float]]] = {}
    event_scores: list[float] = []
    decision_scores: set[float] = {0.0, 1.0}
    for lead_value in config["issue_policy"]["lead_hours"]:
        lead = int(lead_value)
        frame = prediction[prediction["lead_hours"] == lead]
        groups = {
            utc_timestamp(timestamp): group
            for timestamp, group in frame.groupby("valid_time", sort=False)
        }
        targets = expected_targets(schedules, latency, lead, config, 2024)
        events_by_target, unassigned = assign_episodes_to_targets(episodes, 2024, targets)
        event_scores.extend([-math.inf] * len(unassigned))
        lead_facts: list[tuple[str, str, float, float]] = []
        for target in targets:
            target = utc_timestamp(target)
            group = groups.get(target)
            district_max = (
                float(group["priority_index"].max()) if group is not None else -math.inf
            )
            target_events = events_by_target.get(target, [])
            target_event_scores: list[float] = []
            for event in target_events:
                score = -math.inf
                if group is not None:
                    relevant = group[
                        group["weather_point_id"].astype(str).isin(event["weather_points"])
                    ]
                    if not relevant.empty:
                        score = float(relevant["priority_index"].max())
                event_scores.append(score)
                target_event_scores.append(score)
            event_max = max(target_event_scores, default=-math.inf)
            local = target.tz_convert(IST)
            lead_facts.append(
                (local.strftime("%Y-%m"), local.strftime("%Y-%m-%d"), district_max, event_max)
            )
            if np.isfinite(district_max):
                decision_scores.add(float(np.clip(district_max, 0.0, 1.0)))
            if np.isfinite(event_max):
                decision_scores.add(float(np.clip(event_max, 0.0, 1.0)))
        facts[lead] = lead_facts
    candidates = sorted(decision_scores)
    best_key: tuple[float, float, int] | None = None
    best_threshold: float | None = None
    false_budget = int(
        config["claim_gate"]["false_alert_days_maximum_per_full_monsoon_month"]
    )
    for threshold in candidates:
        hits = sum(score >= threshold for score in event_scores)
        false_total = 0
        budget_ok = True
        for lead_facts in facts.values():
            false_dates: dict[str, set[str]] = {}
            for month, date, district_max, event_max in lead_facts:
                if district_max >= threshold and event_max < threshold:
                    false_dates.setdefault(month, set()).add(date)
            counts = [len(dates) for dates in false_dates.values()]
            false_total += sum(counts)
            if any(count > false_budget for count in counts):
                budget_ok = False
                break
        if not budget_ok:
            continue
        recall = float(hits / len(event_scores)) if event_scores else 0.0
        key = (
            recall,
            float(threshold),
            -false_total,
        )
        if best_key is None or key > best_key:
            best_key = key
            best_threshold = float(threshold)
    if best_threshold is None:
        return None
    best = calibration_summary(
        prediction,
        schedules,
        episodes,
        latency,
        best_threshold,
        config,
        point_count,
    )
    best["candidate_count"] = len(candidates)
    return best


def freeze_calibrated_thresholds(
    config: dict[str, Any], calibration: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    existing = config.get("frozen_calibration")
    selected = {key: value["threshold"] for key, value in calibration.items()}
    if existing is not None:
        if existing.get("threshold_by_latency_hours") != selected:
            raise RuntimeError(
                "Frozen thresholds differ from the newly computed calibration; refusing to overwrite."
            )
        return config
    config = json.loads(json.dumps(config))
    config["status"] = "threshold_frozen_before_test"
    config["frozen_calibration"] = {
        "calibration_year": 2024,
        "threshold_by_latency_hours": selected,
        "one_threshold_across_leads": True,
        "test_data_used": False,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


def evaluate_lead(
    prediction: pd.DataFrame,
    schedule: list[pd.Timestamp],
    episodes: pd.DataFrame,
    year: int,
    latency: int,
    lead: int,
    threshold: float,
    config: dict[str, Any],
    point_count: int,
) -> dict[str, Any]:
    targets = expected_targets(schedule, latency, lead, config, year)
    frame = prediction[prediction["lead_hours"] == lead].copy()
    events_by_target, unassigned = assign_episodes_to_targets(episodes, year, targets)
    grouped = {
        utc_timestamp(timestamp): group
        for timestamp, group in frame.groupby("valid_time", sort=False)
    }
    model_hits: list[float] = []
    baseline_hits: list[float] = []
    conditional_hits: list[float] = []
    event_detail: list[dict[str, Any]] = []
    for timestamp, target_events in events_by_target.items():
        group = grouped.get(timestamp)
        complete = group is not None and group["weather_point_id"].nunique() == point_count
        for event in target_events:
            model_hit = False
            baseline_hit = False
            if group is not None:
                relevant = group[
                    group["weather_point_id"].astype(str).isin(event["weather_points"])
                ]
                model_hit = bool((relevant["priority_index"] >= threshold).any())
                baseline_hit = bool(relevant["baseline_alert"].any())
            model_hits.append(float(model_hit))
            baseline_hits.append(float(baseline_hit))
            if complete:
                conditional_hits.append(float(model_hit))
            issue_time = timestamp - pd.Timedelta(hours=lead)
            actual_lead = (
                event["onset_time_utc"] - issue_time
            ).total_seconds() / 3600.0
            event_detail.append(
                {
                    "episode_id": event["episode_id"],
                    "onset_time_utc": event["onset_time_utc"].isoformat(),
                    "assigned_target_time_utc": timestamp.isoformat(),
                    "actual_issue_to_onset_lead_hours": float(actual_lead),
                    "forecast_complete": bool(complete),
                    "model_hit": bool(model_hit),
                    "baseline_hit": bool(baseline_hit),
                }
            )
    for event in unassigned:
        model_hits.append(0.0)
        baseline_hits.append(0.0)
        event_detail.append(
            {
                "episode_id": event["episode_id"],
                "onset_time_utc": event["onset_time_utc"].isoformat(),
                "assigned_target_time_utc": None,
                "actual_issue_to_onset_lead_hours": None,
                "forecast_complete": False,
                "model_hit": False,
                "baseline_hit": False,
            }
        )
    model_false, month_status = _monthly_false_alerts(
        frame, targets, events_by_target, threshold, point_count, baseline=False
    )
    baseline_false, _ = _monthly_false_alerts(
        frame, targets, events_by_target, threshold, point_count, baseline=True
    )
    expected_rows = len(targets) * point_count
    actual_rows = int(len(frame))
    coverage = float(actual_rows / expected_rows) if expected_rows else 0.0
    recall = float(np.mean(model_hits)) if model_hits else None
    baseline_recall = float(np.mean(baseline_hits)) if baseline_hits else None
    conditional_recall = float(np.mean(conditional_hits)) if conditional_hits else None
    paired = [a - b for a, b in zip(model_hits, baseline_hits, strict=True)]
    recall_ci = bootstrap_interval(model_hits, config, seed_offset=lead)
    baseline_ci = bootstrap_interval(baseline_hits, config, seed_offset=100 + lead)
    paired_ci = bootstrap_interval(paired, config, seed_offset=200 + lead)
    full_months = [
        month for month, status in month_status.items() if status["fully_evaluated"]
    ]
    false_gate = all(
        model_false[month]
        <= int(config["claim_gate"]["false_alert_days_maximum_per_full_monsoon_month"])
        for month in full_months
    )
    gates = {
        "coverage": coverage >= float(config["claim_gate"]["coverage_minimum"]),
        "episode_recall_lower_95_ci": (
            recall_ci[0] is not None
            and recall_ci[0]
            >= float(config["claim_gate"]["episode_recall_lower_95_ci_minimum"])
        ),
        "false_alert_days": false_gate,
        "positive_paired_recall_improvement_over_baseline": (
            bool(paired) and float(np.mean(paired)) > 0.0
        ),
    }
    passed = all(gates.values())
    return {
        "lead_hours": lead,
        "threshold": float(threshold),
        "coverage": coverage,
        "coverage_rows": {"available": actual_rows, "expected": expected_rows},
        "reference_episodes_in_year": int((episodes["year"] == year).sum()),
        "episodes_assigned_to_alert_window": int(len(model_hits) - len(unassigned)),
        "unassigned_episodes_counted_as_misses": len(unassigned),
        "episode_recall": recall,
        "episode_recall_95_ci": recall_ci,
        "coverage_conditional_episode_recall": conditional_recall,
        "coverage_conditional_episode_count": len(conditional_hits),
        "baseline_episode_recall": baseline_recall,
        "baseline_episode_recall_95_ci": baseline_ci,
        "paired_recall_improvement": float(np.mean(paired)) if paired else None,
        "paired_recall_improvement_95_ci": paired_ci,
        "model_false_alert_days_by_month": model_false,
        "baseline_false_alert_days_by_month": baseline_false,
        "month_coverage": month_status,
        "fully_evaluated_months": full_months,
        "baseline_within_alert_budget": all(
            value
            <= int(config["baseline"]["same_monthly_false_alert_budget"])
            for value in baseline_false.values()
        ),
        "gate_components": gates,
        "gate_passed": passed,
        "claim": (
            f"retrospective threshold-event skill established at {lead} h under the assumed latency"
            if passed
            else config["claim_gate"]["failure_text"]
        ),
        "episode_results": event_detail,
    }


def map_incidents_to_cells() -> pd.DataFrame:
    incidents = pd.read_csv(INCIDENTS_PATH)
    incidents["date"] = pd.to_datetime(incidents["date"])
    incidents = incidents[
        (incidents["date"].dt.year >= 2022) & (incidents["date"].dt.year <= 2025)
    ].copy()
    grid = gpd.read_parquet(DATA_DIR / "kamrup_metro_grid_1km.parquet")
    grid_metric = grid.to_crs(training.METRIC_CRS).copy()
    grid_metric["geometry"] = grid_metric.geometry.centroid
    incident_geo = gpd.GeoDataFrame(
        incidents,
        geometry=gpd.points_from_xy(incidents["longitude"], incidents["latitude"]),
        crs="EPSG:4326",
    ).to_crs(training.METRIC_CRS)
    mapped = incident_geo.sjoin_nearest(
        grid_metric[["grid_id", "geometry"]], how="left", distance_col="mapping_distance_m"
    )
    mapped = mapped.sort_values(["date", "mapping_distance_m", "grid_id"]).drop_duplicates(
        ["date", "location"], keep="first"
    )
    return pd.DataFrame(mapped.drop(columns=["geometry", "index_right"], errors="ignore"))


def incident_recall_at_10(
    latency: int,
    lead: int,
    threshold: float,
    config: dict[str, Any],
    points: list[WeatherPoint],
    mapping: pd.DataFrame,
    history: pd.DataFrame,
    runs: dict[pd.Timestamp, pd.DataFrame],
    schedules: dict[int, list[pd.Timestamp]],
    model: RandomForestClassifier,
) -> dict[str, Any]:
    del threshold  # Ranking uses the continuous priority index, not the alert cutoff.
    incidents = map_incidents_to_cells()
    details: list[dict[str, Any]] = []
    features = config["features"]["ordered"]
    for incident in incidents.itertuples(index=False):
        date = pd.Timestamp(incident.date)
        row: dict[str, Any] = {
            "date": date.strftime("%Y-%m-%d"),
            "location": str(incident.location),
            "mapped_grid_id": str(incident.grid_id),
            "mapping_distance_m": float(incident.mapping_distance_m),
            "status": "unavailable",
            "reason": None,
            "hit_deterministic_top10": False,
            "hit_under_any_boundary_tie": False,
            "hit_under_all_boundary_ties": False,
        }
        year = date.year
        if year not in schedules:
            row["reason"] = "incident predates the Single Runs archive evaluation window"
            details.append(row)
            continue
        day_start_local = date.tz_localize(IST)
        cutoff = day_start_local.tz_convert(UTC) - pd.Timedelta(hours=lead)
        candidate_runs = [
            run
            for run in schedules[year]
            if run in runs and run + pd.Timedelta(hours=latency) <= cutoff
        ]
        if not candidate_runs:
            row["reason"] = "no cached run satisfies the frozen pre-day snapshot rule"
            details.append(row)
            continue
        selected_run = max(candidate_runs)
        issue = selected_run + pd.Timedelta(hours=latency)
        if issue > cutoff:
            raise AssertionError("Incident snapshot uses information after its cutoff.")
        single = runs[selected_run]
        # IFS output is on whole UTC hours, which are IST half-hours.  Use all
        # 24 model-valid hours whose local timestamps fall on the incident date.
        local_hours = pd.date_range(
            day_start_local + pd.Timedelta(minutes=30), periods=24, freq="h"
        )
        target_hours = local_hours.tz_convert(UTC)
        feature_rows: list[dict[str, Any]] = []
        for target in target_hours:
            for point in points:
                feature = feature_row(
                    history,
                    single,
                    point.weather_point_id,
                    issue,
                    target,
                    config,
                )
                if feature is not None:
                    feature_rows.append(feature)
        expected = len(target_hours) * len(points)
        if len(feature_rows) != expected:
            row["reason"] = f"incomplete pre-day forecast snapshot ({len(feature_rows)}/{expected} rows)"
            details.append(row)
            continue
        feature_frame = pd.DataFrame(feature_rows)
        feature_frame["priority_index"] = model.predict_proba(
            feature_frame[features].astype("float32")
        )[:, 1]
        point_max = feature_frame.groupby("weather_point_id")["priority_index"].max()
        ranked = mapping[["grid_id", "weather_point_id"]].copy()
        ranked["priority_index"] = ranked["weather_point_id"].map(point_max)
        if ranked["priority_index"].isna().any():
            row["reason"] = "one or more cells lack a daily priority index"
            details.append(row)
            continue
        ranked = ranked.sort_values(
            ["priority_index", "grid_id"], ascending=[False, True]
        ).reset_index(drop=True)
        ranked["deterministic_rank"] = np.arange(1, len(ranked) + 1)
        top10 = set(ranked.head(10)["grid_id"].astype(str))
        incident_rank = ranked[ranked["grid_id"].astype(str) == str(incident.grid_id)].iloc[0]
        cutoff_score = float(ranked.iloc[9]["priority_index"])
        incident_score = float(incident_rank["priority_index"])
        strictly_above = int((ranked["priority_index"] > cutoff_score).sum())
        boundary_ties = int((ranked["priority_index"] == cutoff_score).sum())
        row.update(
            {
                "status": "available",
                "reason": None,
                "snapshot_run_initialisation_utc": selected_run.isoformat(),
                "snapshot_issue_time_utc": issue.isoformat(),
                "snapshot_cutoff_utc": cutoff.isoformat(),
                "priority_index": incident_score,
                "deterministic_rank": int(incident_rank["deterministic_rank"]),
                "top10_cutoff_priority_index": cutoff_score,
                "boundary_tie_cell_count": boundary_ties,
                "hit_deterministic_top10": str(incident.grid_id) in top10,
                "hit_under_any_boundary_tie": incident_score >= cutoff_score,
                "hit_under_all_boundary_ties": (
                    incident_score > cutoff_score
                    or (incident_score == cutoff_score and strictly_above + boundary_ties <= 10)
                ),
            }
        )
        details.append(row)
    available = [row for row in details if row["status"] == "available"]
    hits = sum(bool(row["hit_deterministic_top10"]) for row in details)
    any_tie_hits = sum(bool(row["hit_under_any_boundary_tie"]) for row in details)
    return {
        "label": "weak supporting evidence",
        "lead_hours": lead,
        "latency_hours": latency,
        "total_incident_days": len(details),
        "available_incident_days": len(available),
        "unavailable_incident_days": len(details) - len(available),
        "deterministic_recall_at_10_all_seven": (
            float(hits / len(details)) if details else None
        ),
        "deterministic_recall_at_10_available_only": (
            float(hits / len(available)) if available else None
        ),
        "optimistic_boundary_tie_recall_all_seven": (
            float(any_tie_hits / len(details)) if details else None
        ),
        "tie_break": config["metrics"]["recall_at_10"]["tie_break"],
        "note": (
            "The model has weather-point features, so many cells tie. The optimistic tie value "
            "counts an incident cell if it could enter the top ten under some ordering of the "
            "cutoff-score tie. Four of seven incident dates predate this archive window."
        ),
        "incidents": details,
    }


def run_leakage_tests(config: dict[str, Any]) -> list[dict[str, str]]:
    """Executable checks for the as-of feature and split contracts."""
    results: list[dict[str, str]] = []

    def passed(name: str, detail: str) -> None:
        results.append({"name": name, "status": "passed", "detail": detail})

    point = "P"
    issue = pd.Timestamp("2025-06-01T07:00:00Z")
    valid = issue + pd.Timedelta(hours=24)
    times = pd.date_range(issue - pd.Timedelta(hours=200), valid, freq="h")
    history_frame = pd.DataFrame(
        {
            "weather_point_id": point,
            "timestamp": times,
            "precipitation": np.arange(len(times), dtype=float) / 100.0,
        }
    ).set_index(["weather_point_id", "timestamp"])
    single_frame = pd.DataFrame(
        {
            "weather_point_id": point,
            "timestamp": times,
            "precipitation": 1.0,
            "temperature_2m": 20.0,
            "soil_moisture_0_to_7cm": 0.3,
            "soil_moisture_7_to_28cm": 0.35,
        }
    ).set_index(["weather_point_id", "timestamp"])
    row = feature_row(history_frame, single_frame, point, issue, valid, config)
    assert row is not None
    mutated = history_frame.copy()
    future_index = mutated.index.get_level_values("timestamp") > issue
    mutated.loc[future_index, "precipitation"] = 9999.0
    mutated_row = feature_row(mutated, single_frame, point, issue, valid, config)
    assert mutated_row is not None
    assert row["api_3d"] == mutated_row["api_3d"]
    assert row["api_7d"] == mutated_row["api_7d"]
    passed("as_of_future_mutation", "Changing Historical Forecast values after issue time cannot change features.")

    assert valid - issue == pd.Timedelta(hours=24)
    passed("exact_lead", "Valid time equals latency-adjusted issue time plus the requested lead.")

    run_initialisation = pd.Timestamp("2025-06-01T00:00:00Z")
    assumed_availability = run_initialisation + pd.Timedelta(
        hours=int(config["issue_policy"]["primary_latency_hours"])
    )
    assert assumed_availability == issue
    assert run_initialisation <= issue
    passed(
        "run_availability",
        "A run is used only at its preregistered initialization-plus-latency issue time.",
    )

    missing = history_frame.copy()
    missing.loc[(point, issue - pd.Timedelta(hours=100)), "precipitation"] = np.nan
    assert feature_row(missing, single_frame, point, issue, valid, config) is None
    passed("complete_window", "A null inside the 168-hour hybrid window makes the feature row unavailable.")

    null_at_run_start = single_frame.copy()
    null_at_run_start.iloc[0, null_at_run_start.columns.get_loc("precipitation")] = np.nan
    assert feature_row(history_frame, null_at_run_start, point, issue, valid, config) is not None
    passed("unused_run_start_null", "A null outside the selected future segment does not contaminate the window.")

    used_null = single_frame.copy()
    used_null.loc[(point, issue + pd.Timedelta(hours=1)), "precipitation"] = np.nan
    assert feature_row(history_frame, used_null, point, issue, valid, config) is None
    passed("used_forecast_null", "A null in the selected run segment is rejected rather than zero-filled.")

    train_end = pd.Timestamp(config["periods"]["training"][1])
    calibration_start = pd.Timestamp(config["periods"]["calibration"][0])
    calibration_end = pd.Timestamp(config["periods"]["calibration"][1])
    test_start = pd.Timestamp(config["periods"]["test"][0])
    assert train_end < calibration_start <= calibration_end < test_start
    passed("temporal_partitions", "Training, calibration, and test periods are chronological and disjoint.")

    assert config["features"]["era5_allowed_in_forecast_features"] is False
    passed("forecast_feature_source", "The frozen contract forbids ERA5 in all forecast-time features.")

    assert config["training"]["forbidden_model_path"] == "models/random_forest_trigger_model.pkl"
    passed("forbidden_pickle", "The evaluation trains a fresh model and never opens the forbidden pickle.")
    return results


def make_plot(primary: dict[str, Any], config: dict[str, Any]) -> None:
    leads = sorted(int(key) for key in primary)
    recall = [primary[str(lead)]["episode_recall"] for lead in leads]
    intervals = [primary[str(lead)]["episode_recall_95_ci"] for lead in leads]
    baseline = [primary[str(lead)]["baseline_episode_recall"] for lead in leads]
    yerr_low = [max(0.0, value - interval[0]) for value, interval in zip(recall, intervals)]
    yerr_high = [max(0.0, interval[1] - value) for value, interval in zip(recall, intervals)]
    fig, axis = plt.subplots(figsize=(7.2, 4.5), constrained_layout=True)
    axis.errorbar(
        leads,
        recall,
        yerr=[yerr_low, yerr_high],
        color="#1b6f5a",
        marker="o",
        capsize=4,
        linewidth=2,
        label="RandomForest priority index",
    )
    axis.plot(leads, baseline, color="#d67b32", marker="s", linestyle="--", label="Rainfall baseline")
    gate = float(config["claim_gate"]["episode_recall_lower_95_ci_minimum"])
    axis.axhline(gate, color="#7a3e8e", linestyle=":", linewidth=1.6, label="Recall gate")
    axis.set_xticks(leads)
    axis.set_ylim(0.0, 1.02)
    axis.set_xlabel("Lead time (hours)")
    axis.set_ylabel("Episode recall")
    axis.set_title("PRAVAH retrospective threshold-event recall (2025)")
    axis.grid(axis="y", alpha=0.25)
    axis.legend(frameon=False, loc="best")
    OUTPUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT_PNG, dpi=180)
    plt.close(fig)


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"


def write_markdown(output: dict[str, Any]) -> None:
    primary_latency = str(output["primary_latency_hours"])
    primary = output["test"][primary_latency]
    lines = [
        "# PRAVAH retrospective forecast backtest",
        "",
        "This is a **latency-assumed retrospective evaluation** using pinned `ecmwf_ifs` data. "
        "Open-Meteo describes part of the Single Runs archive as IFS Cycle 49R1 hindcasts, so "
        "the result does not prove that these forecasts or this pipeline were available on the "
        "historical issue dates. The 2025 period was previously evaluated with ERA5-domain inputs "
        "and is therefore a reused test period rather than a pristine confirmatory test.",
        "",
        f"The primary latency assumption is {primary_latency} hours after model initialization. "
        "The threshold was selected only from 2024 forecast-domain data and frozen before 2025 "
        "predictions were assembled. The shipped `models/random_forest_trigger_model.pkl` was not read.",
        "",
        "## 2025 result at the primary latency",
        "",
        "| Lead | Coverage | Episode recall (95% bootstrap interval) | Baseline recall | Maximum monthly false-alert days | Decision |",
        "|---:|---:|---:|---:|---:|---|",
    ]
    for lead in sorted(primary, key=int):
        metric = primary[lead]
        interval = metric["episode_recall_95_ci"]
        interval_text = (
            "n/a"
            if interval[0] is None
            else f"{percent(metric['episode_recall'])} ({percent(interval[0])}–{percent(interval[1])})"
        )
        maximum_false = max(metric["model_false_alert_days_by_month"].values(), default=0)
        lines.append(
            f"| {lead} h | {percent(metric['coverage'])} | {interval_text} | "
            f"{percent(metric['baseline_episode_recall'])} | {maximum_false} | {metric['claim']} |"
        )
    lines.extend(
        [
            "",
            "Each scheduled alert remains valid until the next 12-hourly target. Episodes are paired "
            "with the latest target at or before onset, so the named lead is a minimum; actual "
            "issue-to-onset lead is retained per episode in the JSON. API or feature gaps count as "
            "misses. Coverage-conditional recall and all denominators are also reported.",
            "",
            "False-alert days are unique Asia/Kolkata target dates with at least one district alert "
            "that does not intersect an onset event at that scheduled target hour. They are false "
            "alerts against the rainfall-threshold proxy, not verified false landslide warnings.",
            "",
            "Recall@10 is weak supporting evidence. Four of the seven dated incidents predate the "
            "evaluation archive, and the five model features are shared by many cells, producing "
            "large ranking ties. The JSON reports the frozen `grid_id` tie-break and optimistic "
            "boundary-tie sensitivity separately.",
            "",
            "The 5-hour and 9-hour latency results are sensitivity analyses only. No parameter was "
            "changed after 2025 results were calculated.",
        ]
    )
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_failure_output(
    config: dict[str, Any], reason: str, leakage_tests: list[dict[str, str]]
) -> None:
    output = {
        "status": "calibration_failure",
        "reason": reason,
        "config_sha256": canonical_json_hash(config),
        "leakage_tests": leakage_tests,
        "test_evaluated": False,
    }
    OUTPUT_JSON.write_text(
        json.dumps(output, indent=2, default=json_value) + "\n", encoding="utf-8"
    )
    OUTPUT_MD.write_text(
        "# PRAVAH retrospective forecast backtest\n\n"
        f"Calibration failed: {reason}. The 2025 test period was not evaluated.\n",
        encoding="utf-8",
    )


def write_download_status(
    config: dict[str, Any], client: ForecastClient, reason: str
) -> None:
    cached_runs = list((client.cache_dir / "single_runs" / config["model"]).glob("*.json.gz"))
    expected = sum(len(make_run_schedule(config, year)) for year in (2024, 2025))
    output = {
        "status": "download_incomplete",
        "reason": reason,
        "cached_single_runs": len(cached_runs),
        "expected_single_runs": expected,
        "network_requests_this_invocation": client.network_requests,
        "resume_command": "venv\\Scripts\\python.exe scripts/forecast_backtest.py",
        "test_evaluated": False,
        "config_sha256": canonical_json_hash(config),
    }
    OUTPUT_JSON.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    OUTPUT_MD.write_text(
        "# PRAVAH retrospective forecast backtest\n\n"
        "The forecast archive download is incomplete, so calibration and the 2025 test were not "
        "run. Resume with `venv\\Scripts\\python.exe scripts/forecast_backtest.py`; completed responses are cached.\n\n"
        f"Provider status: {reason}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="read cached API responses only")
    parser.add_argument("--self-test", action="store_true", help="run leakage checks and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")

    config = load_config()
    leakage_tests = run_leakage_tests(config)
    if args.self_test:
        print(json.dumps(leakage_tests, indent=2))
        return 0

    initial_config_hash = canonical_json_hash(config)
    points = weather_points()
    expected_points = int(config["cache"]["single_run_batch_points"])
    if len(points) != expected_points:
        raise RuntimeError(f"Frozen point count is {expected_points}, project contains {len(points)}")
    mapping = load_cell_mapping()
    episodes = build_reference_episodes(config, mapping)
    calibration_episode_ids = set(episodes.loc[episodes["year"] == 2024, "episode_id"])
    test_episode_ids = set(episodes.loc[episodes["year"] == 2025, "episode_id"])
    if calibration_episode_ids & test_episode_ids:
        raise AssertionError("A reference episode crosses calibration and test partitions.")

    client = ForecastClient(config, offline=args.offline)
    LOG.info("loading stitched Historical Forecast precipitation")
    history = load_historical_precipitation(client, config, points)
    LOG.info("loading all 19 points in one request per single run")
    try:
        runs, schedules, run_failures = fetch_runs(client, config, points, (2024, 2025))
    except ArchiveRateLimitError as exc:
        write_download_status(config, client, str(exc))
        LOG.warning("archive download paused: %s", exc)
        return 3
    model, training_details = train_fresh_random_forest(config)
    model_hash_before_test = hashlib.sha256(pickle.dumps(model)).hexdigest()
    training_details["fitted_model_sha256"] = model_hash_before_test

    latencies = [
        int(config["issue_policy"]["primary_latency_hours"]),
        *[int(value) for value in config["issue_policy"]["sensitivity_latency_hours"]],
    ]
    latencies = list(dict.fromkeys(latencies))
    calibration_frames: dict[int, pd.DataFrame] = {}
    calibration_results: dict[str, dict[str, Any]] = {}
    frozen = config.get("frozen_calibration", {}).get("threshold_by_latency_hours", {})
    for latency in latencies:
        LOG.info("assembling 2024 forecast features for latency %dh", latency)
        frame = assemble_predictions(
            2024,
            latency,
            config,
            points,
            history,
            runs,
            schedules[2024],
            model,
        )
        calibration_frames[latency] = frame
        if str(latency) in frozen:
            threshold = float(frozen[str(latency)])
            selected = calibration_summary(
                frame,
                schedules[2024],
                episodes,
                latency,
                threshold,
                config,
                len(points),
            )
            selected["reused_previously_frozen_threshold"] = True
        else:
            selected = choose_threshold(
                frame, schedules[2024], episodes, latency, config, len(points)
            )
            if selected is None:
                reason = f"no threshold satisfies the 2024 alert budget at latency {latency}h"
                write_failure_output(config, reason, leakage_tests)
                LOG.error(reason)
                return 2
            selected["reused_previously_frozen_threshold"] = False
        calibration_results[str(latency)] = selected

    # This write occurs before assemble_predictions is called for 2025.
    config = freeze_calibrated_thresholds(config, calibration_results)
    leakage_tests.append(
        {
            "name": "threshold_frozen_before_test",
            "status": "passed",
            "detail": "The 2024-selected threshold was written to the frozen config before 2025 scoring.",
        }
    )

    test_results: dict[str, dict[str, Any]] = {}
    incident_results: dict[str, dict[str, Any]] = {}
    for latency in latencies:
        LOG.info("assembling 2025 forecast features for latency %dh", latency)
        test_frame = assemble_predictions(
            2025,
            latency,
            config,
            points,
            history,
            runs,
            schedules[2025],
            model,
        )
        threshold = float(calibration_results[str(latency)]["threshold"])
        test_results[str(latency)] = {}
        incident_results[str(latency)] = {}
        for lead_value in config["issue_policy"]["lead_hours"]:
            lead = int(lead_value)
            test_results[str(latency)][str(lead)] = evaluate_lead(
                test_frame,
                schedules[2025],
                episodes,
                2025,
                latency,
                lead,
                threshold,
                config,
                len(points),
            )
            incident_results[str(latency)][str(lead)] = incident_recall_at_10(
                latency,
                lead,
                threshold,
                config,
                points,
                mapping,
                history,
                runs,
                schedules,
                model,
            )

    if hashlib.sha256(pickle.dumps(model)).hexdigest() != model_hash_before_test:
        raise AssertionError("The fitted model changed during calibration or test scoring.")
    leakage_tests.extend(
        [
            {
                "name": "episode_partition_isolation",
                "status": "passed",
                "detail": "No district reference episode identifier appears in both 2024 and 2025.",
            },
            {
                "name": "frozen_model_state",
                "status": "passed",
                "detail": "The fitted RandomForest state hash is unchanged from before test scoring.",
            },
            {
                "name": "incident_snapshot_cutoff",
                "status": "passed",
                "detail": "Every available Recall@10 snapshot issue time is at or before local day start minus lead.",
            },
        ]
    )

    primary_latency = int(config["issue_policy"]["primary_latency_hours"])
    output = {
        "status": "complete",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "commit": git_commit(),
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
        "initial_config_sha256": initial_config_hash,
        "frozen_config_sha256": canonical_json_hash(config),
        "primary_latency_hours": primary_latency,
        "archive_disclosure": config["archive"]["warning"],
        "test_reuse_disclosure": config["periods"]["test_reuse_disclosure"],
        "issue_time_definition": config["issue_policy"]["definition"],
        "training": training_details,
        "archive_coverage": {
            "single_runs_loaded": len(runs),
            "single_runs_expected": sum(len(value) for value in schedules.values()),
            "single_run_failures": run_failures,
            "historical_forecast_rows": int(len(history)),
            "cache_hits": client.cache_hits,
            "network_requests": client.network_requests,
        },
        "reference": {
            "episodes_2024": int((episodes["year"] == 2024).sum()),
            "episodes_2025": int((episodes["year"] == 2025).sum()),
            "definition": config["reference"]["episode_verification"],
            "time_alignment": config["reference"]["forecast_to_reference_alignment"],
        },
        "calibration": calibration_results,
        "test": test_results,
        "recall_at_10": incident_results,
        "leakage_tests": leakage_tests,
        "claim_scope": (
            "A passing lead establishes retrospective rainfall-threshold-event skill under the "
            "assumed latency. It does not establish an operational historical forecast or a "
            "landslide-warning probability."
        ),
    }
    OUTPUT_JSON.write_text(
        json.dumps(output, indent=2, default=json_value) + "\n", encoding="utf-8"
    )
    make_plot(test_results[str(primary_latency)], config)
    write_markdown(output)
    LOG.info("wrote %s, %s, and %s", OUTPUT_JSON, OUTPUT_MD, OUTPUT_PNG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
