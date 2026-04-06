"""Batch Planet PSScene search and fetch over the FloodPlanet STAC index."""

import argparse, asyncio, json, os, shutil, sys, time
from pathlib import Path

import geopandas as gpd
import pandas as pd
from planet import Session, data_filter, order_request
from shapely.geometry import mapping, shape


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fetch.coms import get_logger
from fetch.my_secrets import load_planet_secrets


def main_planet_sdk_orders_match_search_batch(path_d, search_d, planet_d, run_d):
    """Run the Planet batch fetch workflow over the flat STAC tile index.

    Parameters
    ----------
    path_d : dict
        Filepath configuration including the input index, output root, and log
        directory.
    search_d : dict
        Search and filtering controls such as event/chip filters and limits.
    planet_d : dict
        Planet SDK order and polling configuration.
    run_d : dict
        Runtime controls such as overwrite behavior.
    """
    assert path_d["index_fp"].exists(), path_d["index_fp"]
    assert search_d["search_limit"] > 0, search_d
    assert search_d["half_window_hours"] > 0, search_d
    assert planet_d["asset_key"] == "ortho_analytic_4b_sr", planet_d

    # Load credentials before any Planet SDK calls.
    load_planet_secrets(override=False)

    # Create run directories and logger.
    path_d["out_dir"].mkdir(parents=True, exist_ok=True)
    path_d["log_dir"].mkdir(parents=True, exist_ok=True)
    logger = get_logger(name="planet_batch_fetch", log_fp=path_d["log_fp"], level="INFO")

    # Read and filter the flat chip index.
    index_gdf = _1_read_index(path_d=path_d, search_d=search_d, logger=logger)
    logger.info(f"processing {len(index_gdf):,} chips from\n    {path_d['index_fp']}")

    # Process one chip at a time for readability and simpler recovery.
    for i, (_, chip_s) in enumerate(index_gdf.iterrows(), start=1):
        chip_context = _2_prepare_chip_context(chip_s=chip_s, path_d=path_d, search_d=search_d)
        logger.info(f"[{i:,}/{len(index_gdf):,}] {chip_context['event']}/{chip_context['chip_id']}")
        try:
            summary_df = asyncio.run(
                _process_one_chip(
                    chip_context=chip_context,
                    path_d=path_d,
                    search_d=search_d,
                    planet_d=planet_d,
                    run_d=run_d,
                    logger=logger,
                )
            )
        except Exception as err:
            logger.exception(f"chip failed: {chip_context['event']}/{chip_context['chip_id']}: {err}")
            summary_df = pd.DataFrame([{
                "event": chip_context["event"],
                "chip_id": chip_context["chip_id"],
                "reference_datetime": chip_context["reference_dt"].isoformat(),
                "item_id": None,
                "acquired": None,
                "instrument": None,
                "has_target_asset": False,
                "covers_chip": False,
                "coverage_ratio": None,
                "eligible": False,
                "order_submitted": False,
                "order_id": None,
                "order_state": None,
                "downloaded": False,
                "raster_fp": None,
                "manifest_fp": None,
                "note": f"chip_failed: {err}",
            }])
        _8_write_chip_summary(summary_df=summary_df, chip_context=chip_context, logger=logger)

    logger.info(f"run complete. log written to\n    {path_d['log_fp']}")


def _1_read_index(path_d, search_d, logger=None):
    """Read the flat STAC index and apply any requested filters."""
    log = logger or get_logger(__name__)
    index_gdf = gpd.read_file(path_d["index_fp"])
    required_col_l = ["Event", "Chip_ID", "PS_datetime", "geometry"]
    missing_col_l = [col for col in required_col_l if col not in index_gdf.columns]
    assert not missing_col_l, missing_col_l

    if search_d["event"] is not None:
        index_gdf = index_gdf.loc[index_gdf["Event"] == search_d["event"]].copy()
    if search_d["chip_id"] is not None:
        index_gdf = index_gdf.loc[index_gdf["Chip_ID"] == search_d["chip_id"]].copy()

    assert not index_gdf.empty, "No chips remain after filtering"
    log.debug(f"filtered index has {len(index_gdf):,} rows")
    return index_gdf.reset_index(drop=True)


def _2_prepare_chip_context(chip_s, path_d, search_d):
    """Prepare one chip context dictionary for the batch flow."""
    reference_dt = pd.Timestamp(chip_s["PS_datetime"])
    reference_dt = reference_dt.tz_localize("UTC") if reference_dt.tzinfo is None else reference_dt.tz_convert("UTC")
    chip_dir = path_d["out_dir"] / chip_s["Event"] / chip_s["Chip_ID"]
    chip_dir.mkdir(parents=True, exist_ok=True)

    chip_context = {
        "event": chip_s["Event"],
        "chip_id": chip_s["Chip_ID"],
        "geometry": chip_s.geometry,
        "geometry_geojson": mapping(chip_s.geometry),
        "reference_dt": reference_dt,
        "start_dt": reference_dt - pd.Timedelta(hours=search_d["half_window_hours"]),
        "end_dt": reference_dt + pd.Timedelta(hours=search_d["half_window_hours"]),
        "chip_dir": chip_dir,
        "summary_fp": chip_dir / "summary.csv",
    }
    assert chip_context["geometry"] is not None, chip_context
    return chip_context


async def _process_one_chip(chip_context, path_d, search_d, planet_d, run_d, logger=None):
    """Search, filter, fetch, and summarize one chip."""
    log = logger or get_logger(__name__)

    # Search once and materialize all candidate metadata up front.
    async with Session() as sess:
        search_result = await _3_search_candidates(
            chip_context=chip_context,
            search_d=search_d,
            planet_d=planet_d,
            sess=sess,
        )
        summary_df = _4_build_candidate_table(
            chip_context=chip_context,
            item_l=search_result["item_l"],
            asset_keys_by_item=search_result["asset_keys_by_item"],
            planet_d=planet_d,
        )
        eligible_df = _5_filter_eligible_candidates(summary_df=summary_df)
        log.info(
            f"search returned {len(summary_df):,} scenes with {len(eligible_df):,} eligible for "
            f"{chip_context['event']}/{chip_context['chip_id']}"
        )

        # Download all eligible scenes sequentially for a readable first pass.
        for _, row in eligible_df.iterrows():
            result_d = await _6_order_and_download_candidate(
                chip_context=chip_context,
                row=row,
                path_d=path_d,
                planet_d=planet_d,
                run_d=run_d,
                sess=sess,
                logger=log,
            )
            for key, value in result_d.items():
                summary_df.loc[summary_df["item_id"] == row["item_id"], key] = value

    if summary_df.empty:
        summary_df = pd.DataFrame([{
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "reference_datetime": chip_context["reference_dt"].isoformat(),
            "item_id": None,
            "acquired": None,
            "instrument": None,
            "has_target_asset": False,
            "covers_chip": False,
            "coverage_ratio": None,
            "eligible": False,
            "order_submitted": False,
            "order_id": None,
            "order_state": None,
            "downloaded": False,
            "raster_fp": None,
            "manifest_fp": None,
            "note": "No scenes returned",
        }])
    elif not bool(summary_df["eligible"].fillna(False).any()):
        summary_df["note"] = summary_df["note"].fillna("No eligible full-coverage SR scenes")

    return summary_df


async def _3_search_candidates(chip_context, search_d, planet_d, sess):
    """Search Planet for scenes intersecting the chip in the event time window."""
    data_client = sess.client("data")
    search_filter = data_filter.and_filter([
        data_filter.permission_filter(),
        data_filter.date_range_filter(
            "acquired",
            gte=chip_context["start_dt"].to_pydatetime(),
            lte=chip_context["end_dt"].to_pydatetime(),
        ),
        data_filter.geometry_filter(chip_context["geometry_geojson"]),
    ])

    item_l = []
    async for item in data_client.search(
        [planet_d["item_type"]],
        search_filter,
        sort="acquired asc",
        limit=search_d["search_limit"],
    ):
        item_l.append(item)

    asset_keys_by_item = {}
    for item in item_l:
        assets = await data_client.list_item_assets(planet_d["item_type"], item["id"])
        asset_keys_by_item[item["id"]] = sorted(assets.keys())

    return {"item_l": item_l, "asset_keys_by_item": asset_keys_by_item}


def _4_build_candidate_table(chip_context, item_l, asset_keys_by_item, planet_d):
    """Build the per-chip candidate summary table before any orders."""
    row_l = []
    chip_area = chip_context["geometry"].area
    for i, item in enumerate(item_l, start=1):
        acquired = pd.Timestamp(item["properties"]["acquired"])
        acquired = acquired.tz_localize("UTC") if acquired.tzinfo is None else acquired.tz_convert("UTC")
        item_geom = shape(item["geometry"])
        intersection = item_geom.intersection(chip_context["geometry"])
        coverage_ratio = float(intersection.area / chip_area) if chip_area > 0 else None
        row_l.append({
            "query_rank": i,
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "reference_datetime": chip_context["reference_dt"].isoformat(),
            "item_id": item["id"],
            "acquired": acquired.isoformat(),
            "time_delta_hours": round(abs((acquired - chip_context["reference_dt"]).total_seconds()) / 3600.0, 3),
            "instrument": item.get("properties", {}).get("instrument") or ",".join(item.get("properties", {}).get("instruments") or []),
            "has_target_asset": planet_d["asset_key"] in asset_keys_by_item.get(item["id"], []),
            "covers_chip": item_geom.buffer(1e-12).covers(chip_context["geometry"]),
            "coverage_ratio": round(coverage_ratio, 6) if coverage_ratio is not None else None,
            "eligible": False,
            "order_submitted": False,
            "order_id": None,
            "order_state": None,
            "downloaded": False,
            "raster_fp": None,
            "manifest_fp": None,
            "note": None,
        })

    summary_df = pd.DataFrame(row_l)
    if not summary_df.empty:
        summary_df["eligible"] = summary_df["has_target_asset"] & summary_df["covers_chip"]
    return summary_df


def _5_filter_eligible_candidates(summary_df):
    """Return the eligible full-coverage SR candidates for one chip."""
    if summary_df.empty:
        return summary_df.copy()
    return summary_df.loc[summary_df["eligible"]].copy().reset_index(drop=True)


async def _6_order_and_download_candidate(chip_context, row, path_d, planet_d, run_d, sess, logger=None):
    """Order one eligible candidate, move the raster into place, and write its manifest."""
    log = logger or get_logger(__name__)
    item_id = row["item_id"]
    raster_fp = chip_context["chip_dir"] / f"{item_id}.tif"
    manifest_fp = chip_context["chip_dir"] / f"{item_id}.manifest.json"
    if raster_fp.exists() and manifest_fp.exists() and not run_d["overwrite"]:
        return {
            "order_submitted": False,
            "order_id": None,
            "order_state": "skipped_existing",
            "downloaded": True,
            "raster_fp": str(raster_fp),
            "manifest_fp": str(manifest_fp),
            "note": "existing_output",
        }

    # Stage raw order outputs under the chip directory before selecting the SR tif.
    stage_dir = chip_context["chip_dir"] / "_staging" / item_id
    if stage_dir.exists() and run_d["overwrite"]:
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    orders_client = sess.client("orders")
    request = order_request.build_request(
        name=f"{chip_context['chip_id'].lower()}_{item_id}",
        products=[order_request.product(
            item_ids=[item_id],
            product_bundle=planet_d["product_bundle"],
            item_type=planet_d["item_type"],
        )],
        tools=[
            order_request.clip_tool(aoi=chip_context["geometry_geojson"]),
            order_request.file_format_tool(planet_d["file_format"]),
        ],
    )

    def _order_state_callback(state):
        """Log order state changes while waiting on Planet processing."""
        log.info(f"order {order['id']} item={item_id} state={state}")

    order_id = None
    order_state = None
    download_path_l = []
    try:
        log.info(f"ordering {chip_context['event']}/{chip_context['chip_id']} item={item_id}")
        order = await orders_client.create_order(request)
        order_id = order["id"]
        log.info(f"submitted order {order_id} for item={item_id}")
        order_state = await orders_client.wait(
            order_id,
            delay=planet_d["poll_delay_seconds"],
            max_attempts=planet_d["poll_max_attempts"],
            callback=_order_state_callback,
        )
        download_path_l = await orders_client.download_order(
            order_id,
            stage_dir,
            overwrite=run_d["overwrite"],
            progress_bar=False,
        )
        primary_raster_fp = _7_pick_primary_raster(item_id=item_id, download_path_l=download_path_l, stage_dir=stage_dir)
        if raster_fp.exists() and run_d["overwrite"]:
            raster_fp.unlink()
        shutil.move(str(primary_raster_fp), str(raster_fp))

        manifest_d = {
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "item_id": item_id,
            "acquired": row["acquired"],
            "time_delta_hours": row["time_delta_hours"],
            "instrument": row["instrument"],
            "asset_key": planet_d["asset_key"],
            "order_id": order_id,
            "order_state": order_state,
            "output_raster": str(raster_fp),
            "chip_bbox_coverage_ratio": row["coverage_ratio"],
            "covers_chip": bool(row["covers_chip"]),
            "search_window_start": chip_context["start_dt"].isoformat(),
            "search_window_end": chip_context["end_dt"].isoformat(),
            "download_path_l": [str(Path(p)) for p in download_path_l],
        }
        _7_write_manifest(manifest_d=manifest_d, manifest_fp=manifest_fp)
        if stage_dir.exists():
            shutil.rmtree(stage_dir)

        return {
            "order_submitted": True,
            "order_id": order_id,
            "order_state": order_state,
            "downloaded": True,
            "raster_fp": str(raster_fp),
            "manifest_fp": str(manifest_fp),
            "note": None,
        }
    except Exception as err:
        log.error(f"item failed {chip_context['event']}/{chip_context['chip_id']} item={item_id}: {err}")
        manifest_d = {
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "item_id": item_id,
            "acquired": row["acquired"],
            "time_delta_hours": row["time_delta_hours"],
            "instrument": row["instrument"],
            "asset_key": planet_d["asset_key"],
            "order_id": order_id,
            "order_state": order_state,
            "output_raster": None,
            "chip_bbox_coverage_ratio": row["coverage_ratio"],
            "covers_chip": bool(row["covers_chip"]),
            "search_window_start": chip_context["start_dt"].isoformat(),
            "search_window_end": chip_context["end_dt"].isoformat(),
            "download_path_l": [str(Path(p)) for p in download_path_l],
            "note": str(err),
        }
        _7_write_manifest(manifest_d=manifest_d, manifest_fp=manifest_fp)
        return {
            "order_submitted": bool(order_id),
            "order_id": order_id,
            "order_state": order_state,
            "downloaded": False,
            "raster_fp": None,
            "manifest_fp": str(manifest_fp),
            "note": str(err),
        }


def _7_pick_primary_raster(item_id, download_path_l, stage_dir):
    """Select the primary SR raster from one order download payload."""
    tif_l = [Path(p) for p in download_path_l if Path(p).suffix.lower() == ".tif"]
    if not tif_l:
        tif_l = sorted(stage_dir.rglob("*.tif"))
    lower_name_l = [p for p in tif_l if item_id in p.name and "analyticms" in p.name.lower() and "_sr" in p.name.lower()]
    if lower_name_l:
        tif_l = lower_name_l
    tif_l = [p for p in tif_l if "udm" not in p.name.lower()]
    assert tif_l, f"No primary SR raster found for {item_id} under {stage_dir}"
    return sorted(tif_l)[0]


def _7_write_manifest(manifest_d, manifest_fp):
    """Write one per-item manifest JSON."""
    manifest_fp.write_text(json.dumps(manifest_d, indent=2))


def _8_write_chip_summary(summary_df, chip_context, logger=None):
    """Write the per-chip summary table to CSV."""
    log = logger or get_logger(__name__)
    summary_df.to_csv(chip_context["summary_fp"], index=False)
    log.info(f"wrote chip summary to\n    {chip_context['summary_fp']}")


def _parse_arguments():
    """Parse CLI arguments for the batch Planet fetch script."""
    parser = argparse.ArgumentParser(description="Batch Planet PSScene search and fetch over the FloodPlanet STAC index.")
    parser.add_argument("--index-fp", default="/workspace/stac_catalog.geojson")
    parser.add_argument("--out-dir", default="/_outputs")
    parser.add_argument("--event", default=None)
    parser.add_argument("--chip-id", default=None)
    parser.add_argument("--search-limit", type=int, default=50)
    parser.add_argument("--poll-delay-seconds", type=int, default=10)
    parser.add_argument("--poll-max-attempts", type=int, default=180)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_arguments()
    path_d = {
        "index_fp": Path(args.index_fp),
        "out_dir": Path(args.out_dir),
    }
    path_d["log_dir"] = path_d["out_dir"] / "logs"
    path_d["log_fp"] = path_d["log_dir"] / f"planet_batch_fetch_{time.strftime('%Y%m%d_%H%M%S')}.log"

    search_d = {
        "event": args.event,
        "chip_id": args.chip_id,
        "search_limit": args.search_limit,
        "half_window_hours": 24,
    }
    planet_d = {
        "item_type": "PSScene",
        "asset_key": "ortho_analytic_4b_sr",
        "product_bundle": "analytic_sr_udm2",
        "file_format": "COG",
        "poll_delay_seconds": args.poll_delay_seconds,
        "poll_max_attempts": args.poll_max_attempts,
    }
    run_d = {"overwrite": args.overwrite}
    main_planet_sdk_orders_match_search_batch(path_d=path_d, search_d=search_d, planet_d=planet_d, run_d=run_d)
