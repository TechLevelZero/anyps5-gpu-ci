#!/bin/bash
# Runs inside the sandboxed runner container (no network, no capabilities).
# PHASE=build: configure, registration check, build, libs, CPU-only tests (no GPU).
# PHASE=test:  one GPU pass with Vulkan validation layers and no shader cache (GPU tests plus CPU tests that
#               failed or skipped without a GPU), retry of new failures, skip check.
# Inputs (env): PHASE SRC BUILD OUT FFMPEG_DIR JOBS TEST_PARALLEL CPU_TEST_PARALLEL VALIDATION_TESTS GPU_NAME
# Appends to OUT/steps.tsv (step, status, seconds) and writes logs and JUnit files.
set -u
mkdir -p "$OUT" "$BUILD"
JOBS=${JOBS:-0}; [ "$JOBS" -gt 0 ] 2>/dev/null || JOBS=$(nproc)

step() {
    local name=$1; shift
    local start=$SECONDS
    "$@" > "$OUT/$name.log" 2>&1
    local code=$?
    printf '%s\t%s\t%s\n' "$name" "$code" "$((SECONDS - start))" >> "$OUT/steps.tsv"
    return $code
}

if [ "$PHASE" = build ]; then
    : > "$OUT/steps.tsv"
    ccache --zero-stats > /dev/null 2>&1
    step configure cmake -S "$SRC" -B "$BUILD" -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON \
        -DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++ \
        -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
        -DANYPS5_ENABLE_SPIRV_TOOLS=ON "-DFFMPEG_PREBUILT_DIR=$FFMPEG_DIR" || exit 0
    step registration python3 "$SRC/tools/check_test_registration.py" "$BUILD"
    step build cmake --build "$BUILD" --parallel "$JOBS" -- -k 0
    BUILD_CODE=$?
    step libs cmake --build "$BUILD" --target libs --parallel "$JOBS" -- -k 0
    LIBS_CODE=$?
    ccache --show-stats > "$OUT/ccache.txt" 2>&1
    rm -f "$BUILD/select.tests" "$BUILD/select.ninja"
    if [ "${MODE:-pr}" = baseline ]; then
        step graph python3 /ci-tools/select_tests.py dump "$BUILD" "$OUT/test-graph.json"
        if [ -f "$OUT/changed.txt" ]; then
            step select python3 /ci-tools/select_tests.py select "$BUILD" "$OUT/test-graph.json" "$OUT/changed.txt"
            [ -f "$BUILD/select.tests" ] && cp "$BUILD/select.tests" "$OUT/select.tests"
        fi
    elif [ -s "${GRAPH:-}" ] && [ -f "$OUT/changed.txt" ]; then
        step select python3 /ci-tools/select_tests.py select "$BUILD" "$GRAPH" "$OUT/changed.txt"
        [ -f "$BUILD/select.tests" ] && cp "$BUILD/select.tests" "$OUT/select.tests"
    fi
    ctest --test-dir "$BUILD" -N > "$OUT/all-tests.txt" 2>&1
    if [ "$BUILD_CODE" = 0 ] && [ "$LIBS_CODE" = 0 ] && [ "${CPU_TESTS:-1}" = 1 ]; then
        SELECT=()
        [ -s "$BUILD/select.tests" ] && SELECT=(-I "$(tr -d '[:space:]' < "$BUILD/select.tests")")
        step cputests env VK_DRIVER_FILES=/nonexistent/icd.json \
            ctest --test-dir "$BUILD" -E "$VALIDATION_TESTS" --output-on-failure --timeout 120 \
            --parallel "${CPU_TEST_PARALLEL:-$JOBS}" "${SELECT[@]}" --output-junit "$OUT/cpu.xml"
    fi
    exit 0
fi
SELECT=()
[ -s "$BUILD/select.tests" ] && SELECT=(-I "$(tr -d '[:space:]' < "$BUILD/select.tests")")

VK_ENV=(ANYPS5_REQUIRE_VULKAN=1 "ANYPS5_GPU=$GPU_NAME" VK_DRIVER_FILES=/etc/vulkan/icd.d/nvidia_icd.json
        ANYPS5_NO_SHADER_CACHE=1 VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation)

if [ "$PHASE" = confirm ]; then
    for i in $(seq 1 "${CONFIRM_REPEAT:-2}"); do
        step "confirm-$i" env "${VK_ENV[@]}" \
            ctest --test-dir "$BUILD" -R "$CONFIRM_TESTS" --timeout 120 --parallel 1 \
            --test-output-size-passed 0 --test-output-size-failed 0 --output-junit "$OUT/confirm-$i.xml"
    done
    exit 0
fi

vulkaninfo --summary > "$OUT/gpu.txt" 2>&1
GPU_SET=()
if [ -f "$OUT/cpu.xml" ]; then
    MOVED=$(python3 - "$OUT/cpu.xml" <<'PY'
import re, sys, xml.etree.ElementTree as ET
names = []
for case in ET.parse(sys.argv[1]).getroot().iter("testcase"):
    if case.get("status") != "run" or case.find("failure") is not None or case.find("skipped") is not None:
        names.append(re.sub(r"([][.*^$+?(){}|\\])", r"\\\1", case.get("name") or ""))
print("|".join(n for n in names if n))
PY
) && GPU_SET=(-R "$VALIDATION_TESTS")
    [ ${#GPU_SET[@]} -gt 0 ] && [ -n "$MOVED" ] && GPU_SET=(-R "($VALIDATION_TESTS)|^($MOVED)\$")
fi
FAILED_LIST="$BUILD/Testing/Temporary/LastTestsFailed.log"
rm -f "$FAILED_LIST"
step tests env "${VK_ENV[@]}" GALLIUM_OVERRIDE_CPU_CAPS=avx \
    ctest --test-dir "$BUILD" --output-on-failure --timeout 120 --parallel "$TEST_PARALLEL" \
    --test-output-size-passed 0 --test-output-size-failed 0 \
    "${GPU_SET[@]}" "${SELECT[@]}" --output-junit "$OUT/ctest.xml"
cp "$OUT/ctest.xml" "$OUT/validation.xml" 2>/dev/null
if [ -s "$FAILED_LIST" ]; then
    RETRY=$(cut -d: -f2- "$FAILED_LIST" | grep -vxF -f <(cat "$OUT/known_failures.txt" 2>/dev/null; echo) | sed 's/[][\.*^$+?(){}|]/\\&/g' | paste -sd'|')
    if [ -n "$RETRY" ]; then
        step rerun env "${VK_ENV[@]}" GALLIUM_OVERRIDE_CPU_CAPS=avx \
            ctest --test-dir "$BUILD" -R "^($RETRY)\$" --output-on-failure --timeout 360 --parallel 2 \
            --output-junit "$OUT/rerun.xml"
    fi
fi
step skips python3 "$SRC/tools/check_skips.py" "$OUT/ctest.xml" --list
exit 0
