"""
scripts/build_flood_ledger.py
Build data/raw/urban_flood_events.csv: one row per (verified urban-flood event, named locality).

Run from the repo root with the pinned venv:

    venv\\Scripts\\python.exe scripts/build_flood_ledger.py

Source of every event, locality, rainfall figure, grade and URL:
    docs/guwahati_flash_flood_events_2018_2025.md   (source-verified inventory, cut-off 18 Sep 2026)
Nothing is added from memory. Coordinates come ONLY from OpenStreetMap Nominatim; a locality
that does not resolve keeps blank lat/lon. Raw geocoder responses are cached in
data/raw/urban_flood_geocode_raw.json (delete it to re-query; Nominatim allows 1 request/second).

These are LOCALITY CENTROIDS (a point the geocoder returns for a named place or road), NOT surveyed
flood polygons. They are weak labels.

Not in the ledger, on purpose:
  * 14 Jun 2022 (Boragaon): its source grades it C for flood validation (B for landslide), so it is
    excluded from the flood ledger.
  * Beharbari and Geetanagar on 14 Jun 2025: the source lists them for fallen trees, not flooding.
  * Unnamed places ("other low-lying roads", "city urban areas").
"""
import csv
import json
import re
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT_CSV = ROOT / "data" / "raw" / "urban_flood_events.csv"
RAW_CACHE = ROOT / "data" / "raw" / "urban_flood_geocode_raw.json"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PRAVAH-SIH2026-flood-evidence/1.0 (hackathon research; one-off geocoding)"
# Kamrup Metropolitan bounding box (left, top, right, bottom); results outside it are not returned.
VIEWBOX = "91.50,26.30,92.10,26.00"
RETRIEVED = date.today().isoformat()

TOI_5AUG_RAIN = "https://timesofindia.indiatimes.com/city/guwahati/guwahati-records-years-highest-single-day-rainfall-life-disrupted/articleshow/112331073.cms"
TOI_5AUG_IMPACT = "https://timesofindia.indiatimes.com/city/guwahati/hour-long-rainfall-causes-chaos-in-city-floods-streets/amp_articleshow/112301324.cms"
PTI_5AUG = "https://theprint.in/india/heavy-rain-leads-to-waterlogging-disruption-of-normal-life-in-assams-guwahati/2209665/"

SKIP = "__SKIP__"   # too generic to geocode without guessing; row kept with blank coordinates

# (locality name as in the source, geocoder query text if different, type)
# type: "neighbourhood" or "road_corridor" (a road/path: the geocoded point is arbitrary along it)
EVENTS = [
    {
        "date": "2024-08-05", "grade": "B",
        "rainfall_mm": 78.4,
        "notes": "78.4 mm is the RMC city total for 08:30 5 Aug to 08:30 6 Aug (24 h), not the ~1-hour downpour that caused the flooding.",
        "urls": [TOI_5AUG_RAIN, TOI_5AUG_IMPACT, PTI_5AUG],
        "localities": [
            ("Chandmari/Maniram Dewan Road", "Chandmari", "neighbourhood"), ("Maligaon", None, "neighbourhood"),
            ("Hatigaon", None, "neighbourhood"), ("Beltola", None, "neighbourhood"),
            ("Lachit Nagar", None, "neighbourhood"), ("Rukminigaon", None, "neighbourhood"),
            ("GS Road", None, "road_corridor"), ("Zoo Road", None, "road_corridor"),
            ("RG Baruah Road", None, "road_corridor"), ("Nabin Nagar", None, "neighbourhood"),
            ("Anil Nagar", None, "neighbourhood"), ("Ganeshguri", None, "neighbourhood"),
            ("Hedayetpur", None, "neighbourhood"), ("Dispur MLA quarters", "MLA Quarters Dispur", "neighbourhood"),
            ("Tarun Nagar", None, "neighbourhood"), ("Jyotikuchi", None, "neighbourhood"),
            ("Ghoramara", None, "neighbourhood"), ("VIP Road", None, "road_corridor"),
            ("Rajgarh Road", None, "road_corridor"), ("Jorabat", None, "neighbourhood"),
            ("Chatribari", None, "neighbourhood"),
        ],
    },
    {
        "date": "2025-05-10", "grade": "B-",
        "rainfall_mm": None,
        "notes": "Source cites 41 mm at Amingaon, a nearby non-city-centre station; not used as the event rainfall.",
        "urls": ["https://timesofindia.indiatimes.com/city/guwahati/2-hr-rain-triggers-power-cuts-waterlogging-in-guwahati/articleshow/121064212.cms"],
        "localities": [(n, None, "neighbourhood") for n in
                       ("Chandmari", "Nabin Nagar", "Rukminigaon", "Geetanagar", "Ganeshguri", "Anil Nagar", "Ambikagiri Nagar")],
    },
    {
        "date": "2025-05-20", "grade": "C+",
        "rainfall_mm": None,
        "notes": "Rainfall not stated in the retained event report. Overnight rain into the morning of 20 May.",
        "urls": ["https://economictimes.indiatimes.com/news/new-updates/assam-schools-shut-traffic-chaos-and-more-flooded-guwahati-grinds-to-a-halt-imd-warns-of-more-rain-ahead/articleshow/121293114.cms",
                 "https://timesofindia.indiatimes.com/city/guwahati/heavy-rain-floods-guwahati-disrupts-life/articleshow/121298862.cms"],
        "localities": [("Jatia", None, "neighbourhood"), ("Rukminigaon", None, "neighbourhood"),
                       ("Beltola", None, "neighbourhood"), ("Anil Nagar", None, "neighbourhood"),
                       ("Nabin Nagar", None, "neighbourhood"), ("Hatigaon", None, "neighbourhood"),
                       ("Chandmari", None, "neighbourhood"), ("Zoo Road", None, "road_corridor"),
                       ("Ganeshguri", None, "neighbourhood")],
    },
    {
        "date": "2025-05-29", "grade": "B",
        "rainfall_mm": None,
        "notes": "Rainfall not stated for 29 May in the retained report; the next day's 24 h figure must not be assigned to this window.",
        "urls": ["https://timesofindia.indiatimes.com/city/guwahati/intense-rain-run-off-from-meghalaya-hills-flood-city/articleshow/121503278.cms"],
        "localities": [("Rukminigaon", None, "neighbourhood"), ("Anil Nagar", None, "neighbourhood"),
                       ("Nabin Nagar", None, "neighbourhood"), ("Beltola", None, "neighbourhood"),
                       ("Hatigaon", None, "neighbourhood"), ("Wireless", SKIP, "neighbourhood"),
                       ("Bishnujyoti Path", None, "road_corridor")],
    },
    {
        "date": "2025-05-30", "grade": "C+",
        "rainfall_mm": None,
        "notes": "Two unreconciled figures: 37 mm in 24 h (initial report) and an IMD all-time May single-day 111 mm (separate report); windows/stations differ, so rainfall_mm is left null rather than pick one.",
        "urls": ["https://timesofindia.indiatimes.com/city/guwahati/half-of-guwahati-under-water-after-37mm-rain-in-24-hrs-9-flights-diverted-1-dies-in-mizoram/articleshow/121521964.cms",
                 "https://timesofindia.indiatimes.com/city/guwahati/guwahati-tezpur-record-highest-single-day-may-rain/articleshow/121541046.cms",
                 "https://timesofindia.indiatimes.com/city/guwahati/power-supply-restored-in-several-areas-of-guwahati/articleshow/121541049.cms"],
        "localities": [(n, None, "neighbourhood") for n in
                       ("Nabin Nagar", "Anil Nagar", "Rajgarh", "Rukminigaon", "Beltola", "Sijubari")],
    },
    {
        "date": "2025-06-14", "grade": "B-",
        "rainfall_mm": 11.3,
        "notes": "11.3 mm is the IMD city figure reported by the newspaper. Fallen trees at Beharbari and Geetanagar are not flood localities and are not included.",
        "urls": ["https://timesofindia.indiatimes.com/city/guwahati/flooded-streets-poor-drainage-frustrate-locals/articleshow/121853970.cms"],
        "localities": [("Beltola Wireless", None, "neighbourhood"), ("Dakhingaon", None, "neighbourhood"),
                       ("Hatigaon", None, "neighbourhood"), ("Rukminigaon", None, "neighbourhood")],
    },
    {
        "date": "2025-07-22", "grade": "B",
        "rainfall_mm": 7.2,
        "notes": "7.2 mm is the IMD figure reported by the newspaper. Early morning event.",
        "urls": ["https://timesofindia.indiatimes.com/city/guwahati/early-morning-rain-floods-city-again-leaves-several-areas-submerged/articleshow/122843140.cms"],
        "localities": [(n, None, "neighbourhood") for n in ("Anil Nagar", "Hatigaon", "Beltola", "Rukminigaon")],
    },
]


def geocode(query: str, cache: dict) -> list:
    """Raw Nominatim candidates for a plain place name, bounded to Kamrup Metropolitan. Cached; 1 request/second.

    The plain name is used, not "name, Guwahati, Assam": OSM files many localities under a different
    parent, and the longer text also makes the geocoder return unrelated shops and offices.
    """
    if query in cache:
        return cache[query]
    params = {"q": query, "format": "jsonv2", "limit": 10, "viewbox": VIEWBOX,
              "bounded": 1, "countrycodes": "in"}
    resp = requests.get(NOMINATIM, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    cache[query] = resp.json()
    time.sleep(1.1)
    return cache[query]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


NEIGHBOURHOOD_CATEGORIES = {"place", "boundary", "landuse"}


def accepted_candidates(results: list, name: str, loc_type: str) -> tuple[str, list]:
    """
    Keep only candidates whose OSM name equals the wanted name (ignoring case, spaces, punctuation).
    This rejects the shops, banks and offices a fuzzy search returns for a locality name.

    Returns (quality, candidates):
      "place_centroid"      neighbourhood: an OSM place/boundary named exactly that
      "named_feature_point" neighbourhood: no such place, but another OSM feature (e.g. a bus stop)
                            named exactly that; a point inside the locality, not its centroid
      "road_segment_point"  road: an OSM highway named exactly that; the point is arbitrary along the road
      "unresolved"          nothing qualifies
    """
    exact = [r for r in results if _norm(r.get("name")) == _norm(name)]
    if loc_type == "road_corridor":
        roads = [r for r in exact if r.get("category") == "highway"]
        return ("road_segment_point", roads) if roads else ("unresolved", [])
    places = [r for r in exact if r.get("category") in NEIGHBOURHOOD_CATEGORIES]
    if places:
        return "place_centroid", places
    return ("named_feature_point", exact) if exact else ("unresolved", [])


def main() -> None:
    cache = json.loads(RAW_CACHE.read_text(encoding="utf-8")) if RAW_CACHE.exists() else {}
    rows, unresolved = [], []
    for ev in EVENTS:
        for name, query_text, loc_type in ev["localities"]:
            query = query_text or name
            if query == SKIP:
                cands, quality, total = [], "unresolved", 0
                why = "not geocoded: name too generic to resolve without guessing"
            else:
                results = geocode(query, cache)
                quality, cands = accepted_candidates(results, query, loc_type)
                total = len(results)
                why = f"no returned candidate (of {total}) has an OSM name equal to '{query}'"
            if cands:
                top = cands[0]
                lat, lon = round(float(top["lat"]), 6), round(float(top["lon"]), 6)
                src = (f"OpenStreetMap Nominatim, plain-name query '{query}' bounded to Kamrup Metropolitan; accepted OSM "
                       f"{top['osm_type']} {top['osm_id']} ({top.get('category')}={top.get('type')}), exact name match, quality "
                       f"{quality}, {len(cands)} accepted of {total} returned, first taken; retrieved {RETRIEVED}; "
                       "a point of an OSM feature, not a surveyed flood polygon")
            else:
                lat = lon = None
                src = f"OpenStreetMap Nominatim: {why}; coordinates left blank, not guessed; retrieved {RETRIEVED}"
                unresolved.append((ev["date"], name))
            rows.append({
                "date": ev["date"], "locality": name, "lat": lat, "lon": lon,
                "rainfall_mm": ev["rainfall_mm"], "evidence_grade": ev["grade"],
                "source_url": " | ".join(ev["urls"]), "geocode_source": src,
                "locality_type": loc_type, "geocode_quality": quality,
                "notes": ev["notes"] + (f" Geocoded as '{query}'." if query not in (name, SKIP) else ""),
            })
    RAW_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")

    header = [
        "# Verified urban-flood event ledger, Guwahati (Kamrup Metropolitan). One row per event-locality pair.",
        "# Source: docs/guwahati_flash_flood_events_2018_2025.md (source-verified inventory). Built by scripts/build_flood_ledger.py.",
        "# lat/lon are LOCALITY CENTROIDS from OpenStreetMap Nominatim, NOT surveyed flood polygons. They are weak labels.",
        "# geocode_quality: place_centroid | named_feature_point (e.g. a bus stop named for the locality) | road_segment_point | unresolved (blank lat/lon).",
        "# A name is accepted only if an OSM feature has EXACTLY that name; unresolved names are left blank, never guessed.",
        "# rainfall_mm is blank where the source reports none or reports unreconciled figures; no nearby or modelled value is substituted.",
        "# Excluded: 14 Jun 2022 (Boragaon) - source grades it C for flood validation (B for landslide).",
        "# Read with pandas.read_csv(path, comment='#').",
    ]
    fields = ["date", "locality", "lat", "lon", "rainfall_mm", "evidence_grade", "source_url",
              "geocode_source", "locality_type", "geocode_quality", "notes"]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        for line in header:
            fh.write(line + "\n")
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} rows written to {OUT_CSV.relative_to(ROOT)}; unresolved localities: {unresolved or 'none'}")


if __name__ == "__main__":
    main()
