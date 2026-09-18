# Boundary sources

## gmc_wards.geojson
- Source: Guwahati Municipal Corporation, "Final Delimitation of 60 Wards Map".
- Official map URL: https://gmc.assam.gov.in/sites/default/files/swf_utility_folder/departments/gmc_webcomindia_org_oid_5/latest/ward_map_gmc_modified.pdf
- Official delimitation / ward descriptions URL: https://gmc.assam.gov.in/sites/default/files/swf_utility_folder/departments/gmc_webcomindia_org_oid_5/this_comm/final-draft-ward-gmc-updated1_0.pdf
- Official GMC landing page: https://gmc.assam.gov.in/latest/final-delimitation-of-60-wards-map-of-guwahati-municipal-corporation
- Downloaded: 2026-09-18 (IST)
- Coverage: 60 wards (Ward 1-60).
- Method: official GMC map rasterized at 150 dpi. The map's printed geographic graticule (longitude roughly 91°37'30"E to 91°51'00"E; latitude roughly 26°4'30"N to 26°13'00"N) was used to map raster pixels to EPSG:4326. Ward regions were extracted from the map's printed ward fill colours, cleaned morphologically at raster scale, and converted to one polygon per ward.
- Georeferencing note: the final GeoJSON was not produced from the earlier QGIS GCP output because that output was found to have invalid Guwahati-scale coordinates. The final coordinate transform uses the official map graticule instead.
- Accuracy: approximate, roughly +/- 100 m. For prototype prioritisation only. NOT an official or survey-grade boundary.
- Ward labels: compact area-based labels derived from the GMC final delimitation "Area Included" lists. Long lists are shortened with `+ N more` to keep `ward_name` concise; the underlying source is the official ward-description PDF.
- Properties: `ward_no` (1-60), `ward_name`.

## osm_places.geojson
- Intended source: OpenStreetMap via Overpass API.
- Licence: ODbL. Display/use of OSM data should credit "© OpenStreetMap contributors".
- Requested Overpass query:
```text
[out:json][timeout:120];
node["place"~"^(suburb|neighbourhood|quarter|village|hamlet|locality)$"](25.98,91.60,26.28,92.20);
out body;
```
- Query bounding box: south 25.98, west 91.60, north 26.28, east 92.20.
- Current file note: direct live Overpass execution was not available in this environment. To keep the deliverable grounded in OSM, this file contains a verified OSM-derived subset of locality nodes whose OSM node IDs and coordinates were checked on public OSM-linked pages. It is therefore NOT an exhaustive replacement for the requested Overpass export.
- Suggested replacement method when live Overpass is available: run the exact query above and replace this file with the exported GeoJSON.
- The file currently contains 22 verified OSM-derived locality points.
- OSM point source records include `name`, `place`, `osm_id`, and an OSM-derived source note.

## Second Overpass query
```text
[out:json][timeout:120];
relation["boundary"="administrative"]["admin_level"~"^(9|10)$"](25.98,91.60,26.28,92.20);
out geom;
```
- Result count: NOT VERIFIED in this environment because the live Overpass API could not be executed here.

## Reproducibility
- All ward geometry in `gmc_wards.geojson` was derived from the attached official GMC map and the attached official ward-description PDF.
- No edits were made to `app/` or any other repository directory for this deliverable.
