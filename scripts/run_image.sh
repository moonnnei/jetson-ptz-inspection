#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
[ "$#" -ge 1 ] || { echo 'Usage: run_image.sh IMAGE [OUTPUT_PREFIX] [LOOPS=5] [THREADS=4]' >&2; exit 2; }
OUTPUT_PREFIX="${2:-var/results/image}"
mkdir -p "$(dirname "$OUTPUT_PREFIX")"
exec ./build/infer models/yolo26n/model.ncnn.param models/yolo26n/model.ncnn.bin \
    models/yolo26n/classes.txt "$1" "$OUTPUT_PREFIX" "${3:-5}" "${4:-4}"
