create a plan to implement 'fetch/PS/planet_sdk_orders_match_search_poc.ipynb' for each tile in `stac_catalog.geojson`
use a python script with CLI entrypoint and nice progress reporting and file logging
keep it simple with minimal parrelization/optimization for now. readability is more important than optimization here. 
use an out_dir kwarg defaulting to /_outputs
outputs should be structured like <event>/<chip_id>/<id>.tif along with a manifest.json for each id. 
keep rasters in a format most true server side (i.e., minimal processing, no normalization)
search for all chips on all sensors within 24 hrs of the event timestamp for 'ortho_analytic_4b_sr'
only fetch those with complete coverage of the chip (i.e., exclude anything with only a partial scene of the chip)
output a per-chip summary table documenting the flow/results
no plotting
use `fetch/my_secrets.py` to setup any necessary API keys or credentials
add a section for 'open questions' if necessary

do now:
- run some tests now to validate the plan and ensure the implementation is on the right track.
- refactor and prove 'my_secrets.py' to work here
- extend plan.md
