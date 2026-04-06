"""Batch Planet PSScene search and fetch over the FloodPlanet STAC index."""

import argparse, asyncio, json, shutil, sys, tempfile, time
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
    assert search_d["lower_window_hours"] > 0, search_d
    assert search_d["upper_window_hours"] > 0, search_d
    assert planet_d["asset_key"] == "ortho_analytic_4b_sr", planet_d

    # Load credentials before any Planet SDK calls.
    load_planet_secrets(override=False)

    # Create run directories and logger.
    path_d["out_dir"].mkdir(parents=True, exist_ok=True)
    path_d["log_dir"].mkdir(parents=True, exist_ok=True)
    logger = get_logger(name="planet_batch_fetch", log_fp=path_d["log_fp"], level=run_d["logging_level"])

    # Read and filter the flat chip index.
    index_gdf = _1_read_index(path_d=path_d, search_d=search_d, logger=logger)
    logger.info(f"processing {len(index_gdf):,} chips from\n    {path_d['index_fp']}")

    # Process one chip at a time here; snakemake handles cross-chip concurrency.
    for i, (_, chip_s) in enumerate(index_gdf.iterrows(), start=1):
        chip_context = _2_prepare_chip_context(chip_s=chip_s, path_d=path_d, search_d=search_d)
        logger.info(f"[{i:,}/{len(index_gdf):,}] {chip_context['event']}/{chip_context['chip_id']}")
        try:
            manifest_d = asyncio.run(
                _process_one_chip_manifest(
                    chip_context=chip_context,
                    path_d=path_d,
                    search_d=search_d,
                    planet_d=planet_d,
                    run_d=run_d,
                    logger=logger,
                )
            )
            summary_df = asyncio.run(
                _process_one_chip_order(
                    chip_context=_2_chip_context_from_manifest(manifest_d=manifest_d, path_d=path_d),
                    manifest_d=manifest_d,
                    path_d=path_d,
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
                "selected": False,
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
    log.debug(f"read {len(index_gdf):,} rows from\n    {path_d['index_fp']}")
    required_col_l = ["Event", "Chip_ID", "PS_datetime", "geometry"]
    missing_col_l = [col for col in required_col_l if col not in index_gdf.columns]
    assert not missing_col_l, missing_col_l

    if search_d["event"] is not None:
        index_gdf = index_gdf.loc[index_gdf["Event"] == search_d["event"]].copy()
        log.debug(f"applied event filter: {search_d['event']}")
    if search_d["chip_id_l"]:
        index_gdf = index_gdf.loc[index_gdf["Chip_ID"].isin(search_d["chip_id_l"])].copy()
        log.debug(f"applied chip filter: {','.join(search_d['chip_id_l'])}")

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
        "start_dt": reference_dt - pd.Timedelta(hours=search_d["lower_window_hours"]),
        "end_dt": reference_dt + pd.Timedelta(hours=search_d["upper_window_hours"]),
        "chip_dir": chip_dir,
        "stage_root": path_d["tmpdir"] / "planet_fetch_stage" / chip_s["Event"] / chip_s["Chip_ID"],
        "order_manifest_fp": chip_dir / "chip_order_manifest.json",
        "search_summary_fp": chip_dir / "chip_search_summary.tsv",
        "summary_fp": chip_dir / "summary.csv",
    }
    assert chip_context["geometry"] is not None, chip_context
    return chip_context


def _2_chip_context_from_manifest(manifest_d, path_d):
    """Rebuild the chip context from a manifest record for the order phase."""
    chip_context_d = manifest_d["chip_context"]
    chip_dir = path_d["out_dir"] / chip_context_d["event"] / chip_context_d["chip_id"]
    chip_dir.mkdir(parents=True, exist_ok=True)
    return {
        "event": chip_context_d["event"],
        "chip_id": chip_context_d["chip_id"],
        "geometry_geojson": chip_context_d["geometry_geojson"],
        "reference_dt": pd.Timestamp(chip_context_d["reference_datetime"]),
        "start_dt": pd.Timestamp(chip_context_d["search_window_start"]),
        "end_dt": pd.Timestamp(chip_context_d["search_window_end"]),
        "chip_dir": chip_dir,
        "stage_root": path_d["tmpdir"] / "planet_fetch_stage" / chip_context_d["event"] / chip_context_d["chip_id"],
        "search_summary_fp": chip_dir / "chip_search_summary.tsv",
        "summary_fp": chip_dir / "summary.csv",
        "order_manifest_fp": chip_dir / "chip_order_manifest.json",
    }


async def _process_one_chip_manifest(chip_context, path_d, search_d, planet_d, run_d, logger=None):
    """Search one chip and write the raw order manifest plus chip-level summary."""
    log = logger or get_logger(__name__)
    log.debug(
        f"chip setup event={chip_context['event']} chip_id={chip_context['chip_id']} "
        f"start={chip_context['start_dt'].isoformat()} end={chip_context['end_dt'].isoformat()}"
    )

    # Search once and materialize all candidate metadata up front.
    async with Session() as sess:
        search_result = await _3_search_candidates(
            chip_context=chip_context,
            search_d=search_d,
            planet_d=planet_d,
            sess=sess,
            logger=log,
        )
        summary_df = _4_build_candidate_table(
            chip_context=chip_context,
            item_l=search_result["item_l"],
            asset_keys_by_item=search_result["asset_keys_by_item"],
            planet_d=planet_d,
        )
        log.info(
            f"search returned {len(summary_df):,} scenes with "
            f"{int(summary_df['covers_chip'].sum()) if not summary_df.empty else 0:,} full-coverage scenes for "
            f"{chip_context['event']}/{chip_context['chip_id']}"
        )
        if not summary_df.empty:
            log.debug(f"returned item ids: {','.join(summary_df['item_id'].tolist())}")

    manifest_d = _3_build_chip_order_manifest(chip_context=chip_context, summary_df=summary_df)
    _3_write_chip_order_manifest(manifest_d=manifest_d, manifest_fp=chip_context["order_manifest_fp"])
    chip_search_summary_df = _5_build_chip_search_summary(chip_context=chip_context, summary_df=summary_df)
    _8_write_chip_search_summary(
        chip_search_summary_df=chip_search_summary_df,
        chip_context=chip_context,
        logger=log,
    )
    log.info(f"wrote chip order manifest to\n    {chip_context['order_manifest_fp']}")
    return manifest_d


async def _process_one_chip_order(chip_context, manifest_d, path_d, planet_d, run_d, logger=None):
    """Read one chip manifest, run the order phase, and return the final summary."""
    log = logger or get_logger(__name__)
    summary_df = pd.DataFrame(manifest_d["summary_rows"])
    if summary_df.empty:
        return pd.DataFrame([{
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

    selected_df = _5_filter_eligible_candidates(summary_df=summary_df)
    if selected_df.empty:
        summary_df["note"] = summary_df["note"].fillna("No selected full-coverage SR scenes within 72h")
        log.info(f"no selected scenes to order for {chip_context['event']}/{chip_context['chip_id']}")
        return summary_df

    async with Session() as sess:
        result_df = await _6_order_and_download_chip(
            chip_context=chip_context,
            eligible_df=selected_df,
            path_d=path_d,
            planet_d=planet_d,
            run_d=run_d,
            sess=sess,
            logger=log,
        )
    for _, row in result_df.iterrows():
        for key in ["order_submitted", "order_id", "order_state", "downloaded", "raster_fp", "manifest_fp", "note"]:
            summary_df.loc[summary_df["item_id"] == row["item_id"], key] = row[key]
    return summary_df


async def _3_search_candidates(chip_context, search_d, planet_d, sess, logger=None):
    """Search Planet for scenes intersecting the chip in the event time window."""
    log = logger or get_logger(__name__)
    data_client = sess.client("data")
    search_filter = data_filter.and_filter([
        data_filter.permission_filter(),
        data_filter.asset_filter([planet_d["asset_key"]]),
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

    if item_l:
        first_id = item_l[0]["id"]
        last_id = item_l[-1]["id"]
    else:
        first_id = None
        last_id = None
    log.debug(
        f"planet search returned {len(item_l):,} items for chip {chip_context['chip_id']} "
        f"first={first_id} last={last_id}"
    )
    return {"item_l": item_l, "asset_keys_by_item": asset_keys_by_item}


def _3_build_chip_order_manifest(chip_context, summary_df):
    """Build the per-chip order manifest payload from the selected summary table."""
    summary_rows = summary_df.where(pd.notna(summary_df), None).to_dict("records")
    return {
        "chip_context": {
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "geometry_geojson": chip_context["geometry_geojson"],
            "reference_datetime": chip_context["reference_dt"].isoformat(),
            "search_window_start": chip_context["start_dt"].isoformat(),
            "search_window_end": chip_context["end_dt"].isoformat(),
        },
        "summary_rows": summary_rows,
    }


def _3_write_chip_order_manifest(manifest_d, manifest_fp):
    """Write one chip-level order manifest JSON."""
    manifest_fp.write_text(json.dumps(manifest_d, indent=2))


def _3_read_chip_order_manifest(manifest_fp):
    """Read one chip-level order manifest JSON."""
    return json.loads(Path(manifest_fp).read_text())


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
            "within_24h": abs((acquired - chip_context["reference_dt"]).total_seconds()) <= 24 * 3600.0,
            "eligible": False,
            "selected": False,
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


def _5_build_chip_search_summary(chip_context, summary_df):
    """Build one chip-level TSV summary row from the raw manifest candidate table."""
    if summary_df.empty:
        chip_summary_df = pd.DataFrame([{
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "reference_datetime": chip_context["reference_dt"].isoformat(),
            "search_window_start": chip_context["start_dt"].isoformat(),
            "search_window_end": chip_context["end_dt"].isoformat(),
            "n_returned": 0,
            "n_full_coverage": 0,
            "n_within_24h": 0,
            "closest_time_delta_hours": None,
            "first_acquired": None,
            "last_acquired": None,
            "sensor_values": None,
            "sensor_counts_json": "{}",
        }])
        return chip_summary_df

    sensor_count_s = summary_df["instrument"].fillna("UNKNOWN").value_counts().sort_index()
    chip_summary_df = pd.DataFrame([{
        "event": chip_context["event"],
        "chip_id": chip_context["chip_id"],
        "reference_datetime": chip_context["reference_dt"].isoformat(),
        "search_window_start": chip_context["start_dt"].isoformat(),
        "search_window_end": chip_context["end_dt"].isoformat(),
        "n_returned": int(len(summary_df)),
        "n_full_coverage": int(summary_df["covers_chip"].sum()),
        "n_within_24h": int(summary_df["within_24h"].sum()),
        "closest_time_delta_hours": float(summary_df["time_delta_hours"].min()),
        "first_acquired": str(summary_df["acquired"].min()),
        "last_acquired": str(summary_df["acquired"].max()),
        "sensor_values": ",".join(sensor_count_s.index.tolist()),
        "sensor_counts_json": json.dumps(sensor_count_s.to_dict(), sort_keys=True),
    }])
    return chip_summary_df


def _5_filter_eligible_candidates(summary_df):
    """Return the selected candidates for fetch under the 24h + 72h fallback rule."""
    if summary_df.empty:
        return summary_df.copy()
    summary_df["eligible"] = summary_df["has_target_asset"] & summary_df["covers_chip"]
    within_24h_df = summary_df.loc[summary_df["eligible"] & summary_df["within_24h"]].copy()
    if not within_24h_df.empty:
        summary_df.loc[summary_df["item_id"].isin(within_24h_df["item_id"]), "selected"] = True
        return within_24h_df.reset_index(drop=True)

    fallback_df = summary_df.loc[summary_df["eligible"]].copy().sort_values(["time_delta_hours", "item_id"]).head(1)
    if not fallback_df.empty:
        summary_df.loc[summary_df["item_id"].isin(fallback_df["item_id"]), "selected"] = True
    return fallback_df.reset_index(drop=True)


def _6_expected_item_outputs(chip_context, item_id):
    """Return the expected output paths for one ordered item."""
    return {
        "raster_fp": chip_context["chip_dir"] / f"{item_id}.tif",
        "manifest_fp": chip_context["chip_dir"] / f"{item_id}.manifest.json",
    }


async def _6_order_and_download_chip(chip_context, eligible_df, path_d, planet_d, run_d, sess, logger=None):
    """Order all eligible scenes for one chip, then write per-item outputs."""
    log = logger or get_logger(__name__)
    assert not eligible_df.empty, "Expected at least one eligible scene"

    result_l = []
    pending_item_id_l = []
    for _, row in eligible_df.iterrows():
        output_d = _6_expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
        if output_d["raster_fp"].exists() and output_d["manifest_fp"].exists() and not run_d["overwrite"]:
            result_l.append({
                "item_id": row["item_id"],
                "order_submitted": False,
                "order_id": None,
                "order_state": "skipped_existing",
                "downloaded": True,
                "raster_fp": str(output_d["raster_fp"]),
                "manifest_fp": str(output_d["manifest_fp"]),
                "note": "existing_output",
            })
        else:
            pending_item_id_l.append(row["item_id"])

    if not pending_item_id_l:
        log.debug(f"all eligible items already exist for {chip_context['chip_id']}")
        return pd.DataFrame(result_l)

    # Stage raw order outputs under the chip directory before selecting the SR tif.
    stage_dir = chip_context["stage_root"]
    if stage_dir.exists() and run_d["overwrite"]:
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    orders_client = sess.client("orders")
    request = order_request.build_request(
        name=f"{chip_context['chip_id'].lower()}_batch",
        products=[order_request.product(
            item_ids=pending_item_id_l,
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
        log.info(f"order {order['id']} chip={chip_context['chip_id']} state={state}")

    order_id = None
    order_state = None
    download_path_l = []
    try:
        log.info(
            f"ordering {chip_context['event']}/{chip_context['chip_id']} "
            f"with {len(pending_item_id_l):,} item(s)"
        )
        log.debug(f"pending item ids: {','.join(pending_item_id_l)}")
        order = await orders_client.create_order(request)
        order_id = order["id"]
        log.info(f"submitted order {order_id} for chip={chip_context['chip_id']}")
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
        log.debug(f"downloaded {len(download_path_l):,} files into\n    {stage_dir}")
        for _, row in eligible_df.loc[eligible_df["item_id"].isin(pending_item_id_l)].iterrows():
            output_d = _6_expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
            primary_raster_fp = _7_pick_primary_raster(
                item_id=row["item_id"],
                download_path_l=download_path_l,
                stage_dir=stage_dir,
            )
            log.debug(f"selected raster for {row['item_id']}:\n    {primary_raster_fp}")
            if output_d["raster_fp"].exists() and run_d["overwrite"]:
                output_d["raster_fp"].unlink()
            shutil.move(str(primary_raster_fp), str(output_d["raster_fp"]))
            log.debug(f"moved raster to\n    {output_d['raster_fp']}")

            manifest_d = {
                "event": chip_context["event"],
                "chip_id": chip_context["chip_id"],
                "item_id": row["item_id"],
                "acquired": row["acquired"],
                "time_delta_hours": row["time_delta_hours"],
                "instrument": row["instrument"],
                "asset_key": planet_d["asset_key"],
                "order_id": order_id,
                "order_state": order_state,
                "output_raster": str(output_d["raster_fp"]),
                "chip_bbox_coverage_ratio": row["coverage_ratio"],
                "covers_chip": bool(row["covers_chip"]),
                "search_window_start": chip_context["start_dt"].isoformat(),
                "search_window_end": chip_context["end_dt"].isoformat(),
                "download_path_l": [str(Path(p)) for p in download_path_l],
            }
            _7_write_manifest(manifest_d=manifest_d, manifest_fp=output_d["manifest_fp"])
            log.debug(f"wrote manifest to\n    {output_d['manifest_fp']}")
            result_l.append({
                "item_id": row["item_id"],
                "order_submitted": True,
                "order_id": order_id,
                "order_state": order_state,
                "downloaded": True,
                "raster_fp": str(output_d["raster_fp"]),
                "manifest_fp": str(output_d["manifest_fp"]),
                "note": None,
            })
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        return pd.DataFrame(result_l)
    except Exception as err:
        log.error(f"chip order failed {chip_context['event']}/{chip_context['chip_id']}: {err}")
        for _, row in eligible_df.loc[eligible_df["item_id"].isin(pending_item_id_l)].iterrows():
            output_d = _6_expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
            manifest_d = {
                "event": chip_context["event"],
                "chip_id": chip_context["chip_id"],
                "item_id": row["item_id"],
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
            _7_write_manifest(manifest_d=manifest_d, manifest_fp=output_d["manifest_fp"])
            log.debug(f"wrote failure manifest to\n    {output_d['manifest_fp']}")
            result_l.append({
                "item_id": row["item_id"],
                "order_submitted": bool(order_id),
                "order_id": order_id,
                "order_state": order_state,
                "downloaded": False,
                "raster_fp": None,
                "manifest_fp": str(output_d["manifest_fp"]),
                "note": str(err),
            })
        return pd.DataFrame(result_l)


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


def _8_write_chip_search_summary(chip_search_summary_df, chip_context, logger=None):
    """Write the per-chip manifest search summary table to TSV."""
    log = logger or get_logger(__name__)
    chip_search_summary_df.to_csv(chip_context["search_summary_fp"], index=False, sep="\t")
    log.info(f"wrote chip search summary to\n    {chip_context['search_summary_fp']}")


def _parse_arguments():
    """Parse CLI arguments for the batch Planet fetch script."""
    parser = argparse.ArgumentParser(description="Batch Planet PSScene search and fetch over the FloodPlanet STAC index.")
    parser.add_argument("--index-fp", default="/workspace/stac_catalog.geojson")
    parser.add_argument("--out-dir", default="/_outputs")
    parser.add_argument("--tmpdir", default=tempfile.gettempdir())
    parser.add_argument("--event", default=None)
    parser.add_argument("--chip-id", nargs="+", default=None)
    parser.add_argument("--search-limit", type=int, default=50)
    parser.add_argument("--lower-window-hours", type=int, default=72)
    parser.add_argument("--upper-window-hours", type=int, default=72)
    parser.add_argument("--poll-delay-seconds", type=int, default=10)
    parser.add_argument("--poll-max-attempts", type=int, default=180)
    parser.add_argument("--logging-level", default="WARNING")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def _parse_id_values(value):
    """Normalize chip or event selectors from CLI or snakemake config."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    value_l = []
    for item in value:
        value_l.extend(_parse_id_values(item))
    return value_l


def main_fetch_ps_chip_manifest(path_d, search_d, planet_d, run_d):
    """Build and write the order manifest for one chip."""
    assert search_d["event"] is not None, search_d
    assert len(search_d["chip_id_l"]) == 1, search_d
    load_planet_secrets(override=False)
    path_d["out_dir"].mkdir(parents=True, exist_ok=True)
    path_d["log_dir"].mkdir(parents=True, exist_ok=True)
    logger = get_logger(name="planet_batch_fetch", log_fp=path_d["log_fp"], level=run_d["logging_level"])
    index_gdf = _1_read_index(path_d=path_d, search_d=search_d, logger=logger)
    chip_context = _2_prepare_chip_context(chip_s=index_gdf.iloc[0], path_d=path_d, search_d=search_d)
    logger.info(f"manifest phase for {chip_context['event']}/{chip_context['chip_id']}")
    asyncio.run(
        _process_one_chip_manifest(
            chip_context=chip_context,
            path_d=path_d,
            search_d=search_d,
            planet_d=planet_d,
            run_d=run_d,
            logger=logger,
        )
    )


def main_fetch_ps_chip_order(path_d, search_d, planet_d, run_d):
    """Read one chip manifest, run the order phase, and write the final summary."""
    assert search_d["event"] is not None, search_d
    assert len(search_d["chip_id_l"]) == 1, search_d
    load_planet_secrets(override=False)
    path_d["out_dir"].mkdir(parents=True, exist_ok=True)
    path_d["log_dir"].mkdir(parents=True, exist_ok=True)
    logger = get_logger(name="planet_batch_fetch", log_fp=path_d["log_fp"], level=run_d["logging_level"])
    manifest_fp = path_d["out_dir"] / search_d["event"] / search_d["chip_id_l"][0] / "chip_order_manifest.json"
    manifest_d = _3_read_chip_order_manifest(manifest_fp=manifest_fp)
    chip_context = _2_chip_context_from_manifest(manifest_d=manifest_d, path_d=path_d)
    logger.info(f"order phase for {chip_context['event']}/{chip_context['chip_id']}")
    summary_df = asyncio.run(
        _process_one_chip_order(
            chip_context=chip_context,
            manifest_d=manifest_d,
            path_d=path_d,
            planet_d=planet_d,
            run_d=run_d,
            logger=logger,
        )
    )
    _8_write_chip_summary(summary_df=summary_df, chip_context=chip_context, logger=logger)


def _run_from_cli(args):
    """Build runtime dictionaries from CLI args and execute the workflow."""
    chip_id_l = []
    for value in args.chip_id or []:
        chip_id_l.extend(_parse_id_values(value))
    path_d = {
        "index_fp": Path(args.index_fp),
        "out_dir": Path(args.out_dir),
        "tmpdir": Path(args.tmpdir),
    }
    path_d["log_dir"] = path_d["out_dir"] / "logs"
    path_d["log_fp"] = path_d["log_dir"] / f"planet_batch_fetch_{time.strftime('%Y%m%d_%H%M%S')}.log"

    search_d = {
        "event": args.event,
        "chip_id_l": chip_id_l,
        "search_limit": args.search_limit,
        "lower_window_hours": args.lower_window_hours,
        "upper_window_hours": args.upper_window_hours,
    }
    planet_d = {
        "item_type": "PSScene",
        "asset_key": "ortho_analytic_4b_sr",
        "product_bundle": "analytic_sr_udm2",
        "file_format": "COG",
        "poll_delay_seconds": args.poll_delay_seconds,
        "poll_max_attempts": args.poll_max_attempts,
    }
    run_d = {"overwrite": args.overwrite, "logging_level": str(args.logging_level).upper()}
    main_planet_sdk_orders_match_search_batch(path_d=path_d, search_d=search_d, planet_d=planet_d, run_d=run_d)


def _run_from_snakemake(snakemake):
    """Build runtime dictionaries from a snakemake script context."""
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
    main_planet_sdk_orders_match_search_batch(path_d=path_d, search_d=search_d, planet_d=planet_d, run_d=run_d)


if "snakemake" in globals():
    _run_from_snakemake(snakemake)
elif __name__ == "__main__":
    _run_from_cli(_parse_arguments())
