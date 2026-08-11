"""Fetch and match one chip using a resolved event-level acquisition datetime."""

import asyncio, json, sys, time
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import coms


def _utc_ts(value):
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _add_selected_event_columns(summary_df, selected_dt):
    if summary_df.empty:
        return summary_df
    acquired_s = pd.to_datetime(summary_df["acquired"], utc=True, errors="coerce")
    summary_df["reference_time_delta_hours"] = summary_df["time_delta_hours"]
    summary_df["selected_event_datetime"] = selected_dt.isoformat()
    summary_df["selected_event_time_delta_hours"] = (
        (acquired_s - selected_dt).abs().dt.total_seconds() / 3600.0
    ).round(3)
    summary_df["selection_mode"] = "resolved_event_datetime"
    return summary_df


def main_3_fetch_resolved_chip(snakemake):
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

    selected_window_hours = float(snakemake.params.resolved_search_window_hours)
    search_d = {
        "event": snakemake.wildcards.event,
        "chip_id_l": [snakemake.wildcards.chip_id],
        "search_limit": int(snakemake.params.resolved_search_limit),
        "lower_window_hours": selected_window_hours,
        "upper_window_hours": selected_window_hours,
        "chip_search_max": int(snakemake.params.resolved_chip_search_max),
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
    coms.load_planet_secrets(override=False)

    resolution_d = json.loads(Path(snakemake.input.resolution_fp).read_text())
    assert resolution_d.get("resolved", False), f"Event is not resolved: {snakemake.input.resolution_fp} {resolution_d.get('reason')}"
    selected_dt = _utc_ts(resolution_d["selected_event_datetime"])

    chip_context = coms.load_chip_context(path_d=path_d, search_d=search_d, logger=logger)
    chip_context["start_dt"] = selected_dt - pd.Timedelta(hours=selected_window_hours)
    chip_context["end_dt"] = selected_dt + pd.Timedelta(hours=selected_window_hours)
    chip_context["selected_event_datetime"] = selected_dt.isoformat()
    chip_context["sample_chip"] = chip_context["chip_id"] in set(resolution_d.get("sample_chips", []))
    chip_context["selection_mode"] = "resolved_event_datetime"

    reference_fp = coms.resolve_reference_fp(chip_context=chip_context, path_d=path_d)
    logger.info(
        f"resolved-event fetch for {chip_context['event']}/{chip_context['chip_id']} "
        f"around {selected_dt.isoformat()} +/- {selected_window_hours}h"
    )
    search_result = asyncio.run(coms.search_candidates(chip_context=chip_context, search_d=search_d, planet_d=planet_d, logger=logger))
    summary_df = coms.build_candidate_table(
        chip_context=chip_context,
        item_l=search_result["item_l"],
        asset_keys_by_item=search_result["asset_keys_by_item"],
        planet_d=planet_d,
    )
    summary_df = _add_selected_event_columns(summary_df=summary_df, selected_dt=selected_dt)
    coms.write_manifest(
        manifest_d=coms.build_chip_search_manifest(chip_context=chip_context, summary_df=summary_df),
        manifest_fp=chip_context["search_manifest_fp"],
    )
    ranked_df = coms.rank_fetch_match_candidates(summary_df=summary_df.copy(), search_d=search_d)
    logger.info(
        f"ranked {len(ranked_df):,} resolved-date candidates from "
        f"{len(summary_df):,} returned scenes for {chip_context['event']}/{chip_context['chip_id']}"
    )

    attempt_l = []
    for _, row in ranked_df.iterrows():
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
        attempt_d = {
            "item_id": row["item_id"],
            "acquired": row["acquired"],
            "instrument": row["instrument"],
            "time_delta_hours": row["time_delta_hours"],
            "reference_time_delta_hours": row.get("reference_time_delta_hours"),
            "selected_event_datetime": row.get("selected_event_datetime"),
            "selected_event_time_delta_hours": row.get("selected_event_time_delta_hours"),
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
                    f"{row['item_id']} at resolved rank {int(row['match_candidate_rank'])}"
                )
                break
        attempt_l.append(attempt_d)

    runtime_seconds = time.time() - start
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
    coms.write_chip_summary(
        summary_df=coms.build_fetch_match_summary(
            chip_context=chip_context,
            reference_fp=reference_fp,
            summary_df=summary_df,
            attempt_l=attempt_l,
            runtime_seconds=runtime_seconds,
        ),
        chip_context=chip_context,
        logger=logger,
    )


if "snakemake" in globals():
    main_3_fetch_resolved_chip(snakemake)
