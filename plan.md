# FloodPlanet PS Match-Fetch Revision Plan

## Goal
- revise the fetch-match workflow so it can accept near-complete scenes (`coverage_ratio >= 0.90`) and recover additional high-confidence matches without losing the current matched set
- publish a master 1:1 match index at [master_match.tsv](/workspace/fetch/PS/master_match.tsv)

## Current Review
- current `_outputs` contain `105` chip summaries and `24` current workflow matches
- two failure modes dominate the requested misses:
- `coverage filter miss`: strong near-time candidates were never fetched because they were just below full coverage
- `threshold miss`: fetched candidates are visually and radiometrically close, but fail the current hard quadrant thresholds

## Requested Chip Findings
- `US-Alabama | NAL_16_10`: current best full-coverage scene is poor (`+48.6 h`, `quad_corr=0.388`), but the raw search manifest contains a much better near-time partial candidate at `coverage_ratio=0.965` and `+0.03 h`; this is primarily a coverage-filter problem
- `US-Dakota | RRN_33_21`: same pattern; current full-coverage attempts are poor, while the raw search manifest contains a `coverage_ratio=0.992` candidate at `+0.03 h`; this is also a coverage-filter problem
- `US-Carolina | FLO_40_23`: already fetched the right near-time full-coverage scene, but current thresholds are too strict (`quad_corr=0.874`, `quad_ssim=0.759`, `mean_corr=0.983`, `mean_mae=7.05`)
- `US-Carolina | FLO_48_43`: near miss on the current quadrant rule (`quad_corr=0.930`, `quad_ssim=0.884`, `mean_corr=0.991`, `mean_mae=4.33`)
- `US-Dakota | RRN_34_29`: near miss on the current quadrant rule (`quad_corr=0.934`, `quad_ssim=0.904`, `mean_corr=0.994`, `mean_mae=3.65`)
- `US-Dakota | RRN_34_52`: near miss on the current quadrant rule (`quad_corr=0.925`, `quad_ssim=0.900`, `mean_corr=0.980`, `mean_mae=5.12`)
- `US-Kansas | USA_29_9`: best fetched candidate is rank `2`, not rank `1`; the current stopping logic finds it, but the thresholds still reject it (`quad_corr=0.934`, `quad_ssim=0.883`, `mean_corr=0.994`, `mean_mae=4.80`)
- `US-Kansas | USA_31_6`: near miss (`quad_corr=0.936`, `quad_ssim=0.888`, `mean_corr=0.994`, `mean_mae=4.52`)
- `US-Kansas | USA_33_26`: near miss (`quad_corr=0.915`, `quad_ssim=0.845`, `mean_corr=0.989`, `mean_mae=5.47`)
- `US-Dakota | RRN_101_35`: already matches under the current workflow

## Workflow Changes
- keep `_1_fetch_ps_chip_manifest` as the raw inventory step
- revise `_2_fetch_match` so the ranking pool is `has_target_asset == True` and `coverage_ratio >= 0.90`, not `covers_chip == True`
- keep ordering by nearest time first, then higher coverage, then stable acquisition/item ordering
- carry `coverage_ratio` and `covers_chip` through the whole fetch loop, diagnostics, summaries, and `master_match.tsv`
- keep match determination fully dynamic: every `_2_fetch_match` run should always re-read the ranked candidates and re-calculate the compare metrics and final best match from the current settings
- only the fetch/download step should be skipped when the raster payload already exists locally

## Current Runtime Note
- the implemented workflow already re-runs the compare step when a tile exists locally; the current skip is only at the Orders/download step
- the refactor should preserve that behavior and make it explicit in the workflow design so there is no notion of a cached final match decision

## Orders Cache Plan
- add an explicit fetch cache rooted at `_outputs/_cache` by default
- cache only the fetched raster payloads and their minimal fetch provenance; do not cache match decisions
- cache should sit between the Data API search manifest and the Orders API request
- `_2_fetch_match` should still rank and re-evaluate candidates every run, but when it needs a tile it should:
- build a cache key for the candidate fetch
- check the cache first
- if cached, symlink the cached raster into the chip output directory and continue with compare
- if not cached, submit/download once into the cache, then symlink into the chip output directory and continue with compare

## Cache Key
- use a liberal cache key derived from the candidate fetch request rather than the final chip output path
- start from fields already present in the search manifest and per-item manifest:
- `item_id`
- `chip_id`
- `event`
- `asset_key`
- `product_bundle`
- `file_format`
- clipped AOI geometry
- probe result from current `_outputs`: fetched rasters are consistently named `{item_id}.tif`, but `item_id` is not globally unique across chips
- current inventory has `238` fetched rasters but only `225` unique `item_id` values, so `item_id.tif` alone is not safe as the cache key
- duplicate `item_id` cases occur because the same source scene is clipped against different chips and therefore produces different raster payloads
- preferred implementation: hash a canonical JSON payload of those fields and use that as the cache directory/file stem
- if an equivalent stable key is already present in the manifest path or order request inputs, reuse it instead of introducing a second identifier
- practical recommendation: keep `{item_id}.tif` as the cache filename inside a per-key directory, but use a cache key built from at least `item_id + chip_id + event + asset_key` or, preferably, the full canonical clipped-order payload including AOI

## Cache Layout
- default cache root: `_outputs/_cache`
- one cache entry per candidate fetch request
- each entry should hold:
- cached raster
- a small cache manifest with the fields used to build the key
- optional link back to the originating Planet item/order metadata
- chip output directories should hold symlinks to the cached raster, not duplicate copies
- practical recommendation from the current file layout:
- `_outputs/_cache/<cache_key>/<item_id>.tif`
- `_outputs/_cache/<cache_key>/cache_manifest.json`

## Cache Migration
- add a one-time migration utility step for existing outputs
- scan `out_dir` for previously fetched `.tif` files and their sidecar item manifests
- rebuild the cache key from the existing manifest contents
- move the raster into `_outputs/_cache`
- replace the original chip-local raster with a symlink to the cache target
- leave match diagnostics and chip summaries in place; only migrate the raster storage layer

## Matching Revision
- keep the current strict rule as `match_rule=strict`; this preserves all current positives
- add a second `match_rule=relaxed_masked` path for candidates with `coverage_ratio >= 0.90`
- in both rules, compare on a common valid mask after warping to the FloodPlanet grid; pixels outside the overlap mask must be ignored consistently in all band and quadrant calculations
- keep the quadrant comparison, but stop using the current rule as a single hard gate
- use a two-tier decision:
- `strict`: current thresholds unchanged
- `relaxed_masked`: lower the quadrant thresholds and rely more on the radiometric agreement terms (`mean_corr`, `mean_mae`) for near-time candidates
- calibrate the relaxed thresholds against the current `_outputs` so all current matches stay matched and the requested near-miss chips above are included
- log which rule passed each accepted match so the relaxed set can be audited separately

## Calibration Target
- preserve all `24` current matches under the `strict` rule
- add the requested chips by two mechanisms:
- `NAL_16_10`, `RRN_33_21`: include by admitting `coverage_ratio >= 0.90` candidates and comparing on the overlap mask
- `FLO_40_23`, `FLO_48_43`, `RRN_34_29`, `RRN_34_52`, `USA_29_9`, `USA_31_6`, `USA_33_26`: include by relaxing the quadrant acceptance logic while keeping the radiometric checks strong
- treat `RRN_101_35` as an existing positive control during calibration

## Outputs
- update [master_match.tsv](/workspace/fetch/PS/master_match.tsv) as the workflow-level 1:1 match index
- keep the tracking table minimal:
- event, chip id, item id, instrument
- selection reason, current workflow matched flag, candidate rank
- coverage ratio, full/partial coverage flag
- reference capture timestamp, fetch capture timestamp, time delta
- reference path, fetch raster path, fetch manifest path, diagnostics path

## Implementation Sequence
- refactor candidate ranking in `coms.py` to admit `coverage_ratio >= 0.90`
- refactor the compare step to build and reuse one common overlap mask for all metrics
- add the dual-rule match decision and emit `match_rule`
- update per-chip summary and diagnostics schemas
- add a workflow step or post-step update that rebuilds `master_match.tsv` from the finalized outputs
- rerun on the reviewed chips first, then rerun the US subset
