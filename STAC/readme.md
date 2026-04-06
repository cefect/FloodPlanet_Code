# FloodPlanet STAC catalog explore

## File Index

- [STAC_index.ipynb](/workspace/STAC/STAC_index.ipynb): main workflow notebook. Builds or reuses the flat PlanetScope tile index, cleans and joins the CSDA datetimes, and plots tile centroids on a single overview map.
- [stac_catalog.geojson](/workspace/stac_catalog.geojson): flat derived tile index used by the notebook. Includes `Event`, chip metadata, hrefs, geometry, and joined `PS_datetime`.
- [csda_dates.tsv](/workspace/STAC/csda_dates.tsv): raw CSDA date table used as the source for the event datetime join.
- [csda_dates_clean.json](/workspace/STAC/csda_dates_clean.json): cleaned event-to-datetime lookup derived from the TSV and keyed to the catalog `Event` values.

This folder uses the STAC-like catalog for some light exploration and for creating a more convenient flat index for downstream lookups.
