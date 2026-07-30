# CCNH_cloud/notebooks — NGIAB pipeline (Docker, non-HPC)

This runs the NextGen-in-a-Box (NGIAB) download → run pipeline directly on a
Docker-capable VM (e.g. `ciroh-nrds`), as an alternative to the Apptainer/SLURM
path described in the top-level [`../README.md`](../README.md). It downloads
and preprocesses hydrofabric/forcing data for a set of gages, then runs the
NGEN model + routing for each.

## Prerequisites

- Python 3.12 venv at `.venv/` (created once: `python3.12 -m venv .venv`)
- Docker, with the current user in the `docker` group
- Network access to pull `quay.io/awiciroh/ngiab-2i2c:v1.2.3`

## Pipeline

Two scripts, run in order:

### 1. `./submit_download_model.sh`

- Installs/updates the venv from `requirements.txt`
- For each gage in its `HYDROFABRIC_IDS` array, launches
  `NextGen_Run_all_test_updated.py --download` **in parallel** (download is
  network-bound, so concurrent gages are safe)
- Each gage's preprocessing (`ngiab-prep -sfr`, via `uvx`) writes to
  `~/ngiab_preprocess_output/<gage-id>/` — `config/`, `forcings/`, `metadata/`,
  `outputs/`
- Per-gage logs: `logs/<gage-id>.log`

### 2. `./submit_run_model.sh`

- For each gage in its own `HYDROFABRIC_IDS` array, runs `run_model.py`
  **sequentially, one gage at a time** — each ngen run already spreads across
  every available core via MPI, so running gages concurrently would
  oversubscribe the node
- Each gage's run happens inside the `ngiab-2i2c` Docker container using the
  container's own Python/model libraries
- Per-gage logs: `logs/run_model_<gage-id>.log`
- Per-gage elapsed time is appended to `logs/run_times.log` as
  `<gage-id>  HH:MM:SS  status=<exit code>`
- Exits non-zero if any gage failed, but still runs the remaining gages

`run_model.py` also accepts `--hydrofabric-id gage-XXXXX` directly (repeatable)
if you want to invoke it outside the submit script for a subset of gages.

## Known gotchas (already fixed in these scripts)

- **`requirements.txt` must not list `ngiab_utils`** — it's a local module
  (`ngiab_utils.py`) in this directory, not a PyPI package. Listing it makes
  `pip install -r requirements.txt` fail to resolve and abort the *entire*
  install (including pandas/matplotlib/etc.), even though pip's cached-metadata
  log lines make it look like the others installed fine.
- **Dates passed to `ngiab-prep` must be `YYYY-MM-DD`**, not a full
  `pandas.Timestamp` string (which renders as `'2020-01-01 00:00:00'` and gets
  rejected). `NextGen_Run_all_test_updated.py` formats these with
  `f"{start_date:%Y-%m-%d}"` before building the `ngiab-prep` command.
- **`run_model.py` can't be loaded as Python's main script from a Docker bind
  mount** in this image — `python run_model.py` fails with `[Errno 13]
  Permission denied` even though the file is readable (`open()`/`os.open()`
  succeed; only the main-script/`io.open_code` loader is blocked). Both
  `submit_run_model.sh` and the `ngen` step in `NextGen_Run_all_test_updated.py`
  work around this by reading the source with `open()` and running it via
  `exec(compile(...))` instead of passing the path directly to `python`.
- **`/home/exouser` needs `o+x`** (traverse-only) so the container's user
  (`jovyan`, uid 1000 — not `exouser`) can reach paths under the bind-mounted
  home directory at all.
- **`~/ngiab_preprocess_output/` needs `o+rwX`** (not just read) — the model
  run rewrites `config/realization.json` in place, deletes stale
  `partitions_*.json`, and writes new output files, all as the container's
  uid.

If a fresh gage's preprocessing/run fails with a permission error, check that
both of the chmod fixes above are still in place — a new gage's freshly
created output directory can inherit different permissions than expected.

## File map

**Active pipeline**
- `submit_download_model.sh`, `submit_run_model.sh` — entry points
- `NextGen_Run_all_test_updated.py` — download/preprocess + run + evaluate
  driver (`--download` / `--run` / `--evaluate` / `--all`)
- `run_model.py` — batch NGIAB model runner (invoked inside the container)
- `ngiab_utils.py` — helper imported by `NextGen_Run_all_test_updated.py`
- `requirements.txt` — host-side Python deps

**Notebook-only (manual/interactive workflows, not part of the batch scripts)**
- `NextGen_Data_Preparation.ipynb`, `NextGen_Run.ipynb`,
  `NextGen_Calibration.ipynb`, `NextGen_Outputs_Analysis.ipynb`,
  `NextGEN_TEEHR_Evaluation.ipynb`
- `cal_utils.py` (calibration notebook), `ngen_outputs_utils.py` (outputs
  notebooks), `hydrofabric_visualization_utils.py` (data prep/outputs
  notebooks), `forcings_utils.py` (data prep notebook)

**Superseded / alternate entry points (not used by the scripts above)**
- `data_download.py` — older standalone download script; superseded by
  `NextGen_Run_all_test_updated.py --download`
- `run_docker_mac.sh` — Docker Desktop (Apple Silicon) equivalent of this
  pipeline, for local Mac use instead of this VM

**Generated (safe to clean, not source)**
- `logs/`, `__pycache__/`, `.venv/`
