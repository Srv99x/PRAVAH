"""
tests/test_localities.py
Checks on data/processed/cell_localities.parquet (built by app/localities.py).

Run from the repo root:

    venv\\Scripts\\python.exe -m unittest tests.test_localities -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import localities as L  # noqa: E402

EXPECTED_COLUMNS = ["grid_id", "nearest_place", "nearest_place_dist_m", "place_type", "source"]


class CellLocalitiesTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = pd.read_parquet(L.OUT_PARQUET)
        cls.grid = gpd.read_parquet(L.GRID_PARQUET)
        cls.named = cls.df["nearest_place"].notna()

    def test_columns(self):
        self.assertEqual(list(self.df.columns), EXPECTED_COLUMNS)

    def test_all_904_cells_appear_exactly_once(self):
        self.assertEqual(len(self.df), 904)
        self.assertTrue(self.df["grid_id"].is_unique)
        self.assertEqual(set(self.df["grid_id"]), set(self.grid["grid_id"]))

    def test_every_row_has_a_name_or_an_explicit_null_with_a_recorded_distance(self):
        # a distance is recorded for every row, named or not
        self.assertTrue(self.df["nearest_place_dist_m"].notna().all())
        unnamed = self.df[~self.named]
        # a null name is only ever due to distance, and that distance is on record
        self.assertTrue((unnamed["nearest_place_dist_m"] > L.MAX_DISTANCE_M).all())
        # named rows carry a non-empty string
        self.assertTrue((self.df.loc[self.named, "nearest_place"].astype(str).str.strip() != "").all())

    def test_no_distance_exceeds_the_cutoff_where_a_name_is_present(self):
        self.assertLessEqual(self.df.loc[self.named, "nearest_place_dist_m"].max(), L.MAX_DISTANCE_M)

    def test_place_type_present_exactly_when_named(self):
        self.assertTrue((self.df["place_type"].notna() == self.named).all())
        self.assertTrue(self.df.loc[self.named, "place_type"].isin(L.PLACE_TYPES).all())

    def test_source_is_the_osm_odbl_credit_on_every_row(self):
        self.assertTrue((self.df["source"] == "OpenStreetMap contributors (ODbL)").all())

    def test_table_matches_a_fresh_build(self):
        """Guards against a stale parquet: rebuilding from the GeoJSON must give the same table."""
        fresh = L.build().reset_index(drop=True)

        def normalised(frame):
            # a parquet round-trip turns None into NaN in string columns; compare nulls as one thing
            return frame.reset_index(drop=True).astype(object).where(frame.notna().to_numpy(), None)

        pd.testing.assert_frame_equal(normalised(self.df), normalised(fresh), check_dtype=False)

    def test_distance_is_in_metres_not_degrees(self):
        # if degrees had been used, no distance could exceed ~0.3; the farthest cell is several km away
        self.assertGreater(self.df["nearest_place_dist_m"].max(), 1000)


class LocalityLabel(unittest.TestCase):
    def test_named(self):
        self.assertEqual(L.locality_label("Bonda Gaon"), "near Bonda Gaon")

    def test_null_shows_unnamed(self):
        for missing in (None, np.nan, pd.NA):
            self.assertEqual(L.locality_label(missing), "unnamed")

    def test_the_word_ward_is_never_produced(self):
        for value in ("Chandmari", None):
            self.assertNotIn("ward", L.locality_label(value).lower().split())


if __name__ == "__main__":
    unittest.main()
