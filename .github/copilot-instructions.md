# ESMR Data Project Guidelines

## Working Style

- **Conda environment**: Always use `conda activate esmr` before running any terminal commands — tests, installs, CLI runs, etc.
- **Shell**: Always use **cmd** (Command Prompt), not PowerShell. Prefix commands with `conda activate esmr &&` when a fresh terminal is needed.
- **Ask before doing**: Always ask clarifying questions before starting work on a task. Confirm intent if the request is ambiguous.
- **Approval before implementation**: Always present a plan and get explicit user approval before writing or modifying any files or running mutating commands.

This repo downloads large (~8 GB+) zipped CSV files from the [CIWQS website](https://ciwqs.waterboards.ca.gov/), parses ESMR (Environmental Surveillance and Monitoring Report) monitoring data, filters it for Delta-area POTWs (publicly owned treatment works), and serves an interactive dashboard. Primary stack: Python, pandas, Click, Panel/hvplot.

## Build and Test

```bash
pip install -e .          # editable install
pytest                    # run tests (outputs junit.xml)
flake8 --max-line-length=100  # lint (ignores E122,E123,E126,E127,E128,E731,E722)
esmr_data show_dash <esmr_csv_file>  # launch dashboard
```

Download + extract pipeline:
```bash
python -m esmr_data.esmr_extract_potws --url <ciwqs_url> --config esmr_data/filter_conditions.yaml --extract_to ./output
python -m esmr_data.esmr_extract_potws --skip-download --config esmr_data/filter_conditions.yaml --extract_to ./output
```

## Architecture

```
esmr_extract_potws.py   download/unzip CSV → process → write filtered CSVs
esmr.py                 ESMR → Facility → Location → Parameter → Variable (dataclass hierarchy)
cli.py                  Click CLI entry point (show_dash command)
dash.py                 Panel + Holoviews interactive dashboard (map tap → time series)
ciwqs.py                CIWQS permit lookup (stub/utility)
filter_conditions.yaml  YAML config: facility → parameter → location/method/unit filters
```

**Data flow**: download zip → stream-extract CSV → `read_data_csv()` (categorical dtypes, datetime parse, pickle cache) → `ESMR` object → `ESMRDash` → browser.

## Key Conventions

- **Pickle caching**: `read_data_csv()` writes a `.pkl` beside the CSV on first load; subsequent loads skip CSV parsing. Delete the `.pkl` to force a re-parse after schema changes.
- **Categorical dtypes**: repetitive string columns (region, location_type, etc.) are cast to `category` on load — reduces memory footprint for the 8 GB+ file. Avoid converting back to `object` unnecessarily.
- **Dataclass hierarchy**: every level (`Facility`, `Location`, `Parameter`, `Variable`) holds a `.df` filtered from its parent in `__post_init__`. Add new filter logic there, not at the `ESMR` root.
- **YAML config keys**: facility and parameter names use underscores in place of spaces (e.g., `EchoWater_RWQCF` → `EchoWater RWQCF` in the data). Ensure YAML keys map correctly when adding facilities.
- **Geo-coordinate fixups**: known bad lat/lon values are corrected in `build_facility_location_lat_lon()`. Add new corrections there with a comment citing the place_id.
- **Aggregation rules**: Flow → daily `sum`; Temperature, EC, DO → daily `mean`. Enforce this in `extract_result()` when adding parameters.
- **Line length**: 100 characters (flake8 configured in `setup.cfg`).

## Potential Pitfalls

- The CSV can exceed 8 GB — always profile memory before adding non-categorical string columns.
- Web scraping in `download_and_unzip()` targets a specific HTML link text ("Zipped CSV"); if the CIWQS page changes, the scraper will fail silently with an empty URL.
- `sampling_date` + `sampling_time` are combined into `sampling_datetime`; null times default to midnight — be careful with sub-daily aggregations.
- Dashboard requires a live display (Panel `show()`); it will not render in headless environments without `panel.extension()` called first.
- `versioneer` is used for version management — do not manually edit `esmr_data/_version.py`.
