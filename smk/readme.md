# PlanetScope Snakemake workflow

This workflow rebuilds PlanetScope fetch/match outputs for FloodPlanet chips listed in [`/workspace/stac_catalog.geojson`](/workspace/stac_catalog.geojson). The default target resolves each chip independently from its FloodPlanet `PS_datetime` seed: search candidate PlanetScope scenes in the configured time window, fetch up to `max_search_count` ranked candidates, and compare them to the reference chip. A chip rule fails if none of its attempted candidates match; use `--keep-going` to let independent chips continue. Outputs are written under `<out_dir>/<event>/<chip_id>/`; clipped Planet rasters are cached under `<out_dir>/.cache`.

Shared backend helpers live in [`scripts/coms.py`](/workspace/smk/scripts/coms.py), workflow configuration is in [`config.yaml`](/workspace/smk/config.yaml), and `snakemake --config` accepts comma-separated `event_ids` and `chip_ids` subsets.

Run the complete US event subset:

```bash
cd /workspace
export SNAKEMAKE_PROFILE=/workspace/smk/profiles/local
event_ids="US-Alabama,US-Arkansas,US-Carolina,US-Dakota,US-Kansas,US-Nebraska,US-Oklahoma,US-Texas"

snakemake -n --keep-going --config event_ids="$event_ids" --cores 6

snakemake --keep-going --config event_ids="$event_ids" --cores 6
```

Useful narrow checks:

```bash
# dry-run the complete US subset
snakemake -n --keep-going --config event_ids="$event_ids" --cores 6

# run one event only
snakemake --keep-going --config event_ids="US-Nebraska" --cores 4
```
