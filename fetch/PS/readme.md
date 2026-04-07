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
- run notebook work, including [`planet_sdk_orders_match_search_manifest_summary.ipynb`](/workspace/fetch/PS/planet_sdk_orders_match_search_manifest_summary.ipynb), from conda env `analysis`


## fetching PSScene tiles

The snakemake workflow runs the batch Planet fetch against [`/workspace/stac_catalog.geojson`](/workspace/stac_catalog.geojson). The step-1 manifest phase inventories all `ortho_analytic_4b_sr` scenes intersecting each chip in the configured search window, and step 2 loops through the ranked full-coverage candidates until one fetched tile matches the original FloodPlanet PS chip. Step 3 concatenates the chip summaries. Shared backend helpers live in [`smk/scripts/coms.py`](/workspace/smk/scripts/coms.py). Outputs are structured as `<out_dir>/<event>/<chip_id>/`.

The preferred entrypoint is the snakemake workflow in [`smk/snakefile`](/workspace/smk/snakefile). The config is in [`smk/config.yaml`](/workspace/smk/config.yaml), and `snakemake --config` accepts `chip_ids` and `event_ids` as comma-separated subsets.

```bash
cd /workspace
export SNAKEMAKE_PROFILE=/workspace/smk/profiles/local
event_ids="US-Alabama,US-Arkansas,US-Carolina,US-Dakota,US-Kansas,US-Nebraska,US-Oklahoma,US-Texas"

# one-chip proof
snakemake --config chip_ids=COL_23_11

# US event subset
snakemake --config event_ids="$event_ids"
```

Invoke each step-specific `_all` target like this:

```bash
cd /workspace
export SNAKEMAKE_PROFILE=/workspace/smk/profiles/local
chip_ids="COL_23_11"

# step 1: search manifests
snakemake --config chip_ids="$chip_ids" _1_fetch_ps_chip_manifest_all

# step 2: fetch and match
snakemake --config chip_ids="$chip_ids" _2_fetch_match_all

# step 3: combined summary
snakemake --config chip_ids="$chip_ids" _3_concat_chip_summaries_all
```
