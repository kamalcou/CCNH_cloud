import subprocess
import os
import json
import shutil
import logging
import argparse
from pathlib import Path

import pandas as pd
import xarray as xr
import hvplot.pandas
from bokeh.io import output_notebook
import teehr
import ngiab_utils
import yaml

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── Argument Parsing ──────────────────────────────────────────
parser = argparse.ArgumentParser(description="NGIAB Pipeline: Download, Run, Evaluate")
parser.add_argument("--hydrofabric-id",  type=str, required=True,  help="Hydrofabric/gage ID (e.g. gage-10109001)")
parser.add_argument("--start-date",      type=str, default="2002-01-01", help="Start date YYYY-MM-DD")
parser.add_argument("--end-date",        type=str, default="2022-12-31", help="End date YYYY-MM-DD")
#parser.add_argument("--precip-sources",  type=str, nargs="+",
#                    default=["aorc", "nldas2", "stage4", "imerg"],
#                    help="Precip sources to run")
#parser.add_argument("--spatial-agg", type=str, nargs="+",
#                    default=["zonal_distributed", "zonal_lumped", "cluster_distributed", "cluster_lumped"],
#                    help="Spatial aggregation to run")
parser.add_argument("--download",        action="store_true", help="Run preprocessing/download step")
parser.add_argument("--run",             action="store_true", help="Run the NGIAB model + routing")
parser.add_argument("--evaluate",        action="store_true", help="Run TEEHR evaluation")
parser.add_argument("--all",             action="store_true", help="Run all steps")
args = parser.parse_args()

if args.all:
    args.download = args.run = args.evaluate = True

# ── Configuration ─────────────────────────────────────────────
hydrofabric_id  = args.hydrofabric_id
start_date      = pd.to_datetime(args.start_date)
end_date        = pd.to_datetime(args.end_date)
#  download data
# start_date      = args.start_date
# end_date        = args.end_date
#precip_sources  = args.precip_sources
#spatial_agg = args.spatial_agg

host_data_path  = Path(f"~/ngiab_preprocess_output/{hydrofabric_id}")
image_name      = "quay.io/awiciroh/ngiab-2i2c:v1.2.3"
num_cpus        = int(os.cpu_count())   

TEEHR_DIR       = host_data_path / "teehr"
CACHE_DIR       = TEEHR_DIR / "cache"
TEMP_DIR        = TEEHR_DIR
TEMP_DIR.mkdir(parents=True, exist_ok=True)

#CONFIGS         = spatial_agg
REALIZATION     = host_data_path / "config/realization.json"

# ── Helpers ───────────────────────────────────────────────────
def run_command(cmd: str, step: str) -> bool:
    """Run a shell command and return True if successful."""
    result = subprocess.run(
        cmd, shell=True, executable="/bin/bash",
        capture_output=True, text=True
    )
    print(result.stdout)
    print(result.stderr)
    if result.returncode != 0:
        logger.error(f"{step} failed.")
        return False
    return True


def update_realization(realization_path: Path, start_date: str, end_date: str, yaml_path: Path = None):
    """Update forcing path and output_root in realization.json."""
    with open(realization_path, "r") as f:
        data = json.load(f)

    #data["global"]["forcing"]["path"] = f"./forcings/{precip_source}/forcings_{config}.nc"
    #data["output_root"]               = "./outputs/ngen"
    
    data["time"]["start_time"] = start_date
    data["time"]["end_time"] = end_date
    
    # Optional Parameter Injection (Only executes if yaml_path is provided)
    if yaml_path is not None:
        yaml_path = Path(yaml_path)
        if yaml_path.exists():
            with open(yaml_path, "r") as yf:
                best_params_data = yaml.safe_load(yf)
            
            new_params = best_params_data.get("parameters", {})
            
            # Navigate to the modules list inside the first formulation
            try:
                formulations = data["global"].get("formulations", [])
                if formulations and "modules" in formulations[0]["params"]:
                    modules = formulations[0]["params"]["modules"]
                    
                    # Iterate through models (SLOTH, NoahOWP, CFE) to update their model_params
                    updated_count = 0
                    for module in modules:
                        mod_params = module.get("params", {})
                        if "model_params" in mod_params:
                            # Safely map matching parameters into this module's config
                            for param_name, param_value in new_params.items():
                                if param_name in mod_params["model_params"]:
                                    mod_params["model_params"][param_name] = param_value
                                    updated_count += 1
                                    
                    logger.info(f"Successfully injected {updated_count} calibration parameters into BMI modules.")
                else:
                    logger.warning("Could not find the formulations -> modules layer in realization.json.")
            except (KeyError, IndexError) as e:
                logger.error(f"Failed parsing the realization nested structure: {e}")
        else:
            logger.warning(f"yaml_path was provided but file not found at: {yaml_path}. Skipping parameter injection.")

    with open(realization_path, "w") as f:
        json.dump(data, f, indent=4)

    logger.info(f"Updated realization.json")


def generate_partitions(host_data_path: Path, image_name: str, num_cpus: int, hydrofabric_id: str) -> int:
    """Generate local partitions file and return actual number of partitions created."""
    logger.info(f"Generating partitions for {num_cpus} CPUs...")

   
    result = subprocess.run(
        f"""docker run --rm \
        -v {host_data_path}:/ngen/ngen/data \
        -v {host_data_path}:/workspace \
        -w /workspace \
        {image_name} \
        python /dmod/utils/partitioning/local_only_partitions.py \
        ./config/{hydrofabric_id}_subset.gpkg \
        {num_cpus} .""",
        shell=True,
        executable="/bin/bash",
        capture_output=True,
        text=True
    )


    print(result.stdout)
    print(result.stderr)

    if result.returncode != 0:
        logger.error("Partition generation failed.")
        return 0

    # Last line of output is the actual number of partitions generated
    actual_partitions = int(result.stdout.strip().split("\n")[-1])
    logger.info(f"Generated {actual_partitions} partitions.")
    return actual_partitions


def move_outputs(host_data_path: Path, tag: str = None):
    """Move ngen and troute outputs to precip/config specific folders."""
    tag = tag

    src_ngen   = host_data_path / "outputs/ngen"
    src_troute = host_data_path / "outputs/troute"
    dst_ngen   = host_data_path / f"results/{tag}/"
    dst_troute = host_data_path / f"results/{tag}/"

    dst_ngen.mkdir(parents=True, exist_ok=True)
    dst_troute.mkdir(parents=True, exist_ok=True)

    if src_ngen.exists():
        shutil.move(str(src_ngen), str(dst_ngen))
        logger.info(f"Moved outputs/ngen → outputs/{tag}/ngen")
    else:
        logger.warning(f"outputs/ngen not found, skipping move.")

    if src_troute.exists():
        shutil.move(str(src_troute), str(dst_troute))
        logger.info(f"Moved outputs/troute → outputs/{tag}/troute")
    else:
        logger.warning(f"outputs/troute not found, skipping move.")


# ── Step 1: Download / Preprocessing ──────────────────────────
if args.download:
    logger.info(f"Step 1: Preprocessing for {hydrofabric_id}...")

    ok = run_command(
        f"source .venv/bin/activate && yes y | uvx ngiab-prep -i {hydrofabric_id} -sfr "
        f"--start '{start_date:%Y-%m-%d}' --end '{end_date:%Y-%m-%d}' --source aorc",
        step="Preprocessing"
    )
    if not ok:
        exit(1)

    logger.info("Preprocessing complete.")


# ── Step 2: Run Model + Routing ────────────────────────────────
if args.run:
    # Generate partitions once per gage before the config loop
    num_partitions = generate_partitions(host_data_path, image_name, num_cpus, hydrofabric_id)
    if num_partitions == 0:
        logger.error("Aborting — partition generation failed.")
        exit(1)

    #for precip_source in precip_sources:
    #    for config in CONFIGS:
    #tag = f"{precip_source}_{config}"
    tag = f"docker_output_{hydrofabric_id}"
    #logger.info(f"── Running config: {tag} ──")

    # 2a. Update realization.json with correct forcing path
    BEST_PARAMS     = None
    update_realization(REALIZATION, f"{start_date}", f"{end_date}", yaml_path=BEST_PARAMS)
            
    # 2b. Create outputs/ngen directory before running
    outputs_dir = host_data_path / "outputs/ngen"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    troute_dir = host_data_path / "outputs/troute"
    troute_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Created outputs directory: {outputs_dir}")

    # 2b. Run ngen via Singularity runscript in auto mode
    # auto {num_partitions} local → uses local_only_partitions, finds existing partitions file
    logger.info(f"Running ngen [{tag}]...")
    ok = run_command(
                f"docker run --rm "
                f"-v /local:/local "
                f"-v /home/exouser:/home/exouser "
                f"-w {Path.cwd()} "
                f"-e NGIAB_HOME=/home/exouser "
                f"{image_name} /ngen/.venv/bin/python run_model.py",
                step=f"ngen [{tag}]"
    )
    if not ok:
        logger.error(f"Skipping routing for {tag} due to ngen failure.")
        #continue

    # 2c. Run routing model
    #logger.info(f"Running route_rs [{tag}]...")
    #ok = run_command(
    #            f"route_rs {host_data_path}",
    #            step=f"route_rs [{tag}]"
    #        )
    #        if not ok:
    #            logger.error(f"Routing failed for {tag}, outputs will not be moved.")
    #            continue

            # 2d. Move outputs to config-specific folders
    move_outputs(host_data_path, tag)

    logger.info("All model runs complete.")