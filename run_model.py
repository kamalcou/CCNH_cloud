#!/usr/bin/env python
"""
Batch NGIAB model runs over multiple gages.

Runs are SEQUENTIAL by design: each ngen-parallel run already spawns as many
MPI ranks as there are cores, so running gages concurrently would oversubscribe
the node. Downloads can be parallel (network-bound); model runs cannot.

Run with the container venv python (the model libs live in the image). Note:
this image's python can't load a bind-mounted file as its main script
(io.open_code is blocked for bind mounts here, even though open()/os.open()
work fine), so run it via exec(open(...).read()) instead of a plain path:

    docker run --rm \
        -v /home/exouser:/home/exouser \
        -w /home/exouser/CCNH_cloud/notebooks \
        -e NGIAB_HOME=/home/exouser \
        quay.io/awiciroh/ngiab-2i2c:v1.2.3 /ngen/.venv/bin/python -c \
        "exec(compile(open('run_model.py').read(), 'run_model.py', 'exec'))"

Redirect data location elsewhere via the NGIAB_HOME env var:

    docker run --rm \
        -v /scratch/mhchowdhury:/scratch/mhchowdhury \
        -w /home/exouser/CCNH_cloud/notebooks \
        -e NGIAB_HOME=/scratch/mhchowdhury \
        quay.io/awiciroh/ngiab-2i2c:v1.2.3 /ngen/.venv/bin/python -c \
        "exec(compile(open('run_model.py').read(), 'run_model.py', 'exec'))"
"""

import os
import sys
import json
import multiprocessing
from pathlib import Path
import geopandas as gpd
from pyngiab import PyNGIAB

# --- MPI/Hydra environment fixes: set BEFORE anything spawns mpirun ---
# Force Hydra's local fork launcher and strip Slurm vars so it can't try to
# spawn ranks via `srun` (which doesn't exist inside the container).
os.environ["HYDRA_LAUNCHER"] = "fork"
os.environ["HYDRA_BOOTSTRAP"] = "fork"
for _k in list(os.environ):
    if _k.startswith("SLURM"):
        del os.environ[_k]



# --------------------------- Config ---------------------------
HYDROFABRIC_IDS = [
    "gage-02464000",
    "gage-02361000",
    "gage-02469800",
    "gage-03574500",
]

# Rank count = min(cores available to this process, catchments in the gage).
# We want the MAX usable processors, but the count must never exceed the
# catchment count or partitionGenerator produces empty/broken partitions
# (this is what the original partitions_96.json crash was: 96 ranks, 66 cats).
# sched_getaffinity reflects the cores actually granted to us (respects cgroups
# and Slurm allocations); fall back to cpu_count where it's unavailable.
try:
    TRUE_CORES = len(os.sched_getaffinity(0))
except AttributeError:
    TRUE_CORES = os.cpu_count() or 1
print(f"Detected {TRUE_CORES} cores available to this process", flush=True)

# Rewrite each realization's time block to match the forcing file's actual
# coverage, preventing the "run past end of forcing" crash. Set False to trust
# the realization's existing time block as-authored.
ALIGN_TIME_TO_FORCING = True

NGIAB_HOME = Path(os.environ.get("NGIAB_HOME", Path.home()))
OUTPUT_ROOT = NGIAB_HOME / "ngiab_preprocess_output"


def set_ranks(n):
    """Pin the rank count PyNGIAB will read from cpu_count() for the next run."""
    os.cpu_count = lambda: n
    multiprocessing.cpu_count = lambda: n


def prepare_realization(gid, data_dir, forcing_real):
    """Make forcing path absolute and (optionally) align time block to forcing."""
    rpath = data_dir / "config" / "realization.json"
    r = json.load(open(rpath))

    # Absolute forcing path -> resolves regardless of ngen's working directory.
    r["global"]["forcing"]["path"] = str(forcing_real)

    if ALIGN_TIME_TO_FORCING:
        try:
            import xarray as xr
            ds = xr.open_dataset(str(forcing_real))
            tname = next((c for c in ds.coords if "time" in c.lower()), None)
            if tname is not None:
                import pandas as pd
                t0 = pd.Timestamp(ds[tname].values.min())
                t1 = pd.Timestamp(ds[tname].values.max())
                fmt = "%Y-%m-%d %H:%M:%S"
                r["time"]["start_time"] = t0.strftime(fmt)
                r["time"]["end_time"] = t1.strftime(fmt)
                print(f"[{gid}] time block -> {t0} .. {t1}", flush=True)
            ds.close()
        except Exception as e:
            print(f"[{gid}] WARN: could not align time block ({e}); "
                  f"leaving realization time as-is", file=sys.stderr, flush=True)

    json.dump(r, open(rpath, "w"), indent=4)


def run_gage(gid):
    data_dir = (OUTPUT_ROOT / gid).resolve()

    # gpkg present?
    gpkg = data_dir / "config" / f"{gid}_subset.gpkg"
    if not gpkg.exists():
        raise FileNotFoundError(f"geopackage missing: {gpkg}")

    ndiv = len(gpd.read_file(gpkg, layer="divides"))
    ranks = max(1, min(TRUE_CORES, ndiv))   # max usable, never exceeding catchments
    set_ranks(ranks)
    print(f"[{gid}] {ndiv} catchments, {TRUE_CORES} cores -> {ranks} ranks", flush=True)

    # forcings.nc resolves to a real file? (dangling symlink -> ngen NcException)
    forcing = data_dir / "forcings" / "forcings.nc"
    forcing_real = Path(os.path.realpath(forcing))
    if not forcing_real.exists():
        raise FileNotFoundError(
            f"forcings.nc does not resolve to a real file: {forcing} -> {forcing_real}"
        )

    # patch realization (absolute forcing path + time alignment)
    prepare_realization(gid, data_dir, forcing_real)

    # remove stale partition files from prior failed runs
    for pf in data_dir.glob("partitions_*.json"):
        pf.unlink()
        print(f"[{gid}] removed stale {pf.name}", flush=True)

    # run from data_dir so any remaining relative paths resolve correctly
    os.chdir(data_dir)
    test_ngiab = PyNGIAB(str(data_dir), serial_execution_mode=False)
    test_ngiab.run()
    print(f"[{gid}] run complete; outputs in {data_dir}/outputs", flush=True)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Run NGIAB model for one or more gages.")
    ap.add_argument("--hydrofabric-id", action="append", default=None,
                    help="Gage/catchment/VPU id. Repeatable. "
                         "If omitted, runs the built-in HYDROFABRIC_IDS list.")
    ap.add_argument("--start-date", default=None,
                    help="Optional; only used to log intent. Time block is aligned "
                         "to the forcing file's actual coverage.")
    ap.add_argument("--end-date", default=None,
                    help="Optional; see --start-date.")
    ap.add_argument("--run", action="store_true",
                    help="Accepted for CLI compatibility; running is the default.")
    args = ap.parse_args()

    gages = args.hydrofabric_id if args.hydrofabric_id else HYDROFABRIC_IDS
    if args.start_date or args.end_date:
        print(f"(requested window {args.start_date}..{args.end_date}; "
              f"actual time block aligned to forcing per gage)", flush=True)

    results, failures = [], []
    for gid in gages:
        print(f"\n{'='*70}\n[{gid}] starting\n{'='*70}", flush=True)
        try:
            run_gage(gid)
            results.append(gid)
        except Exception as e:
            failures.append((gid, e))
            print(f"[{gid}] FAILED: {e}", file=sys.stderr, flush=True)

    print(f"\nDone. {len(results)} succeeded, {len(failures)} failed.")
    for gid in results:
        print(f"  OK     {gid}")
    for gid, e in failures:
        print(f"  FAILED {gid}: {str(e).splitlines()[0]}")

    # non-zero exit if anything failed, so Slurm marks the array task FAILED
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()