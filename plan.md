# Planet Batch Fetch Plan

## Goal

Implement the logic from [planet_sdk_orders_match_search_poc.ipynb](/workspace/fetch/PS/planet_sdk_orders_match_search_poc.ipynb) as a readable Python script that runs against every tile in [stac_catalog.geojson](/workspace/stac_catalog.geojson).

The first implementation should stay simple:

- single-process by default
- minimal optimization
- no plotting
- explicit progress reporting
- file logging
- easy to inspect outputs and rerun per chip

## Proposed Files

- `fetch/PS/planet_sdk_orders_match_search_batch.py`
  Purpose: main batch script with CLI entrypoint
- `fetch/coms.py`
  Purpose: small shared logger helper for console + file logging

## Script Contract

### CLI

The script should expose a runnable CLI with the standard project pattern:

- `main_planet_sdk_orders_match_search_batch(...)` near the top
- `_parse_arguments()` as the last function
- `if __name__ == "__main__":` block that calls `_parse_arguments()` and passes results into `main_planet_sdk_orders_match_search_batch(...)`

Initial CLI arguments:

- `--index-fp`
  Default: `/workspace/stac_catalog.geojson`
- `--out-dir`
  Default: `/_outputs`
- `--overwrite`
  Default: false
- `--event`
  Optional event filter for smaller reruns
- `--chip-id`
  Optional chip filter for debugging one chip
- `--search-limit`
  Default: modest value like `50`
- `--poll-delay-seconds`
  Default: `10`
- `--poll-max-attempts`
  Default: `180`

## Inputs

- [stac_catalog.geojson](/workspace/stac_catalog.geojson)
  Required fields: `Event`, `Chip_ID`, `PS_datetime`, `geometry`
- [fetch/my_secrets.py](/workspace/fetch/my_secrets.py)
  Use this to load `PL_API_KEY`

## Output Layout

Root output directory:

- `/_outputs` by default

Per ordered Planet item:

- `<out_dir>/<event>/<chip_id>/<item_id>.tif`
- `<out_dir>/<event>/<chip_id>/<item_id>.manifest.json`

Per chip summary:

- `<out_dir>/<event>/<chip_id>/summary.csv`

Run-level logs:

- `<out_dir>/logs/<run_name>.log`

## Planned Flow

### 1. Setup

- load secrets with `fetch.my_secrets.load_planet_secrets()`
- configure logger with console + file handlers
- read the flat catalog index with geopandas
- filter by `event` and/or `chip_id` if requested
- assert the required columns are present

### 2. Per-chip context

For each catalog row:

- read `Event`, `Chip_ID`, `PS_datetime`, and `geometry`
- convert `PS_datetime` to UTC
- define the search window as `timestamp +/- 24 hours`
- convert the chip polygon to GeoJSON for the Planet geometry filter
- create the chip output directory

### 3. Search

Use the same core search pattern already proven in the notebook:

- item type: `PSScene`
- geometry filter on the chip polygon
- date range filter on the `PS_datetime +/- 24h` window
- no instrument restriction for now
- search all available PS sensors
- require permission filter

### 4. Candidate filtering

For each returned scene:

- inspect its asset list
- require `ortho_analytic_4b_sr`
- require complete coverage of the chip

The coverage rule should match the notebook:

- convert item geometry to shapely
- keep only scenes where `item_geom.buffer(1e-12).covers(chip_geom)` is true

This is intentionally strict and excludes partial scenes.

### 5. Ordering and download

For each eligible item:

- order only the minimal server-side product needed
- keep output as close to server-side truth as possible
- do not normalize, rescale, or otherwise post-process the raster
- prefer a direct clipped SR asset order rather than any extra raster transforms

Initial order settings:

- item type: `PSScene`
- asset key: `ortho_analytic_4b_sr`
- product bundle: `analytic_sr_udm2`
- file format: `COG`
- no harmonization in the first batch script

### 6. Per-item manifest

Write a JSON manifest beside each raster with:

- `event`
- `chip_id`
- `item_id`
- `acquired`
- `time_delta_hours`
- `instrument`
- `asset_key`
- `order_id`
- `order_state`
- `output_raster`
- `chip_bbox_coverage_ratio`
- `covers_chip`
- `search_window_start`
- `search_window_end`

### 7. Per-chip summary table

Write one `summary.csv` per chip documenting the flow:

- `event`
- `chip_id`
- `reference_datetime`
- `item_id`
- `acquired`
- `instrument`
- `has_target_asset`
- `covers_chip`
- `coverage_ratio`
- `eligible`
- `order_submitted`
- `order_id`
- `order_state`
- `downloaded`
- `raster_fp`
- `manifest_fp`
- `note`

This table should include rows for rejected candidates too, not just downloaded items.

## Suggested Function Layout

Use numbered major steps to keep the script easy to follow:

- `main_planet_sdk_orders_match_search_batch(...)`
- `_1_read_index(...)`
- `_2_prepare_chip_context(...)`
- `_3_search_candidates(...)`
- `_4_build_candidate_table(...)`
- `_5_filter_eligible_candidates(...)`
- `_6_order_and_download_candidate(...)`
- `_7_write_manifest(...)`
- `_8_write_chip_summary(...)`
- `_parse_arguments()`

## Logging And Progress

Keep the logging readable and short:

- one run-level logger configured near the entrypoint
- `INFO` for chip start/end and final counts
- `DEBUG` for search window, filter counts, order ids, and download paths

Console progress should be simple:

- overall chip counter, e.g. `12/366`
- current `event/chip_id`
- eligible scene count
- download result summary


## Open Questions

- Should `out_dir` remain the absolute `/_outputs`, or should it be rooted under `/workspace/_outputs` for easier container writes?
   - use relative path here (e.g. `./_outputs`) 
- For chips with multiple eligible scenes, should the first implementation fetch all eligible scenes or cap the count per chip?
    - cap at 10 per chip (add a `--per-chip-limit` argument)
- Should the batch script reuse existing downloads by searching the output tree, or only skip exact target files when `overwrite=False`?
    - only skip when overwrite=False and target raster file already exists
- Is `analytic_sr_udm2` the correct first-pass bundle for this workflow, or should the script order a narrower bundle if the extra assets are not needed?
    - I think thats right... we only want 'ortho_analytic_4b_sr'
