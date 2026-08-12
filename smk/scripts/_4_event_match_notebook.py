"""Render one event-level match notebook and HTML report."""

from pathlib import Path
import base64, io, json, logging, sys, traceback

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import nbformat


def main_4_event_match_notebook(snakemake):
    """Execute the event report template and write notebook plus lightweight HTML."""
    log_fp = Path(snakemake.log[0])
    log_fp.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("event_match_notebook")
    log.handlers.clear()
    log.setLevel(logging.DEBUG)
    log.propagate = False
    file_handler = logging.FileHandler(log_fp, mode="w")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(file_handler)
    path_d = {
        "template_fp": Path(snakemake.input.template_fp),
        "ipynb_fp": Path(snakemake.output.ipynb_fp),
        "html_fp": Path(snakemake.output.html_fp),
    }
    path_d["ipynb_fp"].parent.mkdir(parents=True, exist_ok=True)
    event = str(snakemake.params.event)
    diagnostics_fp_l = [str(Path(fp)) for fp in snakemake.input.diagnostics_fp_l]
    status_fp_l = [str(Path(fp)) for fp in snakemake.input.status_fp_l]
    log.info(f"rendering event notebook for {event} with {len(diagnostics_fp_l):,} chip(s)")

    nb = nbformat.read(path_d["template_fp"], as_version=4)
    parameter_source = "\n".join([
        f"EVENT = {event!r}",
        f"DIAGNOSTICS_FP_L = {diagnostics_fp_l!r}",
        f"STATUS_FP_L = {status_fp_l!r}",
        f"OUTPUT_DIR = {str(Path(snakemake.params.out_dir))!r}",
    ])
    nb.cells[1].source = parameter_source

    def _display(obj):
        """Provide a minimal notebook display replacement for direct execution."""
        if hasattr(obj, "to_string"):
            print(obj.to_string(index=False))
        else:
            print(obj)

    namespace_d = {"__name__": "__event_match_report__", "display": _display}
    html_part_l = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        f"<title>{event} match report</title>",
        "<style>body{font-family:Arial,sans-serif;margin:24px;max-width:1400px}"
        "pre{background:#f6f8fa;padding:12px;overflow:auto}"
        "table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #ddd;padding:4px 8px}"
        "img{max-width:100%;height:auto}h1,h2,h3{margin-top:1.4em}</style>",
        "</head><body>",
    ]
    for cell_i, cell in enumerate(nb.cells):
        cell.outputs = []
        if cell.cell_type == "markdown":
            source = "".join(cell.source)
            if source.startswith("# "):
                html_part_l.append(f"<h1>{source[2:].strip()}</h1>")
            else:
                html_part_l.append(f"<p>{source}</p>")
            continue
        if cell.cell_type != "code":
            continue
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        fig_start = set(plt.get_fignums())
        try:
            sys.stdout, sys.stderr = stdout, stderr
            exec(cell.source, namespace_d)
        except Exception:
            tb = traceback.format_exc()
            cell.outputs.append(nbformat.v4.new_output("error", ename="ExecutionError", evalue=tb, traceback=tb.splitlines()))
            log.error(f"cell {cell_i} failed\n{tb}")
            raise
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        if stdout.getvalue():
            cell.outputs.append(nbformat.v4.new_output("stream", name="stdout", text=stdout.getvalue()))
            html_part_l.append(f"<pre>{stdout.getvalue()}</pre>")
        if stderr.getvalue():
            cell.outputs.append(nbformat.v4.new_output("stream", name="stderr", text=stderr.getvalue()))
            html_part_l.append(f"<pre>{stderr.getvalue()}</pre>")
        for fig_num in sorted(set(plt.get_fignums()) - fig_start):
            fig = plt.figure(fig_num)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            cell.outputs.append(nbformat.v4.new_output("display_data", data={"image/png": data}, metadata={}))
            html_part_l.append(f"<p><img src='data:image/png;base64,{data}'></p>")
            plt.close(fig)

    nbformat.write(nb, path_d["ipynb_fp"])
    html_part_l.append("</body></html>")
    path_d["html_fp"].write_text("\n".join(html_part_l) + "\n")
    log.info(f"wrote notebook\n    {path_d['ipynb_fp']}")
    log.info(f"wrote html\n    {path_d['html_fp']}")


if "snakemake" in globals():
    main_4_event_match_notebook(snakemake)
