"""Snakemake script for step 2 per-chip Planet fetch-and-match execution."""

import asyncio, sys, time
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import coms


def main_2_fetch_match(snakemake):
    """Run the per-chip fetch loop and stop at the first passing spatial match."""
    start = time.time()
    path_d = {
        "index_fp": Path(snakemake.params.index_fp),
        "out_dir": Path(snakemake.params.out_dir),
        "cache_dir": Path(snakemake.params.cache_dir),
        "tmpdir": Path(snakemake.params.tmpdir),
        "floodplanet_root": Path(snakemake.params.floodplanet_root),
    }
    path_d["log_fp"] = Path(snakemake.log[0])
    path_d["log_dir"] = path_d["log_fp"].parent

    search_d = {
        "event": snakemake.wildcards.event,
        "chip_id_l": [snakemake.wildcards.chip_id],
        "search_limit": int(snakemake.params.search_limit),
        "lower_window_hours": int(snakemake.params.lower_window_hours),
        "upper_window_hours": int(snakemake.params.upper_window_hours),
        "chip_search_max": int(snakemake.params.chip_search_max),
        "candidate_min_coverage_ratio": float(snakemake.params.candidate_min_coverage_ratio),
    }
    planet_d = {
        "item_type": "PSScene",
        "asset_key": "ortho_analytic_4b_sr",
        "product_bundle": "analytic_sr_udm2",
        "file_format": "COG",
        "poll_delay_seconds": int(snakemake.params.poll_delay_seconds),
        "poll_max_attempts": int(snakemake.params.poll_max_attempts),
    }
    compare_d = {
        "percentile_low": float(snakemake.params.compare_percentile_low),
        "percentile_high": float(snakemake.params.compare_percentile_high),
        "quadrants": int(snakemake.params.compare_quadrants),
        "min_valid_fraction": float(snakemake.params.compare_min_valid_fraction),
        "match_mean_quad_corr_min": float(snakemake.params.match_mean_quad_corr_min),
        "match_min_quad_corr_min": float(snakemake.params.match_min_quad_corr_min),
        "match_mean_quad_ssim_min": float(snakemake.params.match_mean_quad_ssim_min),
        "match_mean_mae_max": float(snakemake.params.match_mean_mae_max),
        "resampling": str(snakemake.params.compare_resampling),
        "write_compare_debug": bool(snakemake.params.write_compare_debug),
    }
    logger = coms.build_logger(path_d=path_d, level=str(snakemake.params.logging_level).upper())
    try:
        logger.info(f"fetch-match rule log file\n    {path_d['log_fp']}")
        logger.debug(f"path_d={path_d}")
        logger.debug(f"search_d={search_d}")
        logger.debug(f"planet_d={planet_d}")
        logger.debug(f"compare_d={compare_d}")
        logger.info("loading Planet secrets")
        coms.load_planet_secrets(override=False)

        logger.info(f"reading search manifest\n    {snakemake.input.search_manifest_fp}")
        search_manifest_d = coms.read_manifest(manifest_fp=snakemake.input.search_manifest_fp)
        chip_context = coms.chip_context_from_manifest(manifest_d=search_manifest_d, path_d=path_d)
        logger.info(f"fetch-match phase for {chip_context['event']}/{chip_context['chip_id']}")
        logger.info(f"resolving reference tile under\n    {path_d['floodplanet_root']}")
        reference_fp = coms.resolve_reference_fp(chip_context=chip_context, path_d=path_d)
        logger.info(f"reference tile\n    {reference_fp}")
        summary_df = pd.DataFrame(search_manifest_d.get("summary_rows", []))
        ranked_df = coms.rank_fetch_match_candidates(summary_df=summary_df.copy(), search_d=search_d)

        logger.info(
            f"ranked {len(ranked_df):,} coverage-qualified candidates from "
            f"{len(summary_df):,} returned scenes for {chip_context['event']}/{chip_context['chip_id']}"
        )
        if not summary_df.empty:
            asset_n = int(summary_df["has_target_asset"].fillna(False).sum())
            coverage_n = int((summary_df["coverage_ratio"].fillna(0.0).astype(float) >= search_d["candidate_min_coverage_ratio"]).sum())
            logger.debug(f"candidate table rows={len(summary_df):,} asset_n={asset_n:,} coverage_n={coverage_n:,}")
        if not ranked_df.empty:
            logger.debug(f"ranked candidate ids: {','.join(ranked_df['item_id'].tolist())}")

        attempt_l = []
        for _, row in ranked_df.iterrows():
            logger.info(
                f"attempt rank={int(row['match_candidate_rank'])} item={row['item_id']} "
                f"delta_h={float(row['time_delta_hours']):.3f} coverage={float(row.get('coverage_ratio') or 0.0):.3f}"
            )
            selected_df = pd.DataFrame([row.to_dict()])
            fetch_df = asyncio.run(coms.order_and_download_chip(
                chip_context=chip_context,
                selected_df=selected_df,
                planet_d=planet_d,
                logger=logger,
            ))
            fetch_row = fetch_df.iloc[0].to_dict() if not fetch_df.empty else {
                "order_submitted": False,
                "order_id": None,
                "order_state": None,
                "downloaded": False,
                "raster_fp": None,
                "manifest_fp": None,
                "cache_key": None,
                "cache_hit": False,
                "note": "No fetch result row",
            }
            logger.debug(f"fetch_row={fetch_row}")
            attempt_d = {
                "item_id": row["item_id"],
                "acquired": row["acquired"],
                "instrument": row["instrument"],
                "time_delta_hours": row["time_delta_hours"],
                "coverage_ratio": row.get("coverage_ratio"),
                "match_candidate_rank": int(row["match_candidate_rank"]),
                "order_submitted": bool(fetch_row["order_submitted"]),
                "order_id": fetch_row["order_id"],
                "order_state": fetch_row["order_state"],
                "downloaded": bool(fetch_row["downloaded"]),
                "raster_fp": fetch_row["raster_fp"],
                "manifest_fp": fetch_row["manifest_fp"],
                "cache_key": fetch_row.get("cache_key"),
                "cache_hit": bool(fetch_row.get("cache_hit", False)),
                "note": fetch_row["note"],
                "compare_metrics": None,
            }
            if fetch_row["downloaded"] and fetch_row["raster_fp"] is not None:
                logger.info(f"comparing candidate raster\n    {fetch_row['raster_fp']}")
                compare_result_d = coms.compare_candidate_to_reference(
                    candidate_fp=fetch_row["raster_fp"],
                    reference_fp=reference_fp,
                    compare_d=compare_d,
                    logger=logger,
                )
                attempt_d["compare_metrics"] = compare_result_d
                if fetch_row["manifest_fp"] is not None:
                    coms.update_item_manifest_compare(
                        manifest_fp=fetch_row["manifest_fp"],
                        compare_result_d=compare_result_d,
                        matched=compare_result_d["matched"],
                    )
                if compare_result_d["matched"]:
                    attempt_l.append(attempt_d)
                    logger.info(
                        f"matched {chip_context['event']}/{chip_context['chip_id']} with "
                        f"{row['item_id']} at rank {int(row['match_candidate_rank'])}"
                    )
                    break
            else:
                logger.warning(f"candidate fetch did not produce a raster: {fetch_row['note']}")
            attempt_l.append(attempt_d)

        runtime_seconds = time.time() - start
        logger.info(f"completed {len(attempt_l):,} candidate attempt(s) in {runtime_seconds:,.1f} seconds")
        coms.write_match_diagnostics(
            match_diagnostics_d=coms.build_fetch_match_diagnostics(
                chip_context=chip_context,
                reference_fp=reference_fp,
                summary_df=summary_df,
                ranked_df=ranked_df,
                attempt_l=attempt_l,
                compare_d=compare_d,
                runtime_seconds=runtime_seconds,
            ),
            chip_context=chip_context,
            logger=logger,
        )
        summary_out_df = coms.build_fetch_match_summary(
            chip_context=chip_context,
            reference_fp=reference_fp,
            summary_df=summary_df,
            attempt_l=attempt_l,
            runtime_seconds=runtime_seconds,
        )
        coms.write_chip_summary(
            summary_df=summary_out_df,
            chip_context=chip_context,
            logger=logger,
        )

        fail_on_no_match = bool(getattr(snakemake.params, "fail_on_no_match", False))
        matched = bool(summary_out_df.loc[0, "matched"])
        attempted_n = int(summary_out_df.loc[0, "attempted_candidates"])
        max_search_count = int(snakemake.params.chip_search_max)
        status_d = {
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "status": "matched" if matched else "no_match",
            "matched": matched,
            "attempted_candidates": attempted_n,
            "max_search_count": max_search_count,
            "summary_fp": str(chip_context["summary_fp"]),
            "match_diagnostics_fp": str(chip_context["match_diagnostics_fp"]),
            "runtime_seconds": runtime_seconds,
        }
        coms.write_match_status(status_d=status_d, chip_context=chip_context, logger=logger)
        if not matched:
            log_msg = (
                f"no match for {chip_context['event']}/{chip_context['chip_id']} "
                f"after {attempted_n} candidate attempt(s); max_search_count={max_search_count}"
            )
            logger.warning(log_msg)
        if fail_on_no_match and not matched:
            logger.error(
                f"no match for {chip_context['event']}/{chip_context['chip_id']} "
                f"after {attempted_n} candidate attempt(s); max_search_count={max_search_count}"
            )
            raise RuntimeError(
                f"No match for {chip_context['event']}/{chip_context['chip_id']} "
                f"after {attempted_n} candidate attempt(s); max_search_count={max_search_count}"
            )
    except Exception:
        logger.exception("fetch-match rule failed")
        raise


if "snakemake" in globals():
    main_2_fetch_match(snakemake)
