"""
Shared configuration constants for the Flash Flood Risk system.

Single source of truth for values that are used by more than one module
(prediction, explanation, and the offline cache builder).  Keeping them here
prevents the copies from silently drifting out of sync.
"""

import numpy as np

# ══════════════════════════════════════════════════════════════════════
# PRIORITY DECISION MATRIX  (the rule the app uses)
# ══════════════════════════════════════════════════════════════════════
#
# priority = MATRIX[susceptibility class][trigger tier]
#
# Why a table and not "trigger x weight >= threshold": with weights
# 0.20 / 0.45 / 0.70 / 0.90 and a 0.50 review threshold, a Low or Moderate
# cell can never reach the threshold, whatever the weather (0.45 x 1.0 <
# 0.50). Across all 2,922 replay days every flagged cell-day was High or
# Very High, so 718 of 904 cells could never be reviewed. See
# docs/priority_matrix.md. A table has no hidden arithmetic: each cell of it
# is a sentence a judge can read and argue with.
#
# The numbers in the table are TEAM-ASSIGNED judgements, not values from a
# published study, and are meant to be edited here and nowhere else.

# Trigger tier from the RandomForest daily-peak trigger probability.
# Lower bounds, ascending. T1 < 0.25, T2 0.25-0.50, T3 0.50-0.80, T4 >= 0.80.
TRIGGER_TIER_LOWER_BOUNDS = (0.25, 0.50, 0.80)
TRIGGER_TIER_LABELS = ("T1", "T2", "T3", "T4")
TRIGGER_TIER_WORDS = {
    "T1": "weak trigger, below 0.25",
    "T2": "moderate trigger, 0.25 to 0.50",
    "T3": "strong trigger, 0.50 to 0.80",
    "T4": "extreme trigger, 0.80 or above",
}

# Priority levels, 1 (lowest) to 4 (highest), with the map colour of each.
PRIORITY_NAMES = {1: "Routine", 2: "Watch", 3: "Elevated", 4: "Critical"}
PRIORITY_COLORS = {1: "#C8F7C5", 2: "#FFF176", 3: "#FF8C00", 4: "#C62828"}

# Susceptibility class, lowest to highest (unchanged classes).
SUSCEPTIBILITY_ORDER = ("Low", "Moderate", "High", "Very High")

# The 4x4 table.  Read a row as "a cell of this susceptibility ...",
# a column as "... when today's trigger is in this tier".
#
#                      T1  T2  T3  T4
PRIORITY_MATRIX = {
    "Low":       {"T1": 1, "T2": 1, "T3": 1, "T4": 2},  # extreme rain only earns a Watch
    "Moderate":  {"T1": 1, "T2": 1, "T3": 2, "T4": 3},  # extreme rain reaches Elevated
    "High":      {"T1": 1, "T2": 2, "T3": 3, "T4": 4},  # dry weather stays Routine
    "Very High": {"T1": 1, "T2": 3, "T3": 4, "T4": 4},  # moderate rain is enough for Elevated
}
# The whole T1 column is Routine on purpose: no trigger, no priority. If any
# cell had priority above Routine under a T1 trigger, that cell would sit on
# the list every dry day and the alert-day count would mean nothing.

# Minimum priority level that enters the review queue and gets a red outline.
# CHOSEN BY RULE, not by hand: app/priority_analysis.py picks it on the
# training folds only (maximise episode recall subject to a median queue of
# <= 30 cells per alert day). Do not edit this number without re-running it;
# the rule and result are written up in docs/priority_matrix.md.
REVIEW_MIN_PRIORITY = 3


def trigger_tier(trigger_probability: float) -> str:
    """Map a trigger probability in [0, 1] to 'T1'..'T4'."""
    tier_index = sum(trigger_probability >= bound for bound in TRIGGER_TIER_LOWER_BOUNDS)
    return TRIGGER_TIER_LABELS[tier_index]


def priority_level(susceptibility_class: str, trigger_probability: float) -> int:
    """Look up the priority level (1-4) for one cell on one day."""
    return PRIORITY_MATRIX[susceptibility_class][trigger_tier(trigger_probability)]


_MATRIX_ARRAY = np.array(
    [[PRIORITY_MATRIX[s][t] for t in TRIGGER_TIER_LABELS] for s in SUSCEPTIBILITY_ORDER]
)


def priority_levels(susceptibility_classes, trigger_probabilities) -> np.ndarray:
    """
    Vectorised priority_level() for many cells (and optionally many days).

    susceptibility_classes: sequence of class names, shape (n_cells,).
    trigger_probabilities:  shape (n_cells,) or (n_days, n_cells).
    Returns int levels of the same shape as trigger_probabilities; 0 means
    "no priority can be computed" (class missing/None for a cell with no DEM
    coverage, or a NaN trigger).
    """
    class_index = np.array([SUSCEPTIBILITY_ORDER.index(c) if c in SUSCEPTIBILITY_ORDER else -1
                            for c in susceptibility_classes])
    trigger = np.asarray(trigger_probabilities, dtype=float)
    tier_index = np.searchsorted(np.array(TRIGGER_TIER_LOWER_BOUNDS), np.nan_to_num(trigger), side="right")
    levels = _MATRIX_ARRAY[np.clip(class_index, 0, None), tier_index]
    return np.where((class_index < 0) | np.isnan(trigger), 0, levels)


# ── LEGACY: susceptibility class -> static risk multiplier ────────────
# No longer used to score cells. Kept only so app/priority_analysis.py can
# reproduce the OLD rule (trigger x multiplier >= 0.50) for the side-by-side
# comparison in docs/priority_matrix.md.
#
# TEAM-ASSIGNED weights. NOT derived from a cited study.
#
# The multiplier gates how much of the dynamic trigger probability reaches
# the final score, so it sets the risk scale on high-rainfall dates, where
# trigger probability saturates near 1.0 and final_risk ~= multiplier.
#
# Previous values were 0.1 / 0.3 / 0.7 / 1.0. They were raised because,
# combined with the old slope cutoffs, 73% of the district was multiplied
# by 0.1 and the map could not display a High or Severe cell on any date in
# the record: the single most extreme rainfall hour in eight years produced
# 770 Low / 133 Medium / 1 High / 0 Severe.
#
# Chosen so that on an extreme-rainfall date each susceptibility class lands
# in its own severity band (Low <0.25, Medium 0.25-0.50, High 0.50-0.75,
# Severe >=0.75), while a moderate monsoon date (trigger probability <= 0.09)
# still keeps every cell in Low.
SUSCEPTIBILITY_MULTIPLIERS = {
    "Low": 0.20,
    "Moderate": 0.45,
    "High": 0.70,
    "Very High": 0.90,
}
