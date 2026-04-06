"""Snakemake script for the per-chip Planet order execution phase."""

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fetch.PS import planet_sdk_orders_match_search_batch as batch_mod


def main_fetch_ps_chip_order(snakemake):
    """Run the per-chip order phase from one snakemake job context."""
    path_d = {
        "index_fp": Path(snakemake.params.index_fp),
        "out_dir": Path(snakemake.params.out_dir),
        "tmpdir": Path(snakemake.params.tmpdir),
    }
    path_d["log_fp"] = Path(snakemake.log[0])
    path_d["log_dir"] = path_d["log_fp"].parent

    search_d = {
        "event": snakemake.wildcards.event,
        "chip_id_l": [snakemake.wildcards.chip_id],
        "search_limit": int(snakemake.params.search_limit),
        "lower_window_hours": int(snakemake.params.lower_window_hours),
        "upper_window_hours": int(snakemake.params.upper_window_hours),
    }
    planet_d = {
        "item_type": "PSScene",
        "asset_key": "ortho_analytic_4b_sr",
        "product_bundle": "analytic_sr_udm2",
        "file_format": "COG",
        "poll_delay_seconds": int(snakemake.params.poll_delay_seconds),
        "poll_max_attempts": int(snakemake.params.poll_max_attempts),
    }
    run_d = {
        "overwrite": bool(snakemake.params.overwrite),
        "logging_level": str(snakemake.params.logging_level).upper(),
    }
    batch_mod.main_fetch_ps_chip_order(path_d=path_d, search_d=search_d, planet_d=planet_d, run_d=run_d)


if "snakemake" in globals():
    main_fetch_ps_chip_order(snakemake)
