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


def main(snakemake):
    report_fp = Path(snakemake.output.report_fp)
    report_fp.parent.mkdir(parents=True, exist_ok=True)
    summary_fp_l = [Path(p) for p in snakemake.input.summary_fp_l]
    summary_df = pd.concat([pd.read_csv(fp) for fp in summary_fp_l], ignore_index=True)
    summary_df["matched_bool"] = summary_df["matched"].fillna(False).astype(bool)
    plot_fp_l = [Path(p) for p in snakemake.input.plot_fp_l]
    plot_by_key = {(p.parents[1].name, p.parent.name): p for p in plot_fp_l}

    count_df = summary_df.groupby("event")["matched_bool"].agg(chips="count", matched="sum").sort_index()
    count_df["nomatch"] = count_df["chips"] - count_df["matched"]

    line_l = [
        f"# {snakemake.params.title}",
        "",
        f"Summary sources: {len(summary_fp_l)} selected per-chip `summary.csv` files",
        "",
        "## Counts",
        "",
        "| Event | Chips | Match | Nomatch |",
        "|---|---:|---:|---:|",
    ]
    for event, row_s in count_df.iterrows():
        line_l.append(f"| {event} | {int(row_s['chips'])} | {int(row_s['matched'])} | {int(row_s['nomatch'])} |")

    line_l.extend(["", "## Chips", ""])
    for event, event_df in summary_df.sort_values(["event", "chip_id"]).groupby("event", sort=True):
        line_l.extend([f"### {event}", ""])
        for _, row_s in event_df.iterrows():
            chip_id = row_s["chip_id"]
            title = f"{event} {chip_id}"
            anchor = _anchor(title)
            status = "match" if bool(row_s["matched_bool"]) else "nomatch"
            plot_fp = plot_by_key.get((event, chip_id))
            if plot_fp is None:
                line_l.append(f"- [{chip_id}](#{anchor}) - {status} - plot missing")
            else:
                rel_plot = _rel(plot_fp, report_fp.parent)
                line_l.append(f"- [{chip_id}](#{anchor}) - {status} - [png]({rel_plot})")
        line_l.append("")

    line_l.extend(["## Plots", ""])
    for event, event_df in summary_df.sort_values(["event", "chip_id"]).groupby("event", sort=True):
        line_l.extend([f"### {event} Plots", ""])
        for _, row_s in event_df.iterrows():
            chip_id = row_s["chip_id"]
            title = f"{event} {chip_id}"
            status = "match" if bool(row_s["matched_bool"]) else "nomatch"
            plot_fp = plot_by_key.get((event, chip_id))
            line_l.extend([f"#### {title}", "", f"Status: `{status}`", ""])
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
