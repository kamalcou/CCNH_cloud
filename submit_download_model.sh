#!/bin/bash


mkdir -p logs

# # ── Hydrofabric IDs ───────────────────────────────────────────

HYDROFABRIC_IDS=(
    "gage-02464000"
    "gage-02361000"
    "gage-02469800"
    "gage-03574500"
)

# module load OpenMPI netCDF Apptainer
cd "$HOME/CCNH_cloud/"
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ── Run all steps for each gage, in parallel ──────────────────
# precip-sources: aorc stage4 nldas2 imerg
# spatial-agg: distributed lumped
pids=()
for HYDROFABRIC_ID in "${HYDROFABRIC_IDS[@]}"; do
    echo "Launching $HYDROFABRIC_ID → logs/${HYDROFABRIC_ID}.log"
    python NextGen_Run_all_test_updated.py \
        --hydrofabric-id  "$HYDROFABRIC_ID" \
        --start-date      "2020-01-01" \
        --end-date        "2022-12-31" \
        --download \
        > "logs/${HYDROFABRIC_ID}.log" 2>&1 &
    pids+=($!)
done

status=0
for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
        echo "FAILED: ${HYDROFABRIC_IDS[$i]} (see logs/${HYDROFABRIC_IDS[$i]}.log)"
        status=1
    else
        echo "DONE: ${HYDROFABRIC_IDS[$i]}"
    fi
done

exit $status

