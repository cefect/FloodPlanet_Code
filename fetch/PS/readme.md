# fetching PSScene tiles

this replicates what was done originally by JJ using planetExplorer (website)

- for USFloods_inference notes on FloodPlanet PSScene tiles, see `/home/cefect/LS/09_REPOS/04_TOOLS/USFloods_inference/docs/PS/README.md`
- cef's info page: `c:\GD\04_LIB\Encyclopedias\REMOTE - INUNDATION.xlsx`
- windows data store: `l:\05_DATA\_jobs\2501_NSFc\zhangFloodPlanet2025\`
- WSL data store: `/home/cefect/LS/10_IO/2501_NSFc/zhangFloodPlanet2025`
- original Zenodo page: `https://zenodo.org/records/15238572`
- original paper: `https://spj.science.org/doi/10.34133/remotesensing.0575`


## notes on PS tiles shipped with FloodPLanet dataset
**copied from USFloods_inference**
Quotes from paper:
- "Because PS is commercial data and is not allowed to be released in its raw format, we include an 8-bit per-chip normalized form of PS data in the FloodPlanet dataset (experiments were conducted on raw data"


What we observe
- [`misc/FloodPlanet_PS_diagnostics.ipynb`](/workspace/misc/FloodPlanet_PS_diagnostics.ipynb) shows the FloodPlanet `PS` chips are consistently 4-band, `uint16`, `EPSG:4326`, and `1024 x 1024`
- pixel size varies by event, which is consistent with a fixed chip shape over event-specific geographic footprints rather than one global meter grid
- direct raster probes across all 105 `PS` chips show valid values in an 8-bit-like range with min `>= 1`, max `= 255`, and nodata `= 0`~

## Environment
using image from USFloods_inference: `cefect/usf:dev-v3.6`
could do something skinnier.. but this should work fine. 


## fetching PSScene tiles

`planet_sdk_orders_match_search_batch.py` runs the batch Planet fetch against [`/workspace/stac_catalog.geojson`](/workspace/stac_catalog.geojson). It searches `PSScene` within `PS_datetime +/- 24h`, keeps only scenes with `ortho_analytic_4b_sr` that fully cover the chip, writes a per-chip `summary.csv`, and writes one manifest per ordered item. Outputs are structured as `<out_dir>/<event>/<chip_id>/`.

Run the full fetch like this:

```bash
cd /workspace
python fetch/PS/planet_sdk_orders_match_search_batch.py \
    --index-fp /workspace/stac_catalog.geojson \
    --out-dir /workspace/_outputs
```

Run one chip first to prove the setup:

```bash
cd /workspace
python fetch/PS/planet_sdk_orders_match_search_batch.py \
    --index-fp /workspace/stac_catalog.geojson \
    --out-dir /workspace/_outputs \
    --chip-id COL_23_11
```
