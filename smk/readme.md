# PlanetScope Snakemake workflow

This workflow rebuilds PlanetScope fetch/match outputs for FloodPlanet chips listed in [`/workspace/stac_catalog.geojson`](/workspace/stac_catalog.geojson). The default target resolves each chip independently from its FloodPlanet `PS_datetime` seed: search candidate PlanetScope scenes in the configured time window, fetch up to `max_search_count` ranked candidates, and compare them to the reference chip. A chip rule fails if none of its attempted candidates match; use `--keep-going` to let independent chips continue. Outputs are written under `<out_dir>/<event>/<chip_id>/`; clipped Planet rasters are cached under `<out_dir>/.cache`.

Shared backend helpers live in [`scripts/coms.py`](/workspace/smk/scripts/coms.py), workflow configuration is in [`config.yaml`](/workspace/smk/config.yaml), and `snakemake --config` accepts comma-separated `event_ids` and `chip_ids` subsets.

Run the complete US event subset:

```bash
cd /workspace
export SNAKEMAKE_PROFILE=smk/profiles/local
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

## SETUP

The local devcontainer uses `cefect/usf:dev-v4.0` from [`.devcontainer/docker-compose.yml`](/workspace/.devcontainer/docker-compose.yml).
That image is larger than needed for this workflow.
Use [environment.remote.yml](/workspace/smk/environment.remote.yml) on `sscc-linstat-vm`.
It keeps only the Snakemake, Planet SDK, and geospatial packages imported by `smk/snakefile` and `smk/scripts`.
The workflow paths are machine-specific and belong in local `smk/config.yaml`.
Use absolute paths in that config so the workflow does not depend on the shell working directory.

```bash
ssh sscc-linstat-vm
cd /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code

export TMPDIR="${TMPDIR:-$HOME/tmp}"
export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-$HOME/tmp/conda-pkgs}"
mkdir -p "$TMPDIR" "$CONDA_PKGS_DIRS"
source "$(conda info --base)/etc/profile.d/conda.sh"
CONDA_CHANNEL_PRIORITY=strict conda env create -f smk/environment.remote.yml
conda activate floodplanet-smk
```

Build the remote machine-specific config:

```bash
cd /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code

cat > smk/config.yaml <<'YAML'
index_fp: /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code/stac_catalog.geojson
floodplanet_root: /home/s/sbryant8/LS/10_IO/2501_NSFc/zhangFloodPlanet2025
out_dir: /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code/_outputs
cache_dir: /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code/_outputs/.cache
search_limit: 50
lower_window_hours: 72
upper_window_hours: 72
max_search_count: 10
chip_search_max: 10
event_sample_size: 5
event_sample_seed: 2501
event_resolution_window_hours: 1
resolved_search_limit: 20
resolved_search_window_hours: 1
resolved_chip_search_max: 3
candidate_min_coverage_ratio: 0.90
poll_delay_seconds: 10
poll_max_attempts: 180
compare_percentile_low: 2
compare_percentile_high: 98
compare_quadrants: 2
compare_min_valid_fraction: 0.8
match_mean_quad_corr_min: 0.95
match_min_quad_corr_min: 0.90
match_mean_quad_ssim_min: 0.85
match_mean_mae_max: 15
compare_resampling: bilinear
write_compare_debug: false
logging_level: WARNING
chip_ids: []
event_ids: []
YAML
```

The Planet API key must be available through `$XDG_CONFIG_HOME/USFloods_inference/secrets.toml` or the `PL_API_KEY` environment variable.

```bash
cd /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate floodplanet-smk

export PYTHONPATH="$PWD"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export TMPDIR="${TMPDIR:-$HOME/tmp}"
mkdir -p "$TMPDIR" _outputs

event_ids="US-Alabama,US-Arkansas,US-Carolina,US-Dakota,US-Kansas,US-Nebraska,US-Oklahoma,US-Texas"

snakemake -n --profile smk/profiles/local --config event_ids="$event_ids"
```

Run after the dry-run looks correct.

```bash
snakemake --profile smk/profiles/local --config event_ids="$event_ids"
```

## migrate

Migrate only the `_outputs` directory to the remote project root.
This creates or updates `$REMOTE_REPO_DIR/_outputs`.
Use direct `rsync`; tarballing is not worth it for the current outputs because the tree is mostly already-compressed GeoTIFFs.
The local `_outputs` tree is about 2.3 GB, with about 2.27 GB in `.tif` files.

```bash
cd /workspace
LOCAL_OUTPUT_DIR="_outputs"
REMOTE_HOST="sscc-linstat-vm"
REMOTE_REPO_DIR="/home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code"
RSYNC_FLAGS=(
  # Default quick-check updates files when size or mtime differs.
  --archive          # Recurse and preserve common file metadata.
  --verbose          # Print transferred paths.
  --compress         # Compress file data during transfer.
  --no-perms         # Keep destination-side default permissions.
  --no-owner         # Keep destination-side owner.
  --no-group         # Keep destination-side group.
  --partial          # Keep partial files for resumable transfers.
  --human-readable   # Show readable file sizes.
  --info=progress2   # Show aggregate progress.
  --outbuf=L         # Flush progress and stats promptly.
  --stats            # Summarize transfer counts and sizes.
)

ssh "$REMOTE_HOST" "mkdir -p '$REMOTE_REPO_DIR'"
rsync "${RSYNC_FLAGS[@]}" --dry-run "$LOCAL_OUTPUT_DIR" "$REMOTE_HOST:$REMOTE_REPO_DIR/"


# Run the transfer after the dry-run looks correct
rsync "${RSYNC_FLAGS[@]}" "$LOCAL_OUTPUT_DIR" "$REMOTE_HOST:$REMOTE_REPO_DIR/"
```
