"""Resolve one event-level Planet acquisition datetime from sample chip matches."""

from pathlib import Path
import json

import pandas as pd


def _bool_s(series):
    return series.fillna(False).astype(str).str.lower().isin(["true", "1", "yes"])


def _selected_timestamp(ts_s):
    ts_s = ts_s.dropna().sort_values().reset_index(drop=True)
    if ts_s.empty:
        return None
    ns_l = sorted(ts.value for ts in ts_s)
    mid = len(ns_l) // 2
    selected_ns = ns_l[mid] if len(ns_l) % 2 else (ns_l[mid - 1] + ns_l[mid]) // 2
    return pd.to_datetime(selected_ns, unit="ns", utc=True)


def main(snakemake):
    event = snakemake.wildcards.event
    output_fp = Path(snakemake.output.resolution_fp)
    output_fp.parent.mkdir(parents=True, exist_ok=True)
    summary_fp_l = [Path(p) for p in snakemake.input.summary_fp_l]
    window_hours = float(snakemake.params.resolution_window_hours)

    summary_df = pd.concat([pd.read_csv(fp) for fp in summary_fp_l], ignore_index=True) if summary_fp_l else pd.DataFrame()
    if summary_df.empty:
        out_d = {
            "event": event,
            "resolved": False,
            "reason": "no_sample_summaries",
            "sample_count": 0,
            "matched_sample_count": 0,
            "resolution_window_hours": window_hours,
            "selected_event_datetime": None,
            "sample_fetch_time_span_hours": None,
            "sample_chips": [],
            "sample_records": [],
        }
        output_fp.write_text(json.dumps(out_d, indent=2))
        return

    if "matched" in summary_df.columns:
        summary_df["matched_bool"] = _bool_s(summary_df["matched"])
    else:
        summary_df["matched_bool"] = False
    matched_acquired_s = pd.to_datetime(
        summary_df.loc[summary_df["matched_bool"], "matched_acquired"],
        utc=True,
        errors="coerce",
    ).dropna()
    selected_dt = _selected_timestamp(matched_acquired_s)
    if matched_acquired_s.empty:
        span_hours = None
    else:
        span_hours = round((matched_acquired_s.max() - matched_acquired_s.min()).total_seconds() / 3600.0, 3)

    all_samples_matched = bool(len(matched_acquired_s) == len(summary_df) and summary_df["matched_bool"].all())
    resolved = bool(all_samples_matched and span_hours is not None and span_hours <= window_hours)
    if resolved:
        reason = "sample_matches_within_window"
    elif not all_samples_matched:
        reason = "not_all_sample_chips_matched"
    else:
        reason = "sample_match_times_exceed_window"

    sample_cols = [
        "event",
        "chip_id",
        "matched",
        "matched_item_id",
        "matched_acquired",
        "matched_time_delta_hours",
        "match_rank",
        "best_item_id",
    ]
    sample_cols = [c for c in sample_cols if c in summary_df.columns]
    out_d = {
        "event": event,
        "resolved": resolved,
        "reason": reason,
        "sample_count": int(len(summary_df)),
        "matched_sample_count": int(len(matched_acquired_s)),
        "resolution_window_hours": window_hours,
        "selected_event_datetime": selected_dt.isoformat() if selected_dt is not None else None,
        "sample_fetch_time_span_hours": span_hours,
        "sample_chips": summary_df["chip_id"].astype(str).tolist() if "chip_id" in summary_df.columns else [],
        "sample_records": summary_df[sample_cols].where(pd.notna(summary_df[sample_cols]), None).to_dict("records"),
    }
    output_fp.write_text(json.dumps(out_d, indent=2))


if "snakemake" in globals():
    main(snakemake)
