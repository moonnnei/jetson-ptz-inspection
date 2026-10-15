#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NCNN_ROOT="$PROJECT_DIR/third_party"
if [ "${1:-}" = "--ncnn-root" ]; then
    [ "$#" -eq 2 ] || { echo 'Usage: build_tx2.sh [--ncnn-root EXISTING_ROOT]' >&2; exit 2; }
    NCNN_ROOT="$(cd "$2" && pwd)"
elif [ "$#" -ne 0 ]; then
    echo 'Usage: build_tx2.sh [--ncnn-root EXISTING_ROOT]' >&2; exit 2
fi
python3 "$PROJECT_DIR/scripts/verify_models.py"
if [ ! -f "$NCNN_ROOT/ncnn-build/src/libncnn.a" ]; then
    [ -d "$NCNN_ROOT/ncnn-master" ] || { echo 'Run python3 scripts/fetch_ncnn.py first.' >&2; exit 1; }
    # The fixed source uses one comparison introduced after CMake 3.5.
    python3 - "$NCNN_ROOT/ncnn-master/src/CMakeLists.txt" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]);s=p.read_text()
old='if(ANDROID_NDK_MAJOR AND (ANDROID_NDK_MAJOR GREATER_EQUAL 23))'
new='if(ANDROID_NDK_MAJOR AND (NOT ANDROID_NDK_MAJOR LESS 23))'
if old in s:
    assert s.count(old)==1
    p.write_text(s.replace(old,new))
elif new not in s:
    raise RuntimeError('Unexpected NCNN source revision')
PY
    mkdir -p "$NCNN_ROOT/ncnn-build"
    (
        cd "$NCNN_ROOT/ncnn-build"
        cmake ../ncnn-master -DCMAKE_BUILD_TYPE=Release -DNCNN_VERSION=20260924 \
            -DNCNN_VULKAN=OFF -DNCNN_BUILD_EXAMPLES=OFF -DNCNN_BUILD_TOOLS=OFF \
            -DNCNN_BUILD_BENCHMARK=OFF -DNCNN_BUILD_TESTS=OFF
        make -j2
    )
fi
mkdir -p "$PROJECT_DIR/build"
cd "$PROJECT_DIR/build"
cmake .. -DCMAKE_BUILD_TYPE=Release -DNCNN_ROOT="$NCNN_ROOT"
make -j2
