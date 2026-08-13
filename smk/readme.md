# PlanetScope Snakemake workflow

This workflow rebuilds PlanetScope fetch/match outputs for FloodPlanet chips listed in [`/workspace/stac_catalog.geojson`](/workspace/stac_catalog.geojson). The default target resolves each chip independently from its FloodPlanet `PS_datetime` seed: search candidate PlanetScope scenes in the configured time window, fetch up to `max_search_count` ranked candidates, and compare them to the reference chip. A chip with no passing match writes `match_status.json` as `no_match` and keeps its diagnostics. Outputs are written under `<out_dir>/<event>/<chip_id>/`; clipped Planet rasters are cached under `<out_dir>/.cache`.

Shared backend helpers live in [`scripts/coms.py`](/workspace/smk/scripts/coms.py), workflow configuration is in [`config.yaml`](/workspace/smk/config.yaml), and `snakemake --config` accepts comma-separated `event_ids` and `chip_ids` subsets.

Open a persistent terminal with `tmux`.
Prefer `tmux` on this system.
It is newer than `screen`, has better named sessions and pane controls, and is installed at `/bin/tmux`.
Use `screen` only as a fallback for very old systems or muscle memory.

```bash
cd /home/s/sbryant8/LS/09_REPOS/FloodPlanet_Code
tmux new -s floodplanet 'bash --rcfile .vscode/remote_deploy.bash -i'

# detach: Ctrl-b then d
tmux ls
tmux attach -t floodplanet
tmux kill-session -t floodplanet
```

 
Run the complete US event subset:

```bash
 
export SNAKEMAKE_PROFILE=smk/profiles/local
event_ids="US-Alabama,US-Arkansas,US-Carolina,US-Dakota,US-Kansas,US-Nebraska,US-Oklahoma,US-Texas"

snakemake -n --config event_ids="$event_ids" 

snakemake --config event_ids="$event_ids" --cores 16
```

Useful narrow checks:

 

## SETUP

Use [environment.remote.yml](/workspace/smk/environment.remote.yml) for a standalone workflow environment.
It keeps only the Snakemake, Planet SDK, and geospatial packages imported by `smk/snakefile` and `smk/scripts`.
Create the environment once per machine.

```bash
cd /path/to/FloodPlanet_Code

export TMPDIR="${TMPDIR:-/tmp/$USER}"
export CONDA_PKGS_DIRS="${CONDA_PKGS_DIRS:-$TMPDIR/conda-pkgs}"
mkdir -p "$TMPDIR" "$CONDA_PKGS_DIRS"
source "$(conda info --base)/etc/profile.d/conda.sh"
CONDA_CHANNEL_PRIORITY=strict conda env create -f smk/environment.remote.yml
conda activate floodplanet-smk
```

Workflow paths are machine-specific and belong in local `smk/config.yaml`.
Use absolute paths in that config so the workflow does not depend on the shell working directory.
Keep secrets outside the repo and expose them through `$XDG_CONFIG_HOME/USFloods_inference/secrets.toml` or `PL_API_KEY`.
Use `_outputs` for workflow outputs.

```bash
cd /path/to/FloodPlanet_Code

cat > smk/config.yaml <<'YAML'
index_fp: /absolute/path/to/FloodPlanet_Code/stac_catalog.geojson
floodplanet_root: /absolute/path/to/FloodPlanet
out_dir: /absolute/path/to/FloodPlanet_Code/_outputs
cache_dir: /absolute/path/to/FloodPlanet_Code/_outputs/.cache
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

Update the local `*.code-workspace` file when using VS Code.
Set the Python interpreter to the Conda environment.
Store PROJ, Matplotlib, Jupyter, and IPython runtime paths on the Conda environment.

```bash
conda env config vars set -p /absolute/path/to/miniforge3/envs/floodplanet-smk \
  PROJ_DATA=/absolute/path/to/miniforge3/envs/floodplanet-smk/share/proj \
  PROJ_LIB=/absolute/path/to/miniforge3/envs/floodplanet-smk/share/proj \
  MPLCONFIGDIR=/tmp/$USER/matplotlib \
  JUPYTER_CONFIG_DIR=/tmp/$USER/jupyter/config \
  JUPYTER_DATA_DIR=/tmp/$USER/jupyter/data \
  JUPYTER_RUNTIME_DIR=/tmp/$USER/jupyter/runtime \
  IPYTHONDIR=/tmp/$USER/ipython
```

Use a project rcfile for every new VS Code console.
Keep host-specific activation in the workspace file instead of shared `.vscode/settings.json`.

```json
{
  "settings": {
    "python.defaultInterpreterPath": "/absolute/path/to/miniforge3/envs/floodplanet-smk/bin/python",
    "terminal.integrated.defaultProfile.linux": "bash",
    "terminal.integrated.profiles.linux": {
      "bash": {
        "path": "/bin/bash",
        "args": [
          "--rcfile",
          "${workspaceFolder}/.vscode/remote_deploy.bash",
          "-i"
        ]
      }
    }
  }
}
```

Dry-run the configured workflow before running fetches.

```bash
cd /path/to/FloodPlanet_Code
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate floodplanet-smk

export PYTHONPATH="$PWD"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export TMPDIR="${TMPDIR:-/tmp/$USER}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$TMPDIR/cache}"
mkdir -p \
  "$TMPDIR" \
  "$XDG_CACHE_HOME" \
  "${MPLCONFIGDIR:-$TMPDIR/matplotlib}" \
  "${JUPYTER_CONFIG_DIR:-$TMPDIR/jupyter/config}" \
  "${JUPYTER_DATA_DIR:-$TMPDIR/jupyter/data}" \
  "${JUPYTER_RUNTIME_DIR:-$TMPDIR/jupyter/runtime}" \
  "${IPYTHONDIR:-$TMPDIR/ipython}" \
  _outputs

event_ids="US-Alabama,US-Arkansas,US-Carolina,US-Dakota,US-Kansas,US-Nebraska,US-Oklahoma,US-Texas"

snakemake -n --profile smk/profiles/local --config event_ids="$event_ids"
```

If migrated outputs already exist under `_outputs`, mark them current through Snakemake before proving the dry-run.

```bash
snakemake --touch --profile smk/profiles/local --config event_ids="$event_ids"
snakemake -n --profile smk/profiles/local --config event_ids="$event_ids"
```

Run the workflow only after the dry-run looks correct.

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
