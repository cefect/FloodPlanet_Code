"""Build a Markdown report linking per-chip match plots."""

from pathlib import Path
import re

import pandas as pd


def _anchor(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9 _-]", "", text)
    return text.replace(" ", "-")


def _rel(path, base_dir):
    return Path(path).resolve().relative_to(base_dir.resolve()).as_posix()


def _bool_s(series):
    return series.fillna(False).astype(str).str.lower().isin(["true", "1", "yes"])


def _sample_key_set(snakemake):
    sample_fp = Path(snakemake.input.event_sample) if hasattr(snakemake.input, "event_sample") else None
    if sample_fp is None or not sample_fp.exists():
        return set()
    sample_df = pd.read_csv(sample_fp, sep="\t")
    return set(zip(sample_df["Event"].astype(str), sample_df["Chip_ID"].astype(str)))


def _ordered_summary(summary_df):
    summary_df = summary_df.copy()
    summary_df["sample_sort"] = (~summary_df["sample_chip_bool"]).astype(int)
    return summary_df.sort_values(["event", "sample_sort", "chip_id"])


def main(snakemake):
    report_fp = Path(snakemake.output.report_fp)
    report_fp.parent.mkdir(parents=True, exist_ok=True)
    summary_fp_l = [Path(p) for p in snakemake.input.summary_fp_l]
    summary_df = pd.concat([pd.read_csv(fp) for fp in summary_fp_l], ignore_index=True)
    summary_df["matched_bool"] = _bool_s(summary_df["matched"])
    if "sample_chip" in summary_df.columns:
        summary_df["sample_chip_bool"] = _bool_s(summary_df["sample_chip"])
    else:
        sample_key_set = _sample_key_set(snakemake)
        summary_df["sample_chip_bool"] = [
            (event, chip_id) in sample_key_set
            for event, chip_id in zip(summary_df["event"].astype(str), summary_df["chip_id"].astype(str))
        ]
    plot_fp_l = [Path(p) for p in snakemake.input.plot_fp_l]
    plot_by_key = {(p.parents[1].name, p.parent.name): p for p in plot_fp_l}

    count_df = summary_df.groupby("event")["matched_bool"].agg(chips="count", matched="sum").sort_index()
    count_df["nomatch"] = count_df["chips"] - count_df["matched"]
    sample_count_s = summary_df.groupby("event")["sample_chip_bool"].sum()

    line_l = [
        f"# {snakemake.params.title}",
        "",
        f"Summary sources: {len(summary_fp_l)} selected per-chip `summary.csv` files",
        "",
        "## Counts",
        "",
        "| Event | Chips | Sample chips | Match | Nomatch |",
        "|---|---:|---:|---:|---:|",
    ]
    for event, row_s in count_df.iterrows():
        sample_count = int(sample_count_s.get(event, 0))
        line_l.append(f"| {event} | {int(row_s['chips'])} | {sample_count} | {int(row_s['matched'])} | {int(row_s['nomatch'])} |")

    line_l.extend(["", "## Chips", ""])
    for event, event_df in _ordered_summary(summary_df).groupby("event", sort=True):
        line_l.extend([f"### {event}", ""])
        for _, row_s in event_df.iterrows():
            chip_id = row_s["chip_id"]
            title = f"{event} {chip_id}"
            anchor = _anchor(title)
            status = "match" if bool(row_s["matched_bool"]) else "nomatch"
            sample_label = " `sample`" if bool(row_s["sample_chip_bool"]) else ""
            plot_fp = plot_by_key.get((event, chip_id))
            if plot_fp is None:
                line_l.append(f"- [{chip_id}](#{anchor}){sample_label} - {status} - plot missing")
            else:
                rel_plot = _rel(plot_fp, report_fp.parent)
                line_l.append(f"- [{chip_id}](#{anchor}){sample_label} - {status} - [png]({rel_plot})")
        line_l.append("")

    line_l.extend(["## Plots", ""])
    for event, event_df in _ordered_summary(summary_df).groupby("event", sort=True):
        line_l.extend([f"### {event} Plots", ""])
        for _, row_s in event_df.iterrows():
            chip_id = row_s["chip_id"]
            title = f"{event} {chip_id}"
            status = "match" if bool(row_s["matched_bool"]) else "nomatch"
            sample_label = " sample" if bool(row_s["sample_chip_bool"]) else ""
            plot_fp = plot_by_key.get((event, chip_id))
            line_l.extend([f"#### {title}", "", f"Status: `{status}`{sample_label}", ""])
            if plot_fp is not None:
                rel_plot = _rel(plot_fp, report_fp.parent)
                line_l.extend([f"[![{title}]({rel_plot})]({rel_plot})", ""])
            else:
                line_l.extend(["Plot missing.", ""])
            line_l.append("[Back to chips](#chips)")
            line_l.append("")

    report_fp.write_text("\n".join(line_l) + "\n")
    Path(snakemake.log[0]).write_text(f"wrote report to {report_fp}\nplots={len(plot_fp_l)}\n")


if "snakemake" in globals():
    main(snakemake)
