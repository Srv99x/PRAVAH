"""
app/streamlit_app.py
────────────────────
Flash Flood Early Warning System — Interactive Demo
SIH 2026 | Kamrup Metropolitan District, Assam

Run with:
    streamlit run app/streamlit_app.py

What is real and what is simulated
─────────────────────────────────
REAL       Priority levels, for any date from 2018-01-01 to 2025-12-31.
             priority[cell, date] = PRIORITY_MATRIX[susceptibility class of cell]
                                    [trigger tier of trigger_prob[weather_point(cell), date]]
           The 4x4 matrix and the tier boundaries live in app/config.py.
           Trigger probabilities come from the RandomForest model via the
           precomputed cache (build_trigger_cache.py, 55,518 rows); the
           susceptibility layer is terrain-derived and floored by ASDMA's
           officially identified vulnerable locations
           (build_susceptibility.py). The cache builder asserts this
           matches app/predict.py to 1e-6 on all 904 cells.
SIMULATED  IoT sensor telemetry (app/mqtt_sim.py). No public
           village-level sensor network exists; the panel demonstrates
           the ingestion interface a real feed would drop into. This is
           disclosed in the UI and must stay disclosed.

Regenerating after a model or matrix change
───────────────────────────────────────────────
    python build_susceptibility.py     # susceptibility classes
    python build_trigger_cache.py      # trigger probability cache

Files owned by this module:
    app/streamlit_app.py  ← this file
    app/mqtt_sim.py       ← simulated sensor telemetry

Do NOT import or modify:
    app/grid_utils.py, app/weather_fetch.py, app/terrain_utils.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.explain import explain_cell
from app.localities import ATTRIBUTION, load_cell_localities, locality_label

import json
import time
import os
import sys

from datetime import date

import numpy as np
import pandas as pd
import geopandas as gpd
import folium
import streamlit as st
from streamlit_folium import st_folium

# mqtt_sim lives in the same app/ directory
sys.path.insert(0, os.path.dirname(__file__))
from mqtt_sim import get_sensor_readings  # noqa: E402

from app.config import (  # noqa: E402
    PRIORITY_COLORS,
    PRIORITY_MATRIX,
    PRIORITY_NAMES,
    REVIEW_MIN_PRIORITY,
    SUSCEPTIBILITY_ORDER,
    TRIGGER_TIER_LABELS,
    TRIGGER_TIER_WORDS,
    priority_levels,
    trigger_tier,
)

# ══════════════════════════════════════════════════════════════════════════════
# PAGE CONFIG
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="PRAVAH — Kamrup Metro Priority Queue",
    page_icon="🌊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
GUWAHATI_LAT = 26.05
GUWAHATI_LON = 91.70
DEFAULT_ZOOM = 11
GRID_PARQUET = os.path.join(
    os.path.dirname(__file__), "..", "data", "processed",
    "kamrup_metro_grid_1km.parquet"
)

NO_DATA_COLOR = "#808080"

# Opens on the deadliest documented event in the record.
DEFAULT_DATE = date(2025, 5, 30)

# One-click jumps. Each is a documented event or a reference condition —
# see data/raw/verified_incidents.csv.
DEMO_DATES = [
    ("30 May 2025 — Bonda landslide (5 deaths)", date(2025, 5, 30)),
    ("17 Jun 2023 — Dhirenpara landslide (1 death)", date(2023, 6, 17)),
    ("26 May 2020 — wettest hour on record", date(2020, 5, 26)),
    ("15 Jan 2020 — dry season (contrast)", date(2020, 1, 15)),
]


# ══════════════════════════════════════════════════════════════════════════════
# RISK LOADING
# ══════════════════════════════════════════════════════════════════════════════

# Minimum fraction of grid cells that must resolve to a risk score.
# A synthetic placeholder grid was once committed whose grid_id scheme
# ("KM_0001") did not match the rest of the pipeline ("KM_R000_C028"), so the
# join silently matched 0 of 904 rows and fillna() painted the whole map 0.0
# risk for days. Fail loudly instead of rendering a lie.
MIN_RISK_MATCH_FRACTION = 0.50

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "processed")
TRIGGER_CACHE = os.path.join(DATA_DIR, "trigger_prob_daily.parquet")
MAPPING_PARQUET = os.path.join(DATA_DIR, "grid_weather_mapping.parquet")
SUSCEPTIBILITY_PARQUET = os.path.join(DATA_DIR, "susceptibility_features.parquet")
MISSING_CELLS_JSON = os.path.join(DATA_DIR, "missing_terrain_cells.json")

# ══════════════════════════════════════════════════════════════════════════════
# STATIC LAYER — grid geometry + per-cell susceptibility.  Loaded once.
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def load_static() -> gpd.GeoDataFrame:
    """
    Grid geometry joined to weather point and susceptibility class.

    Everything here is date-independent, so it is loaded exactly once per
    session; only the trigger probability changes when the date changes.
    """
    gdf = gpd.read_parquet(GRID_PARQUET)
    mapping = pd.read_parquet(MAPPING_PARQUET, columns=["grid_id", "weather_point_id"])
    sus = pd.read_parquet(SUSCEPTIBILITY_PARQUET)

    n_before = len(gdf)
    gdf = gdf.merge(mapping, on="grid_id", how="left")
    gdf = gdf.merge(sus, on="grid_id", how="left")
    # Nearest OpenStreetMap place (app/localities.py): a reading aid, null beyond 3 km -> "unnamed".
    localities = load_cell_localities()[["grid_id", "nearest_place"]]
    gdf = gdf.merge(localities, on="grid_id", how="left", validate="one_to_one")
    gdf["locality"] = gdf["nearest_place"].map(locality_label)

    matched = int(gdf["weather_point_id"].notna().sum())
    if matched / n_before < MIN_RISK_MATCH_FRACTION:
        msg = "\n".join([
            f"GRID/WEATHER JOIN FAILED — only {matched} of {n_before} cells "
            f"({matched/n_before:.1%}) matched a weather point.",
            "",
            f"  grid_id in grid file:    {list(gdf['grid_id'].head(2))}",
            f"  grid_id in mapping file: {list(mapping['grid_id'].head(2))}",
            "",
            "The files use different grid_id schemes. Regenerate the grid with "
            "app.grid_utils.generate_grid() against "
            "data/raw/boundaries/kamrup_metropolitan.geojson.",
        ])
        st.error(msg)
        raise RuntimeError(msg)

    with open(MISSING_CELLS_JSON, "r") as fh:
        gdf["no_dem"] = gdf["grid_id"].isin(json.load(fh))

    return gdf


@st.cache_data(show_spinner=False)
def load_trigger_cache() -> pd.DataFrame:
    """
    Daily-peak trigger probability per (weather_point_id, date).

    55,518 rows covering 2018-2025 — small enough to hold in memory, so any
    date in range renders as a lookup and a multiply with no model call.
    Built by build_trigger_cache.py.
    """
    df = pd.read_parquet(TRIGGER_CACHE)
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(show_spinner=False)
def available_date_range() -> tuple:
    """First and last date present in the trigger cache."""
    cache = load_trigger_cache()
    return cache["date"].min().date(), cache["date"].max().date()


# ══════════════════════════════════════════════════════════════════════════════
# RISK FOR A GIVEN DATE
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(show_spinner=False)
def trigger_for_date(target_date) -> np.ndarray:
    """
    Per-cell trigger probability for one date (NaN if the date is not cached).

    The trigger is the model's daily-peak probability for the cell's weather
    point.  Cached per date, so revisiting a date is instant.
    """
    static = load_static()
    cache = load_trigger_cache()

    day = cache[cache["date"] == pd.Timestamp(target_date)]
    if day.empty:
        return np.full(len(static), np.nan)
    return static["weather_point_id"].map(
        day.set_index("weather_point_id")["trigger_prob"]
    ).to_numpy()


def build_display_gdf(target_date) -> gpd.GeoDataFrame:
    """
    Static grid + this date's trigger, trigger tier and matrix priority.

    priority_level is 1-4 from app.config.PRIORITY_MATRIX, or 0 for a cell
    that has no priority: no DEM coverage (no susceptibility class) or no
    cached trigger for the date.  Those render grey "No Data", never as low
    priority.
    """
    gdf = load_static().copy()
    gdf["trigger_prob"] = trigger_for_date(target_date)
    gdf["priority_level"] = priority_levels(
        gdf["gsi_susceptibility_class"], gdf["trigger_prob"]
    )
    gdf.loc[gdf["no_dem"], "priority_level"] = 0

    gdf["priority_name"] = gdf["priority_level"].map(PRIORITY_NAMES).fillna("No Data")
    gdf["trigger_tier"] = [
        "" if pd.isna(p) else trigger_tier(p) for p in gdf["trigger_prob"]
    ]
    gdf["susceptibility"] = gdf["gsi_susceptibility_class"].fillna("No DEM data")
    gdf.loc[gdf["no_dem"], "trigger_prob"] = np.nan

    # Sort key for the review queue: level, then trigger, then susceptibility.
    gdf["_susc_rank"] = gdf["gsi_susceptibility_class"].map(
        {name: i for i, name in enumerate(SUSCEPTIBILITY_ORDER)}
    ).fillna(-1)
    return gdf


def rank_cells(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Order cells for the review queue (ties within a level are broken by trigger, then susceptibility)."""
    return gdf.sort_values(
        ["priority_level", "trigger_prob", "_susc_rank", "grid_id"],
        ascending=[False, False, False, True],
    )


def get_priority_color(level: int) -> str:
    """Map a priority level (0 = no data) to its display hex colour."""
    return PRIORITY_COLORS.get(int(level), NO_DATA_COLOR)


# ══════════════════════════════════════════════════════════════════════════════
# MAP BUILDER
# Not cached with @st.cache_data — folium.Map contains lambdas that
# can't be pickled by Streamlit's cache. The heavy work (grid I/O + risk
# scoring) is cached in load_static(); map build is ~1 s from memory.
# ══════════════════════════════════════════════════════════════════════════════

def build_folium_map(gdf: gpd.GeoDataFrame, review_min: int) -> folium.Map:
    """
    Build the Folium choropleth map of all 904 grid cells.

    Uses a single GeoJson FeatureCollection — style properties are stored
    inside each feature's properties dict so the style_function lambda
    captures nothing from the outer scope (no closure = no pickle issue).
    """
    m = folium.Map(
        location=[GUWAHATI_LAT, GUWAHATI_LON],
        zoom_start=DEFAULT_ZOOM,
        tiles=None,
        control_scale=True,
    )

    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        attr=(
            'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, FAO, NOAA, USGS'
        ),
        name="Esri Dark Gray",
        max_zoom=16,
    ).add_to(m)

    # Build a FeatureCollection with style props embedded in each feature
    features = []
    for _, row in gdf.iterrows():
        level         = int(row["priority_level"])
        fill_color    = get_priority_color(level)
        fill_opacity  = 0.50
        in_review     = level >= review_min
        border_color  = "#FF4444" if in_review else "#555555"
        border_weight = 2.0 if in_review else 0.3

        features.append({
            "type": "Feature",
            "geometry": row["geometry"].__geo_interface__,
            "properties": {
                "grid_id":         row["grid_id"],
                "locality":        str(row["locality"]),
                "priority":        (f"{level} {row['priority_name']}" if level else "No Data"),
                "susceptibility":  str(row["susceptibility"]),
                "trigger":         ("n/a" if pd.isna(row["trigger_prob"])
                                    else f"{row['trigger_tier']} ({row['trigger_prob']:.2f})"),
                "review":          ("Yes" if in_review else "No") if level else "n/a",
                "lat":          float(row["centroid_lat"]),
                "lon":          float(row["centroid_lon"]),
                # Pre-computed style — lambda below reads these, captures nothing
                "fillColor":    fill_color,
                "fillOpacity":  fill_opacity,
                "color":        border_color,
                "weight":       border_weight,
            },
        })

    folium.GeoJson(
        {"type": "FeatureCollection", "features": features},
        style_function=lambda feat: {
            "fillColor":   feat["properties"]["fillColor"],
            "color":       feat["properties"]["color"],
            "weight":      feat["properties"]["weight"],
            "fillOpacity": feat["properties"]["fillOpacity"],
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["locality", "grid_id", "priority", "susceptibility", "trigger", "review"],
            aliases=["Locality", "Grid ID", "Priority", "Susceptibility", "Trigger tier", "In review queue"],
            localize=True,
            sticky=True,
        ),
        popup=folium.GeoJsonPopup(
            fields=["locality", "grid_id", "priority", "susceptibility", "trigger", "review", "lat", "lon"],
            aliases=["Locality", "Grid ID", "Priority", "Susceptibility", "Trigger tier", "In review queue", "Lat", "Lon"],
            max_width=240,
        ),
        name="Risk Grid",
    ).add_to(m)

    # Legend overlay
    swatches = "".join(
        f'<span style="background:{PRIORITY_COLORS[level]};padding:2px 8px;">&nbsp;</span>'
        f'&nbsp;{level} &middot; {PRIORITY_NAMES[level]}<br>'
        for level in sorted(PRIORITY_NAMES)
    )
    review_name = PRIORITY_NAMES[review_min]
    m.get_root().html.add_child(folium.Element(f"""
    <div style="position:fixed;bottom:30px;left:30px;z-index:9999;
                background:rgba(20,20,30,0.88);padding:12px 16px;
                border-radius:8px;border:1px solid #334;
                font-family:monospace;font-size:12px;color:#eee;">
      <b style="color:#4fc3f7;">PRIORITY LEVEL</b><br>
      {swatches}
      <span style="background:{NO_DATA_COLOR};padding:2px 8px;">&nbsp;</span>&nbsp;No DEM data<br>
      <span style="border:2px solid #FF4444;padding:0 6px;">&nbsp;</span>&nbsp;Review queue: {review_min} ({review_name}) and above<br>
      <hr style="border-color:#445;margin:6px 0;">
      <span style="color:#888;font-size:10px;">Susceptibility class x trigger tier, from a lookup table<br>
      (RandomForest trigger; slope + listed hazard sites)<br>
      Locality = nearest OpenStreetMap place. {ATTRIBUTION}</span>
    </div>
    """))

    return m


def matrix_html(susceptibility_class: str, tier: str, review_min: int) -> str:
    """
    The 4x4 decision matrix as a small HTML table, this cell's entry outlined.

    Drawn from app.config.PRIORITY_MATRIX so it can never disagree with the
    lookup that produced the priority.  Not st.dataframe on purpose: the
    screenshot script anchors on the first stDataFrame (the review queue).
    """
    head = "".join(f"<th style='padding:3px 10px;'>{t}</th>" for t in TRIGGER_TIER_LABELS)
    rows = []
    for susc in reversed(SUSCEPTIBILITY_ORDER):
        cells = []
        for t in TRIGGER_TIER_LABELS:
            level = PRIORITY_MATRIX[susc][t]
            is_this = susc == susceptibility_class and t == tier
            outline = "border:3px solid #ffffff;font-weight:700;" if is_this else "border:1px solid #334;"
            cells.append(
                f"<td style='padding:3px 10px;text-align:center;{outline}"
                f"background:{PRIORITY_COLORS[level]};color:#111;'>"
                f"{level} {PRIORITY_NAMES[level]}</td>"
            )
        rows.append(
            f"<tr><td style='padding:3px 10px;color:#90caf9;'>{susc}</td>{''.join(cells)}</tr>"
        )
    return (
        "<div style='margin-top:10px;font-size:13px;'>"
        "<table style='border-collapse:collapse;'>"
        f"<tr><th></th>{head}</tr>{''.join(rows)}</table>"
        f"<div style='color:#78909c;margin-top:4px;'>Rows: susceptibility. Columns: trigger tier. "
        f"White outline: this cell. Review queue starts at level {review_min}.</div>"
        "</div>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOM CSS
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
[data-testid="stAppViewContainer"] {
    background: linear-gradient(135deg, #0d1117 0%, #0f1a2e 60%, #0d1117 100%);
}
[data-testid="stSidebar"] {
    background: rgba(10, 20, 40, 0.95) !important;
    border-right: 1px solid #1e3a5f;
}
[data-testid="stMetric"] {
    background: rgba(14, 40, 80, 0.6);
    border: 1px solid #1e4a80;
    border-radius: 10px;
    padding: 12px 16px;
}
[data-testid="stMetricValue"] { color: #4fc3f7 !important; font-weight: 700; }
[data-testid="stMetricLabel"] { color: #90caf9 !important; }
h2 { color: #4fc3f7 !important; border-bottom: 1px solid #1e4a80; padding-bottom: 6px; }
h3 { color: #81d4fa !important; }
h4 { color: #b3e5fc !important; }
.sidebar-caption { color: #607d8b; font-size: 11px; font-style: italic; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## 🌊 PRAVAH")
    st.markdown("**Kamrup Metro - district priority queue (prototype)**")
    st.markdown("*SIH 2026 — Flash Flood Prediction System*")
    st.divider()

    st.markdown("### 📅 Replay date (historical ERA5-Land)")
    _min_date, _max_date = available_date_range()

    # A jump button on the previous run leaves a pending date here.  It must
    # be applied BEFORE the date_input widget is created — Streamlit forbids
    # writing a widget's session_state key after the widget exists.
    if "forecast_date" not in st.session_state:
        st.session_state.forecast_date = DEFAULT_DATE
    if "_pending_date" in st.session_state:
        st.session_state.forecast_date = st.session_state.pop("_pending_date")

    forecast_date = st.date_input(
        "Select date",
        min_value=_min_date,
        max_value=_max_date,
        key="forecast_date",
        label_visibility="collapsed",
    )
    st.caption(
        f"Any date from {_min_date:%d %b %Y} to {_max_date:%d %b %Y}. "
        "The map recomputes on change."
    )

    st.markdown("**Jump to a documented event**")
    for _label, _d in DEMO_DATES:
        if st.button(_label, use_container_width=True, key=f"demo_{_d.isoformat()}"):
            st.session_state["_pending_date"] = _d
            st.rerun()

    st.divider()

    st.markdown("### ⚠️ Review queue")
    # Level 1 (Routine) would put every cell in the queue, so it is not offered.
    review_options = [level for level in sorted(PRIORITY_NAMES) if level > 1]
    review_min = st.select_slider(
        "Minimum priority for the review queue",
        options=review_options,
        value=REVIEW_MIN_PRIORITY,
        format_func=lambda level: f"{level} · {PRIORITY_NAMES[level]}",
        help="Cells at this priority level or above enter the ranked review queue.",
    )
    st.caption(
        f"Cells at priority **{review_min} ({PRIORITY_NAMES[review_min]})** or above "
        "enter the review queue. The default was chosen by a stated rule on training "
        "folds only — see `docs/priority_matrix.md`. It is a team-set review level, "
        "not a calibrated probability."
    )

    st.divider()

    st.markdown("### 📡 IoT Telemetry")
    iot_active = st.toggle(
        "Ingest Live IoT Telemetry", value=False,
        help="Show live sensor readings from 5 simulated field stations.",
    )
    st.markdown(
        '<p class="sidebar-caption">⚠ Sensor data is simulated for this demonstration. '
        "A live MQTT ingestion pipeline will connect to real sensors when deployed.</p>",
        unsafe_allow_html=True,
    )

    st.divider()
    st.markdown("### 📖 How to read this map")
    st.markdown(
        '<p class="sidebar-caption">'
        "<b>Priority = a lookup of susceptibility class × trigger tier.</b><br><br>"
        "<b>Trigger</b> is real model output — a RandomForest trained on "
        "ERA5-Land soil moisture and antecedent precipitation, labelled from "
        "rainfall intensity–duration thresholds plus 7 verified "
        "landslide/flood incidents (2022–2025).<br><br>"
        "<b>Susceptibility is terrain-derived, floored by listed hazard "
        "sites.</b> That means a cell can show high "
        "risk because it appears on the listed hazard sites, "
        "not only because the terrain model inferred it. We do this "
        "because 1 km mean slope alone ranks these cells wrongly: every "
        "documented landslide site in the district sits in a cell that is "
        "flatter than the district average, since the failure happens on a "
        "local hill cut a 1 km average erases.<br><br>"
        "The lookup table (susceptibility class × trigger tier → priority "
        "level) is a team-assigned judgement, not a value from a published "
        "study. It is written out in full in <code>app/config.py</code>.<br><br>"
        "<b>Locality</b> is the nearest OpenStreetMap place name to the cell "
        "centre (within 3 km, otherwise \"unnamed\"). It is a reading aid only, "
        "not an administrative area. " + ATTRIBUTION + "."
        "</p>",
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANE — HEADER
# ══════════════════════════════════════════════════════════════════════════════

col_title, col_date = st.columns([3, 1])
with col_title:
    st.markdown("## 🗺️ Storm-triggered priority map (flash flood & landslide) — Kamrup Metro")
with col_date:
    st.markdown(f"<br><b>Replay:</b> {forecast_date}", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ══════════════════════════════════════════════════════════════════════════════

with st.spinner("Loading grid and computing priority …"):
    gdf = build_display_gdf(forecast_date)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANE — MAP
# ══════════════════════════════════════════════════════════════════════════════

with st.spinner("Rendering priority map …"):
    flood_map = build_folium_map(gdf, review_min)

st_folium(
    flood_map,
    use_container_width=True,
    height=520,
    returned_objects=[],
)
st.caption(
    "Locality names are the nearest OpenStreetMap place to each cell centre "
    "(within 3 km, otherwise \"unnamed\"), a reading aid only and not an "
    "official area. Place names " + ATTRIBUTION + " (ODbL)."
)

# ══════════════════════════════════════════════════════════════════════════════
# WHY IS THIS CELL AT RISK? — EXPLAINABILITY PANEL
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("### 🔎 Why is this cell flagged?")
st.caption("Contextual explanation (values vs monthly medians)")

# The current Folium map intentionally does not return click events
# (returned_objects=[]), so use the task-approved Grid ID selector fallback.
valid_gdf = gdf[gdf["priority_level"] > 0].copy()

if valid_gdf.empty:
    st.info("No valid terrain cells are available for explanation on this date.")
else:
    # Automatically select the top-ranked valid cell.
    highest_risk_grid = rank_cells(valid_gdf).iloc[0]["grid_id"]

    grid_options = gdf["grid_id"].tolist()

    selected_grid = st.selectbox(
        "Explain a grid cell",
        options=grid_options,
        index=(
            grid_options.index(highest_risk_grid)
            if highest_risk_grid in grid_options
            else 0
        ),
        help=(
            "Select any grid cell to see why its priority is "
            "Routine, Watch, Elevated or Critical."
        ),
    )

    _locality = gdf.loc[gdf["grid_id"] == selected_grid, "locality"].iloc[0]
    st.markdown(f"#### {selected_grid} - {_locality}")

    try:
        explanation = explain_cell(
            selected_grid,
            forecast_date.strftime("%Y-%m-%d"),
            review_min_priority=review_min,
        )
    except Exception as exc:
        st.error(
            f"Explanation failed for {selected_grid} on {forecast_date:%Y-%m-%d}: {exc}"
        )
        st.stop()

    if explanation.get("error_state") == "no_dem":
        st.warning(explanation["summary"])
    else:
        st.info(f"**{explanation['summary']}**")

        metric1, metric2, metric3, metric4 = st.columns(4)

        metric1.metric(
            "Priority level",
            f"{explanation['priority_level']} · {explanation['priority_name']}",
        )

        metric2.metric(
            "Trigger tier",
            f"{explanation['trigger_tier']} · {explanation['trigger_prob']:.2f}",
        )

        metric3.metric(
            "Susceptibility",
            explanation["susceptibility_class"],
        )

        metric4.metric(
            "In review queue",
            "Yes" if explanation["in_review_queue"] else "No",
        )

        st.markdown(
            '<div style="color:#90caf9; font-size:18px; font-weight:700; margin-top:18px;">'
            '1. Static susceptibility — why this place is vulnerable'
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            f'<div style="color:#e0e0e0; font-size:16px; line-height:1.7; margin-top:8px;">'
            f'{explanation["susceptibility_text"]}'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div style="color:#90caf9; font-size:18px; font-weight:700; margin-top:20px;">'
            '2. Dynamic trigger — what is happening now'
            '</div>',
            unsafe_allow_html=True,
        )

        trigger_html = explanation["trigger_text"].replace("\n", "<br>")

        st.markdown(
            f'<div style="color:#e0e0e0; font-size:16px; line-height:1.7; margin-top:8px;">'
            f'{trigger_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div style="color:#90caf9; font-size:18px; font-weight:700; margin-top:20px;">'
            '3. Decision — how the two combine'
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            f'<div style="color:#e0e0e0; font-size:16px; line-height:1.7; margin-top:8px;">'
            f'{explanation["decision_text"]}'
            f'</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            matrix_html(
                explanation["susceptibility_class"],
                explanation["trigger_tier"],
                review_min,
            ),
            unsafe_allow_html=True,
        )

_tier_text = "; ".join(f"{t} {TRIGGER_TIER_WORDS[t]}" for t in TRIGGER_TIER_LABELS)
_level_text = " · ".join(
    f"{emoji} {level} {PRIORITY_NAMES[level]}"
    for emoji, level in zip(["🟢", "🟡", "🟠", "🔴"], sorted(PRIORITY_NAMES))
)
st.caption(
    "**Priority = decision matrix: susceptibility class × trigger tier.** "
    f"Trigger tiers come from the RandomForest daily-peak trigger: {_tier_text}. "
    "Susceptibility is terrain-derived (SRTM slope) and "
    "**floored at cells containing a listed hazard site or a dated "
    "incident**. The hazard list is 47 locations compiled from news "
    "reports and official sources; per-row source in "
    "`data/raw/asdma_vulnerable_locations.csv`. 34 of 904 cells are raised "
    "to at least *High* on that basis. The matrix entries are team-assigned "
    "judgements, not values from a published study; the review level was "
    "chosen by a stated rule, see `docs/priority_matrix.md`.  "
    f"Levels: {_level_text} · "
    "⬜ No Data (93 cells outside DEM coverage)."
)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANE — METRICS + WARNING TABLE
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.markdown("## ⚠️ Ranked review queue")
st.caption("Historical replay - no forecast lead time. Ranking only.")

review_name = PRIORITY_NAMES[review_min]
in_queue = gdf[gdf["priority_level"] >= review_min]
top10 = rank_cells(in_queue).head(10)

m1, m2, m3 = st.columns(3)
m1.metric("🛰️ Total Cells Monitored", f"{len(gdf):,}")
m2.metric(
    "🚨 Cells in Review Queue",
    f"{len(in_queue):,}",
    delta=f"priority ≥ {review_min} ({review_name})",
    delta_color="inverse",
)
_top_level = int(gdf["priority_level"].max())
m3.metric(
    "🔺 Highest Priority Today",
    f"{_top_level} · {PRIORITY_NAMES[_top_level]}" if _top_level else "n/a",
)

st.markdown(
    f"**Top 10 highest-priority cells** at priority {review_min} ({review_name}) "
    "or above. Ties within a level are ordered by trigger, then susceptibility."
)

if top10.empty:
    st.info(
        f"✅ No cells are at priority {review_min} ({review_name}) or above on this "
        "date. Lower the minimum priority in the sidebar to see lower levels."
    )
else:
    display_df = top10[[
        "locality", "grid_id", "centroid_lat", "centroid_lon",
        "priority_level", "trigger_tier", "trigger_prob", "susceptibility",
    ]].copy()
    display_df["Priority"] = [
        f"{lv} · {PRIORITY_NAMES[lv]}" for lv in display_df["priority_level"]
    ]
    display_df["Trigger"] = [
        f"{t} · {p:.2f}" for t, p in zip(display_df["trigger_tier"], display_df["trigger_prob"])
    ]
    display_df = display_df.rename(columns={
        "locality": "Locality", "grid_id": "Grid ID", "centroid_lat": "Lat", "centroid_lon": "Lon",
        "susceptibility": "Susceptibility",
    })[["Locality", "Grid ID", "Lat", "Lon", "Priority", "Trigger", "Susceptibility"]]
    display_df["Lat"] = display_df["Lat"].round(4)
    display_df["Lon"] = display_df["Lon"].round(4)

    def _priority_color(val):
        name = str(val).split("· ")[-1]
        return {
            "Routine":  "color: #a5d6a7",
            "Watch":    "color: #fff176",
            "Elevated": "color: #ffb74d",
            "Critical": "color: #ef5350",
        }.get(name, "")

    styled = (
        display_df.style
        # Styler.applymap was removed in pandas 3.0 — .map is the replacement
        .map(_priority_color, subset=["Priority"])
        .set_properties(**{"background-color": "rgba(10,20,40,0.6)", "color": "#e0e0e0"})
        .set_table_styles([
            {"selector": "th", "props": [("background-color", "#1a3a6b"), ("color", "#90caf9")]},
        ])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)
    if len(in_queue) > len(top10):
        st.caption(f"Showing the top {len(top10)} of {len(in_queue):,} cells in the review queue.")


# ══════════════════════════════════════════════════════════════════════════════
# IOT TELEMETRY PANEL
# ══════════════════════════════════════════════════════════════════════════════

if iot_active:
    st.markdown("---")
    st.markdown(
        "## 📡 Live Sensor Telemetry\n"
        '<span style="color:#4db6ac;font-size:12px;letter-spacing:0.08em;">'
        "⚠ SIMULATED SENSOR TELEMETRY — DEMONSTRATION OF LIVE INGESTION CAPABILITY"
        "</span>",
        unsafe_allow_html=True,
    )
    st.caption(
        "5 virtual IoT sensors placed in hilly northern grid cells. "
        "Readings drift realistically every 3 seconds. "
        "In production this panel consumes a live MQTT feed via paho-mqtt."
    )

    sensor_placeholder = st.empty()
    refresh_label      = st.empty()

    for i in range(600):   # safety cap ~30 min
        readings = get_sensor_readings()

        with sensor_placeholder.container():
            cols = st.columns(len(readings))
            for col, r in zip(cols, readings):
                with col:
                    st.markdown(
                        f"**{r['sensor_id']}**  \n<small>{r['label']}</small>",
                        unsafe_allow_html=True,
                    )
                    st.metric("🌧 Rainfall",     f"{r['rainfall_mm_hr']} mm/hr")
                    st.metric("🌱 Soil Moisture", f"{r['soil_moisture_pct']}%")
                    st.metric("💧 Water Level",   f"{r['water_level_m']} m")
                    st.caption(r["timestamp"])

        refresh_label.caption(
            f"Last refresh: {time.strftime('%H:%M:%S')}  |  Update #{i+1}"
        )
        time.sleep(3)
