"""Snakemake script for step 3 per-chip Planet order execution."""

import asyncio, sys
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import coms


def main_3_fetch_ps_chip_order(snakemake):
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
    logger = coms.build_logger(path_d=path_d, level=str(snakemake.params.logging_level).upper())
    coms.load_planet_secrets(override=False)
    order_manifest_fp = path_d["out_dir"] / search_d["event"] / search_d["chip_id_l"][0] / "chip_order_manifest.json"
    order_manifest_d = coms.read_manifest(manifest_fp=order_manifest_fp)
    chip_context = coms.chip_context_from_manifest(manifest_d=order_manifest_d, path_d=path_d)
    logger.info(f"order phase for {chip_context['event']}/{chip_context['chip_id']}")
    summary_df = pd.DataFrame(order_manifest_d["summary_rows"])
    if summary_df.empty:
        summary_df = pd.DataFrame([{
            "query_rank": None,
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "reference_datetime": chip_context["reference_dt"].isoformat(),
            "item_id": None,
            "acquired": None,
            "time_delta_hours": None,
            "instrument": None,
            "has_target_asset": False,
            "covers_chip": False,
            "coverage_ratio": None,
            "within_24h": False,
            "eligible": False,
            "selected": False,
            "order_submitted": False,
            "order_id": None,
            "order_state": None,
            "downloaded": False,
            "raster_fp": None,
            "manifest_fp": None,
            "note": "No scenes returned",
        }])
    else:
        selected_df = pd.DataFrame(order_manifest_d.get("selected_rows", []))
        if selected_df.empty and "selected" in summary_df.columns:
            selected_df = summary_df.loc[summary_df["selected"].fillna(False)].copy().reset_index(drop=True)
        if selected_df.empty:
            summary_df["note"] = summary_df["note"].fillna("No full-coverage SR scenes returned")
            logger.info(f"no selected scenes to order for {chip_context['event']}/{chip_context['chip_id']}")
        else:
            result_df = asyncio.run(coms.order_and_download_chip(
                chip_context=chip_context,
                selected_df=selected_df,
                planet_d=planet_d,
                logger=logger,
            ))
            for _, row in result_df.iterrows():
                for key in ["order_submitted", "order_id", "order_state", "downloaded", "raster_fp", "manifest_fp", "note"]:
                    summary_df.loc[summary_df["item_id"] == row["item_id"], key] = row[key]
    coms.write_chip_summary(summary_df=summary_df, chip_context=chip_context, logger=logger)


if "snakemake" in globals():
    main_3_fetch_ps_chip_order(snakemake)
