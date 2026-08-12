"""Snakemake script for step 1 per-chip Planet search-manifest generation."""

import asyncio, sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import coms


def main_1_fetch_ps_chip_manifest(snakemake):
    """Build the raw per-chip search manifest from one snakemake job context."""
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
    try:
        logger.info(f"manifest rule log file\n    {path_d['log_fp']}")
        logger.debug(f"path_d={path_d}")
        logger.debug(f"search_d={search_d}")
        logger.debug(f"planet_d={planet_d}")
        logger.info("loading Planet secrets")
        coms.load_planet_secrets(override=False)
        chip_context = coms.load_chip_context(path_d=path_d, search_d=search_d, logger=logger)
        logger.info(f"manifest phase for {chip_context['event']}/{chip_context['chip_id']}")
        search_result = asyncio.run(coms.search_candidates(chip_context=chip_context, search_d=search_d, planet_d=planet_d, logger=logger))
        summary_df = coms.build_candidate_table(
            chip_context=chip_context,
            item_l=search_result["item_l"],
            asset_keys_by_item=search_result["asset_keys_by_item"],
            planet_d=planet_d,
        )
        logger.info(
            f"search returned {len(summary_df):,} scenes with "
            f"{int(summary_df['covers_chip'].sum()) if not summary_df.empty else 0:,} full-coverage scenes for "
            f"{chip_context['event']}/{chip_context['chip_id']}"
        )
        if not summary_df.empty:
            logger.debug(f"returned item ids: {','.join(summary_df['item_id'].tolist())}")
        coms.write_manifest(
            manifest_d=coms.build_chip_search_manifest(chip_context=chip_context, summary_df=summary_df),
            manifest_fp=chip_context["search_manifest_fp"],
        )
        coms.write_chip_search_summary(
            chip_search_summary_df=coms.build_chip_search_summary(chip_context=chip_context, summary_df=summary_df),
            chip_context=chip_context,
            logger=logger,
        )
        logger.info(f"wrote chip search manifest to\n    {chip_context['search_manifest_fp']}")
    except Exception:
        logger.exception("manifest rule failed")
        raise


if "snakemake" in globals():
    main_1_fetch_ps_chip_manifest(snakemake)
