"""Common backend helpers for the Planet PS snakemake workflow."""

import asyncio, json, logging, os, shutil, sys, time
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


def read_index(path_d, search_d, logger=None):
    """Read the flat STAC index and apply any requested filters."""
    log = logger or logging.getLogger(__name__)
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


def prepare_chip_context(chip_s, path_d, search_d):
    """Prepare one chip context dictionary for the workflow."""
    reference_dt = pd.Timestamp(chip_s["PS_datetime"])
    reference_dt = reference_dt.tz_localize("UTC") if reference_dt.tzinfo is None else reference_dt.tz_convert("UTC")
    chip_dir = path_d["out_dir"] / chip_s["Event"] / chip_s["Chip_ID"]
    chip_dir.mkdir(parents=True, exist_ok=True)
    return {
        "event": chip_s["Event"],
        "chip_id": chip_s["Chip_ID"],
        "geometry": chip_s.geometry,
        "geometry_geojson": mapping(chip_s.geometry),
        "reference_dt": reference_dt,
        "start_dt": reference_dt - pd.Timedelta(hours=search_d["lower_window_hours"]),
        "end_dt": reference_dt + pd.Timedelta(hours=search_d["upper_window_hours"]),
        "chip_dir": chip_dir,
        "stage_root": path_d["tmpdir"] / "planet_fetch_stage" / chip_s["Event"] / chip_s["Chip_ID"],
        "search_manifest_fp": chip_dir / "chip_search_manifest.json",
        "order_manifest_fp": chip_dir / "chip_order_manifest.json",
        "search_summary_fp": chip_dir / "chip_search_summary.tsv",
        "summary_fp": chip_dir / "summary.csv",
    }


def chip_context_from_manifest(manifest_d, path_d):
    """Rebuild a chip context from a search or order manifest."""
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
        "search_manifest_fp": chip_dir / "chip_search_manifest.json",
        "order_manifest_fp": chip_dir / "chip_order_manifest.json",
        "search_summary_fp": chip_dir / "chip_search_summary.tsv",
        "summary_fp": chip_dir / "summary.csv",
    }


async def search_candidates(chip_context, search_d, planet_d, logger=None):
    """Search Planet for scenes intersecting the chip in the event time window."""
    log = logger or logging.getLogger(__name__)
    async with Session() as sess:
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

    first_id = item_l[0]["id"] if item_l else None
    last_id = item_l[-1]["id"] if item_l else None
    log.debug(
        f"planet search returned {len(item_l):,} items for chip {chip_context['chip_id']} "
        f"first={first_id} last={last_id}"
    )
    return {"item_l": item_l, "asset_keys_by_item": asset_keys_by_item}


def build_candidate_table(chip_context, item_l, asset_keys_by_item, planet_d):
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


def build_chip_search_summary(chip_context, summary_df):
    """Build one chip-level TSV summary row from the raw manifest candidate table."""
    if summary_df.empty:
        return pd.DataFrame([{
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

    sensor_count_s = summary_df["instrument"].fillna("UNKNOWN").value_counts().sort_index()
    return pd.DataFrame([{
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


def select_candidates(summary_df):
    """Return the single nearest-in-time full-coverage candidate for fetch."""
    if summary_df.empty:
        return summary_df.copy()
    summary_df["eligible"] = summary_df["has_target_asset"] & summary_df["covers_chip"]
    selected_df = summary_df.loc[summary_df["eligible"]].copy().sort_values(["time_delta_hours", "item_id"]).head(1)
    if not selected_df.empty:
        summary_df.loc[summary_df["item_id"].isin(selected_df["item_id"]), "selected"] = True
    return selected_df.reset_index(drop=True)


def build_chip_search_manifest(chip_context, summary_df):
    """Build the per-chip search manifest payload from the raw summary table."""
    return {
        "chip_context": {
            "event": chip_context["event"],
            "chip_id": chip_context["chip_id"],
            "geometry_geojson": chip_context["geometry_geojson"],
            "reference_datetime": chip_context["reference_dt"].isoformat(),
            "search_window_start": chip_context["start_dt"].isoformat(),
            "search_window_end": chip_context["end_dt"].isoformat(),
        },
        "summary_rows": summary_df.where(pd.notna(summary_df), None).to_dict("records"),
    }


def build_chip_order_manifest(search_manifest_d):
    """Build one explicit per-chip order manifest from a raw search manifest."""
    summary_df = pd.DataFrame(search_manifest_d.get("summary_rows", []))
    selected_df = select_candidates(summary_df=summary_df.copy())
    if not summary_df.empty:
        summary_df["selected"] = summary_df["item_id"].isin(selected_df["item_id"]) if not selected_df.empty else False
    return {
        "chip_context": search_manifest_d["chip_context"],
        "selection_method": "nearest_full_coverage",
        "selected_item_id_l": selected_df["item_id"].tolist() if not selected_df.empty else [],
        "selected_rows": selected_df.where(pd.notna(selected_df), None).to_dict("records"),
        "summary_rows": summary_df.where(pd.notna(summary_df), None).to_dict("records"),
    }


def read_manifest(manifest_fp):
    """Read one JSON manifest from disk."""
    return json.loads(Path(manifest_fp).read_text())


def write_manifest(manifest_d, manifest_fp):
    """Write one JSON manifest to disk."""
    Path(manifest_fp).write_text(json.dumps(manifest_d, indent=2))


def write_chip_search_summary(chip_search_summary_df, chip_context, logger=None):
    """Write the per-chip search summary table to TSV."""
    log = logger or logging.getLogger(__name__)
    chip_search_summary_df.to_csv(chip_context["search_summary_fp"], index=False, sep="\t")
    log.info(f"wrote chip search summary to\n    {chip_context['search_summary_fp']}")


def write_chip_summary(summary_df, chip_context, logger=None):
    """Write the per-chip summary table to CSV."""
    log = logger or logging.getLogger(__name__)
    summary_df.to_csv(chip_context["summary_fp"], index=False)
    mtime = max(time.time(), chip_context["order_manifest_fp"].stat().st_mtime + 0.01)
    os.utime(chip_context["summary_fp"], (mtime, mtime))
    log.info(f"wrote chip summary to\n    {chip_context['summary_fp']}")


def expected_item_outputs(chip_context, item_id):
    """Return the expected output paths for one ordered item."""
    return {
        "raster_fp": chip_context["chip_dir"] / f"{item_id}.tif",
        "manifest_fp": chip_context["chip_dir"] / f"{item_id}.manifest.json",
    }


def pick_primary_raster(item_id, download_path_l, stage_dir):
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


async def order_and_download_chip(chip_context, selected_df, planet_d, logger=None):
    """Order the selected scene for one chip, then write per-item outputs."""
    log = logger or logging.getLogger(__name__)
    assert not selected_df.empty, "Expected at least one selected scene"

    result_l = []
    pending_item_id_l = []
    for _, row in selected_df.iterrows():
        output_d = expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
        if output_d["raster_fp"].exists():
            if not output_d["manifest_fp"].exists():
                manifest_d = {
                    "event": chip_context["event"],
                    "chip_id": chip_context["chip_id"],
                    "item_id": row["item_id"],
                    "acquired": row["acquired"],
                    "time_delta_hours": row["time_delta_hours"],
                    "instrument": row["instrument"],
                    "asset_key": planet_d["asset_key"],
                    "order_id": None,
                    "order_state": "skipped_existing",
                    "output_raster": str(output_d["raster_fp"]),
                    "chip_bbox_coverage_ratio": row["coverage_ratio"],
                    "covers_chip": bool(row["covers_chip"]),
                    "search_window_start": chip_context["start_dt"].isoformat(),
                    "search_window_end": chip_context["end_dt"].isoformat(),
                    "download_path_l": [],
                    "note": "existing_raster",
                }
                write_manifest(manifest_d=manifest_d, manifest_fp=output_d["manifest_fp"])
                log.debug(f"wrote existing-raster manifest to\n    {output_d['manifest_fp']}")
            result_l.append({
                "item_id": row["item_id"],
                "order_submitted": False,
                "order_id": None,
                "order_state": "skipped_existing",
                "downloaded": True,
                "raster_fp": str(output_d["raster_fp"]),
                "manifest_fp": str(output_d["manifest_fp"]),
                "note": "existing_raster",
            })
        else:
            pending_item_id_l.append(row["item_id"])

    if not pending_item_id_l:
        log.debug(f"all eligible items already exist for {chip_context['chip_id']}")
        return pd.DataFrame(result_l)

    stage_dir = chip_context["stage_root"]
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    async with Session() as sess:
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
            log.info(f"ordering {chip_context['event']}/{chip_context['chip_id']} with {len(pending_item_id_l):,} item(s)")
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
            download_path_l = await orders_client.download_order(order_id, stage_dir, overwrite=False, progress_bar=False)
            log.debug(f"downloaded {len(download_path_l):,} files into\n    {stage_dir}")
            for _, row in selected_df.loc[selected_df["item_id"].isin(pending_item_id_l)].iterrows():
                output_d = expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
                primary_raster_fp = pick_primary_raster(item_id=row["item_id"], download_path_l=download_path_l, stage_dir=stage_dir)
                log.debug(f"selected raster for {row['item_id']}:\n    {primary_raster_fp}")
                shutil.move(str(primary_raster_fp), str(output_d["raster_fp"]))
                log.debug(f"moved raster to\n    {output_d['raster_fp']}")

                item_manifest_d = {
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
                write_manifest(manifest_d=item_manifest_d, manifest_fp=output_d["manifest_fp"])
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
            for _, row in selected_df.loc[selected_df["item_id"].isin(pending_item_id_l)].iterrows():
                output_d = expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
                item_manifest_d = {
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
                write_manifest(manifest_d=item_manifest_d, manifest_fp=output_d["manifest_fp"])
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


def build_logger(path_d, level):
    """Build the rule logger after ensuring log directories exist."""
    path_d["out_dir"].mkdir(parents=True, exist_ok=True)
    path_d["log_dir"].mkdir(parents=True, exist_ok=True)
    return get_logger(name="planet_batch_fetch", log_fp=path_d["log_fp"], level=level)


def load_chip_context(path_d, search_d, logger=None):
    """Load and prepare one chip context from the flat STAC index."""
    index_gdf = read_index(path_d=path_d, search_d=search_d, logger=logger)
    return prepare_chip_context(chip_s=index_gdf.iloc[0], path_d=path_d, search_d=search_d)
