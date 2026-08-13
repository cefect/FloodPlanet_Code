"""Render one per-chip PlanetScope match diagnostic plot."""

from pathlib import Path
import json
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

BAND_COLOR_D = {
    "blue": "#1f77b4",
    "green": "#2ca02c",
    "red": "#d62728",
    "nir": "#9467bd",
}


def _band_name_l(count):
    label_l = ["blue", "green", "red", "nir"]
    return label_l[:count] + [f"band_{i}" for i in range(len(label_l) + 1, count + 1)]


def _read_raster(fp):
    with rasterio.open(fp) as ds:
        arr = ds.read(masked=True).astype("float32").filled(np.nan)
        meta_d = {
            "height": ds.height,
            "width": ds.width,
            "count": ds.count,
            "res_x": float(ds.res[0]),
            "res_y": float(ds.res[1]),
            "crs": str(ds.crs),
            "transform": ds.transform,
        }
    return arr, meta_d


def _normalize_band(band_arr, low=2, high=98):
    valid_bx = np.isfinite(band_arr)
    out_arr = np.full(band_arr.shape, np.nan, dtype="float32")
    if valid_bx.sum() == 0:
        return out_arr
    low_v = np.nanpercentile(band_arr[valid_bx], low)
    high_v = np.nanpercentile(band_arr[valid_bx], high)
    if (not np.isfinite(low_v)) or (not np.isfinite(high_v)) or (high_v <= low_v):
        out_arr[valid_bx] = 0.0
        return out_arr
    band_arr = np.clip(band_arr, low_v, high_v)
    out_arr[valid_bx] = (band_arr[valid_bx] - low_v) / (high_v - low_v)
    return out_arr


def _warp_to_reference_grid(src_arr, src_meta_d, ref_meta_d):
    dst_arr = np.full((src_arr.shape[0], ref_meta_d["height"], ref_meta_d["width"]), np.nan, dtype="float32")
    for band_i in range(src_arr.shape[0]):
        reproject(
            source=src_arr[band_i],
            destination=dst_arr[band_i],
            src_transform=src_meta_d["transform"],
            src_crs=src_meta_d["crs"],
            src_nodata=np.nan,
            dst_transform=ref_meta_d["transform"],
            dst_crs=ref_meta_d["crs"],
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return dst_arr


def _rgb_plot_arr(arr):
    if arr.shape[0] >= 3:
        rgb_arr = np.stack([_normalize_band(arr[2]), _normalize_band(arr[1]), _normalize_band(arr[0])], axis=-1)
    elif arr.shape[0] == 1:
        gray_arr = _normalize_band(arr[0])
        rgb_arr = np.stack([gray_arr, gray_arr, gray_arr], axis=-1)
    else:
        rgb_arr = np.stack([_normalize_band(arr[i]) for i in range(min(3, arr.shape[0]))], axis=-1)
    return np.nan_to_num(rgb_arr, nan=1.0)


def _hist_ready_arr(arr):
    return np.stack([_normalize_band(arr[i], low=1, high=99) * 255.0 for i in range(arr.shape[0])], axis=0)


def _label_plot_arr(label_arr):
    class_color_d = {
        0: np.array([0.82, 0.82, 0.82], dtype="float32"),
        1: np.array([0.94, 0.94, 0.90], dtype="float32"),
        2: np.array([0.12, 0.47, 0.71], dtype="float32"),
    }
    plot_arr = np.zeros((*label_arr.shape, 3), dtype="float32")
    for value, color_ar in class_color_d.items():
        plot_arr[label_arr == value] = color_ar
    unknown_bx = ~np.isin(label_arr, list(class_color_d))
    plot_arr[unknown_bx] = np.array([0.55, 0.55, 0.55], dtype="float32")
    return plot_arr


def _label_legend_handles(label_arr):
    label_d = {0: "nodata/ignore", 1: "not flood water", 2: "flood water"}
    color_d = {0: "#d1d1d1", 1: "#f0f0e6", 2: "#1f78b4"}
    value_l = [int(v) for v in np.unique(label_arr[np.isfinite(label_arr)])]
    return [Patch(facecolor=color_d.get(v, "#8c8c8c"), edgecolor="0.4", label=f"{v}: {label_d.get(v, 'unknown')}") for v in value_l]


def _meta_text(meta_d):
    return (
        f"shape: {meta_d['count']} x {meta_d['height']} x {meta_d['width']}\n"
        f"res: {meta_d['res_x']:.6f} x {meta_d['res_y']:.6f} deg"
    )


def _fmt_ts(value):
    if value is None or pd.isna(value):
        return "NA"
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_metric(value, precision=3):
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value):.{precision}f}"


def _axis_title(label, chip_context, datetime_value, match_rank):
    """Build a compact per-axis title with chip and match context."""
    return (
        f"{label}\n"
        f"{chip_context['chip_id']} | dt: {_fmt_ts(datetime_value)} | rank: {match_rank or 'NA'}"
    )


def _select_attempt(match_d):
    attempt_l = match_d.get("attempts", [])
    compared_l = [d for d in attempt_l if d.get("compare_metrics") is not None and d.get("raster_fp") and Path(d["raster_fp"]).exists()]
    matched_l = [d for d in compared_l if d["compare_metrics"].get("matched", False)]
    if matched_l:
        return sorted(matched_l, key=lambda d: d.get("match_candidate_rank") or 999)[0], "matched"
    if compared_l:
        return sorted(
            compared_l,
            key=lambda d: (
                -(d["compare_metrics"].get("match_mean_quad_corr") or -999.0),
                -(d["compare_metrics"].get("match_mean_quad_ssim") or -999.0),
                d["compare_metrics"].get("match_mean_mae") or 999999.0,
            ),
        )[0], "best_unmatched"
    return None, "no_plottable_attempt"


def _attempt_text(attempt_d):
    metric_d = attempt_d.get("compare_metrics") or {}
    return (
        f"item: {attempt_d.get('item_id')}\n"
        f"rank: {attempt_d.get('match_candidate_rank')}\n"
        f"matched: {metric_d.get('matched')}\n"
        f"acquired: {_fmt_ts(attempt_d.get('acquired'))}\n"
        f"delta: {_fmt_metric(attempt_d.get('time_delta_hours'), 3)} h\n"
        f"instrument: {attempt_d.get('instrument')}\n"
        f"coverage: {_fmt_metric(attempt_d.get('coverage_ratio'), 3)}\n"
        f"quad_corr: {_fmt_metric(metric_d.get('match_mean_quad_corr'))}\n"
        f"quad_ssim: {_fmt_metric(metric_d.get('match_mean_quad_ssim'))}\n"
        f"mae: {_fmt_metric(metric_d.get('match_mean_mae'), 1)}"
    )


def _write_placeholder(output_fp, chip_context, message):
    fig, ax = plt.subplots(figsize=(9, 4), constrained_layout=True)
    ax.text(0.5, 0.5, message, ha="center", va="center", transform=ax.transAxes, fontsize=12)
    ax.set_title(f"{chip_context['event']} | {chip_context['chip_id']}")
    ax.set_axis_off()
    fig.savefig(output_fp, dpi=120)
    plt.close(fig)


def main(snakemake):
    log_fp = Path(snakemake.log[0])
    log_fp.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_fp,
        level=getattr(logging, str(snakemake.params.logging_level).upper()),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("plot_match_chip")

    match_d = json.loads(Path(snakemake.input.match_diagnostics_fp).read_text())
    chip_context = match_d["chip_context"]
    output_fp = Path(snakemake.output.plot_fp)
    output_fp.parent.mkdir(parents=True, exist_ok=True)

    attempt_d, selection_reason = _select_attempt(match_d)
    if attempt_d is None:
        _write_placeholder(output_fp, chip_context, "No downloaded candidate with comparison metrics")
        logger.info(f"wrote placeholder plot to {output_fp}")
        return

    reference_fp = Path(match_d["reference_fp"])
    label_fp = reference_fp.parents[1] / "labels" / reference_fp.name
    fetch_fp = Path(attempt_d["raster_fp"])

    ref_arr, ref_meta_d = _read_raster(reference_fp)
    fetch_arr, fetch_meta_d = _read_raster(fetch_fp)
    fetch_arr = _warp_to_reference_grid(fetch_arr, fetch_meta_d, ref_meta_d)
    fetch_meta_d = ref_meta_d.copy()
    label_arr, label_meta_d = _read_raster(label_fp) if label_fp.exists() else (None, None)

    fontsize = 9
    fig, ax_ar = plt.subplots(1, 4, figsize=(16, 4.8), constrained_layout=True)
    fig.suptitle(f"{chip_context['event']} | {chip_context['chip_id']} | {selection_reason}", fontsize=fontsize + 3)
    ax_hist, ax_ref, ax_fetch, ax_label = ax_ar

    ref_hist_arr = _hist_ready_arr(ref_arr)
    fetch_hist_arr = _hist_ready_arr(fetch_arr)
    bin_edge_ar = np.linspace(0, 255, 60)
    for band_i, band_name in enumerate(_band_name_l(min(ref_hist_arr.shape[0], fetch_hist_arr.shape[0]))):
        ref_v = ref_hist_arr[band_i][np.isfinite(ref_hist_arr[band_i])]
        fetch_v = fetch_hist_arr[band_i][np.isfinite(fetch_hist_arr[band_i])]
        if ref_v.size > 0:
            hist_v, edge_v = np.histogram(ref_v, bins=bin_edge_ar, density=True)
            center_v = 0.5 * (edge_v[:-1] + edge_v[1:])
            ax_hist.plot(center_v, hist_v, color=BAND_COLOR_D.get(band_name, "black"), linewidth=0.9, label=f"{band_name} ref")
        if fetch_v.size > 0:
            hist_v, edge_v = np.histogram(fetch_v, bins=bin_edge_ar, density=True)
            center_v = 0.5 * (edge_v[:-1] + edge_v[1:])
            ax_hist.plot(center_v, hist_v, color=BAND_COLOR_D.get(band_name, "black"), linewidth=0.9, linestyle="--", label=f"{band_name} fetch")

    ax_ref.imshow(_rgb_plot_arr(ref_arr), interpolation="nearest", aspect="equal")
    ax_ref.set_title(
        _axis_title("FloodPlanet original", chip_context, chip_context.get("reference_datetime"), attempt_d.get("match_candidate_rank")),
        fontsize=fontsize,
    )
    ax_ref.set_axis_off()
    ax_ref.text(0.98, 0.98, f"dt: {_fmt_ts(chip_context.get('reference_datetime'))}", transform=ax_ref.transAxes, ha="right", va="top", fontsize=fontsize, bbox=dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="0.5"))

    ax_fetch.imshow(_rgb_plot_arr(fetch_arr), interpolation="nearest", aspect="equal")
    ax_fetch.set_title(
        _axis_title("Fetch/match RGB", chip_context, attempt_d.get("acquired"), attempt_d.get("match_candidate_rank")),
        fontsize=fontsize,
    )
    ax_fetch.set_axis_off()
    facecolor = "#2ca02c" if bool((attempt_d.get("compare_metrics") or {}).get("matched", False)) else "#d62728"
    ax_fetch.text(0.98, 0.98, _attempt_text(attempt_d), transform=ax_fetch.transAxes, ha="right", va="top", fontsize=fontsize, bbox=dict(boxstyle="round", facecolor=facecolor, alpha=0.5, edgecolor="0.5"))

    ax_label.set_title(
        _axis_title("FloodPlanet label", chip_context, chip_context.get("reference_datetime"), attempt_d.get("match_candidate_rank")),
        fontsize=fontsize,
    )
    ax_label.set_axis_off()
    if label_arr is None:
        ax_label.text(0.5, 0.5, "label missing", transform=ax_label.transAxes, ha="center", va="center", fontsize=fontsize)
    else:
        label_band_arr = label_arr[0] if label_arr.ndim == 3 else label_arr
        ax_label.imshow(_label_plot_arr(label_band_arr), interpolation="nearest", aspect="equal")
        ax_label.legend(handles=_label_legend_handles(label_band_arr), loc="lower left", fontsize=fontsize - 1, frameon=True)
        ax_label.text(0.98, 0.98, _meta_text(label_meta_d), transform=ax_label.transAxes, ha="right", va="top", fontsize=fontsize, bbox=dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="0.5"))

    ax_hist.set_title(
        _axis_title("Per-band histograms", chip_context, attempt_d.get("acquired"), attempt_d.get("match_candidate_rank")),
        fontsize=fontsize,
    )
    ax_hist.set_xlabel("normalized value", fontsize=fontsize)
    ax_hist.set_ylabel("density", fontsize=fontsize)
    ax_hist.grid(True, alpha=0.2)
    ax_hist.legend(loc="upper left", fontsize=fontsize - 2, ncol=2, frameon=False)
    ax_hist.text(0.98, 0.98, "ref\n" + _meta_text(ref_meta_d) + "\n\nfetch\n" + _meta_text(fetch_meta_d), transform=ax_hist.transAxes, ha="right", va="top", fontsize=fontsize - 1, bbox=dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="0.5"))

    fig.savefig(output_fp, dpi=120)
    plt.close(fig)
    logger.info(f"wrote match plot to {output_fp}")


if "snakemake" in globals():
    main(snakemake)
