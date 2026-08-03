#!/bin/bash

mkdir -p logs

cd "$HOME/CCNH_cloud/"

IMAGE_NAME="quay.io/awiciroh/ngiab-2i2c:v1.2.3"
IMAGE_NAME="ngiab-2i2c:arm64"
HYDROFABRIC_IDS=(
    "gage-02464000"
    "gage-02361000"
    "gage-02469800"
    "gage-03574500"
)

# ── Run the model (run_model.py) inside the container ──────────
# run_model.py is SEQUENTIAL by design (each gage already spreads across
# all available cores via MPI), so gages are run one by one, in a loop
# here, rather than in parallel.
#
# Loaded via exec(open(...).read()) rather than `python run_model.py`:
# this image's python refuses to open bind-mounted paths through its
# main-script loader (io.open_code) even though plain open()/os.open()
# on the same path succeed — likely a hook restricting script loading
# to paths baked into the image.
overall_status=0
for HYDROFABRIC_ID in "${HYDROFABRIC_IDS[@]}"; do
    log_file="logs/run_model_${HYDROFABRIC_ID}.log"
    echo "Running model [$HYDROFABRIC_ID] → $log_file"

    start_ts=$(date +%s)
    docker run --rm \
        -v "$HOME/ngiab_preprocess_output/:$HOME/ngiab_preprocess_output/" \
        -v "$(pwd):$(pwd)" \
        -w "$(pwd)" \
        -e NGIAB_HOME=$HOME \
        "$IMAGE_NAME" /ngen/.venv/bin/python -c \
            "exec(compile(open('run_model.py').read(), 'run_model.py', 'exec'))" \
            --hydrofabric-id "$HYDROFABRIC_ID" \
        > "$log_file" 2>&1
    status=$?
    end_ts=$(date +%s)
    elapsed=$((end_ts - start_ts))
    printf -v elapsed_fmt '%02d:%02d:%02d' $((elapsed/3600)) $((elapsed%3600/60)) $((elapsed%60))

    if [ $status -ne 0 ]; then
        echo "FAILED: $HYDROFABRIC_ID after $elapsed_fmt (see $log_file)"
        overall_status=1
    else
        echo "DONE: $HYDROFABRIC_ID in $elapsed_fmt"
    fi
    echo "$HYDROFABRIC_ID  $elapsed_fmt  status=$status" >> logs/run_times.log
done

exit $overall_status
