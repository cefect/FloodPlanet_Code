# fetching PSScene tiles

this replicates what was done originally by JJ using planetExplorer (website)

- for USFloods_inference notes on FloodPlanet PSScene tiles, see `/home/cefect/LS/09_REPOS/04_TOOLS/USFloods_inference/docs/PS/README.md`
- cef's info page: `c:\GD\04_LIB\Encyclopedias\REMOTE - INUNDATION.xlsx`
- windows data store: `l:\05_DATA\_jobs\2501_NSFc\zhangFloodPlanet2025\`
- WSL data store: `/home/cefect/LS/10_IO/2501_NSFc/zhangFloodPlanet2025`
- original Zenodo page: `https://zenodo.org/records/15238572`
- original paper: `https://spj.science.org/doi/10.34133/remotesensing.0575`
- UA path from rohit: `[minnow]:/media/mule/Projects/NASA/CSDAP/Data/Raw_Planet_imgs_for_FloodPlanet`


## rsync UA data store
gave up on this... nasty VPN wsl tunnel issue. 
WinSCP too slow
using globus

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

The Snakemake workflow documentation and current run commands now live in [`/workspace/smk/readme.md`](/workspace/smk/readme.md).


## results notebook
```bash
conda run -n analysis jupyter nbconvert --to notebook --execute --inplace /workspace/fetch/PS/smk_match_summary.ipynb
```
