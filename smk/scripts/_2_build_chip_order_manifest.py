"""Snakemake script for step 2 explicit per-chip order-manifest generation."""

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import coms


def main_2_build_chip_order_manifest(snakemake):
    """Build the explicit per-chip order manifest from one snakemake job context."""
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
    logger = coms.build_logger(path_d=path_d, level=str(snakemake.params.logging_level).upper())
    search_manifest_fp = path_d["out_dir"] / search_d["event"] / search_d["chip_id_l"][0] / "chip_search_manifest.json"
    search_manifest_d = coms.read_manifest(manifest_fp=search_manifest_fp)
    order_manifest_d = coms.build_chip_order_manifest(search_manifest_d=search_manifest_d)
    order_manifest_fp = path_d["out_dir"] / search_d["event"] / search_d["chip_id_l"][0] / "chip_order_manifest.json"
    coms.write_manifest(manifest_d=order_manifest_d, manifest_fp=order_manifest_fp)
    logger.info(f"wrote chip order manifest to\n    {order_manifest_fp}")


if "snakemake" in globals():
    main_2_build_chip_order_manifest(snakemake)
