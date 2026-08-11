"""Common backend helpers for the Planet PS snakemake workflow."""

import asyncio, hashlib, json, logging, os, shutil, sys, time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from planet import Session, data_filter, order_request
from rasterio.enums import Resampling
from rasterio.warp import reproject
from shapely.geometry import mapping, shape
from skimage import filters, metrics


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
        "cache_dir": Path(path_d.get("cache_dir", path_d["out_dir"] / ".cache")),
        "search_manifest_fp": chip_dir / "chip_search_manifest.json",
        "search_summary_fp": chip_dir / "chip_search_summary.tsv",
        "match_diagnostics_fp": chip_dir / "match_diagnostics.json",
        "summary_fp": chip_dir / "summary.csv",
    }


def chip_context_from_manifest(manifest_d, path_d):
    """Rebuild a chip context from a manifest payload."""
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
        "cache_dir": Path(path_d.get("cache_dir", path_d["out_dir"] / ".cache")),
        "search_manifest_fp": chip_dir / "chip_search_manifest.json",
        "search_summary_fp": chip_dir / "chip_search_summary.tsv",
        "match_diagnostics_fp": chip_dir / "match_diagnostics.json",
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
            "order_submitted": False,
            "order_id": None,
            "order_state": None,
            "downloaded": False,
            "raster_fp": None,
            "manifest_fp": None,
            "note": None,
        })

    return pd.DataFrame(row_l)


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
        "n_full_coverage": int(summary_df["covers_chip"].fillna(False).sum()),
        "closest_time_delta_hours": float(summary_df["time_delta_hours"].min()),
        "first_acquired": str(summary_df["acquired"].min()),
        "last_acquired": str(summary_df["acquired"].max()),
        "sensor_values": ",".join(sensor_count_s.index.tolist()),
        "sensor_counts_json": json.dumps(sensor_count_s.to_dict(), sort_keys=True),
    }])


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


def write_match_diagnostics(match_diagnostics_d, chip_context, logger=None):
    """Write the per-chip match diagnostics JSON."""
    log = logger or logging.getLogger(__name__)
    write_manifest(manifest_d=match_diagnostics_d, manifest_fp=chip_context["match_diagnostics_fp"])
    log.info(f"wrote match diagnostics to\n    {chip_context['match_diagnostics_fp']}")


def write_chip_summary(summary_df, chip_context, logger=None):
    """Write the per-chip summary table to CSV."""
    log = logger or logging.getLogger(__name__)
    summary_df.to_csv(chip_context["summary_fp"], index=False)
    dep_fp_l = [chip_context["search_manifest_fp"], chip_context["match_diagnostics_fp"]]
    dep_mtime = max([fp.stat().st_mtime for fp in dep_fp_l if fp.exists()] + [time.time()])
    mtime = max(time.time(), dep_mtime + 0.01)
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


def _json_ready(value):
    """Convert common numpy/pandas/path values into stable JSON primitives."""
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        return value.item()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _row_value(row, key, default=None):
    value = row.get(key, default)
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    return value


def _row_bool(row, key, default=False):
    value = _row_value(row, key, default)
    return bool(value) if value is not None else bool(default)


def fetch_cache_paths(chip_context, row, planet_d):
    """Build the cache paths for one clipped Planet order request."""
    cache_root = Path(chip_context.get("cache_dir") or (chip_context["chip_dir"] / ".cache"))
    payload_d = {
        "event": chip_context["event"],
        "chip_id": chip_context["chip_id"],
        "item_id": _row_value(row, "item_id"),
        "geometry_geojson": chip_context["geometry_geojson"],
        "item_type": planet_d["item_type"],
        "asset_key": planet_d["asset_key"],
        "product_bundle": planet_d["product_bundle"],
        "file_format": planet_d["file_format"],
    }
    payload_json = json.dumps(_json_ready(payload_d), sort_keys=True, separators=(",", ":"))
    cache_key = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()[:24]
    cache_dir = cache_root / cache_key
    item_id = str(_row_value(row, "item_id"))
    return {
        "cache_key": cache_key,
        "cache_dir": cache_dir,
        "raster_fp": cache_dir / f"{item_id}.tif",
        "manifest_fp": cache_dir / "cache_manifest.json",
        "payload": payload_d,
    }


def _link_or_copy_raster(src_fp, dst_fp):
    src_fp = Path(src_fp)
    dst_fp = Path(dst_fp)
    dst_fp.parent.mkdir(parents=True, exist_ok=True)
    if dst_fp.exists() or dst_fp.is_symlink():
        dst_fp.unlink()
    try:
        os.symlink(src_fp, dst_fp)
        return "symlink"
    except OSError:
        shutil.copy2(src_fp, dst_fp)
        return "copy"


def _build_item_manifest(chip_context, row, planet_d, output_d, cache_d, order_id, order_state, download_path_l, note, cache_hit):
    manifest_d = {
        "event": chip_context["event"],
        "chip_id": chip_context["chip_id"],
        "item_id": _row_value(row, "item_id"),
        "acquired": _row_value(row, "acquired"),
        "time_delta_hours": _row_value(row, "time_delta_hours"),
        "instrument": _row_value(row, "instrument"),
        "asset_key": planet_d["asset_key"],
        "order_id": order_id,
        "order_state": order_state,
        "output_raster": str(output_d["raster_fp"]) if output_d.get("raster_fp") else None,
        "chip_bbox_coverage_ratio": _row_value(row, "coverage_ratio"),
        "covers_chip": _row_bool(row, "covers_chip", False),
        "search_window_start": chip_context["start_dt"].isoformat(),
        "search_window_end": chip_context["end_dt"].isoformat(),
        "download_path_l": [str(Path(p)) for p in download_path_l],
        "cache_key": cache_d["cache_key"],
        "cache_raster": str(cache_d["raster_fp"]),
        "cache_manifest": str(cache_d["manifest_fp"]),
        "cache_hit": bool(cache_hit),
        "note": note,
    }
    for key in [
        "reference_time_delta_hours",
        "selected_event_datetime",
        "selected_event_time_delta_hours",
        "selection_mode",
    ]:
        value = _row_value(row, key)
        if value is not None:
            manifest_d[key] = value
    return _json_ready(manifest_d)


async def order_and_download_chip(chip_context, selected_df, planet_d, logger=None):
    """Order selected scenes for one chip, caching clipped rasters by request payload."""
    log = logger or logging.getLogger(__name__)
    assert not selected_df.empty, "Expected at least one selected scene"

    result_l = []
    pending_l = []
    for _, row in selected_df.iterrows():
        output_d = expected_item_outputs(chip_context=chip_context, item_id=row["item_id"])
        cache_d = fetch_cache_paths(chip_context=chip_context, row=row, planet_d=planet_d)
        if output_d["raster_fp"].exists():
            if not output_d["manifest_fp"].exists():
                manifest_d = _build_item_manifest(
                    chip_context=chip_context,
                    row=row,
                    planet_d=planet_d,
                    output_d=output_d,
                    cache_d=cache_d,
                    order_id=None,
                    order_state="skipped_existing",
                    download_path_l=[],
                    note="existing_raster",
                    cache_hit=cache_d["raster_fp"].exists(),
                )
                write_manifest(manifest_d=manifest_d, manifest_fp=output_d["manifest_fp"])
            result_l.append({
                "item_id": row["item_id"],
                "order_submitted": False,
                "order_id": None,
                "order_state": "skipped_existing",
                "downloaded": True,
                "raster_fp": str(output_d["raster_fp"]),
                "manifest_fp": str(output_d["manifest_fp"]),
                "cache_key": cache_d["cache_key"],
                "cache_hit": cache_d["raster_fp"].exists(),
                "note": "existing_raster",
            })
            continue

        if cache_d["raster_fp"].exists():
            link_type = _link_or_copy_raster(src_fp=cache_d["raster_fp"], dst_fp=output_d["raster_fp"])
            manifest_d = _build_item_manifest(
                chip_context=chip_context,
                row=row,
                planet_d=planet_d,
                output_d=output_d,
                cache_d=cache_d,
                order_id=None,
                order_state="cache_hit",
                download_path_l=[],
                note=f"cache_hit_{link_type}",
                cache_hit=True,
            )
            write_manifest(manifest_d=manifest_d, manifest_fp=output_d["manifest_fp"])
            result_l.append({
                "item_id": row["item_id"],
                "order_submitted": False,
                "order_id": None,
                "order_state": "cache_hit",
                "downloaded": True,
                "raster_fp": str(output_d["raster_fp"]),
                "manifest_fp": str(output_d["manifest_fp"]),
                "cache_key": cache_d["cache_key"],
                "cache_hit": True,
                "note": f"cache_hit_{link_type}",
            })
            continue

        pending_l.append((row, output_d, cache_d))

    if not pending_l:
        log.debug(f"all requested items already exist or are cached for {chip_context['chip_id']}")
        return pd.DataFrame(result_l)

    stage_dir = chip_context["stage_root"]
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    pending_item_id_l = [row["item_id"] for row, _, _ in pending_l]
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
                callback=lambda state: log.info(f"order {order_id} chip={chip_context['chip_id']} state={state}"),
            )
            download_path_l = await orders_client.download_order(order_id, stage_dir, overwrite=False, progress_bar=False)
            log.debug(f"downloaded {len(download_path_l):,} files into\n    {stage_dir}")
            for row, output_d, cache_d in pending_l:
                cache_d["cache_dir"].mkdir(parents=True, exist_ok=True)
                primary_raster_fp = pick_primary_raster(item_id=row["item_id"], download_path_l=download_path_l, stage_dir=stage_dir)
                if not cache_d["raster_fp"].exists():
                    shutil.move(str(primary_raster_fp), str(cache_d["raster_fp"]))
                elif primary_raster_fp.exists():
                    primary_raster_fp.unlink()
                cache_manifest_d = {
                    "cache_key": cache_d["cache_key"],
                    "payload": cache_d["payload"],
                    "raster_fp": str(cache_d["raster_fp"]),
                    "order_id": order_id,
                    "order_state": order_state,
                    "download_path_l": [str(Path(p)) for p in download_path_l],
                }
                write_manifest(manifest_d=_json_ready(cache_manifest_d), manifest_fp=cache_d["manifest_fp"])
                link_type = _link_or_copy_raster(src_fp=cache_d["raster_fp"], dst_fp=output_d["raster_fp"])
                item_manifest_d = _build_item_manifest(
                    chip_context=chip_context,
                    row=row,
                    planet_d=planet_d,
                    output_d=output_d,
                    cache_d=cache_d,
                    order_id=order_id,
                    order_state=order_state,
                    download_path_l=download_path_l,
                    note=f"ordered_{link_type}",
                    cache_hit=False,
                )
                write_manifest(manifest_d=item_manifest_d, manifest_fp=output_d["manifest_fp"])
                result_l.append({
                    "item_id": row["item_id"],
                    "order_submitted": True,
                    "order_id": order_id,
                    "order_state": order_state,
                    "downloaded": True,
                    "raster_fp": str(output_d["raster_fp"]),
                    "manifest_fp": str(output_d["manifest_fp"]),
                    "cache_key": cache_d["cache_key"],
                    "cache_hit": False,
                    "note": None,
                })
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            return pd.DataFrame(result_l)
        except Exception as err:
            log.error(f"chip order failed {chip_context['event']}/{chip_context['chip_id']}: {err}")
            for row, output_d, cache_d in pending_l:
                item_manifest_d = _build_item_manifest(
                    chip_context=chip_context,
                    row=row,
                    planet_d=planet_d,
                    output_d={"raster_fp": None, "manifest_fp": output_d["manifest_fp"]},
                    cache_d=cache_d,
                    order_id=order_id,
                    order_state=order_state,
                    download_path_l=download_path_l,
                    note=str(err),
                    cache_hit=False,
                )
                write_manifest(manifest_d=item_manifest_d, manifest_fp=output_d["manifest_fp"])
                result_l.append({
                    "item_id": row["item_id"],
                    "order_submitted": bool(order_id),
                    "order_id": order_id,
                    "order_state": order_state,
                    "downloaded": False,
                    "raster_fp": None,
                    "manifest_fp": str(output_d["manifest_fp"]),
                    "cache_key": cache_d["cache_key"],
                    "cache_hit": False,
                    "note": str(err),
                })
            return pd.DataFrame(result_l)


def resolve_reference_fp(chip_context, path_d):
    """Resolve the original FloodPlanet PS tile for one chip."""
    reference_fp = path_d["floodplanet_root"] / chip_context["event"] / "PS" / f"{chip_context['chip_id']}.tif"
    assert reference_fp.exists(), f"Missing FloodPlanet PS reference tile: {reference_fp}"
    return reference_fp


def rank_fetch_match_candidates(summary_df, search_d):
    """Rank search rows whose asset covers enough of the chip for matching."""
    if summary_df.empty:
        return summary_df.copy()
    min_coverage = float(search_d.get("candidate_min_coverage_ratio", 1.0))
    coverage_s = summary_df["coverage_ratio"].fillna(0.0).astype(float)
    candidate_df = summary_df.loc[
        summary_df["has_target_asset"].fillna(False) & (coverage_s >= min_coverage)
    ].copy()
    if candidate_df.empty:
        return candidate_df
    time_sort_col = "selected_event_time_delta_hours" if "selected_event_time_delta_hours" in candidate_df.columns else "time_delta_hours"
    candidate_df = candidate_df.sort_values(
        [time_sort_col, "coverage_ratio", "acquired", "item_id"],
        ascending=[True, False, True, True],
        kind="stable",
    ).head(int(search_d["chip_search_max"])).reset_index(drop=True)
    candidate_df["match_candidate_rank"] = np.arange(1, len(candidate_df) + 1)
    return candidate_df


def compare_candidate_to_reference(candidate_fp, reference_fp, compare_d, logger=None):
    """Warp one fetched candidate to the reference grid and compute quadrant match metrics."""
    log = logger or logging.getLogger(__name__)
    candidate_fp = Path(candidate_fp)
    reference_fp = Path(reference_fp)
    assert candidate_fp.exists(), candidate_fp
    assert reference_fp.exists(), reference_fp

    with rasterio.open(reference_fp) as ref_ds, rasterio.open(candidate_fp) as cand_ds:
        band_count = min(ref_ds.count, cand_ds.count, 4)
        assert band_count > 0, f"No comparable bands for {candidate_fp}"
        ref_arr = ref_ds.read(indexes=list(range(1, band_count + 1)), masked=True).astype("float32").filled(np.nan)
        cand_arr = np.full((band_count, ref_ds.height, ref_ds.width), np.nan, dtype="float32")
        resampling = getattr(Resampling, str(compare_d["resampling"]).lower())

        # Warp the candidate directly onto the FloodPlanet grid.
        for band_i in range(band_count):
            src_arr = cand_ds.read(band_i + 1, masked=True).astype("float32").filled(np.nan)
            reproject(
                source=src_arr,
                destination=cand_arr[band_i],
                src_transform=cand_ds.transform,
                src_crs=cand_ds.crs,
                src_nodata=np.nan,
                dst_transform=ref_ds.transform,
                dst_crs=ref_ds.crs,
                dst_nodata=np.nan,
                resampling=resampling,
            )

    ref_norm = np.full_like(ref_arr, np.nan)
    cand_norm = np.full_like(ref_arr, np.nan)
    for band_i in range(band_count):
        ref_valid = np.isfinite(ref_arr[band_i])
        cand_valid = np.isfinite(cand_arr[band_i])
        if ref_valid.sum() < 100 or cand_valid.sum() < 100:
            continue
        ref_low_q = np.nanpercentile(ref_arr[band_i][ref_valid], compare_d["percentile_low"])
        ref_high_q = np.nanpercentile(ref_arr[band_i][ref_valid], compare_d["percentile_high"])
        cand_low_q = np.nanpercentile(cand_arr[band_i][cand_valid], compare_d["percentile_low"])
        cand_high_q = np.nanpercentile(cand_arr[band_i][cand_valid], compare_d["percentile_high"])
        if (
            not np.isfinite(ref_low_q) or not np.isfinite(ref_high_q) or ref_high_q <= ref_low_q
            or not np.isfinite(cand_low_q) or not np.isfinite(cand_high_q) or cand_high_q <= cand_low_q
        ):
            continue
        ref_band = np.clip(ref_arr[band_i], ref_low_q, ref_high_q)
        cand_band = np.clip(cand_arr[band_i], cand_low_q, cand_high_q)
        ref_norm[band_i] = ((ref_band - ref_low_q) / (ref_high_q - ref_low_q)) * 255.0
        cand_norm[band_i] = ((cand_band - cand_low_q) / (cand_high_q - cand_low_q)) * 255.0

    valid_mask = np.all(np.isfinite(ref_norm), axis=0) & np.all(np.isfinite(cand_norm), axis=0)
    valid_pixel_fraction = float(valid_mask.mean())
    height, width = valid_mask.shape
    quad_n = int(compare_d["quadrants"])
    row_edge_l = np.linspace(0, height, quad_n + 1, dtype=int)
    col_edge_l = np.linspace(0, width, quad_n + 1, dtype=int)

    ref_edge = np.nanmean(np.stack([filters.sobel(np.nan_to_num(ref_norm[i], nan=0.0)) for i in range(band_count)]), axis=0)
    cand_edge = np.nanmean(np.stack([filters.sobel(np.nan_to_num(cand_norm[i], nan=0.0)) for i in range(band_count)]), axis=0)
    edge_scale = max(float(np.nanmax(ref_edge)), float(np.nanmax(cand_edge)), 1e-6)
    ref_edge = ref_edge / edge_scale
    cand_edge = cand_edge / edge_scale

    quadrant_l = []
    for row_i in range(quad_n):
        for col_i in range(quad_n):
            rs = slice(row_edge_l[row_i], row_edge_l[row_i + 1])
            cs = slice(col_edge_l[col_i], col_edge_l[col_i + 1])
            quad_valid = valid_mask[rs, cs]
            valid_fraction = float(quad_valid.mean())
            quad_d = {
                "quadrant": f"r{row_i}c{col_i}",
                "valid_fraction": valid_fraction,
                "valid": valid_fraction >= compare_d["min_valid_fraction"],
                "edge_corr": None,
                "edge_ssim": None,
            }
            if quad_d["valid"]:
                ref_q = ref_edge[rs, cs]
                cand_q = cand_edge[rs, cs]
                ref_v = ref_q[quad_valid]
                cand_v = cand_q[quad_valid]
                if ref_v.size > 10 and np.nanstd(ref_v) > 0 and np.nanstd(cand_v) > 0:
                    quad_d["edge_corr"] = float(np.corrcoef(ref_v, cand_v)[0, 1])
                ref_fill = np.where(quad_valid, ref_q, 0.0)
                cand_fill = np.where(quad_valid, cand_q, 0.0)
                quad_d["edge_ssim"] = float(metrics.structural_similarity(ref_fill, cand_fill, data_range=1.0))
            quadrant_l.append(quad_d)

    band_metric_l = []
    for band_i in range(band_count):
        band_valid = np.isfinite(ref_norm[band_i]) & np.isfinite(cand_norm[band_i])
        band_d = {"band": band_i + 1, "corr": None, "mae": None}
        if band_valid.sum() > 10:
            ref_v = ref_norm[band_i][band_valid]
            cand_v = cand_norm[band_i][band_valid]
            if np.nanstd(ref_v) > 0 and np.nanstd(cand_v) > 0:
                band_d["corr"] = float(np.corrcoef(ref_v, cand_v)[0, 1])
            band_d["mae"] = float(np.mean(np.abs(ref_v - cand_v)))
        band_metric_l.append(band_d)

    quad_df = pd.DataFrame(quadrant_l)
    band_df = pd.DataFrame(band_metric_l)
    valid_quad_df = quad_df.loc[quad_df["valid"]].copy()
    metric_d = {
        "band_count": int(band_count),
        "valid_pixel_fraction": valid_pixel_fraction,
        "quadrants_requested": int(quad_n * quad_n),
        "quadrants_valid": int(valid_quad_df["valid"].sum()) if not valid_quad_df.empty else 0,
        "match_mean_quad_corr": float(valid_quad_df["edge_corr"].mean()) if not valid_quad_df.empty else None,
        "match_min_quad_corr": float(valid_quad_df["edge_corr"].min()) if not valid_quad_df.empty else None,
        "match_mean_quad_ssim": float(valid_quad_df["edge_ssim"].mean()) if not valid_quad_df.empty else None,
        "match_mean_corr": float(band_df["corr"].dropna().mean()) if band_df["corr"].notna().any() else None,
        "match_mean_mae": float(band_df["mae"].dropna().mean()) if band_df["mae"].notna().any() else None,
        "quadrant_metrics": quadrant_l,
        "band_metrics": band_metric_l,
    }
    metric_d["matched"] = bool(
        metric_d["quadrants_valid"] == metric_d["quadrants_requested"]
        and metric_d["match_mean_quad_corr"] is not None
        and metric_d["match_min_quad_corr"] is not None
        and metric_d["match_mean_quad_ssim"] is not None
        and metric_d["match_mean_mae"] is not None
        and metric_d["match_mean_quad_corr"] >= compare_d["match_mean_quad_corr_min"]
        and metric_d["match_min_quad_corr"] >= compare_d["match_min_quad_corr_min"]
        and metric_d["match_mean_quad_ssim"] >= compare_d["match_mean_quad_ssim_min"]
        and metric_d["match_mean_mae"] <= compare_d["match_mean_mae_max"]
    )
    log.debug(
        f"compare {candidate_fp.stem}: matched={metric_d['matched']} "
        f"quad_corr={metric_d['match_mean_quad_corr']} quad_ssim={metric_d['match_mean_quad_ssim']} "
        f"mae={metric_d['match_mean_mae']}"
    )
    return metric_d


def update_item_manifest_compare(manifest_fp, compare_result_d, matched):
    """Append comparison metrics to one per-item manifest."""
    manifest_d = read_manifest(manifest_fp=manifest_fp)
    manifest_d["compare_metrics"] = compare_result_d
    manifest_d["matched_candidate"] = bool(matched)
    write_manifest(manifest_d=manifest_d, manifest_fp=manifest_fp)


def build_fetch_match_diagnostics(chip_context, reference_fp, summary_df, ranked_df, attempt_l, compare_d, runtime_seconds):
    """Build the per-chip fetch-match diagnostics payload."""
    chip_context_out_d = {
        "event": chip_context["event"],
        "chip_id": chip_context["chip_id"],
        "reference_datetime": chip_context["reference_dt"].isoformat(),
        "search_window_start": chip_context["start_dt"].isoformat(),
        "search_window_end": chip_context["end_dt"].isoformat(),
    }
    for key in ["selected_event_datetime", "sample_chip", "selection_mode"]:
        if key in chip_context:
            chip_context_out_d[key] = _json_ready(chip_context[key])
    return {
        "chip_context": chip_context_out_d,
        "reference_fp": str(reference_fp),
        "search_candidate_count": int(len(summary_df)),
        "ranked_candidate_count": int(len(ranked_df)),
        "compare_parameters": compare_d,
        "runtime_seconds": round(float(runtime_seconds), 3),
        "attempts": attempt_l,
    }


def build_fetch_match_summary(chip_context, reference_fp, summary_df, attempt_l, runtime_seconds):
    """Build the one-row per-chip summary table for the fetch-match stage."""
    best_attempt_d = None
    compared_attempt_l = [d for d in attempt_l if d.get("compare_metrics") is not None]
    matched_attempt_l = [d for d in compared_attempt_l if d["compare_metrics"].get("matched", False)]
    if matched_attempt_l:
        best_attempt_d = matched_attempt_l[0]
    elif compared_attempt_l:
        best_attempt_d = sorted(
            compared_attempt_l,
            key=lambda d: (
                -(d["compare_metrics"].get("match_mean_quad_corr") or -999.0),
                -(d["compare_metrics"].get("match_mean_quad_ssim") or -999.0),
                d["compare_metrics"].get("match_mean_mae") or 999999.0,
            ),
        )[0]

    row_d = {
        "event": chip_context["event"],
        "chip_id": chip_context["chip_id"],
        "reference_fp": str(reference_fp),
        "search_candidates": int(len(summary_df)),
        "search_candidates_with_asset": int(summary_df["has_target_asset"].fillna(False).sum()) if not summary_df.empty else 0,
        "search_candidates_full_coverage": int((summary_df["has_target_asset"].fillna(False) & summary_df["covers_chip"].fillna(False)).sum()) if not summary_df.empty else 0,
        "attempted_candidates": int(len(attempt_l)),
        "matched": bool(best_attempt_d is not None and best_attempt_d["compare_metrics"].get("matched", False)),
        "sample_chip": bool(chip_context.get("sample_chip", False)),
        "selection_mode": chip_context.get("selection_mode", "broad_search"),
        "selected_event_datetime": _json_ready(chip_context.get("selected_event_datetime")),
        "matched_item_id": None,
        "matched_acquired": None,
        "matched_sensor": None,
        "matched_time_delta_hours": None,
        "match_rank": None,
        "match_mean_quad_corr": None,
        "match_min_quad_corr": None,
        "match_mean_quad_ssim": None,
        "match_mean_corr": None,
        "match_mean_mae": None,
        "best_item_id": None,
        "best_mean_quad_corr": None,
        "best_mean_quad_ssim": None,
        "best_mean_mae": None,
        "runtime_seconds": round(float(runtime_seconds), 3),
    }
    if best_attempt_d is not None:
        metric_d = best_attempt_d["compare_metrics"]
        row_d.update({
            "best_item_id": best_attempt_d["item_id"],
            "best_mean_quad_corr": metric_d.get("match_mean_quad_corr"),
            "best_mean_quad_ssim": metric_d.get("match_mean_quad_ssim"),
            "best_mean_mae": metric_d.get("match_mean_mae"),
        })
        if metric_d.get("matched", False):
            row_d.update({
                "matched_item_id": best_attempt_d["item_id"],
                "matched_acquired": best_attempt_d["acquired"],
                "matched_sensor": best_attempt_d["instrument"],
                "matched_time_delta_hours": best_attempt_d["time_delta_hours"],
                "match_rank": best_attempt_d["match_candidate_rank"],
                "match_mean_quad_corr": metric_d.get("match_mean_quad_corr"),
                "match_min_quad_corr": metric_d.get("match_min_quad_corr"),
                "match_mean_quad_ssim": metric_d.get("match_mean_quad_ssim"),
                "match_mean_corr": metric_d.get("match_mean_corr"),
                "match_mean_mae": metric_d.get("match_mean_mae"),
            })
    return pd.DataFrame([row_d])


def build_logger(path_d, level):
    """Build the rule logger after ensuring log directories exist."""
    path_d["out_dir"].mkdir(parents=True, exist_ok=True)
    path_d["log_dir"].mkdir(parents=True, exist_ok=True)
    return get_logger(name="planet_batch_fetch", log_fp=path_d["log_fp"], level=level)


def load_chip_context(path_d, search_d, logger=None):
    """Load and prepare one chip context from the flat STAC index."""
    index_gdf = read_index(path_d=path_d, search_d=search_d, logger=logger)
    return prepare_chip_context(chip_s=index_gdf.iloc[0], path_d=path_d, search_d=search_d)
