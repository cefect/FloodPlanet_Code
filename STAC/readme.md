# FloodPlanet STAC catalog explore

using the stac catalog for some data exploration and creating a more convenient index.

The main workflow is in [STAC_index.ipynb](/workspace/STAC/STAC_index.ipynb), which scans the PlanetScope (`PS`) items in the local STAC-like catalog, builds a flat tile index, and plots the tile centroids on a single overview map.

The current derived index output is [stac_catalog.geojson](/workspace/stac_catalog.geojson).
