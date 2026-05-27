#!/bin/bash
# 00c_fetch_nanosim_model.sh — download NanoSim pre-trained cDNA model.
#
# NanoSim's biocontainer ships the simulator binary but not the training
# artifacts. Those live in the NanoSim GitHub repo's pre-trained_models/
# folder as tarballs. This script downloads the model we use for cDNA
# simulation in phase f and extracts it under $SANDBOX/nanosim_models/.
#
# Caveat — chemistry mismatch: the HGSOC cohort uses R10.4.1, but NanoSim's
# pre-trained cDNA models only cover R9.4.1 (human_NA12878_cDNA_Bham1_guppy,
# basecalled with guppy). The cDNA-vs-DNA distinction matters more than
# R9-vs-R10 for exercising the casetrack multi-assay QC paths, so we use
# the R9.4.1 cDNA model and flag the caveat in containers/README.md.
#
# Author: Samuel Ahuno (ekwame001@gmail.com)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

SANDBOX="${SANDBOX:-$REPO_ROOT/sandbox/hgsoc_sim}"
MODELS_DIR="$SANDBOX/nanosim_models"
mkdir -p "$MODELS_DIR"

# Model selection. Override with NANOSIM_MODEL=... to use a different model.
MODEL_NAME="${NANOSIM_MODEL:-human_NA12878_cDNA_Bham1_guppy}"
MODEL_URL="https://github.com/bcgsc/NanoSim/raw/master/pre-trained_models/${MODEL_NAME}.tar.gz"
MODEL_TGZ="$MODELS_DIR/${MODEL_NAME}.tar.gz"
MODEL_DIR="$MODELS_DIR/${MODEL_NAME}"

# Short-circuit if the model is already extracted.
if [[ -d "$MODEL_DIR" ]] && ls "$MODEL_DIR" | grep -qE '(training|aligned_region|_model_profile)' ; then
    echo "[00c] NanoSim model already installed: $MODEL_DIR"
    exit 0
fi

if [[ ! -s "$MODEL_TGZ" ]]; then
    echo "[00c] downloading $MODEL_URL"
    curl -sSL "$MODEL_URL" -o "$MODEL_TGZ"
fi

echo "[00c] extracting $MODEL_TGZ into $MODELS_DIR"
# The tarball layout inside varies by model. Extract to MODEL_DIR; if the
# archive is already wrapped in a top-level directory with the model name,
# that resolves to $MODELS_DIR/$MODEL_NAME/$MODEL_NAME/... — we flatten
# afterwards.
mkdir -p "$MODEL_DIR"
tar -xzf "$MODEL_TGZ" -C "$MODEL_DIR" --strip-components=0

# If the archive had a single top-level directory matching MODEL_NAME, flatten it.
nested="$MODEL_DIR/$MODEL_NAME"
if [[ -d "$nested" ]]; then
    shopt -s dotglob
    mv "$nested"/* "$MODEL_DIR/"
    rmdir "$nested"
    shopt -u dotglob
fi

echo "[00c] model ready at $MODEL_DIR"
printf "[00c] files: "
ls -1 "$MODEL_DIR" | tr '\n' ' '
echo
